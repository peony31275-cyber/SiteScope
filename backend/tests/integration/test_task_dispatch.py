"""Publishing crawl tasks through the PostgreSQL outbox (app/services/crawl_state.py, Phase 5).

Real PostgreSQL and Redis. These tests pin down the failure windows of architecture section
10.4 at the service level: a publish that fails, a publish whose transaction fails (F2), a worker
that receives a message before the dispatch commits, lost messages (F7), and concurrent
schedulers (F12).
"""

import asyncio
import uuid
from collections.abc import Sequence

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.db.models import JobStatus, UrlState
from app.db.session import Database
from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream
from app.services.crawl_state import (
    Publisher,
    activate_jobs,
    claim_url,
    dispatch_urls,
    inspect_url,
    outstanding_tasks,
    republish_stale,
    running_frontiers,
)
from tests.crawl_support import age_dispatch, execute, new_job, stream_entries, url_row

pytestmark = pytest.mark.anyio

Tasks = Sequence[tuple[uuid.UUID, uuid.UUID]]


@pytest.fixture
async def stream(redis_client: Redis, redis_prefix: str) -> CrawlStream:
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    await stream.ensure_group()
    return stream


def publisher(stream: CrawlStream) -> Publisher:
    async def publish(tasks: Tasks) -> list[str]:
        return await stream.publish([CrawlTask(job_id, url_id) for job_id, url_id in tasks])

    return publish


async def stream_tasks(redis_client: Redis, stream: CrawlStream) -> list[tuple[str, CrawlTask]]:
    return [
        (entry, CrawlTask.decode(fields))
        for entry, fields in await stream_entries(redis_client, stream.key)
    ]


async def claim(database: Database, url_id: uuid.UUID, owner: str = "crawler-a") -> object:
    async with database.session() as session:
        return await claim_url(session, url_id, owner=owner, lease_seconds=60, max_attempts=3)


# --- T3 with publication -------------------------------------------------------------------------


