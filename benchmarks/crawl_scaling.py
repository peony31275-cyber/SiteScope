r"""Crawl throughput, latency and resource use with 1, 3 and 5 crawler worker processes (Phase 10).

Two experiments on deterministic generated sites (``workloads.py``: nested and cyclic links,
duplicate links, BFS levels, delays, 404s, 500s, flaky 503s, redirects, pages of 4 KB to 120 KB),
served on loopback addresses by ``tests/fetch_support.LocalHttpServer``:

* ``single-host``: one site on one host (127.0.0.2), one job. The per-host politeness limits
  (requests in flight per host, spacing between request starts) bound the throughput whatever
  the number of workers.
* ``multi-host``: five sites on five hosts (127.0.0.2 to 127.0.0.6), one job per host. The
  hosts are independent, so their limits add up.

Each run starts from a clean state (tables truncated, a new Redis key prefix), starts the
scheduler and N worker processes (start-up not measured), starts the samplers, creates the jobs
through the service the API uses, and stops the clock when every job is terminal. The runs of
one invocation are interleaved (trial by trial, every size, worker count and code variant), so
that a drift of the machine affects every configuration alike. Nothing is averaged away: every
run is written to the output file.

Measured per run (``benchlib`` for the samplers): wall time and pages per second; each job's
duration by the PostgreSQL clock; URL outcomes, retries, attempts and duplicate fetches, checked
against the workload's expected counts; the correctness invariants of every job
(``tests/crawl_support.assert_job_invariants``); dispatch-to-result latency per URL (PostgreSQL
clock); BFS level spans and the gaps between levels; the workers' ``task.handled`` lines (their
reasons, durations and, from Phase 10 on, queue delay, politeness wait and stage times); requests
in flight per host as each server saw them; PostgreSQL statistics (commits, cache hits,
deadlocks) and activity (connections, lock waits); Redis memory and commands; CPU and memory of
every process; the machine's CPU load and effective clock, the WSL 2 VM and the containers.

Run from backend\ with PostgreSQL and Redis up (see ``benchmarks/README.md``)::

    .\.venv\Scripts\python.exe ..\benchmarks\crawl_scaling.py --experiment multi-host `
        --output ..\benchmarks\results\phase10-crawl-multi-host.json
"""

import argparse
import asyncio
import os
import statistics
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
    timeline,
    trials,
    write_json,
)
from sqlalchemy import text
from workloads import Site, Workload, build_site

from app.core.config import Settings
from app.core.redis import create_redis_client
from app.db.session import Database
from tests.crawl_support import assert_job_invariants, create_local_job
from tests.fetch_support import LocalHttpServer
from tests.integration import helpers

HOSTS = ["127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5", "127.0.0.6"]
EXPERIMENTS = {"single-host": (1, 1000), "multi-host": (5, 400)}  # hosts, pages per host
TERMINAL_JOB = ("completed", "failed", "cancelled")
# Fields of the Phase 10 task.handled lines (absent from earlier code).
STAGE_FIELDS = ("queue_delay_ms", "politeness_wait_ms", "claim_ms", "crawl_ms", "record_ms")

_OUTCOMES = text(
    "SELECT state, coalesce(outcome, '') AS outcome, count(*) AS n FROM crawl_urls GROUP BY 1, 2"
)
_ATTEMPTS = text(
    "SELECT count(*) AS urls, coalesce(sum(attempt_count), 0) AS attempts,"
    " count(*) FILTER (WHERE attempt_count > 1) AS retried FROM crawl_urls"
)
_EVENTS = text("SELECT event_type, count(*) AS n FROM crawl_events GROUP BY 1")
_LATENCY = text(
    "SELECT extract(epoch FROM finished_at - dispatched_at) * 1000 AS ms FROM crawl_urls"
    " WHERE finished_at IS NOT NULL AND dispatched_at IS NOT NULL"
)
_JOBS = text(
    "SELECT id, status, admitted_count, extract(epoch FROM finished_at - started_at) AS seconds"
    " FROM crawl_jobs ORDER BY created_at"
)
_LEVELS = text(
    """
    SELECT job_id, depth, count(*) AS urls,
           extract(epoch FROM min(dispatched_at)) AS first_dispatch,
           extract(epoch FROM max(finished_at)) AS last_finish
    FROM crawl_urls GROUP BY job_id, depth ORDER BY job_id, depth
    """
)
_PAGES = text("SELECT count(*) FROM pages")


