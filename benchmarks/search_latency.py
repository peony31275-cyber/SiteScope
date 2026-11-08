r"""Search latency and throughput, in the service and through the API (Phase 10).

**Datasets.** Two jobs in one disposable database, generated as in Phase 7 (``indexing.py``:
pseudo-words from a 5,000-word Zipf vocabulary, seed 7; a 5-word title, 15-word description, 20
words of headings, ``--words`` words of body per page): ``--sizes`` documents (default 2,000 and
10,000). Both are indexed by the real pipeline (an index dispatcher and four indexer processes),
then ``VACUUM (ANALYZE)`` merges the GIN index's pending list, as autovacuum does in normal
operation. Searches are per job, so each size is measured with both jobs in the index.

**Queries** (by vocabulary rank): a common term (in nearly every page), a medium and a rare one,
a very rare one, two and three terms (AND), an OR, a phrase, no match, and stop words only.

**Measured.**

1. *Service*: each query ``--calls`` times through ``search_job`` (one session per call, as the
   API does; 10 results with snippets), after 5 discarded warm-up calls: p50, p95, p99 (from 100
   calls), maximum.
2. *Pagination*: the common term at offsets 0, 100, 500 and 1000 (the deepest allowed).
3. *Plans*: ``EXPLAIN (ANALYZE, BUFFERS)`` of the search statement for every query and size:
   planning and execution time, and whether the GIN index or a sequential scan found the
   matches. Text plans of the common and the rare term are saved (``--plans``).
4. *API*: a real API process (uvicorn, ``app.main:app``, default connection pool). Each query
   ``--calls`` times over one keep-alive connection (client-side time), then 1, 4 and 16
   concurrent clients sending every query of both jobs in turn for ``--duration`` seconds:
   requests per second, latency percentiles and response statuses, with the samplers running.

Run from backend\ with PostgreSQL and Redis up::

    .\.venv\Scripts\python.exe ..\benchmarks\search_latency.py `
        --output ..\benchmarks\results\phase10-search.json `
        --plans ..\benchmarks\results\phase10-search-plans.txt
"""

import argparse
import asyncio
import itertools
import os
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx2
from benchlib import (
    ActivitySampler,
    MachineSampler,
    ProcessSampler,
    VariantCluster,
    bench_settings,
    delete_keys,
    distribution,
    emit,
    environment,
    fresh_database,
    parse_variants,
    process_environment,
    write_json,
)
from indexing import HOST, dataset, store
from sqlalchemy import text

from app.core.config import Settings
from app.core.redis import create_redis_client
from app.db.session import Database
from app.indexing.documents import TEXT_SEARCH_CONFIG
from app.indexing.queries import HEADLINE_OPTIONS, SNIPPET_SOURCE_CHARS
from app.observability.heartbeat import read_heartbeats
from app.services import search as search_service
from app.services.search import search_job
from tests.crawl_support import new_job
from tests.integration import helpers

WARM_UP = 5
_INDEXED = text("SELECT count(*) FROM search_documents WHERE status = 'indexed'")


def query_set(vocab: list[str], columns: dict[str, list[str]]) -> dict[str, str]:
    """Rank 0 is the most frequent word; higher ranks are rarer (Zipf weights)."""
    first = columns["bodies"][0].split()
    return {
        "common_term": vocab[0],
        "medium_term": vocab[100],
        "rare_term": vocab[3000],
        "very_rare_term": vocab[4900],
        "two_terms": f"{vocab[0]} {vocab[100]}",
        "three_terms": f"{vocab[10]} {vocab[100]} {vocab[1000]}",
        "or_terms": f"{vocab[100]} or {vocab[3000]}",
        "phrase": f'"{first[10]} {first[11]}"',
        "no_match": "zzzqqqxxx",
        "stop_words_only": "the and of",
    }


