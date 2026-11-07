"""Scheduler passes against real PostgreSQL and Redis (app/scheduler/scheduler.py, in-process).

Results are recorded directly through the crawl-state use cases (as a worker would), so these
tests isolate the scheduler: activation, level advancement, dispatch limits, reconciliation and
stream maintenance. Real scheduler processes are tested in tests/integration/test_distributed.py
and tests/fault.
"""

import asyncio
import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.redis import create_redis_client
from app.db.models import CrawlUrl, JobStatus, UrlState
from app.db.session import Database
from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream
from app.scheduler.scheduler import Scheduler, SchedulerConfig
from app.services.crawl_state import (
    LinkRecord,
    PageRecord,
    RecordStatus,
    UrlResult,
    claim_url,
    record_result,
)
from tests.crawl_support import (
    age_dispatch,
    execute,
    job_row,
    new_job,
    states,
    stream_entries,
    url_row,
)
from tests.integration.helpers import unused_port
from tests.process_support import wait_for

pytestmark = pytest.mark.anyio


@pytest.fixture
async def stream(redis_client: Redis, redis_prefix: str) -> CrawlStream:
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    await stream.ensure_group()
    return stream


def scheduler(database: Database, stream: CrawlStream, **changes: Any) -> Scheduler:
    values: dict[str, Any] = {"interval": 0.05, "reconcile_after": 600, "retention_seconds": 3600}
    values.update(changes)
    return Scheduler(database, stream, SchedulerConfig(**values))


async def published(redis_client: Redis, stream: CrawlStream) -> list[CrawlTask]:
    return [
        CrawlTask.decode(fields) for _, fields in await stream_entries(redis_client, stream.key)
    ]


def page(*paths: str) -> UrlResult:
    links = tuple(
        LinkRecord(
            url=f"https://example.test{path}",
            position=index,
            anchor_text=None,
            nofollow=False,
            is_internal=True,
        )
        for index, path in enumerate(paths)
    )
    return UrlResult(
        state=UrlState.FETCHED,
        outcome="html_stored",
        http_status=200,
        page=PageRecord(
            http_status=200,
            content_type="text/html",
            title=None,
            description=None,
            headings="",
            text="text",
            byte_size=4,
            fetch_duration_ms=1,
            noindex=False,
            nofollow=False,
            links=links,
        ),
    )


async def complete(database: Database, url_id: uuid.UUID, result: UrlResult) -> None:
    """Process a queued URL as a worker would: claim, then record."""
    async with database.session() as session:
        lease = await claim_url(
            session, url_id, owner="crawler-test", lease_seconds=60, max_attempts=3
        )
    assert lease is not None
    async with database.session() as session:
        recorded = await record_result(session, lease, result, max_redirect_hops=5)
    assert recorded.status is RecordStatus.RECORDED


async def ids_by_url(database: Database, job_id: uuid.UUID) -> dict[str, uuid.UUID]:
    async with database.session() as session:
        rows = await session.execute(
            select(CrawlUrl.normalized_url, CrawlUrl.id).where(CrawlUrl.job_id == job_id)
        )
        return {url.removeprefix("https://example.test"): url_id for url, url_id in rows}


