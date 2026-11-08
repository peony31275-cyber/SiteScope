r"""Query plans and timings of the hot-path and dashboard statements at scale (Phase 10).

**Dataset.** ``--jobs`` completed jobs built with the Phase 8 generator (``analysis.py``:
``--pages`` stored pages each, in BFS levels of branching 10, ``--links`` links per page, plus
404s, redirects, 503s, timeouts and robots.txt refusals), and one running job with a frontier:
``--frontier`` pending URLs at its current level, 200 of them dispatched (queued) and 20 claimed
(in progress). ``VACUUM (ANALYZE)`` after loading. No HTTP server, no worker.

**Statements.** The crawl pipeline's statements (taken from the code, not rewritten): a claim's
row lock and update, a dispatch's selection, the admission of a level, the scheduler's frontier
scan, its global outstanding count and reconciliation scan, a URL inspection, the cancellation
check, the index dispatcher's selection and counts; the dashboard's job list and job detail
(the statements the services actually execute, captured while they run); the overview's and
``GET /metrics``'s whole-table counts. Writes run in a transaction that is rolled back.

**Measured.** ``EXPLAIN (ANALYZE, BUFFERS)`` of each statement (planning and execution time, the
scans it used) and ``--repeat`` timed executions (p50, p95, max). Text plans go to ``--plans``.

Run from backend\ with PostgreSQL up::

    .\.venv\Scripts\python.exe ..\benchmarks\db_queries.py `
        --output ..\benchmarks\results\phase10-db-queries.json `
        --plans ..\benchmarks\results\phase10-db-plans.txt
"""

import argparse
import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from analysis import store as store_completed_job
from benchlib import bench_settings, distribution, emit, environment, fresh_database, write_json
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.session import Database
from app.services import crawl_cancellation, crawl_state, overview, search_index, system_metrics
from app.services.crawl_jobs import get_crawl_job, list_crawl_jobs
from app.services.crawl_state import claim_url, dispatch_urls
from app.services.overview import system_overview
from app.services.system_metrics import read_system_state
from tests.crawl_support import new_job
from tests.integration import helpers

_OUTSTANDING = text(
    "SELECT count(*) FROM crawl_urls WHERE state IN ('queued', 'in_progress')"
)  # what outstanding_tasks() builds with the ORM


async def fake_publish(tasks: Sequence[object]) -> list[str]:
    return [f"1-{index}" for index in range(len(tasks))]


async def build(
    database: Database, jobs: int, pages: int, links: int, frontier: int
) -> dict[str, Any]:
    completed = [await store_completed_job(database, pages, links) for _ in range(jobs)]
    running, urls = await new_job(
        database, urls=frontier, max_pages=frontier + 10, host="frontier.test"
    )
    async with database.session() as session:
        await dispatch_urls(session, running, limit=200, publish=fake_publish)
    queued = []
    async with database.session() as session:
        rows = await session.execute(
            text("SELECT id FROM crawl_urls WHERE job_id = :job AND state = 'queued' ORDER BY id"),
            {"job": running},
        )
        queued = [row.id for row in rows]
    for url_id in queued[:20]:
        async with database.session() as session:
            await claim_url(
                session, url_id, owner="crawler-bench", lease_seconds=600, max_attempts=3
            )
    async with database.engine.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text("VACUUM (ANALYZE)"))
    async with database.session() as session:
        sizes = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM crawl_jobs) AS jobs,"
                    " (SELECT count(*) FROM crawl_urls) AS urls,"
                    " (SELECT count(*) FROM pages) AS pages,"
                    " (SELECT count(*) FROM page_links) AS links,"
                    " (SELECT count(*) FROM search_documents) AS documents,"
                    " pg_size_pretty(pg_database_size(current_database())) AS size"
                )
            )
        ).one()
    return {
        "completed_jobs": completed,
        "running_job": running,
        "queued_url": queued[25],
        "pending_url": urls[-1],
        "counts": dict(sizes._mapping),
    }


