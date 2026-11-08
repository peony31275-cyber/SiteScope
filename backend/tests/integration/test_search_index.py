"""The index state services (app/services/search_index.py) against PostgreSQL and Redis.

Covers the indexing outbox (dispatch inside the transaction, SKIP LOCKED, rollback after
XADD), attempts counted in PostgreSQL with advisory leases, the vector written from the page's
current content with its weights and size limits, deterministic failures, job indexing
completion, reindexing, reconciliation of lost messages, and the statistics.
"""

import asyncio
import time
import uuid
from collections.abc import Iterator, Sequence

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.errors import NotFoundError
from app.db.models import IndexStatus, JobStatus
from app.db.session import Database
from app.indexing.documents import DEADLINE_EXCEEDED, MAX_ATTEMPTS_EXCEEDED, TSVECTOR_TOO_LARGE
from app.queue.messages import IndexTask
from app.queue.streams import IndexStream
from app.services.search_index import (
    IndexAttempt,
    IndexDeadlineError,
    IndexOutcome,
    IndexResult,
    IndexTaskRef,
    completable_indexing_jobs,
    complete_job_indexing,
    dispatch_index_tasks,
    fail_exhausted_document,
    index_document,
    indexing_overview,
    inspect_document,
    job_indexing,
    note_attempt_error,
    queued_index_tasks,
    republish_stale_index_tasks,
    request_reindex,
    sqlstate,
    start_index_attempt,
)
from app.workers.consumer import Disposition, Handled
from app.workers.indexer import Retry, judge_unstarted
from tests.crawl_support import events, job_row, new_job
from tests.index_support import (
    add_page,
    change_content,
    document,
    execute,
    finish_job,
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


async def dispatch(database: Database, stream: IndexStream, limit: int = 100) -> list[uuid.UUID]:
    async with database.session() as session:
        return await dispatch_index_tasks(session, limit=limit, publish=publisher(stream))


async def messages(stream: IndexStream, consumer: str = "indexer-a") -> list[IndexTask]:
    deliveries = await stream.read(consumer, count=1000, block_ms=10)
    return [delivery.task for delivery in deliveries if delivery.task is not None]


async def start(
    database: Database,
    task: IndexTaskRef,
    *,
    owner: str = "indexer-a",
    lease_seconds: float = 60,
    max_attempts: int = 3,
) -> IndexAttempt | None:
    async with database.session() as session:
        return await start_index_attempt(
            session, task, owner=owner, lease_seconds=lease_seconds, max_attempts=max_attempts
        )


async def index(
    database: Database,
    task: IndexTaskRef,
    *,
    max_body_chars: int = 100_000,
    budget: float = 20.0,
) -> IndexResult:
    async with database.session() as session:
        return await index_document(
            session, task, max_body_chars=max_body_chars, budget_seconds=budget
        )


async def judged(database: Database, task: IndexTaskRef, max_attempts: int = 3) -> Handled | Retry:
    async with database.session() as session:
        status = await inspect_document(session, task.page_id)
    message = IndexTask(task.job_id, task.page_id, task.content_version)
    return judge_unstarted(status, message, max_attempts=max_attempts)


async def expire_index_lease(database: Database, page_id: uuid.UUID) -> None:
    await execute(
        database,
        "UPDATE search_documents SET lease_expires_at = clock_timestamp() - interval '1 second'"
        " WHERE page_id = :id AND lease_owner IS NOT NULL",
        id=page_id,
    )


async def age_index_dispatch(database: Database, page_id: uuid.UUID, seconds: float) -> None:
    await execute(
        database,
        "UPDATE search_documents"
        " SET dispatched_at = clock_timestamp() - make_interval(secs => :s) WHERE page_id = :id",
        id=page_id,
        s=seconds,
    )


def task_of(job_id: uuid.UUID, page_id: uuid.UUID, version: int = 1) -> IndexTaskRef:
    return IndexTaskRef(job_id, page_id, version)


# --- I1: dispatch --------------------------------------------------------------------------------


async def test_dispatch_publishes_pending_documents_with_their_content_version(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    first = await add_page(database, job_id, path="a")
    second = await add_page(database, job_id, path="b", content_version=3)
    await add_page(database, job_id, path="hidden", noindex=True)  # no search document

    dispatched = await dispatch(database, stream)

    assert sorted(dispatched) == sorted([first, second])
    published = {task.page_id: task for task in await messages(stream)}
    assert published == {
        first: IndexTask(job_id, first, 1),
        second: IndexTask(job_id, second, 3),
    }
    for page_id in (first, second):
        row = await document(database, page_id)
        assert row.status == IndexStatus.QUEUED.value
        assert row.dispatched_at is not None
        assert row.stream_entry_id is not None
    assert await stream.length() == 2


async def test_dispatch_takes_the_oldest_change_first_and_respects_its_limit(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    pages = [await add_page(database, job_id, path=f"p{index}") for index in range(3)]

    assert await dispatch(database, stream, limit=2) == pages[:2]
    assert await dispatch(database, stream, limit=2) == pages[2:]
    assert await dispatch(database, stream, limit=2) == []
    async with database.session() as session:
        assert await queued_index_tasks(session) == 3


async def test_concurrent_dispatchers_never_publish_a_document_twice(
    database: Database, stream: IndexStream
) -> None:
    """Both hold their row locks inside XADD at the same time (the barrier): SKIP LOCKED gives
    each a disjoint set of rows."""
    job_id, _ = await new_job(database)
    pages = {await add_page(database, job_id, path=f"p{index}") for index in range(20)}
    barrier = asyncio.Barrier(2)
    publish = publisher(stream)

    async def slow_publish(tasks: Sequence[IndexTaskRef]) -> list[str]:
        await barrier.wait()
        return await publish(tasks)

    async def one() -> list[uuid.UUID]:
        async with database.session() as session:
            return await dispatch_index_tasks(session, limit=10, publish=slow_publish)

    first, second = await asyncio.wait_for(asyncio.gather(one(), one()), timeout=10)

    assert len(first) == len(second) == 10
    assert set(first) | set(second) == pages
    assert not set(first) & set(second)
    assert await stream.length() == 20


async def test_a_dispatch_that_fails_after_xadd_leaves_the_documents_pending(
    database: Database, stream: IndexStream
) -> None:
    """Failure window: the dispatcher dies between XADD and its commit. The rows stay pending
    and are published again; the orphan message finds nothing to do, or does the work once."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a")
    publish = publisher(stream)

    async def publish_then_crash(tasks: Sequence[IndexTaskRef]) -> list[str]:
        await publish(tasks)
        raise ConnectionError("the dispatcher died before its commit")

    with pytest.raises(ConnectionError):
        async with database.session() as session:
            await dispatch_index_tasks(session, limit=10, publish=publish_then_crash)

    assert (await document(database, page_id)).status == IndexStatus.PENDING.value
    [orphan] = await messages(stream)
    task = task_of(orphan.job_id, orphan.page_id, orphan.content_version)
    assert await start(database, task) is None
    assert await judged(database, task) == Handled(Disposition.ACK, "not_dispatched")

    assert await dispatch(database, stream) == [page_id]  # published again
    [again] = await messages(stream)
    assert again == orphan  # the same task; a duplicate message
    # Whichever message comes first does the work once; the other finds it done.
    attempt = await start(database, task)
    assert attempt is not None
    assert (await index(database, task)).outcome is IndexOutcome.INDEXED
    assert await start(database, task) is None
    assert await judged(database, task) == Handled(Disposition.ACK, "already_indexed")
    assert (await document(database, page_id)).attempt_count == 1


async def test_an_attempt_waits_for_a_dispatch_still_in_flight(
    database: Database, stream: IndexStream
) -> None:
    """An indexer can receive a message before the dispatch that published it commits; its
    start locks the row first, so it waits, and then sees the row queued (ADR-0016)."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a")
    published = asyncio.Event()
    release = asyncio.Event()
    publish = publisher(stream)

    async def held_publish(tasks: Sequence[IndexTaskRef]) -> list[str]:
        entries = await publish(tasks)
        published.set()
        await release.wait()
        return entries

    async def dispatching() -> list[uuid.UUID]:
        async with database.session() as session:
            return await dispatch_index_tasks(session, limit=10, publish=held_publish)

    dispatcher = asyncio.create_task(dispatching())
    await asyncio.wait_for(published.wait(), timeout=5)
    starting = asyncio.create_task(start(database, task_of(job_id, page_id)))
    await until_sessions_wait_for_locks(database)
    assert not starting.done()

    release.set()
    assert await dispatcher == [page_id]
    attempt = await asyncio.wait_for(starting, timeout=5)
    assert attempt is not None
    assert attempt.attempt == 1


# --- I2: attempts and leases ---------------------------------------------------------------------


async def test_an_attempt_is_counted_and_leased_before_any_work(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a")
    await dispatch(database, stream)
    task = task_of(job_id, page_id)

    attempt = await start(database, task, owner="indexer-a", lease_seconds=60)

    assert attempt == IndexAttempt(page_id, job_id, attempt=1, content_version=1, takeover=False)
    row = await document(database, page_id)
    assert (row.attempt_count, row.lease_owner, row.status) == (1, "indexer-a", "queued")
    assert row.lease_expires_at is not None
    # A duplicate message while the attempt runs counts nothing and keeps its message.
    assert await start(database, task, owner="indexer-b") is None
    assert await judged(database, task) == Handled(Disposition.KEEP, "leased")
    assert (await document(database, page_id)).attempt_count == 1


async def test_an_expired_attempt_is_taken_over_and_counts_another_attempt(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a")
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task, owner="indexer-a") is not None
    await expire_index_lease(database, page_id)

    assert await judged(database, task) is Retry.START
    attempt = await start(database, task, owner="indexer-b")

    assert attempt is not None
    assert (attempt.attempt, attempt.takeover) == (2, True)
    assert (await document(database, page_id)).lease_owner == "indexer-b"


async def test_used_up_attempts_fail_the_document_once_no_attempt_runs(
    database: Database, stream: IndexStream
) -> None:
    """Crashed attempts count: after max_attempts the document fails, with the last error."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a")
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    for owner in ("indexer-a", "indexer-b"):
        assert await start(database, task, owner=owner, max_attempts=2) is not None
        async with database.session() as session:
            await note_attempt_error(session, page_id, owner=owner, reason=DEADLINE_EXCEEDED)

    assert await start(database, task, owner="indexer-c", max_attempts=2) is None
    assert await judged(database, task, max_attempts=2) is Retry.FAIL
    async with database.session() as session:
        failed = await fail_exhausted_document(session, task, max_attempts=2, worker_id="indexer-c")
        again = await fail_exhausted_document(session, task, max_attempts=2)

    assert (failed, again) == (True, False)
    row = await document(database, page_id)
    assert row.status == IndexStatus.FAILED.value
    assert row.last_error == f"{MAX_ATTEMPTS_EXCEEDED} (last error: {DEADLINE_EXCEEDED})"
    assert (row.lease_owner, row.lease_expires_at) == (None, None)
    [event] = await events(database, job_id, "page.index_failed")
    assert event.worker_id == "indexer-c"
    assert event.detail == {
        "page_id": str(page_id),
        "reason": MAX_ATTEMPTS_EXCEEDED,
        "attempts": 2,
        "content_version": 1,
    }


async def test_used_up_attempts_do_not_fail_a_document_whose_last_attempt_still_runs(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a")
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task, max_attempts=1) is not None  # the last attempt, live

    async with database.session() as session:
        assert not await fail_exhausted_document(session, task, max_attempts=1)

    assert await judged(database, task, max_attempts=1) == Handled(Disposition.KEEP, "leased")
    assert (await index(database, task)).outcome is IndexOutcome.INDEXED


async def test_a_message_for_an_older_content_version_indexes_the_current_content(
    database: Database, stream: IndexStream
) -> None:
    """An older message is not dropped as outdated (independent review): the current version
    may have no message of its own. Its attempt indexes the current content."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="old words")
    await dispatch(database, stream)
    await change_content(database, page_id, body="new text")
    await dispatch(database, stream)
    old, current = task_of(job_id, page_id, 1), task_of(job_id, page_id, 2)

    attempt = await start(database, old)

    assert attempt is not None
    assert (attempt.attempt, attempt.content_version) == (1, 2)
    result = await index(database, old)
    assert (result.outcome, result.content_version) == (IndexOutcome.INDEXED, 2)
    assert await lexemes(database, page_id) == {"new", "text"}
    assert await start(database, current) is None
    assert await judged(database, current) == Handled(Disposition.ACK, "already_indexed")


async def test_messages_that_do_not_match_the_database_are_dead_lettered(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    other_job, _ = await new_job(database, host="other.test")
    page_id = await add_page(database, job_id, path="a")
    await dispatch(database, stream)

    cases = {
        "unknown_document": task_of(job_id, uuid.uuid4()),
        "job_mismatch": task_of(other_job, page_id),
        "version_ahead": task_of(job_id, page_id, 2),
    }
    for reason, task in cases.items():
        assert await start(database, task) is None
        assert await judged(database, task) == Handled(Disposition.DEAD_LETTER, reason)
    assert (await document(database, page_id)).attempt_count == 0


# --- I3: the vector -------------------------------------------------------------------------------


async def test_the_vector_weights_title_description_headings_and_body(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    page_id = await add_page(
        database,
        job_id,
        path="a",
        title="Redis Streams",
        description="Consumer groups",
        headings="Reclaiming messages",
        body="The dispatcher publishes tasks",
    )
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task) is not None

    result = await index(database, task)

    assert (result.outcome, result.content_version, result.truncated) == (
        IndexOutcome.INDEXED,
        1,
        False,
    )
    assert result.lexemes == 9
    assert await lexemes(database, page_id, "A") == {"redi", "stream"}
    assert await lexemes(database, page_id, "B") == {"consum", "group"}
    assert await lexemes(database, page_id, "C") == {"reclaim", "messag"}
    assert await lexemes(database, page_id, "D") == {"dispatch", "publish", "task"}
    row = await document(database, page_id)
    assert (row.status, row.indexed_content_version, row.last_error) == ("indexed", 1, None)
    assert row.indexed_at is not None
    assert (row.lease_owner, row.lease_expires_at) == (None, None)


async def test_empty_and_missing_fields_index_safely(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    empty = await add_page(database, job_id, path="empty")
    stop_words = await add_page(database, job_id, path="stop", body="the and of to a")
    repeated = await add_page(database, job_id, path="rep", body="queue " * 10_000)
    odd = await add_page(
        database, job_id, path="odd", title="�", body="emoji \U0001f600 control \x07 é"
    )
    await dispatch(database, stream)

    results = {}
    for page_id in (empty, stop_words, repeated, odd):
        task = task_of(job_id, page_id)
        assert await start(database, task) is not None
        results[page_id] = await index(database, task)

    assert all(result.outcome is IndexOutcome.INDEXED for result in results.values())
    assert results[empty].lexemes == 0
    assert results[stop_words].lexemes == 0
    assert results[repeated].lexemes == 1
    assert {"emoji", "control", "é"} <= await lexemes(database, odd)


async def test_only_the_first_max_body_chars_of_the_body_are_indexed(
    database: Database, stream: IndexStream
) -> None:
    """The cut at 2000 characters splits a word ("al|pha"): that partial word is dropped, so
    "al" is not indexed; "omega", after the cut, is not either."""
    job_id, _ = await new_job(database)
    body = "alpha " * 500 + "omega"  # "omega" starts at character 3000
    page_id = await add_page(database, job_id, path="a", title="Bounded", body=body)
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task) is not None

    result = await index(database, task, max_body_chars=2000)

    assert (result.body_chars, result.indexed_body_chars, result.truncated) == (
        len(body),
        1998,  # 333 whole "alpha " words
        True,
    )
    assert await lexemes(database, page_id) == {"bound", "alpha"}


@pytest.mark.parametrize(
    ("token_length", "cut_inside", "expected"),
    [
        # Longer than the 100-character look-back of the first version (independent review).
        (300, 150, {"alpha", "beta"}),
        # Longer than PostgreSQL's longest word: left as it is, and ignored by the parser.
        (5000, 3000, {"alpha", "beta"}),
    ],
)
async def test_a_long_word_split_by_the_cut_is_not_indexed_in_part(
    database: Database,
    stream: IndexStream,
    token_length: int,
    cut_inside: int,
    expected: set[str],
) -> None:
    job_id, _ = await new_job(database)
    head = "alpha beta "
    body = head + "x" * token_length + " omega"
    page_id = await add_page(database, job_id, path="a", body=body)
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task) is not None

    result = await index(database, task, max_body_chars=len(head) + cut_inside)

    assert result.outcome is IndexOutcome.INDEXED
    assert await lexemes(database, page_id) == expected


async def test_a_cut_between_words_keeps_the_last_whole_word(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="alpha beta gamma")
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task) is not None

    result = await index(database, task, max_body_chars=10)  # "alpha beta", then a space

    assert result.indexed_body_chars == 10
    assert await lexemes(database, page_id) == {"alpha", "beta"}


def cjk_word(number: int) -> str:
    return chr(0x4E00 + number // 1000) + chr(0x4E00 + number % 1000)


def dense_cjk_text(words: int) -> str:
    """Distinct two-character CJK words: about 3.7 bytes of tsvector lexemes and positions
    (what PostgreSQL limits to 1 MB) per character."""
    return " ".join(cjk_word(index) for index in range(words))


def denser_cjk_text(words: int) -> str:
    """Distinct hyphenated CJK compounds ("ab-cd"): the parser indexes the compound and both
    parts, about 6.7 bytes of lexemes and positions per character, the densest text measured."""
    return " ".join(f"{cjk_word(2 * index)}-{cjk_word(2 * index + 1)}" for index in range(words))


async def test_an_oversized_vector_is_built_again_from_a_quarter_of_the_body(
    database: Database, stream: IndexStream
) -> None:
    """400,000 dense characters exceed PostgreSQL's tsvector limit; a quarter of them does not.
    (The configured limit is at most 150,000 characters; adversarial text can exceed 1 MB with
    that too, which is what the fallback is for.)"""
    job_id, _ = await new_job(database)
    body = dense_cjk_text(133_334)[:400_000]
    page_id = await add_page(database, job_id, path="a", title="Dense", body=body)
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task) is not None

    result = await index(database, task, max_body_chars=400_000)

    assert result.outcome is IndexOutcome.INDEXED
    assert result.truncated
    assert result.indexed_body_chars is not None
    assert 99_990 < result.indexed_body_chars <= 100_000  # a quarter, less a split word
    assert len(await lexemes(database, page_id)) > 33_000


async def test_a_vector_too_large_even_for_the_fallback_fails_the_document(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    body = denser_cjk_text(166_667)[:1_000_000]  # a quarter is still about 1.7 MB
    page_id = await add_page(database, job_id, path="a", body=body)
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task) is not None

    result = await index(database, task, max_body_chars=1_000_000)

    assert (result.outcome, result.reason) == (IndexOutcome.FAILED, TSVECTOR_TOO_LARGE)
    row = await document(database, page_id)
    assert (row.status, row.last_error, row.search_vector) == ("failed", TSVECTOR_TOO_LARGE, None)
    [event] = await events(database, job_id, "page.index_failed")
    assert event.detail["reason"] == TSVECTOR_TOO_LARGE


async def test_a_statement_timeout_cuts_the_attempt_off_and_leaves_it_retryable(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="lorem ipsum dolor " * 8000)
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task, owner="indexer-a") is not None

    with pytest.raises(DBAPIError) as cut_off:
        await index(database, task, budget=0.001)

    assert sqlstate(cut_off.value) == "57014"
    row = await document(database, page_id)
    assert (row.status, row.search_vector) == ("queued", None)  # rolled back
    async with database.session() as session:
        assert not await note_attempt_error(
            session, page_id, owner="indexer-b", reason=DEADLINE_EXCEEDED
        )  # not its lease
        assert await note_attempt_error(
            session, page_id, owner="indexer-a", reason=DEADLINE_EXCEEDED
        )
    row = await document(database, page_id)
    assert (row.last_error, row.lease_owner, row.attempt_count) == (DEADLINE_EXCEEDED, None, 1)
    assert await judged(database, task) is Retry.START  # the next attempt may start at once


def stepping_clock(*readings: float) -> Iterator[float]:
    yield from readings
    while True:
        yield readings[-1]


async def test_the_statements_of_an_attempt_share_one_budget(
    database: Database, stream: IndexStream
) -> None:
    """The first statement fails (vector too large); by then the budget is spent, so the
    fallback statement never starts. Before the review, each statement had the whole deadline
    of its own, and an attempt could outlive its lease (independent review)."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body=dense_cjk_text(133_334)[:400_000])
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task) is not None
    clock = stepping_clock(0.0, 0.0, 10.0)  # the start, the first statement, the fallback

    with pytest.raises(IndexDeadlineError):
        async with database.session() as session:
            await index_document(
                session,
                task,
                max_body_chars=400_000,
                budget_seconds=5,
                clock=lambda: next(clock),
            )

    row = await document(database, page_id)
    assert (row.status, row.search_vector, row.indexed_content_version) == ("queued", None, None)


async def test_a_lock_wait_inside_an_attempt_ends_with_its_budget(
    database: Database, stream: IndexStream
) -> None:
    """Another transaction holds the document row: the index statement waits for it only until
    the attempt's budget is spent (statement_timeout counts lock waits)."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="locked words")
    await dispatch(database, stream)
    task = task_of(job_id, page_id)
    assert await start(database, task) is not None

    async with database.session() as holder:
        await holder.execute(
            text("SELECT 1 FROM search_documents WHERE page_id = :id FOR UPDATE"), {"id": page_id}
        )
        began = time.monotonic()
        with pytest.raises(DBAPIError) as cut_off:
            await index(database, task, budget=0.5)
        waited = time.monotonic() - began
        await holder.rollback()

    assert sqlstate(cut_off.value) == "57014"
    assert 0.4 <= waited < 3.0
    assert (await document(database, page_id)).status == "queued"


async def test_indexing_a_document_that_is_not_queued_writes_nothing(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="words")
    task = task_of(job_id, page_id)

    pending = await index(database, task)

    assert (pending.outcome, pending.status) == (IndexOutcome.NOT_QUEUED, IndexStatus.PENDING)
    assert (await document(database, page_id)).search_vector is None
    await dispatch(database, stream)
    assert await start(database, task) is not None
    assert (await index(database, task)).outcome is IndexOutcome.INDEXED
    again = await index(database, task)
    assert (again.outcome, again.status) == (IndexOutcome.NOT_QUEUED, IndexStatus.INDEXED)


# --- I5, I6: job indexing completion and reindexing ----------------------------------------------


async def test_a_running_jobs_indexing_is_never_complete(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a")
    await index_everything(database, stream)

    async with database.session() as session:
        assert job_id not in await completable_indexing_jobs(session, limit=100)
        assert not await complete_job_indexing(session, job_id)
    assert (await job_row(database, job_id)).indexing_completed_at is None


async def test_a_finished_jobs_indexing_completes_once_nothing_is_pending_or_queued(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    first = await add_page(database, job_id, path="a")
    await add_page(database, job_id, path="b")
    await finish_job(database, job_id)
    await dispatch(database, stream, limit=1)  # one queued, one pending

    async with database.session() as session:
        assert await completable_indexing_jobs(session, limit=100) == []  # work is left
        assert not await complete_job_indexing(session, job_id)
    await index_everything(database, stream)
    await execute(
        database,
        "UPDATE search_documents SET status = 'failed', last_error = 'x' WHERE page_id = :id",
        id=first,
    )
    async with database.session() as session:
        assert await completable_indexing_jobs(session, limit=100) == [job_id]
        assert await complete_job_indexing(session, job_id)
        assert not await complete_job_indexing(session, job_id)  # once
        assert await completable_indexing_jobs(session, limit=100) == []

    job = await job_row(database, job_id)
    assert job.indexing_completed_at is not None
    assert job.finished_at is not None
    assert job.indexing_completed_at >= job.finished_at
    [event] = await events(database, job_id, "job.indexing_completed")
    assert event.detail == {"indexed": 1, "failed": 1}
    async with database.session() as session:
        progress = await job_indexing(session, job_id)
    assert (progress.indexed, progress.failed, progress.state.value) == (1, 1, "failed")


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_every_finished_status_completes_its_indexing(
    database: Database, stream: IndexStream, status: str
) -> None:
    """Cancelled jobs too: their stored pages are indexed and stay searchable (ADR-0018)."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a")
    await finish_job(database, job_id, status)
    await index_everything(database, stream)

    async with database.session() as session:
        assert await complete_job_indexing(session, job_id)
        progress = await job_indexing(session, job_id)

    assert (await document(database, page_id)).status == "indexed"
    assert progress.state.value == "indexed"


async def test_jobs_still_being_indexed_never_delay_the_completion_of_others(
    database: Database,
) -> None:
    """More unfinished jobs than one pass looks at, all finished earlier: the job that is ready
    is still found (before the review, only the 100 oldest unfinished jobs were looked at)."""
    for index in range(101):
        older, _ = await new_job(database, host=f"older-{index}.test")
        await add_page(database, older, path="waiting", host=f"older-{index}.test")
        await finish_job(database, older)
    ready, _ = await new_job(database, host="ready.test")
    await finish_job(database, ready)

    async with database.session() as session:
        candidates = await completable_indexing_jobs(session, limit=100)

    assert candidates == [ready]


async def test_a_job_without_indexable_pages_completes_at_once(database: Database) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="hidden", noindex=True)
    await finish_job(database, job_id)

    async with database.session() as session:
        assert await complete_job_indexing(session, job_id)
        progress = await job_indexing(session, job_id)

    assert (progress.documents, progress.state.value) == (0, "indexed")


async def test_reindexing_resets_finished_documents_and_reopens_the_job(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    indexed = await add_page(database, job_id, path="a")
    failed = await add_page(database, job_id, path="b")
    await index_everything(database, stream)
    await execute(
        database,
        "UPDATE search_documents SET status = 'failed', last_error = 'x', attempt_count = 3"
        " WHERE page_id = :id",
        id=failed,
    )
    queued = await add_page(database, job_id, path="c")
    await dispatch(database, stream)
    await finish_job(database, job_id)
    await execute(
        database,
        "UPDATE search_documents SET status = 'indexed', indexed_content_version = 1,"
        " indexed_at = clock_timestamp(), search_vector = to_tsvector('english', 'x')"
        " WHERE page_id = :id",
        id=queued,
    )
    async with database.session() as session:
        assert await complete_job_indexing(session, job_id)
    await execute(
        database,
        "UPDATE search_documents SET status = 'queued', dispatched_at = clock_timestamp()"
        " WHERE page_id = :id",
        id=queued,
    )

    async with database.session() as session:
        reset = await request_reindex(session, job_id)

    assert reset == 2
    for page_id in (indexed, failed):
        row = await document(database, page_id)
        assert (row.status, row.attempt_count, row.last_error) == ("pending", 0, None)
    assert (await document(database, indexed)).indexed_content_version == 1  # kept meanwhile
    assert (await document(database, queued)).status == "queued"  # on its way already
    assert (await job_row(database, job_id)).indexing_completed_at is None
    [event] = await events(database, job_id, "job.reindex_requested")
    assert event.detail == {"documents": 2}
    with pytest.raises(NotFoundError):
        async with database.session() as session:
            await request_reindex(session, uuid.uuid4())


# --- Reconciliation ------------------------------------------------------------------------------


async def test_queued_documents_whose_messages_were_lost_are_published_again(
    database: Database, stream: IndexStream, redis_client: Redis
) -> None:
    """Redis lost the index stream (here: its key is deleted, as a restart without persistence
    would); the reconciliation publishes the stale tasks again, with their current version."""
    job_id, _ = await new_job(database)
    lost = await add_page(database, job_id, path="lost")
    await dispatch(database, stream)
    await change_content(database, lost, body="second version")
    await dispatch(database, stream)
    await redis_client.delete(stream.key)
    await stream.ensure_group()
    await age_index_dispatch(database, lost, 600)
    old_entry = (await document(database, lost)).stream_entry_id

    async with database.session() as session:
        result = await republish_stale_index_tasks(
            session,
            older_than=300,
            limit=10,
            publish=publisher(stream),
            delivering=stream.delivering,
        )

    assert (result.examined, result.republished, result.resume_after) == (1, 1, None)
    [task] = await messages(stream)
    assert task == IndexTask(job_id, lost, 2)
    row = await document(database, lost)
    assert row.stream_entry_id != old_entry
    assert row.status == "queued"


async def entry_of(database: Database, page_id: uuid.UUID) -> str:
    entry = (await document(database, page_id)).stream_entry_id
    assert entry is not None
    return entry


async def test_reconciliation_leaves_delivering_young_and_running_tasks_alone(
    database: Database, stream: IndexStream
) -> None:
    """Each document is dispatched and read in an order that leaves its entry in the named
    state: delivered and pending, acknowledged, leased, too young, never delivered."""
    job_id, _ = await new_job(database)
    pending = await add_page(database, job_id, path="pending")
    await dispatch(database, stream)
    await stream.read("indexer-a", count=1, block_ms=10)  # delivered, not acknowledged
    acknowledged = await add_page(database, job_id, path="acked")
    await dispatch(database, stream)
    await stream.read("indexer-a", count=1, block_ms=10)
    await stream.ack(await entry_of(database, acknowledged))
    leased = await add_page(database, job_id, path="leased")
    await dispatch(database, stream)
    await stream.read("indexer-a", count=1, block_ms=10)
    assert await start(database, task_of(job_id, leased)) is not None
    young = await add_page(database, job_id, path="young")
    await dispatch(database, stream)
    undelivered = await add_page(database, job_id, path="undelivered")
    await dispatch(database, stream)  # never read
    for page_id in (undelivered, pending, leased, acknowledged):
        await age_index_dispatch(database, page_id, 600)
    entries = [await entry_of(database, page) for page in (pending, undelivered, acknowledged)]
    assert await stream.delivering(entries) == [True, True, False]
    entry = entries[2]

    async with database.session() as session:
        result = await republish_stale_index_tasks(
            session,
            older_than=300,
            limit=10,
            publish=publisher(stream),
            delivering=stream.delivering,
        )

    # Examined: the undelivered, the pending and the acknowledged ones (old enough, no live
    # lease). Only the acknowledged one lost its message.
    assert (result.examined, result.republished) == (3, 1)
    assert (await document(database, acknowledged)).stream_entry_id != entry
    assert young  # never examined: dispatched too recently


async def test_reconciliation_pages_through_stale_tasks(
    database: Database, stream: IndexStream, redis_client: Redis
) -> None:
    job_id, _ = await new_job(database)
    pages = [await add_page(database, job_id, path=f"p{index}") for index in range(3)]
    await dispatch(database, stream)
    await redis_client.delete(stream.key)
    await stream.ensure_group()
    for age, page_id in zip((900, 800, 700), pages, strict=True):
        await age_index_dispatch(database, page_id, age)

    async def reconcile(after: object) -> object:
        async with database.session() as session:
            return await republish_stale_index_tasks(
                session,
                older_than=300,
                limit=2,
                publish=publisher(stream),
                delivering=stream.delivering,
                after=after,  # type: ignore[arg-type]  # the cursor of the previous call
            )

    first = await reconcile(None)
    assert first.republished == 2  # type: ignore[attr-defined]
    assert first.resume_after is not None  # type: ignore[attr-defined]
    second = await reconcile(first.resume_after)  # type: ignore[attr-defined]
    assert second.republished == 1  # type: ignore[attr-defined]
    assert {task.page_id for task in await messages(stream)} == set(pages)


# --- Statistics ----------------------------------------------------------------------------------


async def test_the_overview_counts_real_state(database: Database, stream: IndexStream) -> None:
    job_id, _ = await new_job(database)
    finished, _ = await new_job(database, host="other.test")
    await finish_job(database, finished)
    await add_page(database, job_id, path="pending")
    queued = await add_page(database, job_id, path="queued")
    await dispatch(database, stream)
    await add_page(database, job_id, path="pending-2")
    assert await start(database, task_of(job_id, queued)) is not None

    async with database.session() as session:
        overview = await indexing_overview(session)
        progress = await job_indexing(session, job_id)

    assert (overview.pending, overview.queued, overview.indexed, overview.failed) == (1, 2, 0, 0)
    assert overview.active == 1
    assert overview.oldest_pending_seconds is not None
    assert overview.oldest_pending_seconds >= 0
    assert overview.oldest_queued_seconds is not None
    assert overview.jobs_awaiting_completion == 1
    assert (progress.pending, progress.queued, progress.active) == (1, 2, 1)
    assert progress.state.value == "running"


async def test_job_indexing_of_an_unknown_job_is_not_found(database: Database) -> None:
    with pytest.raises(NotFoundError):
        async with database.session() as session:
            await job_indexing(session, uuid.uuid4())


async def test_cancelled_jobs_keep_their_pages_indexable(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database, status=JobStatus.CANCELLING)
    page_id = await add_page(database, job_id, path="a")

    assert await dispatch(database, stream) == [page_id]