async def build(
    settings: Settings, database: Database, sizes: list[int], words: int, logs: Path
) -> tuple[dict[int, uuid.UUID], list[str], dict[str, list[str]]]:
    """Store and index one job per size with the real pipeline; returns the job IDs."""
    vocab, columns = dataset(max(sizes), words)
    prefix = f"bench-{uuid.uuid4().hex[:10]}"
    env = process_environment(
        settings,
        prefix=prefix,
        hosts=["127.0.0.1"],
        ports=[80],
        extra={
            "LOG_LEVEL": "INFO",  # their start lines are INFO, and Cluster.started waits for them
            "INDEX_DISPATCH_BATCH_SIZE": "1000",
            "INDEX_MAX_OUTSTANDING": "2000",
            "INDEX_DISPATCHER_INTERVAL_SECONDS": "0.1",
        },
    )
    run_dir = logs / f"index-{prefix}"
    run_dir.mkdir(parents=True)
    cluster = VariantCluster(env, run_dir)
    jobs: dict[int, uuid.UUID] = {}
    try:
        processes = [cluster.index_dispatcher()] + [cluster.indexer() for _ in range(4)]
        await cluster.started(*processes)
        for size in sizes:
            job_id, _ = await new_job(database, host=HOST)
            await store(database, job_id, {key: values[:size] for key, values in columns.items()})
            jobs[size] = job_id
        began = time.perf_counter()
        while True:
            async with database.session() as session:
                if int(await session.scalar(_INDEXED) or 0) == sum(sizes):
                    break
            if time.perf_counter() - began > 1800:
                raise RuntimeError("indexing took more than 30 minutes")
            await asyncio.sleep(0.5)
        await cluster.stop_all()
    finally:
        cluster.kill_all()
        await delete_keys(settings, prefix)  # streams, heartbeats and their index
    async with database.engine.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text("VACUUM (ANALYZE) pages, search_documents"))
    return jobs, vocab, columns


async def service_latency(
    database: Database, job_id: uuid.UUID, query: str, *, calls: int, offset: int = 0
) -> dict[str, Any]:
    timings: list[float] = []
    matches = 0
    searchable = True
    for call in range(WARM_UP + calls):
        async with database.session() as session:
            begun = time.perf_counter()
            found = await search_job(session, job_id, query, limit=10, offset=offset)
            elapsed = (time.perf_counter() - begun) * 1000
        if call >= WARM_UP:
            timings.append(elapsed)
        matches, searchable = found.total, found.searchable
    return {"matches": matches, "searchable": searchable, "ms": distribution(timings)}


def _plan_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [node]
    for child in node.get("Plans", []):
        nodes += _plan_nodes(child)
    return nodes


async def explain(database: Database, job_id: uuid.UUID, query: str) -> tuple[dict[str, Any], str]:
    params = {
        "config": TEXT_SEARCH_CONFIG,
        "query": query,
        "job_id": job_id,
        "limit": 10,
        "offset": 0,
        "snippet_chars": SNIPPET_SOURCE_CHARS,
        "headline_options": HEADLINE_OPTIONS,
    }
    statement = str(search_service._SEARCH.text)
    async with database.session() as session:
        plan = (
            await session.execute(
                text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement), params
            )
        ).scalar_one()
        lines = [
            row[0]
            for row in await session.execute(
                text("EXPLAIN (ANALYZE, BUFFERS) " + statement), params
            )
        ]
    root = plan[0]
    nodes = _plan_nodes(root["Plan"])
    scans = sorted(
        {
            " ".join(
                [node["Node Type"], node.get("Index Name") or node.get("Relation Name") or ""]
            ).strip()
            for node in nodes
            if "Scan" in node["Node Type"]
        }
    )
    return (
        {
            "planning_ms": round(root["Planning Time"], 3),
            "execution_ms": round(root["Execution Time"], 3),
            "gin_used": any(
                node.get("Index Name") == "ix_search_documents_search_vector" for node in nodes
            ),
            "scans": scans,
        },
        "\n".join(lines),
    )


