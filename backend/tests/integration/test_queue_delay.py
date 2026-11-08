"""Queue delay by the database clock (Phase 10): a claim of a queued URL and the first attempt
of a queued search document return the time since their dispatch; a takeover and a later
attempt do not (their delay would include the lost attempt)."""

from collections.abc import Sequence

import pytest

from app.db.session import Database
from app.services.crawl_state import claim_url, dispatch_urls
from app.services.search_index import IndexTaskRef, dispatch_index_tasks, start_index_attempt
from tests.crawl_support import age_dispatch, execute, expire_lease, new_job
from tests.index_support import add_page

pytestmark = pytest.mark.anyio


async def fake_publish(tasks: Sequence[object]) -> list[str]:
    return [f"1-{index}" for index in range(len(tasks))]


async def test_a_claim_of_a_queued_url_returns_its_wait_and_a_takeover_does_not(
    database: Database,
) -> None:
    job_id, [seed] = await new_job(database)
    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=1, publish=fake_publish)
    await age_dispatch(database, seed, seconds=30)

    async with database.session() as session:
        first = await claim_url(session, seed, owner="crawler-a", lease_seconds=60, max_attempts=3)
    await expire_lease(database, seed)
    async with database.session() as session:
        takeover = await claim_url(
            session, seed, owner="crawler-b", lease_seconds=60, max_attempts=3
        )

    assert first is not None
    assert first.queued_seconds is not None
    assert 30 <= first.queued_seconds < 300
    assert takeover is not None
    assert takeover.takeover
    assert takeover.queued_seconds is None


async def test_the_first_index_attempt_returns_its_wait_and_a_later_one_does_not(
    database: Database,
) -> None:
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="page")
    async with database.session() as session:
        await dispatch_index_tasks(session, limit=1, publish=fake_publish)
    await execute(
        database,
        "UPDATE search_documents SET dispatched_at = clock_timestamp() - interval '30 seconds'"
        " WHERE page_id = :id",
        id=page_id,
    )
    ref = IndexTaskRef(job_id, page_id, 1)

    async with database.session() as session:
        first = await start_index_attempt(
            session, ref, owner="indexer-a", lease_seconds=60, max_attempts=3
        )
    await execute(
        database,
        "UPDATE search_documents SET lease_expires_at = clock_timestamp() - interval '1 second'"
        " WHERE page_id = :id",
        id=page_id,
    )
    async with database.session() as session:
        second = await start_index_attempt(
            session, ref, owner="indexer-b", lease_seconds=60, max_attempts=3
        )

    assert first is not None
    assert first.attempt == 1
    assert first.queued_seconds is not None
    assert 30 <= first.queued_seconds < 300
    assert second is not None
    assert second.attempt == 2
    assert second.queued_seconds is None
