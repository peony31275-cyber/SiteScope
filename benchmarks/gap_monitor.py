r"""A read-only monitor of the benchmark database while a benchmark runs (Phase 10 diagnostics).

Every half second it reads, from ``<SITESCOPE_POSTGRES_DB>_test_bench``: the jobs' status and
level, the unfinished URLs by depth and state (printed when they change), and every session that
has been running a statement or a transaction for more than a second (its application, state,
wait event and query). When such a session runs the scheduler's level admission, it prints
``EXPLAIN`` of the same statement with the same parameters (the plan the current statistics
give; nothing is executed) and the tables' analyze state. It changes nothing.

This is how the slow BFS level transitions of the crawl benchmarks were diagnosed: the raw output
of that run is ``results/phase10-admission-gap-monitor.txt``. Run it from backend\ beside a
benchmark, for at most ``seconds``::

    .\.venv\Scripts\python.exe ..\benchmarks\gap_monitor.py 300
"""

import asyncio
import sys
import time
from typing import Any

from benchlib import bench_settings
from sqlalchemy import text

from app.db.session import Database
from app.services.crawl_state import _ADMIT_NEXT_LEVEL, _HASH_JOINS_ONLY

STATES = text(
    "SELECT depth, state, count(*) AS n FROM crawl_urls"
    " WHERE state IN ('pending', 'queued', 'in_progress') GROUP BY 1, 2 ORDER BY 1, 2"
)
SESSIONS = text(
    r"""
    SELECT pid, application_name AS app, state, wait_event_type AS wtype, wait_event AS wevent,
           round(extract(epoch FROM clock_timestamp() - xact_start)::numeric, 2) AS xact_s,
           round(extract(epoch FROM clock_timestamp() - query_start)::numeric, 2) AS query_s,
           left(regexp_replace(query, '\s+', ' ', 'g'), 160) AS query
    FROM pg_stat_activity
    WHERE datname = current_database() AND pid <> pg_backend_pid() AND state <> 'idle'
    """
)
JOBS = text("SELECT status, current_level FROM crawl_jobs")
TABLE_STATS = text(
    "SELECT relname, n_live_tup, n_mod_since_analyze, last_autoanalyze, last_analyze,"
    " (SELECT reltuples FROM pg_class AS c WHERE c.oid = s.relid) AS reltuples"
    " FROM pg_stat_user_tables AS s WHERE relname IN ('crawl_urls', 'pages', 'page_links')"
)
RUNNING_JOBS = text(
    "SELECT id, current_level, max_pages - admitted_count AS remaining FROM crawl_jobs"
    " WHERE status = 'running' ORDER BY created_at, id LIMIT 5"
)


def say(*parts: object) -> None:
    print(*parts, flush=True)  # noqa: T201 - the monitor's output


async def explain_admission(database: Database) -> None:
    """``EXPLAIN`` (without ANALYZE) of the admission for each running job (the monitor cannot
    tell which job the slow session admits): planned freely, and as the scheduler runs it since
    Phase 10 (nested loops off). Rolled back; nothing is executed."""
    async with database.engine.connect() as connection:
        jobs = (await connection.execute(RUNNING_JOBS)).all()
        for job in jobs:
            params = {
                "job_id": job.id,
                "level": job.current_level,
                "next_level": job.current_level + 1,
                "remaining": job.remaining,
            }
            for label, setting in (("free", None), ("as run", _HASH_JOINS_ONLY)):
                transaction = await connection.begin()
                try:
                    if setting is not None:
                        await connection.execute(setting)
                    plan = await connection.execute(
                        text("EXPLAIN " + str(_ADMIT_NEXT_LEVEL.text)), params
                    )
                    say(f"PLAN job {job.id} ({label})\n" + "\n".join(row[0] for row in plan))
                finally:
                    await transaction.rollback()
        for row in await connection.execute(TABLE_STATS):
            say("STATS", dict(row._mapping))


def slow(session: dict[str, Any]) -> bool:
    """A statement or a transaction that has been running for more than a second."""
    return (session["query_s"] or 0) > 1.0 or (session["xact_s"] or 0) > 1.0


async def main(seconds: float) -> None:
    database = Database(bench_settings(), application_name="sitescope-gap-monitor")
    started = time.monotonic()
    last: object = None
    explained = 0
    try:
        while time.monotonic() - started < seconds:
            try:
                async with database.session() as session:
                    states = [tuple(row) for row in await session.execute(STATES)]
                    sessions = [dict(row._mapping) for row in await session.execute(SESSIONS)]
                    jobs = [tuple(row) for row in await session.execute(JOBS)]
            except Exception:  # the database may not exist yet, or be between runs
                await asyncio.sleep(1)
                continue
            stamp = f"{time.monotonic() - started:7.2f}"
            picture = (tuple(states), tuple(jobs))
            if picture != last:
                say(stamp, "jobs", jobs, "unfinished", states)
                last = picture
            for item in filter(slow, sessions):
                say(stamp, "SESSION", item)
                if "WITH candidates" in (item["query"] or "") and explained < 3:
                    explained += 1
                    await explain_admission(database)
            await asyncio.sleep(0.5)
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 300.0))
