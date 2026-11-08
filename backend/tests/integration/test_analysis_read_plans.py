"""Whole-job analysis reads with statistics that have not seen the job (Phase 10 regression).

With autovacuum off, so that the statistics stay as an earlier job left them, the benchmark
measured the job's statistics at 5.6 s instead of 15 ms, and the links of one class at 5.8 s
instead of 11 ms; with a larger job both ran into the analysis time limit (10 s, an HTTP 503)
(benchmarks/admission_plans.py). The planner estimated each of the new job's tables at one row
and chained nested loops that scanned the job's pages and URLs again for every link; for a job
of 11,111 pages every read but the graph ran into the limit. Every analysis read now runs with
hash joins only (site_analysis._read_only; ADR-0023), a page of links included: it costs more
with fresh statistics (19.5 ms instead of 0.5 for 22,220 links), but stays linear.

The test checks the plans' work (rows produced, a count) and the statements, not time.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.services import site_analysis
from app.services.site_analysis import LinkClass
from tests.integration.test_admission_plan import analyze, build_job

pytestmark = pytest.mark.anyio

Read = Callable[[AsyncSession], Awaitable[Any]]


def work(node: dict[str, Any]) -> int:
    """Rows produced by a plan node and its children, over all their loops: the work a plan did
    (a quadratic plan's grows with the square of the job)."""
    rows = round(float(node["Actual Rows"]) * int(node["Actual Loops"]))
    return rows + sum(work(child) for child in node.get("Plans", []))


async def plan_work(database: Database, job_id: uuid.UUID, *, hash_joins_only: bool) -> int:
    """``EXPLAIN (ANALYZE)`` of the link classification counts, rolled back."""
    statement = text("EXPLAIN (ANALYZE, FORMAT JSON) " + site_analysis._LINK_CLASS_COUNTS.text)
    async with database.engine.connect() as connection:
        transaction = await connection.begin()
        try:
            if hash_joins_only:
                await connection.execute(site_analysis._HASH_JOINS_ONLY)
            document = (await connection.execute(statement, {"job_id": job_id})).scalar_one()
        finally:
            await transaction.rollback()
    return work(document[0]["Plan"])


async def statements_of(database: Database, read: Read) -> list[str]:
    executed: list[str] = []

    def record(*args: Any) -> None:  # (connection, cursor, statement, parameters, ...)
        executed.append(" ".join(str(args[2]).split()))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with database.session() as session:
            await read(session)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return executed


def planned_with_hash_joins(executed: list[str]) -> bool:
    """The setting comes before the first statement that reads the job's links or pages."""
    setting = [index for index, sql in enumerate(executed) if "enable_nestloop = off" in sql]
    reads = [
        index
        for index, sql in enumerate(executed)
        if "page_links" in sql or "FROM pages" in sql or "WITH RECURSIVE" in sql
    ]
    return bool(setting) and bool(reads) and setting[0] < reads[0]


async def test_analysis_reads_use_hash_joins_when_the_statistics_have_not_seen_the_job(
    database: Database,
) -> None:
    # The statistics describe an earlier job only.
    await build_job(database, "earlier.test", [1, 10, 100, 1000], links=12, targets=3000)
    await analyze(database)
    # A new job stored since: 211 URLs, its 200 pages of the last level with 12 links each.
    job_id = await build_job(database, "new.test", [1, 10, 200], links=12, targets=900)
    links = 200 * 12

    # The scenario reproduces the problem: planned freely, the classification's work grows with
    # the number of links times the number of URLs; with hash joins, with their sum.
    free = await plan_work(database, job_id, hash_joins_only=False)
    hashed = await plan_work(database, job_id, hash_joins_only=True)
    assert free > 50 * links, free
    assert hashed < 10 * links, hashed

    reads: dict[str, Read] = {
        "statistics": lambda s: site_analysis.crawl_stats(s, job_id),
        "broken links": lambda s: site_analysis.list_broken_links(s, job_id, limit=50),
        "links of a class": lambda s: site_analysis.list_links(
            s, job_id, limit=50, classification=LinkClass.BROKEN
        ),
        "verified links": lambda s: site_analysis.list_links(s, job_id, limit=50, verified=True),
        "a page of links": lambda s: site_analysis.list_links(s, job_id, limit=50),
        "one page's links": lambda s: site_analysis.list_links(
            s, job_id, limit=50, source_page_id=uuid.uuid4()
        ),
        "a page of pages": lambda s: site_analysis.list_pages(s, job_id, limit=50),
        "the graph": lambda s: site_analysis.link_graph(s, job_id, max_nodes=300, max_edges=1500),
    }
    for name, read in reads.items():
        assert planned_with_hash_joins(await statements_of(database, read)), name
