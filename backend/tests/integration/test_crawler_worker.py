"""The crawler worker's message handling against real PostgreSQL and Redis (in-process).

``PageFetcher`` and ``AllowAll`` are test doubles of the safe fetcher and the robots.txt policy:
they answer every URL with a small HTML page without any network request, and ``PageFetcher``
can hold a fetch until the test releases it. Real worker processes, real HTTP servers and real
crashes are exercised in tests/integration/test_distributed.py and tests/fault.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, text

from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.crawler.destinations import IPAddress
from app.crawler.errors import FailureKind, FetchError
from app.crawler.html_parser import ParseLimits
from app.crawler.politeness import PolitenessConfig, PolitenessGate, RedisSlotStore
from app.crawler.results import FetchResult
from app.crawler.robots import RobotsDecision, RobotsStatus
from app.crawler.scope import CrawlTargetPolicy
from app.crawler.urls import NormalizedUrl, normalize_url
from app.db.models import CrawlUrl, JobStatus, Page, UrlState
from app.db.session import Database
from app.observability.heartbeat import Heartbeat, HeartbeatPublisher, read_heartbeats
from app.observability.metrics import Registry
from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream
from app.services.crawl_cancellation import cancel_crawl_job, settle_cancellation
from app.services.crawl_state import (
    EXHAUSTED_OUTCOME,
    MAX_DELIVERIES_OUTCOME,
    AdvanceAction,
    RecordStatus,
    advance_job,
    claim_url,
    dispatch_urls,
    record_result,
)
from app.workers.crawl_task import CrawlContext
from app.workers.crawl_task import PageFetcher as TaskFetcher
from app.workers.crawler import CrawlWorker, WorkerConfig
from app.workers.retry import RetryPolicy
from tests.crawl_support import (
    events,
    execute,
    expire_lease,
    html_result,
    job_row,
    new_job,
    stream_entries,
    url_row,
)
from tests.fetch_support import RecordingBackend, ScriptedResolver, make_fetcher
from tests.integration.helpers import unused_port

pytestmark = pytest.mark.anyio

HOST = "example.test"
ALLOWED = RobotsDecision(allowed=True, status=RobotsStatus.PARSED, crawl_delay=None, detail=None)


@dataclass
class PageFetcher:
    """Test double of the safe fetcher: every URL is a small HTML page.

    While ``gate`` is cleared, fetches wait for it (a slow or hung target).
    """

    gate: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[str] = field(default_factory=list)
    # Errors raised by the next fetches of a URL, in order (a flaky target; Phase 6).
    failures: dict[str, list[BaseException]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.gate.set()

    async def fetch(self, url: str, *, max_redirects: int | None = None) -> FetchResult:
        self.calls.append(url)
        await self.gate.wait()
        if self.failures.get(url):
            raise self.failures[url].pop(0)
        body = b"<html><head><title>Page</title></head><body><p>Hello</p></body></html>"
        return FetchResult(
            requested_url=url,
            url=url,
            status_code=200,
            content_type="text/html",
            charset="utf-8",
            headers={},
            body=body,
            redirects=(),
            redirect_location=None,
            remote_address="192.0.2.1",
            wire_bytes=len(body),
            elapsed_seconds=0.01,
        )


class AllowAll:
    """Test double of the robots.txt policy: allows everything."""

    async def check(self, url: NormalizedUrl) -> RobotsDecision:
        return ALLOWED


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
async def stream(redis_client: Redis, redis_prefix: str) -> CrawlStream:
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    await stream.ensure_group()
    return stream


@pytest.fixture
def fetcher() -> PageFetcher:
    return PageFetcher()


def crawl_context(fetcher: TaskFetcher) -> CrawlContext:
    return CrawlContext(
        fetcher=fetcher,
        robots=AllowAll(),
        targets=CrawlTargetPolicy.from_config(hosts=(HOST,), schemes=("https",), ports=(443,)),
        parse_limits=ParseLimits(),
        crawler_name="SiteScopeBot",
    )


def config(**changes: Any) -> WorkerConfig:
    """Short timings: a lease of 2 s, messages reclaimed after 2 s idle."""
    values: dict[str, Any] = {
        "concurrency": 2,
        "lease_seconds": 2.0,
        "task_deadline": 1.5,
        "max_attempts": 3,
        "read_block_ms": 100,
        "reclaim_interval": 0.2,
        "reclaim_min_idle": 2.0,
        "reclaim_batch": 10,
        "grace_seconds": 1.0,
    }
    values.update(changes)
    return WorkerConfig(**values)


Condition = Callable[[], Awaitable[bool]]


@dataclass
class Running:
    worker: CrawlWorker
    stop: asyncio.Event
    task: asyncio.Task[None]

    async def until(self, condition: Condition, *, within: float = 10.0) -> None:
        async with asyncio.timeout(within):
            while not await condition():
                if self.task.done():
                    self.task.result()  # re-raise a crash
                    raise AssertionError("the worker stopped")
                await asyncio.sleep(0.05)

    async def stopped(self) -> None:
        self.stop.set()
        async with asyncio.timeout(10):
            await self.task


@pytest.fixture
async def start(
    database: Database, stream: CrawlStream, fetcher: PageFetcher
) -> AsyncIterator[Callable[..., Running]]:
    started: list[Running] = []

    def start_worker(
        *,
        worker_config: WorkerConfig | None = None,
        worker_database: Database | None = None,
        worker_stream: CrawlStream | None = None,
        worker_fetcher: TaskFetcher | None = None,
    ) -> Running:
        worker = CrawlWorker(
            worker_database or database,
            worker_stream or stream,
            crawl_context(worker_fetcher or fetcher),
            worker_config or config(),
        )
        stop = asyncio.Event()
        running = Running(worker, stop, asyncio.create_task(worker.run(stop)))
        started.append(running)
        return running

    yield start_worker
    for running in started:
        running.stop.set()
    await asyncio.gather(*(running.task for running in started), return_exceptions=True)


async def dispatch(database: Database, stream: CrawlStream, job_id: uuid.UUID) -> list[str]:
    """Dispatch the job's pending URLs as the scheduler does; returns the entry IDs."""
    entries: list[str] = []

    async def publish(tasks: Any) -> list[str]:
        ids = await stream.publish([CrawlTask(job, url) for job, url in tasks])
        entries.extend(ids)
        return ids

    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=100, publish=publish)
    return entries


