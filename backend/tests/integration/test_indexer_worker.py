"""The indexer and the index dispatcher against real PostgreSQL and Redis (in-process).

The failure windows of ADR-0018 are produced directly: a consumer that reads a message and then
"dies" (never acknowledges it), an attempt started and abandoned, a commit whose XACK never
happened, a stream deleted as if Redis had lost its data, and PostgreSQL cut off by a TCP proxy.
Real indexer and dispatcher processes are exercised in tests/fault/test_indexing_processes.py.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.db.models import IndexStatus
from app.db.session import Database
from app.indexing.documents import DEADLINE_EXCEEDED, MAX_ATTEMPTS_EXCEEDED
from app.queue.messages import IndexTask
from app.queue.streams import IndexStream
from app.scheduler.index_dispatcher import IndexDispatcher, IndexDispatcherConfig
from app.services.search import search_job
from app.services.search_index import (
    IndexTaskRef,
    dispatch_index_tasks,
    index_document,
    job_indexing,
    start_index_attempt,
)
from app.workers.indexer import IndexerConfig, IndexWorker
from tests.crawl_support import events, new_job, stream_entries
from tests.index_support import (
    add_page,
    document,
    execute,
    finish_job,
    publisher,
)
from tests.process_support import TcpProxy

pytestmark = pytest.mark.anyio


class JsonLines(logging.Handler):
    """Formats records as the processes do (with the task context), and keeps them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(JsonFormatter(service="test"))
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))

    def events(self, name: str) -> list[dict[str, Any]]:
        return [line for line in self.lines if line.get("event") == name]


@pytest.fixture
def logs() -> Iterator[JsonLines]:
    handler = JsonLines()
    logger = logging.getLogger("app")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@pytest.fixture
async def stream(redis_client: Redis, redis_prefix: str) -> IndexStream:
    stream = IndexStream(redis_client, key_prefix=redis_prefix)
    await stream.ensure_group()
    return stream


def config(**changes: Any) -> IndexerConfig:
    """Short timings: statements cut off after 1 s, messages reclaimed after 2 s idle, leases
    of 1.5 s."""
    values: dict[str, Any] = {
        "concurrency": 2,
        "max_attempts": 3,
        "task_deadline": 1.0,
        "reclaim_min_idle": 2.0,
        "read_block_ms": 100,
        "reclaim_interval": 0.2,
        "reclaim_batch": 10,
        "grace_seconds": 1.0,
    }
    values.update(changes)
    return IndexerConfig(**values)


def dispatcher_config(**changes: Any) -> IndexDispatcherConfig:
    values: dict[str, Any] = {"interval": 0.05, "reconcile_after": 1.0, "retention_seconds": 600}
    values.update(changes)
    return IndexDispatcherConfig(**values)


Condition = Callable[[], Awaitable[bool]]


@dataclass
class Running:
    task: asyncio.Task[None]
    stop: asyncio.Event

    async def until(self, condition: Condition, *, within: float = 15.0) -> None:
        async with asyncio.timeout(within):
            while not await condition():
                if self.task.done():
                    self.task.result()  # re-raise a crash
                    raise AssertionError("the process loop stopped")
                await asyncio.sleep(0.05)

    async def stopped(self) -> None:
        self.stop.set()
        async with asyncio.timeout(10):
            await self.task


@pytest.fixture
async def run_indexer(
    database: Database, stream: IndexStream
) -> AsyncIterator[Callable[..., Running]]:
    started: list[Running] = []

    def start(
        indexer_config: IndexerConfig | None = None,
        indexer_database: Database | None = None,
    ) -> Running:
        worker = IndexWorker(indexer_database or database, stream, indexer_config or config())
        stop = asyncio.Event()
        running = Running(asyncio.create_task(worker.run(stop)), stop)
        started.append(running)
        return running

    yield start
    for running in started:
        running.stop.set()
    await asyncio.gather(*(running.task for running in started), return_exceptions=True)