async def test_dispatch_publishes_each_url_once_and_records_its_entry(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    job_id, url_ids = await new_job(database, urls=2)

    async with database.session() as session:
        dispatched = await dispatch_urls(session, job_id, limit=10, publish=publisher(stream))

    assert sorted(dispatched) == sorted(url_ids)
    published = await stream_tasks(redis_client, stream)
    assert sorted(task.url_id for _, task in published) == sorted(url_ids)
    assert {task.job_id for _, task in published} == {job_id}
    for entry, task in published:
        row = await url_row(database, task.url_id)
        assert (row.state, row.stream_entry_id) == (UrlState.QUEUED.value, entry)
        assert row.dispatched_at is not None


async def test_a_failed_publish_leaves_the_urls_pending(database: Database) -> None:
    job_id, url_ids = await new_job(database, urls=2)

    async def unreachable(tasks: Tasks) -> list[str]:
        raise RedisConnectionError("Redis is down")

    async with database.session() as session:
        with pytest.raises(RedisConnectionError):
            await dispatch_urls(session, job_id, limit=10, publish=unreachable)

    for url_id in url_ids:
        row = await url_row(database, url_id)
        assert (row.state, row.stream_entry_id, row.dispatched_at) == ("pending", None, None)


async def test_a_publish_whose_transaction_fails_is_published_again(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    """F2: XADD succeeded, the commit did not. The rows stay pending and the next dispatch
    publishes them again: a duplicate message, never a lost task."""
    job_id, [seed] = await new_job(database)
    publish = publisher(stream)

    async def publish_then_fail(tasks: Tasks) -> list[str]:
        await publish(tasks)
        raise RuntimeError("the transaction fails after XADD")

    async with database.session() as session:
        with pytest.raises(RuntimeError):
            await dispatch_urls(session, job_id, limit=10, publish=publish_then_fail)
    assert (await url_row(database, seed)).state == "pending"

    async with database.session() as session:
        assert await dispatch_urls(session, job_id, limit=10, publish=publish) == [seed]

    published = await stream_tasks(redis_client, stream)
    assert [task.url_id for _, task in published] == [seed, seed]  # the orphan, then the real one
    assert (await url_row(database, seed)).stream_entry_id == published[1][0]


async def test_a_claim_waits_for_the_dispatch_that_published_its_message(
    database: Database, stream: CrawlStream
) -> None:
    """A worker can read a message before the dispatch commits (XADD comes first). Its claim
    waits for the row lock, then sees the committed 'queued' state and succeeds."""
    job_id, [seed] = await new_job(database)
    published, release = asyncio.Event(), asyncio.Event()
    publish = publisher(stream)

    async def slow_commit(tasks: Tasks) -> list[str]:
        entries = await publish(tasks)
        published.set()
        await release.wait()  # the dispatch transaction stays open, its row lock held
        return entries

    async def dispatch() -> list[uuid.UUID]:
        async with database.session() as session:
            return await dispatch_urls(session, job_id, limit=10, publish=slow_commit)

    dispatching = asyncio.create_task(dispatch())
    await published.wait()
    claiming = asyncio.create_task(claim(database, seed))
    await asyncio.sleep(0.3)
    assert not claiming.done()  # blocked on the row lock, not refused

    release.set()
    assert await dispatching == [seed]
    lease = await claiming
    assert lease is not None
    assert (await url_row(database, seed)).state == UrlState.IN_PROGRESS.value


async def test_a_claim_waiting_for_a_dispatch_that_rolls_back_is_refused(
    database: Database, stream: CrawlStream
) -> None:
    """The orphaned message then finds the URL pending, which tells the worker to ack it."""
    job_id, [seed] = await new_job(database)
    published, release = asyncio.Event(), asyncio.Event()
    publish = publisher(stream)

    async def fail_after_waiting(tasks: Tasks) -> list[str]:
        await publish(tasks)
        published.set()
        await release.wait()
        raise RuntimeError("the dispatch rolls back")

    async def dispatch() -> None:
        async with database.session() as session:
            await dispatch_urls(session, job_id, limit=10, publish=fail_after_waiting)

    dispatching = asyncio.create_task(dispatch())
    await published.wait()
    claiming = asyncio.create_task(claim(database, seed))
    await asyncio.sleep(0.3)
    assert not claiming.done()

    release.set()
    with pytest.raises(RuntimeError):
        await dispatching
    assert await claiming is None
    async with database.session() as session:
        status = await inspect_url(session, seed)
    assert status is not None
    assert status.state is UrlState.PENDING


async def test_concurrent_dispatchers_publish_each_url_once(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    """F12: two schedulers dispatching the same job at once take disjoint rows."""
    job_id, url_ids = await new_job(database, urls=29)
    publish = publisher(stream)

    async def slow_publish(tasks: Tasks) -> list[str]:
        await asyncio.sleep(0.05)  # hold the row locks while the other one runs
        return await publish(tasks)

    async def dispatch() -> list[uuid.UUID]:
        async with database.session() as session:
            return await dispatch_urls(session, job_id, limit=10, publish=slow_publish)

    results = await asyncio.gather(*(dispatch() for _ in range(4)))

    dispatched = [url_id for result in results for url_id in result]
    assert len(dispatched) == len(set(dispatched)) == len(url_ids)
    published = [task.url_id for _, task in await stream_tasks(redis_client, stream)]
    assert sorted(published) == sorted(url_ids)


@pytest.mark.parametrize("with_publish", [False, True])
async def test_dispatch_never_exceeds_its_limit(
    database: Database, stream: CrawlStream, with_publish: bool
) -> None:
    """Regression (Phase 5): ``UPDATE ... WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP
    LOCKED)`` can update more than n rows, because PostgreSQL may rescan the subquery for each
    outer row, and a rescan skips the rows the statement already updated. The selection is
    now materialized once."""
    job_id, url_ids = await new_job(database, urls=7)

    async with database.session() as session:
        dispatched = await dispatch_urls(
            session, job_id, limit=3, publish=publisher(stream) if with_publish else None
        )

    assert len(dispatched) == 3
    queued = [url_id for url_id in url_ids if (await url_row(database, url_id)).state == "queued"]
    assert sorted(queued) == sorted(dispatched)


# --- Reconciliation ------------------------------------------------------------------------------


async def test_reconciliation_republishes_only_tasks_whose_entry_is_gone(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    """F7: a stale task whose message still waits in the stream (undelivered, or pending for
    XAUTOCLAIM) is left alone; one whose entry is gone is published again."""
    job_id, [kept, lost, finished_entry] = await new_job(database, urls=2)
    publish = publisher(stream)
    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=10, publish=publish)
    rows = {url_id: await url_row(database, url_id) for url_id in (kept, lost, finished_entry)}
    lost_entry = rows[lost].stream_entry_id
    assert lost_entry is not None
    await redis_client.xdel(stream.key, lost_entry)  # lost with Redis data
    # Acknowledged although the URL is still queued (only a bug or an operator could do this).
    deliveries = await stream.read("crawler-a", count=10, block_ms=100)
    for delivery in deliveries:
        if delivery.entry_id == rows[finished_entry].stream_entry_id:
            await stream.ack(delivery.entry_id)
    for url_id in rows:
        await age_dispatch(database, url_id, seconds=1000)

    async with database.session() as session:
        result = await republish_stale(
            session,
            older_than=600,
            limit=10,
            publish=publish,
            delivering=stream.delivering,
        )

    assert (result.examined, result.republished, result.resume_after) == (3, 2, None)
    assert (await url_row(database, kept)).stream_entry_id == rows[kept].stream_entry_id
    for url_id in (lost, finished_entry):
        row, before = await url_row(database, url_id), rows[url_id]
        assert row.state == UrlState.QUEUED.value
        assert row.stream_entry_id != before.stream_entry_id
        assert row.dispatched_at is not None
        assert before.dispatched_at is not None
        assert row.dispatched_at > before.dispatched_at


async def test_reconciliation_waits_until_a_task_is_old_enough(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    job_id, [_seed] = await new_job(database)
    publish = publisher(stream)
    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=10, publish=publish)
    await redis_client.delete(stream.key)  # everything lost, but the task is recent

    async with database.session() as session:
        result = await republish_stale(
            session, older_than=600, limit=10, publish=publish, delivering=stream.delivering
        )

    assert (result.examined, result.republished) == (0, 0)


async def test_reconciliation_covers_expired_leases_but_never_live_ones(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    job_id, [expired, live] = await new_job(database, urls=1)
    publish = publisher(stream)
    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=10, publish=publish)
    for url_id in (expired, live):
        assert await claim(database, url_id) is not None
    await redis_client.delete(stream.key)  # the messages are gone
    await age_dispatch(database, expired, seconds=1000)  # its lease expired long ago
    await execute(
        database,
        "UPDATE crawl_urls SET dispatched_at = clock_timestamp() - interval '1000 seconds'"
        " WHERE id = :id",
        id=live,
    )  # dispatched long ago, but its lease is live: it is making progress

    async with database.session() as session:
        result = await republish_stale(
            session, older_than=600, limit=10, publish=publish, delivering=stream.delivering
        )

    assert (result.examined, result.republished) == (1, 1)
    row = await url_row(database, expired)
    assert row.state == UrlState.IN_PROGRESS.value  # a worker takes it over on delivery
    assert row.stream_entry_id is not None


async def test_reconciliation_ignores_jobs_that_are_not_running(
    database: Database, stream: CrawlStream
) -> None:
    job_id, [seed] = await new_job(database)
    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=10, publish=publisher(stream))
    await age_dispatch(database, seed, seconds=1000)
    await execute(
        database,
        "UPDATE crawl_jobs SET status = 'cancelling', cancel_requested_at = clock_timestamp()"
        " WHERE id = :id",
        id=job_id,
    )

    async with database.session() as session:
        result = await republish_stale(
            session,
            older_than=600,
            limit=10,
            publish=publisher(stream),
            delivering=stream.delivering,
        )

    assert result.examined == 0


async def test_reconciliation_pages_through_every_stale_task(
    database: Database, stream: CrawlStream
) -> None:
    """With many stale tasks whose messages are fine, successive calls still reach them all."""
    job_id, url_ids = await new_job(database, urls=4)
    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=10, publish=publisher(stream))
    for index, url_id in enumerate(url_ids):
        await age_dispatch(database, url_id, seconds=1000 + index)

    seen: list[int] = []
    cursor = None
    for _ in range(4):
        async with database.session() as session:
            result = await republish_stale(
                session,
                older_than=600,
                limit=2,
                publish=publisher(stream),
                delivering=stream.delivering,
                after=cursor,
            )
        seen.append(result.examined)
        cursor = result.resume_after
        if cursor is None:
            break

    assert seen == [2, 2, 1]  # 5 tasks, then the scan starts over


async def test_concurrent_reconcilers_never_republish_a_task_twice(
    database: Database, stream: CrawlStream, redis_client: Redis
) -> None:
    job_id, url_ids = await new_job(database, urls=9)
    publish = publisher(stream)
    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=100, publish=publish)
    await redis_client.delete(stream.key)
    await stream.ensure_group()
    for url_id in url_ids:
        await age_dispatch(database, url_id, seconds=1000)

    async def slow_publish(tasks: Tasks) -> list[str]:
        await asyncio.sleep(0.05)
        return await publish(tasks)

    async def reconcile() -> int:
        async with database.session() as session:
            result = await republish_stale(
                session,
                older_than=600,
                limit=4,
                publish=slow_publish,
                delivering=stream.delivering,
            )
            return result.republished

    counts = await asyncio.gather(*(reconcile() for _ in range(3)))

    assert sum(counts) == len(url_ids)
    entries = await stream_entries(redis_client, stream.key)
    assert len(entries) == len(url_ids)  # one new message per task, no duplicates


