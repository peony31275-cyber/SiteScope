"""The crawl task stream adapter against real Redis (app/queue/streams.py).

Every test uses its own key prefix in Redis database 15 (``redis_prefix``).
"""

import asyncio
import logging
import uuid

import pytest
from redis.asyncio import Redis

from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream
from tests.crawl_support import stream_entries

pytestmark = pytest.mark.anyio


def tasks(count: int) -> list[CrawlTask]:
    return [CrawlTask(uuid.uuid4(), uuid.uuid4()) for _ in range(count)]


@pytest.fixture
async def stream(redis_client: Redis, redis_prefix: str) -> CrawlStream:
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    await stream.ensure_group()
    return stream


async def test_ensure_group_is_idempotent(stream: CrawlStream) -> None:
    assert await stream.ensure_group() is False  # the fixture created it
    assert await stream.ensure_group() is False

    described = await stream.describe()
    assert described["group"]["name"] == "crawlers"


async def test_ensure_group_reports_a_group_it_had_to_create_again(
    stream: CrawlStream, redis_client: Redis
) -> None:
    """After Redis lost the stream (the group with it), the next call creates both again."""
    await redis_client.delete(stream.key)

    assert await stream.ensure_group() is True
    assert await stream.ensure_group() is False


async def test_reads_and_reclaims_recreate_a_lost_group(
    stream: CrawlStream, redis_client: Redis, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="app.queue.streams")
    await redis_client.delete(stream.key)
    assert await stream.read("worker-a", count=1, block_ms=10) == []
    await redis_client.delete(stream.key)
    reclaimed = await stream.reclaim("worker-a", min_idle_ms=0, start="0-0", count=1)

    assert (reclaimed.next_start, reclaimed.deliveries) == ("0-0", [])
    assert [getattr(r, "event", None) for r in caplog.records] == ["stream.group_recreated"] * 2
    assert await stream.ensure_group() is False


async def test_published_tasks_are_delivered_once_to_one_consumer(stream: CrawlStream) -> None:
    published = tasks(3)
    ids = await stream.publish(published)

    first = await stream.read("worker-a", count=10, block_ms=100)
    second = await stream.read("worker-b", count=10, block_ms=100)

    assert [delivery.entry_id for delivery in first] == ids
    assert [delivery.task for delivery in first] == published
    assert second == []  # each message goes to one consumer of the group


async def test_a_read_waits_for_the_block_time_and_returns_nothing(stream: CrawlStream) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()

    assert await stream.read("worker-a", count=1, block_ms=200) == []
    assert loop.time() - started >= 0.15


async def test_a_message_stays_pending_until_acknowledged(stream: CrawlStream) -> None:
    [entry] = await stream.publish(tasks(1))
    await stream.read("worker-a", count=1, block_ms=100)

    pending = await stream.pending()
    assert (pending.count, pending.oldest, pending.per_consumer) == (1, entry, {"worker-a": 1})

    assert await stream.ack(entry) is True
    assert (await stream.pending()).count == 0
    assert await stream.ack(entry) is False  # a second XACK is a no-op


async def test_an_idle_pending_message_is_reclaimed_only_after_the_minimum_idle_time(
    stream: CrawlStream,
) -> None:
    """XAUTOCLAIM: the takeover path for messages of crashed or stalled workers."""
    [entry] = await stream.publish(tasks(1))
    await stream.read("worker-a", count=1, block_ms=100)

    early = await stream.reclaim("worker-b", min_idle_ms=60_000, start="0-0", count=10)
    assert early.deliveries == []

    await asyncio.sleep(0.2)
    late = await stream.reclaim("worker-b", min_idle_ms=100, start="0-0", count=10)
    assert [delivery.entry_id for delivery in late.deliveries] == [entry]
    assert late.next_start == "0-0"  # the whole pending list was scanned
    assert (await stream.pending()).per_consumer == {"worker-b": 1}