@pytest.fixture
async def run_dispatcher(
    database: Database, stream: IndexStream
) -> AsyncIterator[Callable[..., Running]]:
    started: list[Running] = []

    def start(dispatcher: IndexDispatcherConfig | None = None) -> Running:
        loop = IndexDispatcher(database, stream, dispatcher or dispatcher_config())
        stop = asyncio.Event()
        running = Running(asyncio.create_task(loop.run(stop)), stop)
        started.append(running)
        return running

    yield start
    for running in started:
        running.stop.set()
    await asyncio.gather(*(running.task for running in started), return_exceptions=True)


async def statuses(database: Database, job_id: uuid.UUID) -> dict[str, int]:
    async with database.session() as session:
        progress = await job_indexing(session, job_id)
    counts = {
        "pending": progress.pending,
        "queued": progress.queued,
        "indexed": progress.indexed,
        "failed": progress.failed,
    }
    return {status: count for status, count in counts.items() if count}


async def dispatch(database: Database, stream: IndexStream) -> None:
    async with database.session() as session:
        await dispatch_index_tasks(session, limit=100, publish=publisher(stream))


async def pending_messages(stream: IndexStream) -> int:
    return (await stream.pending()).count


# --- The pipeline --------------------------------------------------------------------------------


async def test_pages_flow_through_the_dispatcher_and_an_indexer_into_search(
    database: Database,
    stream: IndexStream,
    run_indexer: Callable[..., Running],
    run_dispatcher: Callable[..., Running],
    logs: JsonLines,
) -> None:
    job_id, _ = await new_job(database)
    pages = [
        await add_page(database, job_id, path=f"p{index}", title=f"Page {index}", body=body)
        for index, body in enumerate(
            ["streams and groups", "leases and fencing", "secretbodyword appears here"]
        )
    ]
    await add_page(database, job_id, path="hidden", noindex=True, body="streams")

    indexer = run_indexer()
    dispatcher = run_dispatcher()
    await indexer.until(lambda: _equals(statuses(database, job_id), {"indexed": 3}))
    await finish_job(database, job_id)
    await dispatcher.until(lambda: _completed(database, job_id))
    await indexer.until(lambda: _equals(pending_messages(stream), 0))
    await indexer.stopped()
    await dispatcher.stopped()

    async with database.session() as session:
        results = await search_job(session, job_id, "streams")
    assert [hit.page_id for hit in results.hits] == [pages[0]]
    assert results.indexing.state.value == "indexed"
    handled = [line for line in logs.events("task.handled") if line["reason"] == "indexed"]
    assert len(handled) == 3
    for line in handled:
        assert {"job_id", "page_id", "content_version", "worker_id", "attempt"} <= set(line)
        assert line["duration_ms"] >= 0
        assert line["indexed_version"] == 1
    assert not any("secretbodyword" in json.dumps(line) for line in logs.lines)  # no bodies


async def test_duplicate_messages_are_indexed_once_and_all_acknowledged(
    database: Database, stream: IndexStream, run_indexer: Callable[..., Running]
) -> None:
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="duplicated")
    await dispatch(database, stream)
    await stream.publish([IndexTask(job_id, page_id, 1)] * 3)

    # One task at a time: each duplicate meets the finished document, never a live attempt.
    indexer = run_indexer(config(concurrency=1))
    await indexer.until(lambda: _equals(statuses(database, job_id), {"indexed": 1}))
    await indexer.until(lambda: _equals(pending_messages(stream), 0))
    await indexer.stopped()

    row = await document(database, page_id)
    assert (row.attempt_count, row.indexed_content_version) == (1, 1)
    assert await stream.length() == 4