# --- T2 for the scheduler, frontier counts, inspection --------------------------------------------


async def test_concurrent_activations_never_exceed_the_running_jobs_cap(
    database: Database,
) -> None:
    for _ in range(6):
        await new_job(database, status=JobStatus.QUEUED)
    await new_job(database, status=JobStatus.CANCELLING)  # counts toward the cap

    async def activate() -> list[uuid.UUID]:
        async with database.session() as session:
            return await activate_jobs(session, max_running=3, limit=10)

    activated = await asyncio.gather(*(activate() for _ in range(4)))

    assert sum(len(batch) for batch in activated) == 2
    async with database.session() as session:
        running = await session.scalar(
            text("SELECT count(*) FROM crawl_jobs WHERE status IN ('running', 'cancelling')")
        )
    assert running == 3


async def test_activation_starts_the_oldest_queued_jobs_first(database: Database) -> None:
    first, _ = await new_job(database, status=JobStatus.QUEUED)
    second, _ = await new_job(database, status=JobStatus.QUEUED)
    await new_job(database, status=JobStatus.QUEUED)

    async with database.session() as session:
        activated = await activate_jobs(session, max_running=2, limit=10)

    assert activated == [first, second]


async def test_frontier_counts(database: Database, stream: CrawlStream) -> None:
    job_id, _url_ids = await new_job(database, urls=3)
    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=2, publish=publisher(stream))
    assert await claim(database, (await _queued(database, job_id))[0]) is not None

    async with database.session() as session:
        [frontier] = await running_frontiers(session, limit=10)
    async with database.session() as session:
        outstanding = await outstanding_tasks(session)

    assert (frontier.job_id, frontier.unfinished, frontier.in_flight) == (job_id, 4, 2)
    assert outstanding == 2