def statements(data: dict[str, Any]) -> list[tuple[str, Any, dict[str, Any], bool]]:
    """(name, statement, parameters, writes) of every statement measured."""
    job = data["completed_jobs"][0]
    running = data["running_job"]
    return [
        ("claim: row lock", crawl_state._CLAIM_LOCK, {"url_id": data["queued_url"]}, True),
        (
            "claim: update",
            crawl_state._CLAIM,
            {
                "url_id": data["queued_url"],
                "job_id": running,
                "owner": "crawler-x",
                "lease_seconds": 60,
                "max_attempts": 3,
            },
            True,
        ),
        ("inspect a URL", crawl_state._INSPECT, {"url_id": data["queued_url"]}, False),
        ("dispatch: select", crawl_state._DISPATCH_LOCK, {"job_id": running, "limit": 100}, True),
        (
            "admit a level",
            crawl_state._ADMIT_NEXT_LEVEL,
            {"job_id": job, "level": 2, "next_level": 3, "remaining": 100_000},
            True,
        ),
        ("has unfinished URLs", crawl_state._HAS_UNFINISHED, {"job_id": running}, False),
        ("running frontiers", crawl_state._RUNNING_FRONTIERS, {"limit": 1000}, False),
        ("outstanding tasks (all jobs)", _OUTSTANDING, {}, False),
        (
            "reconciliation scan",
            crawl_state._STALE_LOCK,
            {"older_than": 300, "limit": 100, "after_at": None, "after_id": None},
            True,
        ),
        ("cancelling jobs", crawl_cancellation._CANCELLING_JOBS, {"limit": 1000}, False),
        ("index dispatch: select", search_index._DISPATCH_LOCK, {"limit": 200}, True),
        ("index queued count", search_index._QUEUED_COUNT, {}, False),
        ("indexing overview (all documents)", search_index._OVERVIEW, {}, False),
        ("overview: URLs by state", overview._URLS_BY_STATE, {}, False),
        ("overview: documents by status", overview._DOCUMENTS_BY_STATUS, {}, False),
        ("metrics: URLs by state", system_metrics._URLS, {}, False),
    ]


def _scans(node: dict[str, Any]) -> list[str]:
    found = []
    if "Scan" in node["Node Type"]:
        target = node.get("Index Name") or node.get("Relation Name") or ""
        found.append(f"{node['Node Type']} {target}".strip())
    for child in node.get("Plans", []):
        found += _scans(child)
    return found


async def explain(
    connection: AsyncConnection, sql: str, params: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    plan = (
        await connection.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql), params)
    ).scalar_one()
    lines = [
        row[0]
        for row in await connection.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + sql), params)
    ]
    root = plan[0]
    return (
        {
            "planning_ms": round(root["Planning Time"], 3),
            "execution_ms": round(root["Execution Time"], 3),
            "rows": root["Plan"].get("Actual Rows"),
            "scans": sorted(set(_scans(root["Plan"]))),
        },
        "\n".join(lines),
    )


# Session settings the service sets before a statement, in the same transaction, so the
# statement is planned and timed as production runs it.
_SETTINGS: dict[str, Any] = {"admit a level": crawl_state._HASH_JOINS_ONLY}


async def measure(
    database: Database, name: str, statement: Any, params: dict[str, Any], repeat: int
) -> tuple[dict[str, Any], str]:
    sql = str(statement.text)
    setting = _SETTINGS.get(name)
    async with database.engine.connect() as connection:
        transaction = await connection.begin()
        try:
            if setting is not None:
                await connection.execute(setting)
            summary, rendered = await explain(connection, sql, params)
        finally:
            await transaction.rollback()
    timings = []
    for _ in range(repeat):
        async with database.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                if setting is not None:
                    await connection.execute(setting)
                begun = time.perf_counter()
                await connection.execute(statement, params)
                timings.append((time.perf_counter() - begun) * 1000)
            finally:
                await transaction.rollback()
    return {"statement": name, **summary, "ms": distribution(timings)}, rendered