async def test_reclaim_reports_pending_entries_that_no_longer_exist(
    stream: CrawlStream, redis_client: Redis
) -> None:
    [entry] = await stream.publish(tasks(1))
    await stream.read("worker-a", count=1, block_ms=100)
    await redis_client.xdel(stream.key, entry)  # lost from the stream, still pending
    await asyncio.sleep(0.05)

    reclaimed = await stream.reclaim("worker-b", min_idle_ms=10, start="0-0", count=10)

    assert reclaimed.deliveries == []
    assert reclaimed.deleted == [entry]
    assert (await stream.pending()).count == 0


async def test_a_malformed_entry_is_copied_to_the_dead_letter_stream_and_acknowledged(
    stream: CrawlStream, redis_client: Redis
) -> None:
    entry = await redis_client.xadd(stream.key, {"v": "9", "job_id": "x", "url_id": "y"})
    [delivery] = await stream.read("worker-a", count=1, block_ms=100)
    assert delivery.task is None
    assert delivery.error

    await stream.dead_letter(delivery, delivery.error)

    assert (await stream.pending()).count == 0
    [(_, fields)] = await stream_entries(redis_client, stream.dead_letter_key)
    assert fields["original_id"] == entry
    assert fields["original.v"] == "9"
    assert fields["reason"] == delivery.error


async def test_an_entry_that_is_not_utf8_does_not_fail_its_batch(
    stream: CrawlStream, redis_client: Redis
) -> None:
    """Regression (Phase 5 review): decoded strictly, one entry with bytes that are not UTF-8
    failed the whole XREADGROUP reply, after Redis had made every entry of the batch pending;
    the valid tasks were never handed out. Now the entry decodes leniently and is malformed."""
    [first] = await stream.publish(tasks(1))
    poison = await redis_client.xadd(
        stream.key, {b"v\xff": b"1", "job_id": b"\xff\xfe\x00", "url_id": b"\x80abc"}
    )
    [last] = await stream.publish(tasks(1))

    deliveries = await stream.read("worker-a", count=10, block_ms=100)

    assert [delivery.entry_id for delivery in deliveries] == [first, poison, last]
    assert [delivery.task is not None for delivery in deliveries] == [True, False, True]
    assert deliveries[1].error


async def test_reclaiming_a_batch_with_an_entry_that_is_not_utf8(
    stream: CrawlStream, redis_client: Redis
) -> None:
    await stream.publish(tasks(1))
    poison = await redis_client.xadd(stream.key, {"v": b"\xc3\x28", "job_id": b"\xa0\xa1"})
    await stream.publish(tasks(1))
    await stream.read("worker-a", count=10, block_ms=100)
    await asyncio.sleep(0.05)

    reclaimed = await stream.reclaim("worker-b", min_idle_ms=10, start="0-0", count=10)

    assert len(reclaimed.deliveries) == 3
    [malformed] = [delivery for delivery in reclaimed.deliveries if delivery.task is None]
    assert malformed.entry_id == poison


async def test_a_lost_group_is_created_again_by_the_next_read(
    stream: CrawlStream, redis_client: Redis
) -> None:
    """After Redis loses its data, workers recreate the group instead of failing forever."""
    await redis_client.delete(stream.key)

    assert await stream.read("worker-a", count=1, block_ms=50) == []
    published = await stream.publish(tasks(1))
    [delivery] = await stream.read("worker-a", count=1, block_ms=100)
    assert delivery.entry_id == published[0]


async def entries(redis_client: Redis, stream: CrawlStream) -> list[str]:
    return [entry for entry, _ in await stream_entries(redis_client, stream.key)]


async def test_trimming_keeps_pending_and_undelivered_entries(
    stream: CrawlStream, redis_client: Redis
) -> None:
    acked, pending, undelivered = await stream.publish(tasks(3))
    deliveries = await stream.read("worker-a", count=2, block_ms=100)
    assert [item.entry_id for item in deliveries] == [acked, pending]
    await stream.ack(acked)
    await asyncio.sleep(0.01)  # a retention of 0 ms then makes every entry old

    removed = await stream.trim(retention_ms=0, exact=True)

    assert removed == 1
    assert await entries(redis_client, stream) == [pending, undelivered]


