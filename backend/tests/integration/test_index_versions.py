"""Stale index tasks can never replace a newer index (ADR-0018), proven deterministically.

The interleavings are forced with row locks held by test transactions, and each step waits until
PostgreSQL shows the other transaction blocked on a lock: the order of events is fixed by the
database, not by task arrival or sleeps. Layers under test:

1. the index statement reads the page under ``FOR SHARE`` and writes the vector together with
   the version it was built from, only while the document is ``queued``;
2. its version guard writes nothing over a newer indexed version;
3. triggers (migration 0004) reject any update that moves a version back.
"""

import asyncio
import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.models import IndexStatus
from app.db.session import Database
from app.queue.messages import IndexTask
from app.queue.streams import IndexStream
from app.services.search_index import (
    IndexOutcome,
    IndexResult,
    IndexTaskRef,
    dispatch_index_tasks,
    index_document,
    inspect_document,
    start_index_attempt,
)
from app.workers.consumer import Disposition, Handled
from app.workers.indexer import judge_unstarted
from tests.crawl_support import new_job
from tests.index_support import (
    CHANGE_PAGE,
    RESET_DOCUMENT,
    add_page,
    change_content,
    document,
    execute,
    index_everything,
    lexemes,
    publisher,
    until_sessions_wait_for_locks,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def stream(redis_client: Redis, redis_prefix: str) -> IndexStream:
    stream = IndexStream(redis_client, key_prefix=redis_prefix)
    await stream.ensure_group()
    return stream


async def dispatch(database: Database, stream: IndexStream) -> None:
    async with database.session() as session:
        await dispatch_index_tasks(session, limit=100, publish=publisher(stream))


async def start(database: Database, task: IndexTaskRef, owner: str = "indexer-a") -> bool:
    async with database.session() as session:
        attempt = await start_index_attempt(
            session, task, owner=owner, lease_seconds=60, max_attempts=3
        )
    return attempt is not None


async def index(database: Database, task: IndexTaskRef) -> IndexResult:
    async with database.session() as session:
        return await index_document(session, task, max_body_chars=100_000, budget_seconds=20)


async def queued_page(
    database: Database, stream: IndexStream, body: str
) -> tuple[uuid.UUID, uuid.UUID]:
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body=body)
    await dispatch(database, stream)
    return job_id, page_id


# --- Triggers ------------------------------------------------------------------------------------


async def test_the_indexed_version_can_never_move_back_or_be_cleared(
    database: Database, stream: IndexStream
) -> None:
    job_id, page_id = await queued_page(database, stream, "v1")
    await execute(
        database,
        "UPDATE search_documents SET indexed_content_version = 2 WHERE page_id = :id",
        id=page_id,
    )

    for value in ("1", "NULL"):
        with pytest.raises(DBAPIError, match="may not move back"):
            await execute(
                database,
                f"UPDATE search_documents SET indexed_content_version = {value}"  # noqa: S608
                " WHERE page_id = :id",
                id=page_id,
            )
    await execute(  # the same version again, or a newer one, is fine
        database,
        "UPDATE search_documents SET indexed_content_version = 2 WHERE page_id = :id",
        id=page_id,
    )
    await execute(
        database,
        "UPDATE search_documents SET indexed_content_version = 3 WHERE page_id = :id",
        id=page_id,
    )
    assert (await document(database, page_id)).indexed_content_version == 3
    assert job_id


async def test_a_pages_content_version_can_never_move_back(database: Database) -> None:
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", content_version=5)

    with pytest.raises(DBAPIError, match="may not move back"):
        await execute(database, "UPDATE pages SET content_version = 4 WHERE id = :id", id=page_id)
    await execute(database, "UPDATE pages SET content_version = 5 WHERE id = :id", id=page_id)
    await execute(database, "UPDATE pages SET content_version = 6 WHERE id = :id", id=page_id)


# --- The version guard ---------------------------------------------------------------------------