async def _queued(database: Database, job_id: uuid.UUID) -> list[uuid.UUID]:
    async with database.session() as session:
        rows = await session.execute(
            text("SELECT id FROM crawl_urls WHERE job_id = :id AND state = 'queued' ORDER BY id"),
            {"id": job_id},
        )
        return [row.id for row in rows]


async def test_inspect_url_reports_the_authoritative_state(
    database: Database, stream: CrawlStream
) -> None:
    job_id, [seed] = await new_job(database)
    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=1, publish=publisher(stream))
    assert await claim(database, seed, owner="crawler-x") is not None

    async with database.session() as session:
        live = await inspect_url(session, seed)
        missing = await inspect_url(session, uuid.uuid4())
    await age_dispatch(database, seed, seconds=5)
    async with database.session() as session:
        expired = await inspect_url(session, seed)

    assert missing is None
    assert live is not None
    assert expired is not None
    assert (live.job_id, live.job_status) == (job_id, JobStatus.RUNNING)
    assert live.state is UrlState.IN_PROGRESS
    assert (live.lease_owner, live.lease_live, live.attempt_count) == ("crawler-x", True, 1)
    assert expired.lease_live is False


async def test_a_stalled_scheduler_session_releases_its_row_locks(
    test_database: Settings, database: Database
) -> None:
    """The scheduler's sessions end themselves when idle inside a transaction (here after
    1 s), so a scheduler stalled between XADD and COMMIT cannot block workers' claims."""
    job_id, [seed] = await new_job(database)
    stalled = Database(
        test_database,
        application_name="sitescope-tests-stalled",
        server_settings={"idle_in_transaction_session_timeout": "1000"},
    )
    try:
        async with stalled.session() as session:
            await session.begin()
            await session.execute(
                text("SELECT 1 FROM crawl_urls WHERE id = :id FOR UPDATE"), {"id": seed}
            )
            await asyncio.sleep(2.5)  # the "stall": idle in the transaction
            async with database.session() as other, other.begin():
                await other.execute(  # NOWAIT: fails at once if the lock were still held
                    text("SELECT 1 FROM crawl_urls WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": seed},
                )
            with pytest.raises(DBAPIError):
                await session.execute(text("SELECT 1"))
    finally:
        await stalled.dispose()
    assert (await url_row(database, seed)).job_id == job_id