async def pending(stream: CrawlStream) -> int:
    return (await stream.pending()).count


async def url_state(database: Database, url_id: uuid.UUID) -> str:
    return (await url_row(database, url_id)).state


async def pages(database: Database, job_id: uuid.UUID) -> int:
    async with database.session() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(Page).where(Page.job_id == job_id)
            )
            or 0
        )


async def claim_as(database: Database, url_id: uuid.UUID, owner: str, lease: float = 60.0) -> Any:
    async with database.session() as session:
        return await claim_url(session, url_id, owner=owner, lease_seconds=lease, max_attempts=3)


# --- The happy path, and messages that need no work ----------------------------------------------


async def test_a_task_is_crawled_recorded_and_then_acknowledged(
    database: Database, stream: CrawlStream, start: Callable[..., Running], logs: JsonLines
) -> None:
    job_id, [seed] = await new_job(database)
    [entry] = await dispatch(database, stream, job_id)
    running = start()

    async def done() -> bool:
        return await url_state(database, seed) == "fetched" and await pending(stream) == 0

    await running.until(done)
    await running.stopped()

    row = await url_row(database, seed)
    assert (row.outcome, row.lease_owner, row.result_epoch) == ("html_stored", None, 1)
    assert await pages(database, job_id) == 1
    [line] = logs.events("task.handled")
    assert line["disposition"] == "ack"
    assert line["reason"] == "completed"
    assert line["settled"] is True
    assert line["reclaimed"] is False
    assert (line["job_id"], line["url_id"], line["message_id"]) == (str(job_id), str(seed), entry)
    assert line["worker_id"] == line["consumer"] == running.worker.name
    assert (line["lease_epoch"], line["depth"], line["attempt"]) == (1, 0, 1)
    assert (line["state"], line["outcome"], line["http_status"]) == ("fetched", "html_stored", 200)
    assert isinstance(line["duration_ms"], int)
    [started] = logs.events("worker.started")
    assert started["consumer"] == running.worker.name


async def test_a_duplicate_message_is_acknowledged_without_a_second_crawl(
    database: Database,
    stream: CrawlStream,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    logs: JsonLines,
) -> None:
    """Duplicate delivery (the same task published twice): one crawl, one page, two acks."""
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    await stream.publish([CrawlTask(job_id, seed)])  # the duplicate
    running = start(worker_config=config(concurrency=1))  # one at a time: a deterministic order

    async def done() -> bool:
        return len(logs.events("task.handled")) == 2

    await running.until(done)
    await running.stopped()

    assert fetcher.calls == [f"https://{HOST}/"]
    assert await pages(database, job_id) == 1
    assert await pending(stream) == 0
    reasons = [line["reason"] for line in logs.events("task.handled")]
    assert reasons == ["completed", "already_finished"]