async def test_messages_that_can_never_be_processed_are_dead_lettered(
    database: Database,
    stream: IndexStream,
    redis_client: Redis,
    run_indexer: Callable[..., Running],
) -> None:
    job_id, _ = await new_job(database)
    other, _ = await new_job(database, host="other.test")
    page_id = await add_page(database, job_id, path="a", body="valid")
    await dispatch(database, stream)
    await redis_client.xadd(stream.key, {"v": "1", "job_id": "not-a-uuid"})
    await stream.publish(
        [
            IndexTask(job_id, uuid.uuid4(), 1),  # unknown page
            IndexTask(other, page_id, 1),  # another job's page
            IndexTask(job_id, page_id, 7),  # a version the page never had
        ]
    )

    indexer = run_indexer()
    await indexer.until(lambda: _equals(pending_messages(stream), 0))
    await indexer.until(lambda: _equals(statuses(database, job_id), {"indexed": 1}))
    await indexer.stopped()

    letters = await stream_entries(redis_client, stream.dead_letter_key)
    assert sorted(fields["kind"] for _, fields in letters) == [
        "job_mismatch",
        "malformed",
        "unknown_document",
        "version_ahead",
    ]
    assert (await document(database, page_id)).attempt_count == 1


# --- Failure windows -----------------------------------------------------------------------------


async def test_a_commit_whose_xack_was_lost_is_acknowledged_on_redelivery(
    database: Database, stream: IndexStream, run_indexer: Callable[..., Running]
) -> None:
    """The indexer committed, then died before XACK: the message stays pending; another
    indexer reclaims it, finds the document indexed, and only acknowledges."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="committed")
    await dispatch(database, stream)
    [delivery] = await stream.read("indexer-dead", count=1, block_ms=10)
    task = IndexTaskRef(job_id, page_id, 1)
    async with database.session() as session:
        assert await start_index_attempt(
            session, task, owner="indexer-dead", lease_seconds=1.5, max_attempts=3
        )
    async with database.session() as session:
        await index_document(session, task, max_body_chars=100_000, budget_seconds=5)
    indexed_at = (await document(database, page_id)).indexed_at

    indexer = run_indexer(config(reclaim_min_idle=1.0, task_deadline=0.5))
    await indexer.until(lambda: _equals(pending_messages(stream), 0))
    await indexer.stopped()

    row = await document(database, page_id)
    assert (row.attempt_count, row.indexed_at) == (1, indexed_at)  # not indexed again
    assert delivery.entry_id


async def test_an_abandoned_attempt_is_taken_over_by_another_indexer(
    database: Database,
    stream: IndexStream,
    run_indexer: Callable[..., Running],
    logs: JsonLines,
) -> None:
    """An indexer started an attempt and died: its lease expires before its message is
    reclaimed, and the next attempt indexes the page."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="abandoned")
    await dispatch(database, stream)
    await stream.read("indexer-dead", count=1, block_ms=10)
    async with database.session() as session:
        assert await start_index_attempt(
            session,
            IndexTaskRef(job_id, page_id, 1),
            owner="indexer-dead",
            lease_seconds=0.75,
            max_attempts=3,
        )

    indexer = run_indexer(config(reclaim_min_idle=1.0, task_deadline=0.5))
    await indexer.until(lambda: _equals(statuses(database, job_id), {"indexed": 1}))
    await indexer.until(lambda: _equals(pending_messages(stream), 0))
    await indexer.stopped()

    assert (await document(database, page_id)).attempt_count == 2
    [takeover] = logs.events("index.lease_taken_over")
    assert takeover["previous_owner"] == "indexer-dead"