async def test_the_guard_writes_nothing_over_a_newer_indexed_version(
    database: Database, stream: IndexStream
) -> None:
    """A document whose index is newer than its page (a state no service produces) is left as
    it is: the statement's guard stops the write before the trigger would have to."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="older words", content_version=2)
    await dispatch(database, stream)
    await execute(
        database,
        "UPDATE search_documents SET indexed_content_version = 3,"
        " search_vector = to_tsvector('english', 'newer') WHERE page_id = :id",
        id=page_id,
    )
    task = IndexTaskRef(job_id, page_id, 2)
    assert await start(database, task)

    result = await index(database, task)

    assert (result.outcome, result.status) == (IndexOutcome.NOT_QUEUED, IndexStatus.QUEUED)
    row = await document(database, page_id)
    assert row.indexed_content_version == 3
    assert await lexemes(database, page_id) == {"newer"}


# --- Out-of-order completion ---------------------------------------------------------------------


async def test_a_task_for_an_old_version_that_arrives_last_changes_nothing(
    database: Database, stream: IndexStream
) -> None:
    """Version 2 is indexed first; the version-1 task then arrives (order reversed)."""
    job_id, page_id = await queued_page(database, stream, "original words")
    old = IndexTaskRef(job_id, page_id, 1)
    assert await change_content(database, page_id, body="replacement text") == 2
    await dispatch(database, stream)
    new = IndexTaskRef(job_id, page_id, 2)
    assert await start(database, new)
    assert (await index(database, new)).content_version == 2

    assert not await start(database, old, owner="indexer-late")
    async with database.session() as session:
        status = await inspect_document(session, page_id)
    verdict = judge_unstarted(status, IndexTask(job_id, page_id, 1), max_attempts=3)

    assert verdict == Handled(Disposition.ACK, "already_indexed")
    row = await document(database, page_id)
    assert (row.status, row.indexed_content_version, row.attempt_count) == ("indexed", 2, 1)
    assert await lexemes(database, page_id) == {"replac", "text"}


async def test_an_old_task_delivered_while_the_new_version_waits_indexes_the_new_version(
    database: Database, stream: IndexStream
) -> None:
    """The version-1 task arrives while version 2 is queued: it does version 2's work, from
    the locked current page, and the version-2 task then finds it done."""
    job_id, page_id = await queued_page(database, stream, "original words")
    await change_content(database, page_id, body="replacement text")
    await dispatch(database, stream)
    old = IndexTaskRef(job_id, page_id, 1)

    assert await start(database, old)
    result = await index(database, old)

    assert (result.outcome, result.content_version) == (IndexOutcome.INDEXED, 2)
    assert await lexemes(database, page_id) == {"replac", "text"}
    assert not await start(database, IndexTaskRef(job_id, page_id, 2), owner="indexer-b")
    async with database.session() as session:
        status = await inspect_document(session, page_id)
    assert judge_unstarted(status, IndexTask(job_id, page_id, 2), max_attempts=3) == Handled(
        Disposition.ACK, "already_indexed"
    )
    assert (await document(database, page_id)).attempt_count == 1


# --- Content changes racing an index transaction -------------------------------------------------


async def test_a_content_change_that_commits_while_indexing_waits_wins(
    database: Database, stream: IndexStream
) -> None:
    """The change holds the page row; the index statement waits for it, then finds the document
    pending again and writes nothing. The new version is indexed afterwards."""
    job_id, page_id = await queued_page(database, stream, "original words")
    task = IndexTaskRef(job_id, page_id, 1)
    assert await start(database, task)

    async with database.session() as change:
        await change.execute(CHANGE_PAGE, {"page_id": page_id, "title": None, "body": "new text"})
        indexing = asyncio.create_task(index(database, task))
        await until_sessions_wait_for_locks(database)
        assert not indexing.done()
        await change.execute(RESET_DOCUMENT, {"page_id": page_id})
        await change.commit()
    result = await asyncio.wait_for(indexing, timeout=10)

    assert (result.outcome, result.status) == (IndexOutcome.NOT_QUEUED, IndexStatus.PENDING)
    row = await document(database, page_id)
    assert (row.indexed_content_version, row.search_vector) == (None, None)
    await index_everything(database, stream)
    row = await document(database, page_id)
    assert (row.status, row.indexed_content_version) == ("indexed", 2)
    assert await lexemes(database, page_id) == {"new", "text"}


async def test_a_content_change_waits_for_an_index_transaction_that_read_the_page(
    database: Database, stream: IndexStream
) -> None:
    """The index statement holds the page (FOR SHARE) while it waits for the document row; the
    change must wait for it. The index commits version 1 with version 1's vector, then the change
    sets the document pending: the vector and its version never disagree, and search hides the
    replaced content until version 2 is indexed."""
    job_id, page_id = await queued_page(database, stream, "original words")
    task = IndexTaskRef(job_id, page_id, 1)
    assert await start(database, task)

    async with database.session() as holder:
        await holder.execute(
            text("SELECT 1 FROM search_documents WHERE page_id = :id FOR UPDATE"), {"id": page_id}
        )
        indexing = asyncio.create_task(index(database, task))
        await until_sessions_wait_for_locks(database, 1)  # the index waits for the document
        changing = asyncio.create_task(change_content(database, page_id, body="new text"))
        await until_sessions_wait_for_locks(database, 2)  # the change waits for the page
        assert not indexing.done()
        assert not changing.done()
        await holder.rollback()

    result = await asyncio.wait_for(indexing, timeout=10)
    assert await asyncio.wait_for(changing, timeout=10) == 2

    assert (result.outcome, result.content_version) == (IndexOutcome.INDEXED, 1)
    row = await document(database, page_id)
    assert (row.status, row.indexed_content_version) == ("pending", 1)
    assert await lexemes(database, page_id) == {"origin", "word"}  # version 1's own vector
    await index_everything(database, stream)
    row = await document(database, page_id)
    assert (row.status, row.indexed_content_version) == ("indexed", 2)
    assert await lexemes(database, page_id) == {"new", "text"}


# --- Concurrent indexers -------------------------------------------------------------------------


async def test_two_indexers_with_duplicate_messages_index_once(
    database: Database, stream: IndexStream
) -> None:
    job_id, page_id = await queued_page(database, stream, "shared document")
    task = IndexTaskRef(job_id, page_id, 1)

    started = await asyncio.gather(
        start(database, task, "indexer-a"), start(database, task, "indexer-b")
    )
    assert sorted(started) == [False, True]  # one attempt; the duplicate sees a live lease
    results = await asyncio.gather(index(database, task), index(database, task))

    assert sorted(result.outcome.value for result in results) == ["indexed", "not_queued"]
    row = await document(database, page_id)
    assert (row.status, row.indexed_content_version, row.attempt_count) == ("indexed", 1, 1)