async def test_a_message_of_a_dispatch_that_rolled_back_is_acknowledged(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    """The scheduler published, then its transaction failed: the URL is still pending. The
    orphaned message is acknowledged; the next dispatch publishes the task again."""
    job_id, [seed] = await new_job(database)
    await stream.publish([CrawlTask(job_id, seed)])
    running = start()

    async def acknowledged() -> bool:
        return await pending(stream) == 0 and running.worker.handled["not_dispatched"] == 1

    await running.until(acknowledged)
    assert await url_state(database, seed) == "pending"
    assert fetcher.calls == []

    await dispatch(database, stream, job_id)  # the next dispatch

    async def fetched() -> bool:
        return await url_state(database, seed) == "fetched"

    await running.until(fetched)
    await running.stopped()
    assert await pages(database, job_id) == 1


@pytest.mark.parametrize("job_status", [JobStatus.COMPLETED, JobStatus.CANCELLING])
async def test_a_task_of_a_job_that_is_not_running_is_acknowledged(
    database: Database,
    stream: CrawlStream,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    job_status: JobStatus,
) -> None:
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    finished = (
        "finished_at = clock_timestamp(),"
        if job_status is JobStatus.COMPLETED
        else "cancel_requested_at = clock_timestamp(),"
    )
    await execute(
        database,
        f"UPDATE crawl_jobs SET {finished} status = :status WHERE id = :id",  # noqa: S608 - constant
        status=job_status.value,
        id=job_id,
    )
    running = start()

    async def acknowledged() -> bool:
        return running.worker.handled["job_not_running"] == 1 and await pending(stream) == 0

    await running.until(acknowledged)
    await running.stopped()
    assert fetcher.calls == []
    assert await url_state(database, seed) == "queued"


# --- Messages that must stay pending -------------------------------------------------------------


async def test_a_live_lease_of_another_worker_keeps_the_message_pending(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    assert await claim_as(database, seed, "crawler-other") is not None
    running = start()

    async def kept() -> bool:
        return running.worker.handled["leased"] >= 1

    await running.until(kept)
    await running.stopped()
    assert await pending(stream) == 1  # never acknowledged
    assert fetcher.calls == []
    row = await url_row(database, seed)
    assert (row.state, row.lease_owner) == ("in_progress", "crawler-other")


async def test_a_result_fenced_by_a_takeover_leaves_the_message_pending(
    database: Database,
    stream: CrawlStream,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    logs: JsonLines,
) -> None:
    """Scenario D (an unresponsive worker): while this worker's fetch hangs, its lease expires
    and another worker takes the URL over and records it. The late result is fenced out, writes
    nothing, and the message is not acknowledged (XACK would act for the whole group)."""
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    fetcher.gate.clear()
    running = start(worker_config=config(lease_seconds=60, task_deadline=50, reclaim_min_idle=60))

    async def fetching() -> bool:
        return bool(fetcher.calls)

    await running.until(fetching)
    await expire_lease(database, seed)
    takeover = await claim_as(database, seed, "crawler-new")
    assert takeover is not None
    assert takeover.token == 2
    async with database.session() as session:
        recorded = await record_result(session, takeover, html_result("new"), max_redirect_hops=5)
    assert recorded.status is RecordStatus.RECORDED
    fetcher.gate.set()

    async def fenced() -> bool:
        return running.worker.handled["fenced"] == 1

    await running.until(fenced)
    await running.stopped()
    assert await pending(stream) == 1
    row = await url_row(database, seed)
    assert (row.state, row.result_epoch) == ("fetched", 2)  # the takeover's result stands
    assert await pages(database, job_id) == 1
    [line] = logs.events("task.fenced")
    assert line["lease_epoch"] == 1


async def test_an_infrastructure_error_leaves_the_message_pending(
    test_database: Settings, stream: CrawlStream, start: Callable[..., Running], logs: JsonLines
) -> None:
    """Scenario F at the handler: PostgreSQL unreachable. The outcome is unknown, so the
    message stays pending, and the worker pauses instead of churning through the stream."""
    task = CrawlTask(uuid.uuid4(), uuid.uuid4())
    await stream.publish([task])
    down = Database(
        test_database.model_copy(
            update={"postgres_port": unused_port(), "db_connect_timeout_seconds": 1.0}
        ),
        application_name="sitescope-tests-down",
    )
    try:
        running = start(worker_database=down)

        async def failed() -> bool:
            return running.worker.handled["error"] >= 1

        await running.until(failed)
        await running.stopped()
    finally:
        await down.dispose()
    assert await pending(stream) == 1
    errors = logs.events("worker.error")
    assert errors
    assert errors[0]["source"] == "task"
    assert errors[0]["retry_in_ms"] >= 0
    # A transient error: a warning without a traceback (no log flood during an outage).
    assert errors[0]["level"] == "WARNING"
    assert "exception" not in errors[0]
    # Phase 10: the message's line names the stage that failed and the error's type.
    handled = [line for line in logs.events("task.handled") if line["reason"] == "error"]
    assert handled
    assert handled[0]["stage"] == "claim"
    assert handled[0]["error_type"]


# --- Observability (Phase 10) --------------------------------------------------------------------


async def test_a_crawled_tasks_line_and_metrics_carry_its_stage_times_and_queue_delay(
    database: Database, stream: CrawlStream, fetcher: PageFetcher, logs: JsonLines
) -> None:
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    await asyncio.sleep(0.3)  # the task waits in the stream before any worker runs
    registry = Registry()
    worker = CrawlWorker(database, stream, crawl_context(fetcher), config(), registry=registry)
    stop = asyncio.Event()
    running = Running(worker, stop, asyncio.create_task(worker.run(stop)))

    async def done() -> bool:
        return await url_state(database, seed) == "fetched" and await pending(stream) == 0

    await running.until(done)
    await running.stopped()

    [line] = logs.events("task.handled")
    # Dispatch to claim, by the PostgreSQL clock: at least the time it waited in the stream.
    assert line["queue_delay_ms"] >= 250
    assert min(line["claim_ms"], line["crawl_ms"], line["record_ms"]) >= 0
    assert line["politeness_wait_ms"] == 0  # this test's fetcher has no politeness gate
    assert "failure_category" not in line  # a stored page
    families = {family["name"]: family for family in registry.snapshot()}
    assert families["sitescope_tasks_handled_total"]["series"] == [[["completed", "ack"], 1.0]]
    assert families["sitescope_crawl_results_total"]["series"] == [
        [["fetched", "html_stored"], 1.0]
    ]
    [[_labels, _buckets, delay_sum, delay_count]] = families["sitescope_queue_delay_seconds"][
        "series"
    ]
    assert delay_count == 1
    assert delay_sum >= 0.25
    operations = {labels[0] for labels, *_ in families["sitescope_db_operation_seconds"]["series"]}
    assert {"claim", "record"} <= operations
    assert families["sitescope_crawl_phase_seconds"]["series"][0][3] == 1


async def test_a_worker_writes_a_heartbeat_with_its_state_and_removes_it_when_it_stops(
    database: Database,
    stream: CrawlStream,
    fetcher: PageFetcher,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    worker = CrawlWorker(database, stream, crawl_context(fetcher), config(), registry=Registry())
    heartbeat = HeartbeatPublisher(
        redis_client,
        key_prefix=redis_prefix,
        name=worker.name,
        role="crawler",
        interval=0.2,
        state=worker.heartbeat_state,
    )
    stop = asyncio.Event()
    running = Running(worker, stop, asyncio.create_task(worker.run(stop, heartbeat=heartbeat)))
    beats: list[Heartbeat] = []

    async def beating() -> bool:
        beats[:] = await read_heartbeats(redis_client, key_prefix=redis_prefix)
        return bool(beats)

    await running.until(beating)
    await running.stopped()

    [beat] = beats
    assert (beat.role, beat.name) == ("crawler", worker.name)
    assert beat.state["concurrency"] == 2
    assert beat.state["in_flight"] == 0
    assert beat.state["stream_failures"] == 0
    assert isinstance(beat.payload["metrics"], list)
    # A graceful stop removes the heartbeat at once (a crashed process's expires).
    assert await read_heartbeats(redis_client, key_prefix=redis_prefix) == []


# --- Takeover and recovery ------------------------------------------------------------------------


async def test_a_crashed_workers_message_is_taken_over_after_the_idle_time(
    database: Database, stream: CrawlStream, start: Callable[..., Running], logs: JsonLines
) -> None:
    """Scenario B in-process: a worker read the message and claimed the URL, then vanished.
    After its lease expired and the message idled long enough, another worker reclaims the
    message (XAUTOCLAIM) and takes the lease over with a new token."""
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    await stream.read("crawler-dead", count=1, block_ms=100)
    assert await claim_as(database, seed, "crawler-dead", lease=1.0) is not None
    running = start()

    async def done() -> bool:
        return await url_state(database, seed) == "fetched" and await pending(stream) == 0

    await running.until(done, within=15)
    await running.stopped()
    row = await url_row(database, seed)
    assert (row.result_epoch, row.attempt_count) == (2, 2)
    [recovered] = await events(database, job_id, "url.recovered")
    assert recovered.worker_id == running.worker.name
    assert recovered.detail["previous_owner"] == "crawler-dead"
    [line] = logs.events("task.handled")
    assert line["reclaimed"] is True
    assert line["reason"] == "completed"


async def test_an_expired_lease_after_the_last_attempt_fails_the_url(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    for _ in range(3):  # three crashed attempts
        assert await claim_as(database, seed, "crawler-crashing") is not None
        await expire_lease(database, seed)
    running = start()

    async def acknowledged() -> bool:
        return running.worker.handled[EXHAUSTED_OUTCOME] == 1 and await pending(stream) == 0

    await running.until(acknowledged)
    await running.stopped()
    assert fetcher.calls == []
    row = await url_row(database, seed)
    assert (row.state, row.outcome, row.attempt_count) == ("failed", EXHAUSTED_OUTCOME, 3)


class FlakyAckStream(CrawlStream):
    """The real stream, except that the first ``fail`` XACKs fail (Redis lost mid-way)."""

    def __init__(self, *args: Any, fail: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failures_left = fail

    async def ack(self, entry_id: str) -> bool:
        if self.failures_left > 0:
            self.failures_left -= 1
            raise RedisConnectionError("Connection lost")
        return await super().ack(entry_id)


async def test_a_lost_acknowledgment_is_repaired_by_the_redelivery(
    database: Database,
    redis_client: Redis,
    redis_prefix: str,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    logs: JsonLines,
) -> None:
    """Scenario C: the result is committed but the XACK fails. The message stays pending, is
    reclaimed after the idle time, finds the URL finished, and is acknowledged then. The page
    is crawled and stored once."""
    flaky = FlakyAckStream(redis_client, key_prefix=redis_prefix, fail=1)
    await flaky.ensure_group()
    job_id, [_seed] = await new_job(database)
    await dispatch(database, flaky, job_id)
    running = start(worker_stream=flaky)

    async def repaired() -> bool:
        return await pending(flaky) == 0 and running.worker.handled["already_finished"] == 1

    await running.until(repaired, within=15)
    await running.stopped()
    assert fetcher.calls == [f"https://{HOST}/"]
    assert await pages(database, job_id) == 1
    [failed] = logs.events("task.settle_failed")
    assert failed["disposition"] == "ack"
    reasons = [(line["reason"], line["settled"]) for line in logs.events("task.handled")]
    assert reasons == [("completed", False), ("already_finished", True)]


# --- Poison and foreign messages ----------------------------------------------------------------


async def test_a_malformed_message_is_dead_lettered(
    stream: CrawlStream, redis_client: Redis, start: Callable[..., Running]
) -> None:
    entry = await redis_client.xadd(stream.key, {"v": "1", "job_id": "nope", "url_id": "x"})
    running = start()

    async def handled() -> bool:
        return running.worker.handled["malformed"] == 1

    await running.until(handled)
    await running.stopped()
    assert await pending(stream) == 0
    [(_, fields)] = await stream_entries(redis_client, stream.dead_letter_key)
    assert fields["original_id"] == entry
    assert fields["reason"] == "job_id and url_id must be UUIDs"


async def test_messages_that_do_not_match_the_database_are_dead_lettered(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    # Take the real message out of the way: only the foreign ones below reach the worker.
    await stream.ack((await stream.read("crawler-x", count=1, block_ms=100))[0].entry_id)
    await stream.publish(
        [CrawlTask(uuid.uuid4(), seed), CrawlTask(job_id, uuid.uuid4())]  # wrong job, no URL
    )
    running = start()

    async def handled() -> bool:
        worker = running.worker
        return worker.handled["job_mismatch"] == 1 and worker.handled["unknown_url"] == 1

    await running.until(handled)
    await running.stopped()
    assert fetcher.calls == []
    assert await url_state(database, seed) == "queued"  # never claimed through a wrong job
    assert await pending(stream) == 0


async def test_an_entry_that_is_not_utf8_is_dead_lettered_and_its_batch_processed(
    database: Database,
    stream: CrawlStream,
    redis_client: Redis,
    start: Callable[..., Running],
) -> None:
    """Regression (Phase 5 review): the poison entry used to fail every batch it was read or
    reclaimed in, so the valid tasks of the batch were never processed and the job stalled."""
    job_id, url_ids = await new_job(database, urls=1)
    await dispatch(database, stream, job_id)
    poison = await redis_client.xadd(stream.key, {b"v\xff": b"\xfe", "job_id": b"\x80"})
    running = start(worker_config=config(concurrency=4))  # one read gets all three entries

    async def done() -> bool:
        states = [await url_state(database, url_id) for url_id in url_ids]
        malformed = running.worker.handled["malformed"] == 1
        return states == ["fetched", "fetched"] and malformed and await pending(stream) == 0

    await running.until(done)
    await running.stopped()
    [(_, fields)] = await stream_entries(redis_client, stream.dead_letter_key)
    assert fields["original_id"] == poison


# --- Backpressure and shutdown -------------------------------------------------------------------


async def test_a_worker_reads_no_more_messages_than_it_has_free_slots(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    job_id, _ = await new_job(database, urls=4)
    await dispatch(database, stream, job_id)
    fetcher.gate.clear()
    running = start(worker_config=config(lease_seconds=60, task_deadline=50, reclaim_min_idle=60))

    async def busy() -> bool:
        return len(fetcher.calls) == 2

    await running.until(busy)
    await asyncio.sleep(0.5)  # several read cycles
    summary = await stream.pending()
    assert summary.count == 2  # the other three messages are still undelivered
    assert running.worker.in_flight == 2

    fetcher.gate.set()

    async def all_done() -> bool:
        async with database.session() as session:
            left = await session.scalar(
                select(func.count())
                .select_from(CrawlUrl)
                .where(CrawlUrl.job_id == job_id, CrawlUrl.state != "fetched")
            )
        return left == 0

    await running.until(all_done)
    await running.stopped()


async def test_shutdown_lets_running_tasks_finish_within_the_grace_period(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    fetcher.gate.clear()
    running = start(worker_config=config(grace_seconds=5))

    async def fetching() -> bool:
        return bool(fetcher.calls)

    await running.until(fetching)
    running.stop.set()
    await asyncio.sleep(0.2)
    fetcher.gate.set()  # the task finishes during the grace period
    await running.stopped()

    assert await url_state(database, seed) == "fetched"
    assert await pending(stream) == 0


async def test_shutdown_cancels_tasks_after_the_grace_period_and_keeps_them_recoverable(
    database: Database,
    stream: CrawlStream,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    logs: JsonLines,
) -> None:
    """A cancelled task records nothing: the URL stays in progress under its lease, and the
    message stays pending, so another worker recovers it after the lease expires."""
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    fetcher.gate.clear()
    running = start(
        worker_config=config(
            grace_seconds=0.3, lease_seconds=60, task_deadline=50, reclaim_min_idle=60
        )
    )

    async def fetching() -> bool:
        return bool(fetcher.calls)

    await running.until(fetching)
    await running.stopped()

    row = await url_row(database, seed)
    assert (row.state, row.lease_owner) == ("in_progress", running.worker.name)
    assert await pending(stream) == 1
    [cancelled] = logs.events("worker.tasks_cancelled")
    assert cancelled["count"] == 1
    assert row.state == UrlState.IN_PROGRESS.value


# --- Phase 6: retries ---------------------------------------------------------------------------

SEED_URL = f"https://{HOST}/"
FAST_RETRIES = RetryPolicy(max_attempts=3, base_delay=0.05, max_delay=0.1)


def read_timeout() -> FetchError:
    return FetchError(FailureKind.READ_TIMEOUT, "no data within 10 s", url=SEED_URL)


def http_error(status: int, retry_after: float | None = None) -> FetchError:
    return FetchError(
        FailureKind.HTTP_ERROR,
        f"HTTP {status} from {HOST}",
        url=SEED_URL,
        http_status=status,
        retry_after=retry_after,
    )


async def scheduled_until(
    database: Database,
    stream: CrawlStream,
    running: Running,
    job_id: uuid.UUID,
    url_id: uuid.UUID,
    *final: str,
    within: float = 10.0,
) -> None:
    """Act as the scheduler's dispatcher (publish due retries) until the URL's state is final."""

    async def done() -> bool:
        await dispatch(database, stream, job_id)
        return await url_state(database, url_id) in final

    await running.until(done, within=within)


async def test_a_transient_failure_is_retried_by_a_new_claim_and_then_succeeds(
    database: Database,
    stream: CrawlStream,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    logs: JsonLines,
) -> None:
    job_id, [seed] = await new_job(database)
    fetcher.failures[SEED_URL] = [read_timeout()]
    running = start(worker_config=config(retry=FAST_RETRIES))

    await scheduled_until(database, stream, running, job_id, seed, "fetched")
    await running.stopped()

    row = await url_row(database, seed)
    assert (row.outcome, row.attempt_count, row.lease_epoch, row.result_epoch) == (
        "html_stored",
        2,
        2,
        2,
    )
    assert fetcher.calls == [SEED_URL, SEED_URL]
    [scheduled] = await events(database, job_id, "url.retry_scheduled")
    assert (scheduled.lease_epoch, scheduled.detail["outcome"]) == (1, "read_timeout")
    assert scheduled.detail["delay_ms"] <= 50  # attempt 1: full jitter up to the base delay
    reasons = [line["reason"] for line in logs.events("task.handled")]
    assert reasons == ["retry_scheduled", "completed"]  # each acknowledged after its commit
    assert await pending(stream) == 0


@pytest.mark.parametrize("status", [404, 410])
async def test_a_permanent_failure_is_final_at_once(
    database: Database,
    stream: CrawlStream,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    status: int,
) -> None:
    job_id, [seed] = await new_job(database)
    fetcher.failures[SEED_URL] = [http_error(status)]
    await dispatch(database, stream, job_id)
    running = start(worker_config=config(retry=FAST_RETRIES))

    async def final() -> bool:
        return await url_state(database, seed) == "fetched" and await pending(stream) == 0

    await running.until(final)
    await running.stopped()
    row = await url_row(database, seed)
    assert (row.outcome, row.http_status, row.attempt_count) == ("http_error", status, 1)
    assert await events(database, job_id, "url.retry_scheduled") == []
    assert fetcher.calls == [SEED_URL]


async def test_the_failure_of_the_last_attempt_is_final(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    job_id, [seed] = await new_job(database)
    fetcher.failures[SEED_URL] = [http_error(503), http_error(502), http_error(500)]
    running = start(worker_config=config(retry=FAST_RETRIES))

    await scheduled_until(database, stream, running, job_id, seed, "fetched")
    await running.stopped()

    row = await url_row(database, seed)
    assert (row.outcome, row.http_status, row.attempt_count, row.result_epoch) == (
        "http_error",
        500,
        3,
        3,
    )
    retries = await events(database, job_id, "url.retry_scheduled")
    assert [event.detail["http_status"] for event in retries] == [503, 502]
    assert len(fetcher.calls) == 3


async def test_retry_after_is_waited_for(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    job_id, [seed] = await new_job(database)
    fetcher.failures[SEED_URL] = [http_error(429, retry_after=1.0)]
    await dispatch(database, stream, job_id)
    running = start(worker_config=config(retry=FAST_RETRIES))

    async def scheduled() -> bool:
        return bool(await events(database, job_id, "url.retry_scheduled"))

    await running.until(scheduled)
    [event] = await events(database, job_id, "url.retry_scheduled")
    assert (event.detail["retry_after"], event.detail["delay_ms"]) == (1.0, 1000)
    assert await dispatch(database, stream, job_id) == []  # not due yet: Retry-After holds it
    await scheduled_until(database, stream, running, job_id, seed, "fetched")
    await running.stopped()
    async with database.session() as session:
        waited = await session.scalar(
            select(func.extract("epoch", CrawlUrl.finished_at - event.occurred_at)).where(
                CrawlUrl.id == seed
            )
        )
    assert waited is not None
    assert float(waited) >= 1.0


async def test_a_retry_after_beyond_the_cap_is_final(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    job_id, [seed] = await new_job(database)
    fetcher.failures[SEED_URL] = [http_error(503, retry_after=3600.0)]
    await dispatch(database, stream, job_id)
    policy = RetryPolicy(max_attempts=3, base_delay=0.05, max_delay=0.1, retry_after_max=300.0)
    running = start(worker_config=config(retry=policy))

    async def final() -> bool:
        return await url_state(database, seed) == "fetched"

    await running.until(final)
    await running.stopped()
    row = await url_row(database, seed)
    assert (row.outcome, row.http_status, row.attempt_count) == ("http_error", 503, 1)


async def test_an_infrastructure_error_during_a_task_is_a_retryable_attempt(
    database: Database,
    stream: CrawlStream,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    logs: JsonLines,
) -> None:
    """Redis failed inside the task (the politeness gate or the robots.txt cache): the attempt
    is recorded as infrastructure_error and retried, instead of waiting for the lease."""
    job_id, [seed] = await new_job(database)
    fetcher.failures[SEED_URL] = [RedisConnectionError("Redis is unavailable")]
    running = start(worker_config=config(retry=FAST_RETRIES))

    await scheduled_until(database, stream, running, job_id, seed, "fetched")
    await running.stopped()

    [scheduled] = await events(database, job_id, "url.retry_scheduled")
    assert scheduled.detail["outcome"] == "infrastructure_error"
    [line] = logs.events("task.infrastructure_error")
    assert line["error_type"] == "ConnectionError"
    assert (await url_row(database, seed)).attempt_count == 2


# --- Phase 6: cancellation --------------------------------------------------------------------


async def test_a_task_of_a_cancelled_job_is_stopped_and_recorded_cancelled(
    database: Database,
    stream: CrawlStream,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    logs: JsonLines,
) -> None:
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    fetcher.gate.clear()  # the fetch hangs
    running = start(
        worker_config=config(
            lease_seconds=60, task_deadline=50, reclaim_min_idle=60, cancel_check_interval=0.1
        )
    )

    async def fetching() -> bool:
        return bool(fetcher.calls)

    await running.until(fetching)
    async with database.session() as session:
        requested = await cancel_crawl_job(session, job_id)
    assert requested.status is JobStatus.CANCELLING
    started = time.monotonic()

    async def cancelled() -> bool:
        return await url_state(database, seed) == "cancelled" and await pending(stream) == 0

    await running.until(cancelled, within=5)
    latency = time.monotonic() - started
    await running.stopped()

    assert latency < 3.0  # a few cancellation checks (0.1 s each); the fetch itself never ends
    row = await url_row(database, seed)
    assert (row.outcome, row.result_epoch, row.lease_owner) == ("job_cancelled", 1, None)
    [line] = [line for line in logs.events("task.handled") if line["reason"] == "cancelled"]
    assert (line["disposition"], line["settled"]) == ("ack", True)
    async with database.session() as session:
        assert (await settle_cancellation(session, job_id)).finalized
    assert (await job_row(database, job_id)).status == "cancelled"


async def test_a_stale_task_of_a_completed_job_is_not_stopped_but_fenced(
    database: Database,
    stream: CrawlStream,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    logs: JsonLines,
) -> None:
    """Regression (Phase 6 self-review, found by the fault test of a frozen worker): the watcher
    stopped every task whose job was no longer running, so a stale task of a completed job was
    reported as cancelled. Only cancellation stops a task; a stale result is fenced as before."""
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    fetcher.gate.clear()
    running = start(
        worker_config=config(
            lease_seconds=60, task_deadline=50, reclaim_min_idle=60, cancel_check_interval=0.1
        )
    )

    async def fetching() -> bool:
        return bool(fetcher.calls)

    await running.until(fetching)
    await expire_lease(database, seed)
    takeover = await claim_as(database, seed, "crawler-new")
    assert takeover is not None
    async with database.session() as session:
        await record_result(session, takeover, html_result("new"), max_redirect_hops=5)
    async with database.session() as session:
        assert (await advance_job(session, job_id)).action is AdvanceAction.COMPLETED
    await asyncio.sleep(0.5)  # several cancellation checks
    fetcher.gate.set()

    async def fenced() -> bool:
        return running.worker.handled["fenced"] == 1

    await running.until(fenced)
    await running.stopped()
    assert logs.events("task.stopping_cancelled") == []
    assert [line["lease_epoch"] for line in logs.events("task.fenced")] == [1]
    assert await pending(stream) == 1  # not acknowledged by the stale worker


async def test_messages_of_a_cancelled_job_cannot_revive_it(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    job_id, url_ids = await new_job(database, urls=2)
    await dispatch(database, stream, job_id)
    await stream.publish([CrawlTask(job_id, url_ids[0])])  # a duplicate, too
    async with database.session() as session:
        assert (await cancel_crawl_job(session, job_id)).status is JobStatus.CANCELLED
    running = start()

    async def settled() -> bool:
        return running.worker.handled["already_finished"] == 4 and await pending(stream) == 0

    await running.until(settled)
    await running.stopped()
    assert fetcher.calls == []
    assert {await url_state(database, url_id) for url_id in url_ids} == {"cancelled"}


# --- Phase 6: poison messages and dead letters ------------------------------------------------


async def deliver_times(redis: Redis, stream: CrawlStream, entry: str, times: int) -> None:
    """Deliver an entry ``times`` times to a consumer that never settles it (XCLAIM counts)."""
    await stream.read("crawler-poisoned", count=10, block_ms=100)
    for _ in range(times - 1):
        await redis.xclaim(stream.key, stream.group, "crawler-poisoned", 0, [entry])


async def test_a_poison_message_fails_its_url_and_is_dead_lettered_once(
    database: Database,
    stream: CrawlStream,
    redis_client: Redis,
    start: Callable[..., Running],
    fetcher: PageFetcher,
) -> None:
    """A queued URL that no claim can take any more (its attempts were used up before
    SITESCOPE_CRAWL_MAX_ATTEMPTS was lowered): its message could never be settled."""
    job_id, [seed] = await new_job(database)
    [entry] = await dispatch(database, stream, job_id)
    await execute(database, "UPDATE crawl_urls SET attempt_count = 3 WHERE id = :id", id=seed)
    await deliver_times(redis_client, stream, entry, 4)
    running = start(worker_config=config(max_deliveries=4, reclaim_min_idle=2.0))

    async def quarantined() -> bool:
        return running.worker.handled[MAX_DELIVERIES_OUTCOME] == 1 and await pending(stream) == 0

    await running.until(quarantined)
    await running.stopped()

    assert fetcher.calls == []  # never crawled
    row = await url_row(database, seed)
    assert (row.state, row.outcome, row.attempt_count) == ("failed", MAX_DELIVERIES_OUTCOME, 3)
    [(_, fields)] = await stream_entries(redis_client, stream.dead_letter_key)
    assert fields["original_id"] == entry
    assert (fields["kind"], fields["reason"]) == (MAX_DELIVERIES_OUTCOME, MAX_DELIVERIES_OUTCOME)
    assert fields["deliveries"] == "5"  # four before, and the worker's reclaim
    assert (fields["job_id"], fields["url_id"]) == (str(job_id), str(seed))
    assert fields["consumer"] == running.worker.name
    [dead] = await events(database, job_id, "url.dead_lettered")
    assert dead.detail == {"reason": MAX_DELIVERIES_OUTCOME, "deliveries": 5}


async def test_delivery_counts_inflated_by_an_outage_fail_no_url(
    database: Database,
    stream: CrawlStream,
    redis_client: Redis,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    logs: JsonLines,
) -> None:
    """Regression (Phase 6 independent review, high). While PostgreSQL is down, workers keep
    reclaiming the pending messages, and every XAUTOCLAIM counts a delivery; the handling fails
    before the claim each time. Afterwards, a URL that was never tried must be claimed and
    crawled as usual (its attempts still bound the work), not failed as poison."""
    job_id, [seed] = await new_job(database)
    [entry] = await dispatch(database, stream, job_id)
    await deliver_times(redis_client, stream, entry, 9)  # nine fruitless deliveries
    running = start(worker_config=config(max_deliveries=4, reclaim_min_idle=2.0))

    async def crawled() -> bool:
        return await url_state(database, seed) == "fetched" and await pending(stream) == 0

    await running.until(crawled)
    await running.stopped()
    row = await url_row(database, seed)
    assert (row.outcome, row.attempt_count, row.lease_epoch) == ("html_stored", 1, 1)
    assert fetcher.calls == [f"https://{HOST}/"]
    assert await redis_client.xlen(stream.dead_letter_key) == 0
    assert await events(database, job_id, "url.dead_lettered") == []
    [handled] = logs.events("task.handled")
    assert (handled["reason"], handled["deliveries"]) == ("completed", 10)


class UncountedStream(CrawlStream):
    """The task stream, with XPENDING failing (Redis trouble right after an XAUTOCLAIM)."""

    async def delivery_counts(self, entry_ids: Sequence[str]) -> dict[str, int]:
        raise RedisConnectionError("Redis is unavailable")


async def test_reclaimed_messages_are_handled_even_if_their_counts_cannot_be_read(
    database: Database,
    stream: CrawlStream,
    redis_client: Redis,
    redis_prefix: str,
    start: Callable[..., Running],
    logs: JsonLines,
) -> None:
    """Regression (Phase 6 independent review, low): the XAUTOCLAIM had already moved the
    messages to this worker, so dropping the batch when XPENDING failed hid them for another
    reclaim idle time each round (and counted another delivery)."""
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    await stream.read("crawler-dead", count=1, block_ms=100)  # abandoned before its claim
    uncounted = UncountedStream(redis_client, key_prefix=redis_prefix)
    running = start(worker_config=config(reclaim_min_idle=2.0), worker_stream=uncounted)

    async def fetched() -> bool:
        return await url_state(database, seed) == "fetched" and await pending(stream) == 0

    await running.until(fetched)
    await running.stopped()
    [line] = logs.events("task.delivery_counts_failed")
    assert line["error_type"] == "ConnectionError"


async def test_a_poison_message_of_a_live_lease_stays_pending(
    database: Database,
    stream: CrawlStream,
    redis_client: Redis,
    start: Callable[..., Running],
) -> None:
    job_id, [seed] = await new_job(database)
    [entry] = await dispatch(database, stream, job_id)
    assert await claim_as(database, seed, "crawler-other") is not None  # a live lease
    await deliver_times(redis_client, stream, entry, 4)
    running = start(worker_config=config(max_deliveries=4, reclaim_min_idle=2.0))

    async def kept() -> bool:
        return running.worker.handled["leased"] >= 1

    await running.until(kept)
    await running.stopped()
    assert await pending(stream) == 1
    assert await redis_client.xlen(stream.dead_letter_key) == 0
    assert (await url_row(database, seed)).state == "in_progress"


async def test_dead_lettering_an_entry_twice_copies_it_once(
    stream: CrawlStream, redis_client: Redis
) -> None:
    """Two consumers (or a retry after a lost reply) dead-letter the same entry: the Lua script
    copies it only when its own XACK removed the entry from the pending list."""
    entry = await redis_client.xadd(stream.key, {"v": "2", "payload": "https://secret.example/"})
    [delivery] = await stream.read("crawler-a", count=1, block_ms=100)

    first = await stream.dead_letter(delivery, delivery.error or "", consumer="crawler-a")
    second = await stream.dead_letter(delivery, delivery.error or "", consumer="crawler-b")

    assert (first, second) == (True, False)
    [(_, fields)] = await stream_entries(redis_client, stream.dead_letter_key)
    assert fields["original_id"] == entry
    assert fields["original.v"] == "2"
    assert fields["original.other_fields"] == "payload"  # names only: the value is not copied
    assert "https://secret.example/" not in json.dumps(fields)
    assert await pending(stream) == 0


async def test_recent_dead_letters_are_listed_newest_first(
    stream: CrawlStream, redis_client: Redis
) -> None:
    entries = [await redis_client.xadd(stream.key, {"v": "9", "n": str(n)}) for n in range(3)]
    for delivery in await stream.read("crawler-a", count=3, block_ms=100):
        await stream.dead_letter(delivery, "malformed", consumer="crawler-a")

    listed = await stream.recent_dead_letters(2)

    assert [item["original_id"] for item in listed] == [entries[2], entries[1]]
    assert await stream.recent_dead_letters(0) == []


@dataclass
class SlotFetcher(PageFetcher):
    """``PageFetcher`` behind a real politeness gate (Redis), as the safe fetcher is."""

    politeness: PolitenessGate | None = None

    async def fetch(self, url: str, *, max_redirects: int | None = None) -> FetchResult:
        assert self.politeness is not None
        async with self.politeness.slot(normalize_url(url)):
            return await super().fetch(url, max_redirects=max_redirects)


CANCEL_FAST = {
    "lease_seconds": 60.0,
    "task_deadline": 50.0,
    "reclaim_min_idle": 60.0,
    "cancel_check_interval": 0.1,
}


async def test_cancellation_while_waiting_for_a_politeness_slot_leaves_nothing_in_redis(
    database: Database,
    stream: CrawlStream,
    redis_client: Redis,
    redis_prefix: str,
    start: Callable[..., Running],
) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    _, active, _, queue, seen = store.keys(HOST)
    # Another request holds the host's only slot, so the task waits in the gate (Redis).
    assert (
        await store.acquire(
            HOST, "holder", interval=0.0, max_concurrent=1, slot_ttl=60.0, waiter_ttl=10.0
        )
    ).granted
    politeness = PolitenessGate(
        store,
        PolitenessConfig(interval=0.0, max_concurrent=1, max_wait=30.0, poll_interval=0.05),
    )
    slow = SlotFetcher(politeness=politeness)
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    running = start(worker_config=config(**CANCEL_FAST), worker_fetcher=slow)

    async def waiting() -> bool:
        return await redis_client.zcard(queue) == 1

    await running.until(waiting)
    async with database.session() as session:
        await cancel_crawl_job(session, job_id)

    async def cancelled() -> bool:
        return await url_state(database, seed) == "cancelled" and await pending(stream) == 0

    await running.until(cancelled, within=5)
    await running.stopped()
    assert slow.calls == []  # never sent
    assert (await redis_client.zcard(queue), await redis_client.zcard(seen)) == (0, 0)
    assert await redis_client.zrange(active, 0, -1) == ["holder"]  # someone else's slot stays


async def test_a_result_being_committed_is_not_interrupted_by_a_cancellation(
    database: Database,
    stream: CrawlStream,
    start: Callable[..., Running],
    fetcher: PageFetcher,
    logs: JsonLines,
) -> None:
    """Cancellation during persistence: the crawl phase is over, so the watcher leaves the
    commit alone; the result is recorded and acknowledged, then the job is finalized."""
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    fetcher.gate.clear()
    running = start(worker_config=config(**CANCEL_FAST))

    async def fetching() -> bool:
        return bool(fetcher.calls)

    await running.until(fetching)
    async with database.engine.connect() as blocker:
        transaction = await blocker.begin()
        # Hold the URL row: the worker's fenced update will wait for it.
        await blocker.execute(
            text("SELECT 1 FROM crawl_urls WHERE id = :id FOR UPDATE"), {"id": seed}
        )
        fetcher.gate.set()

        async def committing() -> bool:
            async with database.session() as session:
                waiting = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            return bool(waiting)

        await running.until(committing)
        async with database.session() as session:
            assert (await cancel_crawl_job(session, job_id)).status is JobStatus.CANCELLING
        await asyncio.sleep(0.5)  # five cancellation checks
        await transaction.rollback()

    async def recorded() -> bool:
        return await url_state(database, seed) == "fetched" and await pending(stream) == 0

    await running.until(recorded)
    await running.stopped()
    assert logs.events("task.stopping_cancelled") == []
    async with database.session() as session:
        assert (await settle_cancellation(session, job_id)).finalized
    assert (await job_row(database, job_id)).status == "cancelled"


async def test_a_worker_shutting_down_still_stops_tasks_of_a_cancelled_job(
    database: Database, stream: CrawlStream, start: Callable[..., Running], fetcher: PageFetcher
) -> None:
    """Cancellation during a graceful shutdown: the task is stopped and recorded cancelled at
    once, instead of running out the grace period and being cut off with its lease."""
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    fetcher.gate.clear()
    running = start(worker_config=config(**CANCEL_FAST, grace_seconds=30.0))

    async def fetching() -> bool:
        return bool(fetcher.calls)

    await running.until(fetching)
    running.stop.set()  # draining: waiting for the task, up to 30 s
    started = time.monotonic()
    async with database.session() as session:
        await cancel_crawl_job(session, job_id)
    async with asyncio.timeout(5):
        await running.task
    assert time.monotonic() - started < 3.0
    assert await url_state(database, seed) == "cancelled"
    assert await pending(stream) == 0


class HangingResolver(ScriptedResolver):
    """DNS that never answers: a hung lookup."""

    def __init__(self) -> None:
        super().__init__({})
        self.waiting = asyncio.Event()

    async def resolve(self, host: str) -> list[IPAddress]:
        self.calls.append(host)
        self.waiting.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_cancellation_during_dns_resolution_stops_the_task_before_any_connection(
    database: Database, stream: CrawlStream, start: Callable[..., Running]
) -> None:
    """The real safe fetcher, waiting for DNS: the cancellation interrupts the lookup, so no
    connection is ever opened, and the URL is recorded cancelled under the worker's token."""
    resolver = HangingResolver()
    backend = RecordingBackend()
    fetcher = make_fetcher(
        resolver=resolver,
        port=443,
        hosts=(HOST,),
        internal=(HOST,),
        schemes=("https",),
        backend=backend,
        connect_timeout=40.0,
        read_timeout=40.0,
        deadline=45.0,
    )
    job_id, [seed] = await new_job(database)
    await dispatch(database, stream, job_id)
    running = start(worker_config=config(**CANCEL_FAST), worker_fetcher=fetcher)

    async with asyncio.timeout(10):
        await resolver.waiting.wait()
    async with database.session() as session:
        await cancel_crawl_job(session, job_id)

    async def cancelled() -> bool:
        return await url_state(database, seed) == "cancelled" and await pending(stream) == 0

    await running.until(cancelled, within=5)
    await running.stopped()
    assert resolver.calls == [HOST]
    assert backend.targets == []  # nothing was connected to
    row = await url_row(database, seed)
    assert (row.outcome, row.result_epoch) == ("job_cancelled", 1)