async def test_attempts_that_keep_timing_out_fail_the_document_for_good(
    database: Database, stream: IndexStream, run_indexer: Callable[..., Running]
) -> None:
    """A poison document: every attempt runs past the statement timeout. Attempts, counted in
    PostgreSQL, bound the work; the message is then acknowledged."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="slow words " * 12_000)
    await dispatch(database, stream)

    indexer = run_indexer(
        config(task_deadline=0.001, reclaim_min_idle=0.6, max_attempts=2, reclaim_interval=0.1)
    )
    await indexer.until(lambda: _equals(statuses(database, job_id), {"failed": 1}), within=20)
    await indexer.until(lambda: _equals(pending_messages(stream), 0))
    await indexer.stopped()

    row = await document(database, page_id)
    assert row.attempt_count == 2
    assert row.last_error == f"{MAX_ATTEMPTS_EXCEEDED} (last error: {DEADLINE_EXCEEDED})"
    [event] = await events(database, job_id, "page.index_failed")
    assert event.detail["attempts"] == 2


async def test_an_attempt_ends_with_its_deadline_and_releases_its_lease(
    database: Database,
    stream: IndexStream,
    run_indexer: Callable[..., Running],
    logs: JsonLines,
) -> None:
    """The test holds the page row, so the attempt starts (its start does not lock the page) but
    its index statement cannot read the page. It ends by its deadline (0.5 s), records why and
    releases its lease, well before the lease (1.5 s) would expire: no other indexer can take
    over an attempt that is still running (independent review)."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="held words")
    await dispatch(database, stream)

    async with database.session() as holder:
        await holder.execute(text("SELECT 1 FROM pages WHERE id = :id FOR UPDATE"), {"id": page_id})
        indexer = run_indexer(config(task_deadline=0.5, reclaim_min_idle=2.5))

        async def given_up() -> bool:
            handled = logs.events("task.handled")
            return any(line["reason"] == DEADLINE_EXCEEDED for line in handled)

        await indexer.until(given_up)
        await holder.rollback()
    await indexer.stopped()

    [line] = [line for line in logs.events("task.handled") if line["reason"] == DEADLINE_EXCEEDED]
    assert line["disposition"] == "keep"
    assert line["duration_ms"] < 1500  # the lease: (0.5 + 2.5) / 2 s
    row = await document(database, page_id)
    assert (row.status, row.last_error, row.lease_owner) == ("queued", DEADLINE_EXCEEDED, None)


async def test_an_older_message_is_indexed_when_the_current_version_has_none(
    database: Database,
    stream: IndexStream,
    redis_client: Redis,
    run_indexer: Callable[..., Running],
) -> None:
    """The document is queued for version 2, but the only message names version 1 (a dispatch
    that raced the content change publishes that). It is indexed, not dropped as outdated and
    left waiting for the reconciler (independent review)."""
    job_id, _ = await new_job(database)
    page_id = await add_page(database, job_id, path="a", body="first words", content_version=2)
    await dispatch(database, stream)
    await redis_client.delete(stream.key)  # the version-2 message is gone
    await stream.ensure_group()
    await stream.publish([IndexTask(job_id, page_id, 1)])

    indexer = run_indexer()
    await indexer.until(lambda: _equals(statuses(database, job_id), {"indexed": 1}))
    await indexer.until(lambda: _equals(pending_messages(stream), 0))
    await indexer.stopped()

    row = await document(database, page_id)
    assert (row.indexed_content_version, row.attempt_count) == (2, 1)


async def test_an_indexer_rides_out_a_postgresql_outage(
    database: Database,
    stream: IndexStream,
    test_database: Settings,
    run_indexer: Callable[..., Running],
) -> None:
    job_id, _ = await new_job(database)
    for index in range(4):
        await add_page(database, job_id, path=f"p{index}", body=f"outage words {index}")
    async with TcpProxy(test_database.postgres_host, test_database.postgres_port) as proxy:
        proxied = Database(
            test_database.model_copy(
                update={"postgres_host": "127.0.0.1", "postgres_port": proxy.port}
            ),
            application_name="sitescope-test-indexer",
        )
        try:
            proxy.cut()
            indexer = run_indexer(config(reclaim_min_idle=1.0, task_deadline=0.5), proxied)
            await dispatch(database, stream)
            await asyncio.sleep(1.5)  # the indexer meets the outage and backs off
            assert await statuses(database, job_id) == {"queued": 4}
            proxy.restore()
            await indexer.until(
                lambda: _equals(statuses(database, job_id), {"indexed": 4}), within=60
            )
            await indexer.until(lambda: _equals(pending_messages(stream), 0), within=30)
            await indexer.stopped()
        finally:
            await proxied.dispose()


# --- The dispatcher ------------------------------------------------------------------------------