async def test_trimming_keeps_undelivered_entries_after_acknowledged_ones(
    stream: CrawlStream, redis_client: Redis
) -> None:
    _first, second, third = await stream.publish(tasks(3))
    [delivery] = await stream.read("worker-a", count=1, block_ms=100)
    await stream.ack(delivery.entry_id)
    await asyncio.sleep(0.01)

    await stream.trim(retention_ms=0, exact=True)

    assert await entries(redis_client, stream) == [second, third]  # never read: kept


async def test_trimming_removes_old_acknowledged_entries_only(
    stream: CrawlStream, redis_client: Redis
) -> None:
    ids = await stream.publish(tasks(5))
    for delivery in await stream.read("worker-a", count=5, block_ms=100):
        await stream.ack(delivery.entry_id)

    assert await stream.trim(retention_ms=60_000, exact=True) == 0  # too recent
    await asyncio.sleep(0.01)
    assert await stream.trim(retention_ms=0, exact=True) == len(ids)
    assert await entries(redis_client, stream) == []
    assert await stream.trim(retention_ms=0) == 0  # approximate mode: nothing left, no error


async def test_idle_consumers_are_removed_only_when_nothing_is_pending(
    stream: CrawlStream,
) -> None:
    """One Lua script checks and deletes atomically (Phase 5 review): a consumer that reads
    between a separate check and the deletion would lose its new pending entries."""
    await stream.publish(tasks(1))
    await stream.read("worker-busy", count=1, block_ms=100)  # holds a pending message
    await stream.read("worker-idle", count=1, block_ms=10)  # registered, nothing pending
    await stream.read("worker-recent", count=1, block_ms=10)
    await asyncio.sleep(0.2)
    await stream.read("worker-recent", count=1, block_ms=10)  # active again just now

    removed = await stream.remove_idle_consumers(max_idle_ms=150)

    assert removed == ["worker-idle"]
    assert (await stream.pending()).per_consumer == {"worker-busy": 1}
    names = {consumer["name"] for consumer in (await stream.describe())["consumers"]}
    assert names == {"worker-busy", "worker-recent"}


async def test_removing_idle_consumers_without_a_stream_or_group(
    redis_client: Redis, redis_prefix: str
) -> None:
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    assert await stream.remove_idle_consumers(max_idle_ms=0) == []  # no stream

    await stream.publish(tasks(1))  # a stream without the group
    assert await stream.remove_idle_consumers(max_idle_ms=0) == []


async def test_delivering_tells_owed_entries_from_finished_or_lost_ones(
    stream: CrawlStream, redis_client: Redis
) -> None:
    """The reconciler republishes a task only when its entry is no longer owed to a consumer."""
    acked, pending, undelivered, deleted = await stream.publish(tasks(4))
    deliveries = await stream.read("worker-a", count=2, block_ms=100)
    assert [item.entry_id for item in deliveries] == [acked, pending]
    await stream.ack(acked)
    await redis_client.xdel(stream.key, deleted)

    assert await stream.delivering([acked, pending, undelivered, deleted, None, "1-1"]) == [
        False,  # acknowledged: nobody will deliver it again
        True,  # pending: XAUTOCLAIM hands it over
        True,  # not read yet: a worker will read it
        False,  # gone from the stream before delivery
        False,  # never published
        False,  # unknown
    ]


async def test_delivering_counts_undelivered_entries_as_owed(stream: CrawlStream) -> None:
    [entry] = await stream.publish(tasks(1))

    assert await stream.delivering([entry]) == [True]


async def test_delivering_without_the_stream_or_the_group(
    redis_client: Redis, redis_prefix: str
) -> None:
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    assert await stream.delivering(["1-0"]) == [False]  # no stream

    [entry] = await stream.publish(tasks(1))  # a stream without the group
    assert await stream.delivering([entry]) == [False]