async def api_interpreter_pid(settings: Settings, prefix: str) -> int | None:
    """The API interpreter's PID, from its heartbeat's name (``api-<host>-<pid>-<random>``);
    None for code without heartbeats (before Phase 10)."""
    redis = create_redis_client(settings, client_name="sitescope-benchmark")
    try:
        for _ in range(50):
            beats = await read_heartbeats(redis, key_prefix=prefix)
            api = next((beat for beat in beats if beat.role == "api"), None)
            if api is not None:
                return int(api.name.rsplit("-", 2)[1])
            await asyncio.sleep(0.1)
    finally:
        await redis.aclose()
    return None


async def wait_for_api(base: str) -> None:
    async with httpx2.AsyncClient(base_url=base, timeout=2) as client:
        for _ in range(300):
            try:
                if (await client.get("/health")).status_code == 200:
                    return
            except httpx2.TransportError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError("the API did not start")


async def api_sequential(
    client: httpx2.AsyncClient, job_id: uuid.UUID, query: str, calls: int
) -> dict[str, Any]:
    timings: list[float] = []
    statuses: Counter[int] = Counter()
    for call in range(WARM_UP + calls):
        begun = time.perf_counter()
        response = await client.get(
            "/api/v1/search", params={"job_id": str(job_id), "q": query, "page_size": 10}
        )
        elapsed = (time.perf_counter() - begun) * 1000
        if call >= WARM_UP:
            timings.append(elapsed)
            statuses[response.status_code] += 1
    return {"ms": distribution(timings), "statuses": dict(statuses)}