async def test_tasks_lost_with_redis_data_are_published_again(
    database: Database,
    stream: IndexStream,
    redis_client: Redis,
    run_indexer: Callable[..., Running],
) -> None:
    """Redis lost the index stream after the dispatch committed (its key is deleted, as by a
    restart without persistence). A dispatcher's reconciliation publishes the tasks again."""
    job_id, _ = await new_job(database)
    for index in range(3):
        await add_page(database, job_id, path=f"p{index}", body=f"lost {index}")
    first = IndexDispatcher(database, stream, dispatcher_config())
    assert (await first.run_pass()).dispatched == 3
    await redis_client.delete(stream.key)
    await execute(
        database,
        "UPDATE search_documents SET dispatched_at = clock_timestamp() - interval '10 seconds'",
    )

    report = await IndexDispatcher(database, stream, dispatcher_config()).run_pass()

    assert report.republished == 3
    indexer = run_indexer()
    await indexer.until(lambda: _equals(statuses(database, job_id), {"indexed": 3}))
    await indexer.stopped()


async def test_the_dispatcher_keeps_at_most_max_outstanding_documents_queued(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    for index in range(5):
        await add_page(database, job_id, path=f"p{index}")
    dispatcher = IndexDispatcher(
        database, stream, dispatcher_config(dispatch_batch=2, max_outstanding=3)
    )

    reports = [await dispatcher.run_pass() for _ in range(3)]

    assert [report.dispatched for report in reports] == [2, 1, 0]
    assert [report.queued for report in reports] == [0, 2, 3]
    assert await statuses(database, job_id) == {"queued": 3, "pending": 2}


async def test_concurrent_dispatchers_publish_each_document_once(
    database: Database, stream: IndexStream
) -> None:
    job_id, _ = await new_job(database)
    for index in range(40):
        await add_page(database, job_id, path=f"p{index}")
    dispatchers = [
        IndexDispatcher(database, stream, dispatcher_config(dispatch_batch=15)) for _ in range(3)
    ]

    for _ in range(2):
        await asyncio.gather(*(dispatcher.run_pass() for dispatcher in dispatchers))

    assert await statuses(database, job_id) == {"queued": 40}
    assert await stream.length() == 40


async def test_the_dispatcher_completes_finished_jobs_and_logs_real_statistics(
    database: Database,
    stream: IndexStream,
    run_indexer: Callable[..., Running],
    logs: JsonLines,
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a", body="done")
    await finish_job(database, job_id)
    dispatcher = IndexDispatcher(database, stream, dispatcher_config())
    first = await dispatcher.run_pass()  # dispatches; the job is not complete yet
    indexer = run_indexer()
    await indexer.until(lambda: _equals(statuses(database, job_id), {"indexed": 1}))
    await indexer.stopped()

    second = await dispatcher.run_pass()

    assert (first.dispatched, first.completed_jobs) == (1, 0)
    assert second.completed_jobs == 1
    [stats] = logs.events("index_dispatcher.stats")  # the first pass maintains at once
    assert (stats["documents_pending"], stats["documents_queued"]) == (0, 1)
    assert stats["jobs_awaiting_completion"] == 1
    assert stats["stream_length"] == 1


async def test_the_job_detail_reports_indexing(
    database: Database, stream: IndexStream, crawl_client: TestClient
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a")
    await add_page(database, job_id, path="b")

    before = crawl_client.get(f"/api/v1/crawls/{job_id}").json()["indexing"]
    await dispatch(database, stream)
    await execute(
        database,
        "UPDATE search_documents SET lease_owner = 'indexer-x',"
        " lease_expires_at = clock_timestamp() + interval '1 minute'",
    )
    during = crawl_client.get(f"/api/v1/crawls/{job_id}").json()["indexing"]

    assert before == {
        "status": "pending",
        "documents_total": 2,
        "documents_by_status": {"pending": 2, "queued": 0, "indexed": 0, "failed": 0},
        "active": 0,
        "completed_at": None,
    }
    assert during["status"] == "running"
    assert (during["documents_by_status"]["queued"], during["active"]) == (2, 2)
    assert IndexStatus.QUEUED.value in during["documents_by_status"]


async def _equals(value: Awaitable[object], expected: object) -> bool:
    return bool(await value == expected)


async def _completed(database: Database, job_id: uuid.UUID) -> bool:
    async with database.session() as session:
        return (await job_indexing(session, job_id)).completed_at is not None
