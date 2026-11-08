"""The level admission with statistics that have not seen the job (Phase 10 regression).

The crawl benchmarks found BFS level transitions that took 18 to 22 s: PostgreSQL planned the
admission statement from statistics that described earlier jobs only, estimated every row count
of the new job at one, and chose nested loops that scanned the whole job for each link
(benchmarks/admission_plans.py: over 5 s for this size, over 60 s for a level of 1,393 URLs).
The scheduler now admits a level with hash joins only (crawl_state.advance_job).

The test checks the plan and the statements, not the time (no timing assertion: the slow plan
took over 5 s here, the fast one milliseconds, but a loaded machine must not fail the test).
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import event, text

from app.db.session import Database
from app.services import crawl_state
from app.services.crawl_state import AdvanceAction, advance_job
from tests.crawl_support import execute, new_job

pytestmark = pytest.mark.anyio

URLS = """
INSERT INTO crawl_urls (job_id, normalized_url, depth, state, outcome, http_status, result_epoch,
                        finished_at)
SELECT :job_id, 'https://' || CAST(:host AS text) || '/p' || g, CAST(:depth AS smallint),
       'fetched', 'html_stored', 200, 0, clock_timestamp()
FROM generate_series(CAST(:first AS integer), CAST(:last AS integer)) AS g
"""
PAGES = """
INSERT INTO pages (job_id, crawl_url_id, normalized_url, http_status, content_type, title,
                   meta_description, headings, extracted_text, content_hash, byte_size, fetched_at)
SELECT u.job_id, u.id, u.normalized_url, 200, 'text/html', 'Page', 'A page', '', 'body',
       sha256(convert_to(u.normalized_url, 'UTF8')), 4, clock_timestamp()
FROM crawl_urls AS u
WHERE u.job_id = :job_id AND u.outcome = 'html_stored'
  AND NOT EXISTS (SELECT 1 FROM pages AS p WHERE p.crawl_url_id = u.id)
"""
# Each page of the level links to ``:links`` targets of the next level (shared between pages,
# so many are duplicates) and back to the seed.
LINKS = """
INSERT INTO page_links (source_page_id, target_normalized_url, job_id, anchor_text, position,
                        is_internal, nofollow)
SELECT p.id, 'https://' || CAST(:host AS text) || '/next'
                || ((n * 7 + k * 13) % CAST(:targets AS integer)),
       :job_id, 'link', k, true, false
FROM (SELECT p.id, row_number() OVER (ORDER BY p.id) AS n
      FROM pages AS p JOIN crawl_urls AS u ON u.id = p.crawl_url_id
      WHERE p.job_id = :job_id AND u.depth = CAST(:depth AS smallint)) AS p,
     generate_series(1, CAST(:links AS integer)) AS k
ON CONFLICT DO NOTHING
"""


async def build_job(
    database: Database, host: str, levels: list[int], *, links: int, targets: int
) -> uuid.UUID:
    """A running job whose levels are stored; its last level links to ``targets`` new URLs."""
    job_id, _ = await new_job(database, max_depth=10, max_pages=100_000, host=host)
    await execute(
        database,
        "UPDATE crawl_urls SET state = 'fetched', outcome = 'html_stored', http_status = 200,"
        " result_epoch = 0, finished_at = clock_timestamp() WHERE job_id = :job_id",
        job_id=job_id,
    )
    first = 1
    for depth, count in enumerate(levels[1:], start=1):
        await execute(
            database,
            URLS,
            job_id=job_id,
            host=host,
            depth=depth,
            first=first,
            last=first + count - 1,
        )
        first += count
    await execute(database, PAGES, job_id=job_id)
    await execute(
        database,
        LINKS,
        job_id=job_id,
        host=host,
        depth=len(levels) - 1,
        links=links,
        targets=targets,
    )
    await execute(
        database,
        "UPDATE crawl_jobs SET current_level = :level,"
        " admitted_count = (SELECT count(*) FROM crawl_urls WHERE job_id = :job_id)"
        " WHERE id = :job_id",
        job_id=job_id,
        level=len(levels) - 1,
    )
    return job_id


async def analyze(database: Database) -> None:
    async with database.engine.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text("ANALYZE crawl_urls, pages, page_links"))


def node_types(node: dict[str, Any]) -> list[str]:
    found = [str(node["Node Type"])]
    for child in node.get("Plans", []):
        found += node_types(child)
    return found


async def admission_plan(
    database: Database, job_id: uuid.UUID, *, hash_joins_only: bool
) -> list[str]:
    """The node types of the admission's plan from the statistics as they are (EXPLAIN without
    ANALYZE: nothing is executed)."""
    params = {"job_id": job_id, "level": 2, "next_level": 3, "remaining": 10_000}
    async with database.engine.connect() as connection:
        transaction = await connection.begin()
        try:
            if hash_joins_only:
                await connection.execute(crawl_state._HASH_JOINS_ONLY)
            explain = text("EXPLAIN (FORMAT JSON) " + crawl_state._ADMIT_NEXT_LEVEL.text)
            document = (await connection.execute(explain, params)).scalar_one()
        finally:
            await transaction.rollback()
    return node_types(document[0]["Plan"])


async def test_a_level_is_admitted_with_hash_joins_when_the_statistics_have_not_seen_the_job(
    database: Database,
) -> None:
    # The statistics describe an earlier job only.
    await build_job(database, "earlier.test", [1, 10, 100, 1000], links=12, targets=3000)
    await analyze(database)
    # A new job, stored since: levels 0 to 2, its 200 pages of level 2 linking to 900 targets.
    job_id = await build_job(database, "new.test", [1, 10, 200], links=12, targets=900)

    # The scenario reproduces the problem: planned freely, the admission nests loops.
    assert "Nested Loop" in await admission_plan(database, job_id, hash_joins_only=False)
    assert "Nested Loop" not in await admission_plan(database, job_id, hash_joins_only=True)

    executed: list[str] = []

    def record(*args: Any) -> None:  # (connection, cursor, statement, parameters, ...)
        executed.append(str(args[2]))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with database.session() as session:
            result = await advance_job(session, job_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert result.action is AdvanceAction.ADVANCED
    assert result.admitted == 900
    # The service runs the admission under the setting, and restores it right after.
    admission = next(
        index for index, statement in enumerate(executed) if "WITH candidates AS" in statement
    )
    settings = [
        (index, statement.strip())
        for index, statement in enumerate(executed)
        if "enable_nestloop" in statement
    ]
    before = [statement for index, statement in settings if index < admission]
    after = [statement for index, statement in settings if index > admission]
    assert before[-1:] == ["SET LOCAL enable_nestloop = off"]
    assert after[:1] == ["SET LOCAL enable_nestloop = DEFAULT"]
