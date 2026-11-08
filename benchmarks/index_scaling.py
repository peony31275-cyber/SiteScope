r"""Indexing throughput, latency and resource use with 1, 2 and 4 indexer processes (Phase 10).

Extends the Phase 7 benchmark (``indexing.py``, whose dataset and loading it reuses: one job of
``--pages`` generated pages with a ``--words``-word body each, seed 7) with what is needed to
explain its numbers and their variation between runs:

* **Interleaved runs.** Trial by trial, every indexer count (and every variant and GIN setting)
  runs once, so a drift of the machine during the session affects each configuration alike.
  Phase 7 ran all 1-indexer runs, then all 2-indexer runs, then all 4-indexer runs.
* **Per-run samplers** (``benchlib``): the machine's CPU load and effective clock (and whether it
  was limited), the WSL 2 VM and the PostgreSQL container's CPU, every process's CPU time,
  PostgreSQL's cumulative statistics (commits, buffer reads and hits, rows written, deadlocks,
  temporary files) and its sessions (connections, active, waiting for a lock).
* **Per-document times** from the indexers' ``task.handled`` lines: the whole message, the
  attempt's start and the index transaction (``start_ms``, ``index_ms``, Phase 10 code), and
  the queue delay (dispatch to first attempt, by the PostgreSQL clock).
* **GIN maintenance.** ``--gin-fastupdate on,off`` runs every configuration with the search
  vector index's ``fastupdate`` on (PostgreSQL's default: new entries go to a pending list that
  an insert merges once it exceeds ``gin_pending_list_limit``) and off (every insert updates the
  index); ``pgstattuple``'s ``pgstatginindex`` reports the pending list after each run.

The clock starts at the commit that stores every page (PostgreSQL's clock); a document's latency
is its ``indexed_at`` minus that commit; throughput is pages divided by the time to the last
``indexed_at``. Process start-up is excluded. Every run is written to the output file.

Run from backend\ with PostgreSQL and Redis up::

    .\.venv\Scripts\python.exe ..\benchmarks\index_scaling.py `
        --output ..\benchmarks\results\phase10-indexing.json
"""

import argparse
import asyncio
import os
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from benchlib import (
    ActivitySampler,
    MachineSampler,
    ProcessSampler,
    Variant,
    VariantCluster,
    bench_settings,
    distribution,
    emit,
    environment,
    fresh_database,
    handled_lines,
    parse_variants,
    pg_database_stats,
    process_environment,
    stats_delta,
    trials,
    write_json,
)
from indexing import HOST, LATENCIES, SEED, dataset, store
from sqlalchemy import text

from app.core.config import Settings
from app.core.redis import create_redis_client
from app.db.session import Database
from tests.crawl_support import new_job
from tests.integration import helpers

GIN_INDEX = "ix_search_documents_search_vector"
_ATTEMPTS = text(
    "SELECT count(*) FILTER (WHERE attempt_count > 1) AS retried,"
    " count(*) FILTER (WHERE status = 'failed') AS failed,"
    " coalesce(sum(attempt_count), 0) AS attempts FROM search_documents"
)
_GIN_STATE = text(
    "SELECT pending_pages, pending_tuples FROM pgstatginindex(CAST(:index AS regclass))"
)
_GIN_SIZE = text("SELECT pg_relation_size(CAST(:index AS regclass))")


async def set_fastupdate(database: Database, mode: str) -> None:
    """``default`` resets the index to PostgreSQL's default (on): a setting from an earlier run
    survives TRUNCATE."""
    statement = (
        f"ALTER INDEX {GIN_INDEX} RESET (fastupdate)"
        if mode == "default"
        else f"ALTER INDEX {GIN_INDEX} SET (fastupdate = {mode})"
    )
    async with database.transaction() as session:
        await session.execute(text(statement))