async def test_a_pass_starts_jobs_up_to_the_cap_and_publishes_their_seeds(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    jobs = [(await new_job(database, status=JobStatus.QUEUED))[0] for _ in range(3)]

    report = await scheduler(database, stream, max_running_jobs=2).run_pass()

    assert (report.activated, report.dispatched) == (2, 2)
    statuses = [(await job_row(database, job_id)).status for job_id in jobs]
    assert statuses == ["running", "running", "queued"]  # the oldest first
    assert {task.job_id for task in await published(redis_client, stream)} == set(jobs[:2])


async def test_dispatch_respects_the_per_job_window_and_the_global_limit(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    first, _ = await new_job(database, urls=9)
    second, _ = await new_job(database, urls=9)
    run = scheduler(database, stream, max_in_flight_per_job=4, max_outstanding=6)

    report = await run.run_pass()

    assert report.dispatched == 6  # the global limit
    tasks = await published(redis_client, stream)
    per_job = [sum(1 for task in tasks if task.job_id == job_id) for job_id in (first, second)]
    assert per_job == [3, 3]  # shared fairly
    again = await run.run_pass()
    assert again.dispatched == 0  # still 6 outstanding


async def test_the_per_job_window_counts_tasks_already_in_flight(
    database: Database, stream: CrawlStream
) -> None:
    job_id, _ = await new_job(database, urls=9)
    run = scheduler(database, stream, max_in_flight_per_job=4, dispatch_batch=3)

    assert (await run.run_pass()).dispatched == 3  # the batch size
    assert (await run.run_pass()).dispatched == 1  # the window: 4 in flight
    assert (await states(database, job_id)) == {"queued": 4, "pending": 6}


async def test_levels_advance_strictly_and_the_job_completes(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    """Level-synchronous BFS driven by the scheduler: the next level is admitted and
    published only after every URL of the current level is finished."""
    job_id, [seed] = await new_job(database, max_depth=2)
    run = scheduler(database, stream)

    assert (await run.run_pass()).dispatched == 1
    await complete(database, seed, page("/a", "/b"))
    report = await run.run_pass()
    assert (report.advanced, report.dispatched) == (1, 2)
    ids = await ids_by_url(database, job_id)
    await complete(database, ids["/a"], page("/c"))
    report = await run.run_pass()
    assert (report.advanced, report.dispatched) == (0, 0)  # /b is unfinished: level 1 goes on
    assert "/c" not in await ids_by_url(database, job_id)
    await complete(database, ids["/b"], page("/c", "/d"))
    report = await run.run_pass()
    assert (report.advanced, report.dispatched) == (1, 2)
    ids = await ids_by_url(database, job_id)
    for path in ("/c", "/d"):
        await complete(database, ids[path], page("/e"))  # depth 2 = max_depth: not admitted
    report = await run.run_pass()
    assert report.finished == 1
    job = await job_row(database, job_id)
    assert (job.status, job.current_level, job.admitted_count) == ("completed", 2, 5)
    url_ids = [task.url_id for task in await published(redis_client, stream)]
    assert sorted(url_ids) == sorted((await ids_by_url(database, job_id)).values())  # each once


async def test_concurrent_scheduler_passes_publish_each_url_once(
    test_database: Settings, database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    """F12: schedulers running at the same time, each with its own connections."""
    jobs = [(await new_job(database, status=JobStatus.QUEUED, urls=20))[0] for _ in range(3)]
    databases = [Database(test_database, application_name=f"scheduler-{n}") for n in range(3)]
    try:
        runs = [scheduler(db, stream, max_running_jobs=2) for db in databases]
        for _ in range(3):
            await asyncio.gather(*(run.run_pass() for run in runs))
    finally:
        for db in databases:
            await db.dispose()

    statuses = [(await job_row(database, job_id)).status for job_id in jobs]
    assert statuses.count("running") == 2  # the cap held across schedulers
    url_ids = [task.url_id for task in await published(redis_client, stream)]
    assert len(url_ids) == len(set(url_ids)) == 42  # 2 jobs x 21 URLs, each published once


async def test_the_pass_republishes_tasks_whose_message_was_lost(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    job_id, [seed] = await new_job(database)
    run = scheduler(database, stream, reconcile_after=60)
    await run.run_pass()
    await redis_client.delete(stream.key)  # Redis lost its data
    await age_dispatch(database, seed, seconds=120)

    fresh = scheduler(database, stream, reconcile_after=60)  # e.g. a restarted scheduler
    report = await fresh.run_pass()

    assert report.republished == 1
    [task] = await published(redis_client, stream)
    assert task == CrawlTask(job_id, seed)
    assert (await url_row(database, seed)).state == "queued"
    described = await stream.describe()
    assert described["group"] is not None  # the pass recreated the group too


async def test_maintenance_trims_the_stream_and_removes_idle_consumers(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    _job_id, _ = await new_job(database, urls=2)
    run = scheduler(database, stream)
    await run.run_pass()
    for delivery in await stream.read("crawler-gone", count=10, block_ms=100):
        await stream.ack(delivery.entry_id)
    await asyncio.sleep(0.05)

    maintained = scheduler(database, stream, retention_seconds=0, consumer_max_idle_seconds=0)
    await maintained.run_pass()

    assert await redis_client.xlen(stream.key) == 0  # delivered and acknowledged: trimmed
    assert (await stream.describe())["consumers"] == []


async def test_a_pass_with_redis_down_leaves_the_frontier_unchanged(
    test_database: Settings, database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    """Scenario E at the scheduler: publishing fails, so the dispatch rolls back; the URLs stay
    pending and are dispatched once Redis is back."""
    job_id, _url_ids = await new_job(database, urls=2)
    down_client = create_redis_client(
        test_database.model_copy(
            update={"redis_port": unused_port(), "redis_socket_timeout_seconds": 1.0}
        ),
        client_name="sitescope-tests-down",
    )
    try:
        down = CrawlStream(down_client, key_prefix=stream.key.removesuffix(":crawl"))
        # Refused at once on Linux; on Windows the SYN retries outlast the connect timeout.
        with pytest.raises(RedisError):
            await scheduler(database, down).run_pass()
    finally:
        await down_client.aclose()
    assert await states(database, job_id) == {"pending": 3}

    report = await scheduler(database, stream).run_pass()
    assert report.dispatched == 3


async def test_a_job_whose_step_fails_does_not_stop_the_others(
    database: Database, stream: CrawlStream
) -> None:
    """Regression (Phase 5 review): an error of one job aborted the whole pass, so a job that
    fails every time stopped the scheduling of all jobs. Here the older job has lost its seed
    row, so finishing it fails (NoResultFound); the younger job is dispatched anyway."""
    broken, [broken_seed] = await new_job(database)
    healthy, [healthy_seed] = await new_job(database)
    await execute(database, "DELETE FROM crawl_urls WHERE id = :id", id=broken_seed)

    report = await scheduler(database, stream).run_pass()

    assert (report.failed_jobs, report.dispatched) == (1, 1)
    assert (await url_row(database, healthy_seed)).state == "queued"
    assert (await job_row(database, broken)).status == "running"  # left as it was
    assert (await job_row(database, healthy)).status == "running"


async def test_every_running_job_is_scheduled_even_beyond_the_cap(
    database: Database, stream: CrawlStream
) -> None:
    """Regression (Phase 5 review): only the oldest max_running_jobs running jobs were looked
    at, so jobs started another way (the single-process driver), or before the cap was
    lowered, were starved. The cap limits activation only."""
    for _ in range(3):
        await new_job(database)  # already running

    report = await scheduler(database, stream, max_running_jobs=1).run_pass()

    assert (report.activated, report.dispatched) == (0, 3)


async def test_the_scheduler_loop_stops_on_request(database: Database, stream: CrawlStream) -> None:
    job_id, _ = await new_job(database, status=JobStatus.QUEUED)
    run = scheduler(database, stream)
    stop = asyncio.Event()
    loop = asyncio.create_task(run.run(stop))

    async def started() -> bool:
        return (await job_row(database, job_id)).status == "running"

    await wait_for(started, within=10, what="the job to start")
    stop.set()
    async with asyncio.timeout(5):
        await loop

    async with database.session() as session:
        queued = await session.scalar(
            select(func.count()).select_from(CrawlUrl).where(CrawlUrl.state == "queued")
        )
    assert queued == 1