async def captured(database: Database, call: Any) -> list[tuple[str, Any]]:
    """The statements a service call executes (SQL text and driver parameters)."""
    seen: list[tuple[str, Any]] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        seen.append((statement, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with database.session() as session:
            await call(session)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    return seen


async def measure_service(
    database: Database, name: str, call: Any, repeat: int
) -> tuple[dict[str, Any], str]:
    """Time a read-only service call, and EXPLAIN every statement it executed (through the
    driver, with its own parameters)."""
    timings = []
    for _ in range(repeat):
        async with database.session() as session:
            begun = time.perf_counter()
            await call(session)
            timings.append((time.perf_counter() - begun) * 1000)
    rendered: list[str] = []
    plans = []
    for sql, parameters in await captured(database, call):
        if not sql.lstrip().upper().startswith("SELECT"):
            continue
        async with database.engine.connect() as connection:
            raw = await connection.get_raw_connection()
            driver = raw.driver_connection
            if driver is None:
                raise RuntimeError("no driver connection")
            plan = await driver.fetchval(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, *parameters
            )
            text_plan = await driver.fetch("EXPLAIN (ANALYZE, BUFFERS) " + sql, *parameters)
        root = (plan if isinstance(plan, list) else json.loads(plan))[0]
        plans.append(
            {
                "execution_ms": round(root["Execution Time"], 3),
                "scans": sorted(set(_scans(root["Plan"]))),
            }
        )
        rendered.append("\n".join(row[0] for row in text_plan))
    return {"statement": name, "ms": distribution(timings), "statements": plans}, "\n\n".join(
        rendered
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jobs", type=int, default=10, help="completed jobs")
    parser.add_argument("--pages", type=int, default=5000, help="stored pages per completed job")
    parser.add_argument("--links", type=int, default=20, help="links per page")
    parser.add_argument(
        "--frontier", type=int, default=2000, help="pending URLs of the running job"
    )
    parser.add_argument("--repeat", type=int, default=30, help="timed executions per statement")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plans", type=Path, required=True)
    args = parser.parse_args()

    settings = bench_settings()
    await fresh_database(settings)
    database = Database(settings, application_name="sitescope-benchmark")
    results: list[dict[str, Any]] = []
    plan_text: list[str] = []
    try:
        env_info = await environment(settings, database)
        began = time.perf_counter()
        data = await build(database, args.jobs, args.pages, args.links, args.frontier)
        build_seconds = round(time.perf_counter() - began, 1)
        emit({"dataset": data["counts"], "build_seconds": build_seconds})
        for name, statement, params, _writes in statements(data):
            result, rendered = await measure(database, name, statement, params, args.repeat)
            results.append(result)
            plan_text.append(f"--- {name} ---\n{rendered}\n")
            emit({key: result[key] for key in ("statement", "execution_ms", "scans", "ms")})
        job = data["completed_jobs"][0]
        for name, call in (
            (
                "GET /metrics: the PostgreSQL snapshot",
                lambda session: read_system_state(session, timeout_seconds=10),
            ),
            (
                "GET /api/v1/overview: the snapshot",
                lambda session: system_overview(session, timeout_seconds=10),
            ),
            ("job list (first page of 20)", lambda session: list_crawl_jobs(session, limit=20)),
            ("job detail (a completed job)", lambda session: get_crawl_job(session, job)),
            (
                "job detail (the running job)",
                lambda session: get_crawl_job(session, data["running_job"]),
            ),
        ):
            result, rendered = await measure_service(database, name, call, args.repeat)
            results.append(result)
            plan_text.append(f"--- {name} ---\n{rendered}\n")
            emit({key: result[key] for key in ("statement", "ms", "statements")})
    finally:
        await database.dispose()
        await helpers.drop_database(settings)
    counts = {
        key: (str(value) if isinstance(value, uuid.UUID) else value)
        for key, value in data["counts"].items()
    }
    write_json(
        args.output,
        {
            "benchmark": "db_queries",
            "environment": env_info,
            "configuration": {
                "completed_jobs": args.jobs,
                "pages_per_job": args.pages,
                "links_per_page": args.links,
                "frontier_urls": args.frontier,
                "repeat": args.repeat,
                "build_seconds": build_seconds,
                "dataset": counts,
            },
            "results": results,
        },
    )
    args.plans.write_text("\n".join(plan_text), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    asyncio.run(main())