async def one_run(
    settings: Settings,
    database: Database,
    columns: dict[str, list[str]],
    *,
    variant: Variant,
    indexers: int,
    fastupdate: str,
    trial: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    await helpers.truncate_all(database)
    await set_fastupdate(database, fastupdate)
    pages = len(columns["paths"])
    job_id, _ = await new_job(database, host=HOST)
    prefix = f"bench-{uuid.uuid4().hex[:10]}"
    redis = create_redis_client(settings, client_name="sitescope-benchmark")
    env = process_environment(
        settings,
        prefix=prefix,
        hosts=["127.0.0.1"],
        ports=[80],
        extra={
            "LOG_LEVEL": "INFO",
            "INDEX_CONCURRENCY": str(args.concurrency),
            "INDEX_DISPATCH_BATCH_SIZE": "1000",
            "INDEX_MAX_OUTSTANDING": str(max(1000, pages)),
            "INDEX_DISPATCHER_INTERVAL_SECONDS": "0.1",
        },
    )
    run_dir = args.logs / f"{variant.name}-i{indexers}-gin{fastupdate}-{prefix}"
    run_dir.mkdir(parents=True)
    cluster = VariantCluster(env, run_dir, backend_dir=variant.backend_dir)
    machine = MachineSampler()
    record: dict[str, Any] = {
        "variant": variant.name,
        "trial": trial,
        "indexers": indexers,
        "gin_fastupdate": fastupdate,
        "concurrency_per_indexer": args.concurrency,
        "pages": pages,
    }
    try:
        processes = [cluster.index_dispatcher()] + [cluster.indexer() for _ in range(indexers)]
        await cluster.started(*processes)
        pids = {process.name: process.interpreter_pid for process in processes}
        sampler = ProcessSampler({**pids, "benchmark": os.getpid()})
        activity = ActivitySampler(database)
        # The samplers start after the pages are stored, so the figures cover indexing only,
        # not the benchmark's own bulk load.
        committed = await store(database, job_id, columns)
        stats_before = await pg_database_stats(database)
        await machine.start()
        sampler.start()
        activity.start()
        began = time.perf_counter()
        while True:
            async with database.session() as session:
                row = (
                    await session.execute(LATENCIES, {"job_id": job_id, "committed": committed})
                ).one()
            if row.indexed == pages:
                break
            if time.perf_counter() - began > args.timeout:
                raise RuntimeError(f"a run took more than {args.timeout} s")
            await asyncio.sleep(0.1)
        record["activity"] = await activity.stop()
        record["processes"] = await sampler.stop()
        record["machine"] = await machine.stop()
        record["exit_codes"] = await cluster.stop_all()
        await asyncio.sleep(1.0)  # closed sessions flush their statistics
        stats_after = await pg_database_stats(database)
    finally:
        cluster.kill_all()
        await machine.stop()
        keys = [key async for key in redis.scan_iter(match=f"{prefix}:*", count=1000)]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
    async with database.session() as session:
        attempts = (await session.execute(_ATTEMPTS)).one()
        gin = (await session.execute(_GIN_STATE, {"index": GIN_INDEX})).one()
        gin_bytes = int(await session.scalar(_GIN_SIZE, {"index": GIN_INDEX}) or 0)
    lines = handled_lines(cluster.indexers())
    indexed = [line for line in lines if line["reason"] == "indexed"]
    p50, p95, maximum = (round(float(value), 3) for value in row.quantiles)
    span = float(row.span)
    record.update(
        {
            "seconds_commit_to_last_indexed": round(span, 3),
            "pages_per_second": round(pages / span, 1),
            "latency_seconds_p50": p50,
            "latency_seconds_p95": p95,
            "latency_seconds_max": maximum,
            "documents_retried": int(attempts.retried),
            "documents_failed": int(attempts.failed),
            "attempts": int(attempts.attempts),
            "handled_by_reason": dict(Counter(line["reason"] for line in lines)),
            "indexed_per_indexer": {
                process.name: sum(
                    1 for line in process.events("task.handled") if line["reason"] == "indexed"
                )
                for process in cluster.indexers()
            },
            "message_ms": distribution([float(line["duration_ms"]) for line in indexed]),
            "stages_ms": {
                name: distribution([float(line[name]) for line in indexed if name in line])
                for name in ("queue_delay_ms", "start_ms", "index_ms")
                if any(name in line for line in indexed)
            },
            "gin": {
                "pending_pages_after": int(gin.pending_pages),
                "pending_tuples_after": int(gin.pending_tuples),
                "size_mb": round(gin_bytes / 2**20, 2),
            },
            "postgres": stats_delta(stats_before, stats_after),
        }
    )
    return record


def _sampled(runs: list[dict[str, Any]], key: str) -> list[float]:
    """The per-run means of a machine sampler's reading, where it was sampled."""
    return [run["machine"][key]["mean"] for run in runs if key in run["machine"]]


def summarize(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault((run["variant"], run["gin_fastupdate"], run["indexers"]), []).append(run)
    return [
        {
            "variant": variant,
            "gin_fastupdate": fastupdate,
            "indexers": indexers,
            "pages_per_second": trials([run["pages_per_second"] for run in members]),
            "seconds": trials([run["seconds_commit_to_last_indexed"] for run in members]),
            "latency_p50_seconds": trials([run["latency_seconds_p50"] for run in members]),
            "index_ms_p50": trials(
                [
                    run["stages_ms"]["index_ms"]["p50"]
                    for run in members
                    if "index_ms" in run["stages_ms"]
                ]
            ),
            "effective_clock_pct": trials(_sampled(members, "processor_performance_pct")),
            "postgres_cpu_cores": trials(_sampled(members, "postgres_cpu_cores")),
        }
        for (variant, fastupdate, indexers), members in sorted(groups.items())
    ]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--indexers", default="1,2,4", help="indexer counts (comma-separated)")
    parser.add_argument("--concurrency", type=int, default=4, help="tasks per indexer process")
    parser.add_argument("--pages", type=int, default=2000)
    parser.add_argument("--words", type=int, default=1000, help="body words per page")
    parser.add_argument("--repeat", type=int, default=3, help="trials per configuration")
    parser.add_argument(
        "--gin-fastupdate", default="default", help="default, on, off (comma-separated)"
    )
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds per run, at most")
    parser.add_argument("--variant", action="append", help="name=<backend dir> (repeatable)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--logs", type=Path, default=Path(tempfile.gettempdir()) / "sitescope-bench-index"
    )
    args = parser.parse_args()
    indexer_counts = [int(value) for value in args.indexers.split(",")]
    modes = args.gin_fastupdate.split(",")
    if any(mode not in ("default", "on", "off") for mode in modes):
        raise SystemExit("--gin-fastupdate takes default, on and off")
    variants = parse_variants(args.variant)
    _, columns = dataset(args.pages, args.words)

    settings = bench_settings()
    await fresh_database(settings)
    database = Database(settings, application_name="sitescope-benchmark")
    runs: list[dict[str, Any]] = []
    try:
        async with database.transaction() as session:
            await session.execute(text("CREATE EXTENSION IF NOT EXISTS pgstattuple"))
        env_info = await environment(settings, database, variants)
        for trial in range(1, args.repeat + 1):
            for indexers in indexer_counts:
                for mode in modes:
                    for variant in variants:
                        result = await one_run(
                            settings,
                            database,
                            columns,
                            variant=variant,
                            indexers=indexers,
                            fastupdate=mode,
                            trial=trial,
                            args=args,
                        )
                        runs.append(result)
                        emit(
                            {
                                key: result[key]
                                for key in (
                                    "variant",
                                    "trial",
                                    "indexers",
                                    "gin_fastupdate",
                                    "seconds_commit_to_last_indexed",
                                    "pages_per_second",
                                    "documents_retried",
                                )
                            }
                        )
    finally:
        await database.dispose()
        await helpers.drop_database(settings)
    write_json(
        args.output,
        {
            "benchmark": "index_scaling",
            "environment": env_info,
            "configuration": {
                "pages": args.pages,
                "body_words": args.words,
                "seed": SEED,
                "indexers": indexer_counts,
                "tasks_per_indexer": args.concurrency,
                "gin_fastupdate": modes,
                "repeat": args.repeat,
                "order": "interleaved (trial, indexers, GIN setting, variant)",
                "dispatcher": "1,000 tasks per pass every 0.1 s",
                "timings": "tests/process_support.FAST_TIMING",
                "variants": {variant.name: str(variant.backend_dir) for variant in variants},
            },
            "summary": summarize(runs),
            "runs": runs,
        },
    )


if __name__ == "__main__":
    asyncio.run(main())