def level_report(rows: list[Any]) -> dict[str, Any]:
    """Each level's span (first dispatch to last result) and the gaps between levels: the time
    the scheduler took to admit and dispatch the next level after the last result."""
    spans: dict[int, list[float]] = {}
    gaps: list[float] = []
    by_job: dict[Any, list[Any]] = {}
    for row in rows:
        by_job.setdefault(row.job_id, []).append(row)
    for levels in by_job.values():
        for index, row in enumerate(levels):
            if row.first_dispatch is None or row.last_finish is None:
                continue
            spans.setdefault(row.depth, []).append(float(row.last_finish - row.first_dispatch))
            if index + 1 < len(levels) and levels[index + 1].first_dispatch is not None:
                gaps.append(float(levels[index + 1].first_dispatch - row.last_finish) * 1000)
    return {
        "level_seconds_median": {
            str(depth): round(statistics.median(values), 3) for depth, values in spans.items()
        },
        "gap_ms": distribution(gaps),
    }


async def one_run(
    settings: Settings,
    database: Database,
    site: Site,
    *,
    experiment: str,
    variant: Variant,
    workers: int,
    hosts: list[str],
    trial: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    await helpers.truncate_all(database)
    prefix = f"bench-{uuid.uuid4().hex[:10]}"
    redis = create_redis_client(settings, client_name="sitescope-benchmark")
    servers = [
        LocalHttpServer(site.routes, host=host, sequences=site.fresh_sequences()) for host in hosts
    ]
    record: dict[str, Any] = {
        "experiment": experiment,
        "variant": variant.name,
        "trial": trial,
        "workers": workers,
        "hosts": len(hosts),
        "pages_per_host": args.current_pages,
        "pages_total": args.current_pages * len(hosts),
    }
    try:
        for server in servers:
            await server.__aenter__()
        ports = [server.port for server in servers]
        env = process_environment(
            settings,
            prefix=prefix,
            hosts=hosts,
            ports=ports,
            extra={
                "LOG_LEVEL": "INFO",
                "CRAWL_CONCURRENCY": str(args.concurrency),
                "POLITENESS_INTERVAL_SECONDS": str(args.interval),
                "POLITENESS_MAX_CONCURRENT_PER_HOST": str(args.per_host),
                **args.settings,
            },
        )
        run_dir = (
            args.logs / f"{experiment}-{variant.name}-w{workers}-p{args.current_pages}-{prefix}"
        )
        run_dir.mkdir(parents=True)
        cluster = VariantCluster(env, run_dir, backend_dir=variant.backend_dir)
        machine = MachineSampler()
        try:
            processes = [cluster.scheduler()] + [cluster.worker() for _ in range(workers)]
            await cluster.started(*processes)
            pids = {process.name: process.interpreter_pid for process in processes}
            pids["benchmark"] = os.getpid()
            sampler = ProcessSampler(pids)
            activity = ActivitySampler(database)
            memory_before = (await redis.info("memory"))["used_memory"]
            commands_before = (await redis.info("stats"))["total_commands_processed"]
            stats_before = await pg_database_stats(database)
            await machine.start()
            sampler.start()
            activity.start()
            began = time.perf_counter()
            window_start = time.monotonic()
            jobs = [
                await create_local_job(
                    database,
                    settings,
                    host=host,
                    ports=ports,
                    port=server.port,
                    max_depth=args.depth,
                    max_pages=site.expected["admitted"] + 10,
                )
                for host, server in zip(hosts, servers, strict=True)
            ]
            while True:
                async with database.session() as session:
                    statuses = [row.status for row in await session.execute(_JOBS)]
                if all(status in TERMINAL_JOB for status in statuses):
                    break
                if time.perf_counter() - began > args.timeout:
                    raise RuntimeError(f"a run took more than {args.timeout} s")
                await asyncio.sleep(0.05)
            elapsed = time.perf_counter() - began
            window_end = time.monotonic()
            record["activity"] = await activity.stop()
            record["processes"] = await sampler.stop()
            record["machine"] = await machine.stop()
            record["exit_codes"] = await cluster.stop_all()
            await asyncio.sleep(1.0)  # closed sessions flush their statistics
            stats_after = await pg_database_stats(database)
            memory_after = (await redis.info("memory"))["used_memory"]
            commands_after = (await redis.info("stats"))["total_commands_processed"]
            stream_length = await redis.xlen(f"{prefix}:crawl")
        finally:
            cluster.kill_all()
            await machine.stop()
    finally:
        for server in servers:
            await server.__aexit__(None, None, None)
        keys = [key async for key in redis.scan_iter(match=f"{prefix}:*", count=1000)]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()

    async with database.session() as session:
        outcomes = {f"{row.state}:{row.outcome}": row.n for row in await session.execute(_OUTCOMES)}
        attempts = (await session.execute(_ATTEMPTS)).one()
        events = {row.event_type: row.n for row in await session.execute(_EVENTS)}
        latency = [float(row.ms) for row in await session.execute(_LATENCY)]
        job_rows = (await session.execute(_JOBS)).all()
        levels = level_report((await session.execute(_LEVELS)).all())
        pages = int(await session.scalar(_PAGES) or 0)
    invariants: list[str] = []
    for job_id in jobs:
        try:
            await assert_job_invariants(database, job_id)
        except AssertionError as exc:
            invariants.append(f"{job_id}: {exc!r}"[:300])

    expected = {key: value * len(hosts) for key, value in site.expected.items()}
    html_stored = outcomes.get("fetched:html_stored", 0)
    per_server = [
        Counter(item.target.split("?")[0] for item in server.served) for server in servers
    ]
    page_requests = sum(counts[path] for counts in per_server for path in site.page_paths)
    # Requests a page should get: 1, a flaky page 2 (503, then 200), a failing page 3 (500s).
    allowed = {
        path: 1
        + (1 if path in site.sequences else 0)
        + (2 if site.routes[path].status == 500 else 0)
        for path in site.page_paths
    }
    duplicates = sum(
        max(0, counts[path] - allowed[path]) for counts in per_server for path in site.page_paths
    )
    workers_lines = handled_lines(cluster.workers())
    durations = [float(line["duration_ms"]) for line in workers_lines]
    slots = args.concurrency * workers
    record.update(
        {
            "elapsed_seconds": round(elapsed, 3),
            "html_pages_per_second": round(html_stored / elapsed, 2),
            "page_requests_per_second": round(page_requests / elapsed, 2),
            "job_statuses": [row.status for row in job_rows],
            "job_seconds_db": [round(float(row.seconds), 3) for row in job_rows if row.seconds],
            "admitted": sum(row.admitted_count for row in job_rows),
            "pages_stored": pages,
            "outcomes": outcomes,
            "attempts": int(attempts.attempts),
            "urls_retried": int(attempts.retried),
            "events": events,
            "page_requests": page_requests,
            "duplicate_page_fetches": duplicates,
            "expected": expected,
            "checks": {
                "admitted": sum(row.admitted_count for row in job_rows) == expected["admitted"],
                "html_stored": html_stored == expected["html_stored"],
                "pages_stored": pages == expected["html_stored"],
                "redirects": outcomes.get("fetched:redirect", 0) == expected["redirects"],
                "http_errors": outcomes.get("fetched:http_error", 0)
                == expected["not_found"] + expected["failing"],
                "retries": events.get("url.retry_scheduled", 0) == expected["retries"],
                "page_requests": page_requests == expected["page_requests"],
                "invariants": not invariants,
            },
            "invariant_errors": invariants,
            "url_latency_ms": distribution(latency),
            "levels": levels,
            "workers_handled": dict(Counter(line["reason"] for line in workers_lines)),
            "completed_per_worker": {
                process.name: sum(
                    1
                    for line in process.events("task.handled")
                    if line["reason"] in ("completed", "retry_scheduled")
                )
                for process in cluster.workers()
            },
            "task_ms": distribution(durations),
            "slot_occupancy": round(sum(durations) / 1000 / (elapsed * slots), 3),
            "stages_ms": {
                name: distribution([float(line[name]) for line in workers_lines if name in line])
                for name in STAGE_FIELDS
                if any(name in line for line in workers_lines)
            },
            "hosts_timeline": [
                {
                    "host": host,
                    **timeline(server.served, window_start, window_end, paths=site.page_paths),
                }
                for host, server in zip(hosts, servers, strict=True)
            ],
            "postgres": stats_delta(stats_before, stats_after),
            "redis": {
                "used_memory_delta_mb": round((memory_after - memory_before) / 2**20, 2),
                "commands": commands_after - commands_before,
                "stream_length_end": stream_length,
            },
        }
    )
    return record


def summarize(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Medians and ranges per configuration (variant, size, workers)."""
    groups: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault((run["variant"], run["pages_total"], run["workers"]), []).append(run)
    summary = []
    for (variant, pages, workers), members in sorted(groups.items()):
        summary.append(
            {
                "variant": variant,
                "pages_total": pages,
                "workers": workers,
                "html_pages_per_second": trials([run["html_pages_per_second"] for run in members]),
                "elapsed_seconds": trials([run["elapsed_seconds"] for run in members]),
                "url_latency_p50_ms": trials([run["url_latency_ms"]["p50"] for run in members]),
                "url_latency_p95_ms": trials([run["url_latency_ms"]["p95"] for run in members]),
                "all_checks_passed": all(all(run["checks"].values()) for run in members),
            }
        )
    return summary


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--experiment", choices=sorted(EXPERIMENTS), required=True)
    parser.add_argument("--pages", help="pages per host, comma-separated (a size sweep)")
    parser.add_argument("--workers", default="1,3,5", help="worker counts, comma-separated")
    parser.add_argument("--repeat", type=int, default=3, help="trials per configuration")
    parser.add_argument("--concurrency", type=int, default=4, help="tasks per worker process")
    parser.add_argument("--per-host", type=int, default=4, help="requests in flight per host")
    parser.add_argument("--interval", type=float, default=0.025, help="seconds between starts")
    parser.add_argument("--depth", type=int, default=4, help="BFS levels below the seed")
    parser.add_argument(
        "--links-per-page", type=int, default=10, help="links of a page that is not a leaf"
    )
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds per run, at most")
    parser.add_argument("--variant", action="append", help="name=<backend dir> (repeatable)")
    parser.add_argument(
        "--setting",
        action="append",
        default=[],
        help="NAME=VALUE: a SITESCOPE_NAME setting of every process (repeatable)",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--logs", type=Path, default=Path(tempfile.gettempdir()) / "sitescope-bench-crawl"
    )
    args = parser.parse_args()
    args.settings = {}
    for item in args.setting:
        name, separator, value = item.partition("=")
        if not separator or not name.isidentifier() or not name.isupper():
            raise SystemExit(f"--setting {item!r}: expected NAME=VALUE (without SITESCOPE_)")
        args.settings[name] = value

    host_count, default_pages = EXPERIMENTS[args.experiment]
    hosts = HOSTS[:host_count]
    sizes = [int(value) for value in (args.pages or str(default_pages)).split(",")]
    worker_counts = [int(value) for value in args.workers.split(",")]
    variants = parse_variants(args.variant)
    sites = {
        pages: build_site(
            Workload(pages=pages, depth=args.depth, links_per_page=args.links_per_page)
        )
        for pages in sizes
    }

    settings = bench_settings()
    await fresh_database(settings)
    database = Database(settings, application_name="sitescope-benchmark")
    runs: list[dict[str, Any]] = []
    try:
        env_info = await environment(settings, database, variants)
        for trial in range(1, args.repeat + 1):
            # Variants alternate their order from trial to trial (A B, B A, A B, ...), so that
            # neither always runs first after a pause or second on a warmer machine.
            ordered = variants if trial % 2 else variants[::-1]
            for pages in sizes:
                for workers in worker_counts:
                    for variant in ordered:
                        args.current_pages = pages
                        result = await one_run(
                            settings,
                            database,
                            sites[pages],
                            experiment=args.experiment,
                            variant=variant,
                            workers=workers,
                            hosts=hosts,
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
                                    "pages_total",
                                    "workers",
                                    "elapsed_seconds",
                                    "html_pages_per_second",
                                    "checks",
                                )
                            }
                        )
    finally:
        await database.dispose()
        await helpers.drop_database(settings)
    write_json(
        args.output,
        {
            "benchmark": "crawl_scaling",
            "experiment": args.experiment,
            "environment": env_info,
            "configuration": {
                "hosts": hosts,
                "pages_per_host": sizes,
                "workers": worker_counts,
                "repeat": args.repeat,
                "tasks_per_worker": args.concurrency,
                "requests_in_flight_per_host": args.per_host,
                "politeness_interval_seconds": args.interval,
                "scheduler": "defaults: at most 100 tasks in flight per job (since Phase 10, "
                "ADR-0022: at most 2 per request in flight per host, so "
                f"{min(100, 2 * args.per_host)} here; code before Phase 10: 100), 1000 "
                "outstanding, batch 100; 0.1 s interval (tests/process_support.FAST_TIMING); "
                "extra_settings override these",
                "variant_order": "alternating by trial (A B, B A, ...) since the Phase 10 "
                "review; the Phase 10 A/B files ran each pair baseline first",
                "database_pool": "defaults (5 + 5 overflow per process)",
                "timings": "tests/process_support.FAST_TIMING (7 s leases, 0.2-1 s retry backoff)",
                "workload": Workload(
                    pages=sizes[0], depth=args.depth, links_per_page=args.links_per_page
                ).describe(),
                "levels_per_site": {str(pages): site.levels for pages, site in sites.items()},
                "html_bytes_per_site": {
                    str(pages): site.body_bytes for pages, site in sites.items()
                },
                "variants": {variant.name: str(variant.backend_dir) for variant in variants},
                "extra_settings": args.settings,
            },
            "summary": summarize(runs),
            "runs": runs,
        },
    )


if __name__ == "__main__":
    asyncio.run(main())