async def api_concurrent(
    client: httpx2.AsyncClient, work: list[tuple[uuid.UUID, str]], *, clients: int, duration: float
) -> dict[str, Any]:
    latencies: list[float] = []
    statuses: Counter[str] = Counter()
    stop_at = time.perf_counter() + duration

    async def one_client(offset: int) -> None:
        cycle = itertools.islice(itertools.cycle(work), offset, None)
        for job_id, query in cycle:
            if time.perf_counter() >= stop_at:
                return
            begun = time.perf_counter()
            try:
                response = await client.get(
                    "/api/v1/search", params={"job_id": str(job_id), "q": query, "page_size": 10}
                )
                statuses[str(response.status_code)] += 1
            except httpx2.HTTPError as exc:
                statuses[type(exc).__name__] += 1
            latencies.append((time.perf_counter() - begun) * 1000)

    began = time.perf_counter()
    await asyncio.gather(*(one_client(index * 3) for index in range(clients)))
    elapsed = time.perf_counter() - began
    return {
        "clients": clients,
        "seconds": round(elapsed, 2),
        "requests": len(latencies),
        "requests_per_second": round(len(latencies) / elapsed, 1),
        "ms": distribution(latencies),
        "statuses": dict(statuses),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sizes", default="2000,10000", help="documents per job")
    parser.add_argument("--words", type=int, default=1000, help="body words per page")
    parser.add_argument("--calls", type=int, default=200, help="measured calls per query")
    parser.add_argument("--clients", default="1,4,16", help="concurrent API clients")
    parser.add_argument("--duration", type=float, default=15.0, help="seconds per client count")
    parser.add_argument("--variant", action="append", help="name=<backend dir> of the API")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plans", type=Path, required=True)
    parser.add_argument(
        "--logs", type=Path, default=Path(tempfile.gettempdir()) / "sitescope-bench-search"
    )
    args = parser.parse_args()
    sizes = sorted(int(value) for value in args.sizes.split(","))
    [variant] = parse_variants(args.variant)

    settings = bench_settings()
    await fresh_database(settings)
    database = Database(settings, application_name="sitescope-benchmark")
    plan_text: list[str] = []
    try:
        env_info = await environment(settings, database, [variant])
        jobs, vocab, columns = await build(settings, database, sizes, args.words, args.logs)
        cases = query_set(vocab, columns)
        service: list[dict[str, Any]] = []
        plans: list[dict[str, Any]] = []
        for size, job_id in jobs.items():
            for kind, query in cases.items():
                measured = await service_latency(database, job_id, query, calls=args.calls)
                record = {"documents": size, "query_kind": kind, "query": query, **measured}
                service.append(record)
                emit({key: record[key] for key in ("documents", "query_kind", "matches", "ms")})
                if measured["searchable"]:
                    summary, rendered = await explain(database, job_id, query)
                    plans.append({"documents": size, "query_kind": kind, **summary})
                    if kind in ("common_term", "rare_term"):
                        plan_text.append(
                            f"--- {size} documents, {kind}: {query!r} ---\n{rendered}\n"
                        )
        paging = []
        for size, job_id in jobs.items():
            for offset in (0, 100, 500, 1000):
                measured = await service_latency(
                    database, job_id, cases["common_term"], calls=args.calls // 2, offset=offset
                )
                paging.append({"documents": size, "offset": offset, **measured})
                emit({"documents": size, "offset": offset, "ms": measured["ms"]})

        port = helpers.unused_port()
        api_prefix = f"bench-{uuid.uuid4().hex[:10]}"
        env = process_environment(
            settings,
            prefix=api_prefix,
            hosts=["127.0.0.1"],
            ports=[80],
            extra={"LOG_LEVEL": "WARNING"},
        )
        run_dir = args.logs / f"api-{port}"
        run_dir.mkdir(parents=True)
        cluster = VariantCluster(env, run_dir, backend_dir=variant.backend_dir)
        base = f"http://127.0.0.1:{port}"
        api_records: list[dict[str, Any]] = []
        concurrency: list[dict[str, Any]] = []
        try:
            cluster.api(port)
            await wait_for_api(base)
            async with httpx2.AsyncClient(base_url=base, timeout=30) as client:
                for size, job_id in jobs.items():
                    for kind, query in cases.items():
                        measured = await api_sequential(client, job_id, query, args.calls)
                        api_records.append({"documents": size, "query_kind": kind, **measured})
                        emit(
                            {
                                "api": True,
                                "documents": size,
                                "query_kind": kind,
                                "ms": measured["ms"],
                            }
                        )
                work = [(job_id, query) for job_id in jobs.values() for query in cases.values()]
                api_pid = await api_interpreter_pid(settings, api_prefix)
                for clients in (int(value) for value in args.clients.split(",")):
                    machine = MachineSampler()
                    activity = ActivitySampler(database)
                    pids = {"benchmark": os.getpid()}
                    if api_pid:
                        pids["api"] = api_pid
                    sampler = ProcessSampler(pids)
                    await machine.start()
                    sampler.start()
                    activity.start()
                    measured = await api_concurrent(
                        client, work, clients=clients, duration=args.duration
                    )
                    measured["activity"] = await activity.stop()
                    measured["processes"] = await sampler.stop()
                    measured["machine"] = await machine.stop()
                    concurrency.append(measured)
                    emit(
                        {
                            key: measured[key]
                            for key in ("clients", "requests_per_second", "ms", "statuses")
                        }
                    )
            await cluster.stop_all()
        finally:
            cluster.kill_all()
            await delete_keys(settings, api_prefix)
        async with database.session() as session:
            sizes_row = (
                await session.execute(
                    text(
                        "SELECT pg_size_pretty(pg_total_relation_size('search_documents'))"
                        " AS total,"
                        " pg_size_pretty(pg_relation_size('ix_search_documents_search_vector'))"
                        " AS gin"
                    )
                )
            ).one()
    finally:
        await database.dispose()
        await helpers.drop_database(settings)
    write_json(
        args.output,
        {
            "benchmark": "search_latency",
            "environment": env_info,
            "configuration": {
                "documents_per_job": sizes,
                "body_words": args.words,
                "calls_per_query": args.calls,
                "warm_up_calls_discarded": WARM_UP,
                "results_per_page": 10,
                "api_clients": args.clients,
                "seconds_per_client_count": args.duration,
                "search_documents_total": sizes_row.total,
                "gin_index": sizes_row.gin,
                "variant": {variant.name: str(variant.backend_dir)},
            },
            "queries": cases,
            "service": service,
            "pagination": paging,
            "plans": plans,
            "api_sequential": api_records,
            "api_concurrent": concurrency,
        },
    )
    args.plans.write_text("\n".join(plan_text), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    asyncio.run(main())
