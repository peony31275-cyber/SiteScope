"""The politeness slot store against real Redis (the Lua scripts of app/crawler/politeness.py).

Every test uses its own key prefix in Redis database 15. Grants happen at the moment of the
call (by Redis time), so spacing tests wait for real, with short intervals; the "last start"
key, in microseconds of Redis time, shows the exact spacing.
"""

import asyncio
import itertools

import pytest
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.redis import create_redis_client
from app.crawler.errors import RateLimitedError
from app.crawler.politeness import Grant, PolitenessConfig, PolitenessGate, RedisSlotStore
from app.crawler.urls import normalize_url

pytestmark = pytest.mark.anyio

HOST = "a.test"
URL = normalize_url(f"https://{HOST}/")


async def attempt(
    store: RedisSlotStore,
    token: str,
    *,
    interval: float = 0.0,
    max_concurrent: int = 1,
    slot_ttl: float = 70.0,
    waiter_ttl: float = 15.0,
    host: str = HOST,
) -> Grant:
    return await store.acquire(
        host,
        token,
        interval=interval,
        max_concurrent=max_concurrent,
        slot_ttl=slot_ttl,
        waiter_ttl=waiter_ttl,
    )


async def acquire_waiting(store: RedisSlotStore, token: str, *, interval: float) -> None:
    """Attempt until granted, sleeping as the store says (a minimal gate)."""
    for _ in range(200):
        grant = await attempt(store, token, interval=interval)
        if grant.granted:
            return
        await asyncio.sleep(grant.wait if grant.wait is not None else 0.01)
    raise AssertionError("never granted")


async def last_start(redis: Redis, store: RedisSlotStore) -> int:
    value = await redis.get(store.keys(HOST)[0])
    assert value is not None
    return int(value)


async def test_starts_are_at_least_one_interval_apart(
    redis_client: Redis, redis_prefix: str
) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    marks = []
    for n in range(4):
        await acquire_waiting(store, f"t{n}", interval=0.1)
        marks.append(await last_start(redis_client, store))
        await store.release(HOST, f"t{n}")
    gaps = [later - earlier for earlier, later in itertools.pairwise(marks)]
    assert all(100_000 <= gap < 200_000 for gap in gaps), gaps


async def test_the_next_in_line_learns_the_remaining_spacing(
    redis_client: Redis, redis_prefix: str
) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    assert (await attempt(store, "t1", interval=10.0)).granted
    await store.release(HOST, "t1")
    grant = await attempt(store, "t2", interval=10.0)
    assert not grant.granted
    assert grant.wait is not None
    assert 9.0 < grant.wait <= 10.0


async def test_the_concurrency_cap_and_release(redis_client: Redis, redis_prefix: str) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    assert (await attempt(store, "t1", max_concurrent=2)).granted
    assert (await attempt(store, "t2", max_concurrent=2)).granted
    assert await attempt(store, "t3", max_concurrent=2) == Grant(granted=False, wait=None)
    await store.release(HOST, "t1")
    assert (await attempt(store, "t3", max_concurrent=2)).granted
    assert await redis_client.zcard(store.keys(HOST)[1]) == 2


async def test_waiters_are_served_in_arrival_order_across_clients(
    service_settings: Settings, redis_client: Redis, redis_prefix: str
) -> None:
    other_process = create_redis_client(service_settings, client_name="sitescope-tests-other")
    try:
        first = RedisSlotStore(redis_client, key_prefix=redis_prefix)
        second = RedisSlotStore(other_process, key_prefix=redis_prefix)
        assert (await attempt(first, "holder")).granted
        assert not (await attempt(second, "early")).granted  # queued first, other process
        assert not (await attempt(first, "late")).granted
        await first.release(HOST, "holder")
        # The holder's process asks again at once: it queues behind both waiters.
        assert not (await attempt(first, "holder-again")).granted
        assert not (await attempt(first, "late")).granted  # "early" is still ahead
        assert (await attempt(second, "early")).granted
        await second.release(HOST, "early")
        assert (await attempt(first, "late")).granted
        queue = await redis_client.zrange(first.keys(HOST)[3], 0, -1)
        assert queue == ["holder-again"]
    finally:
        await other_process.aclose()


async def test_hosts_have_independent_queues(redis_client: Redis, redis_prefix: str) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    assert (await attempt(store, "a", interval=30.0)).granted
    assert (await attempt(store, "b", interval=30.0, host="b.test")).granted


async def test_an_abandoned_slot_expires(redis_client: Redis, redis_prefix: str) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    active = store.keys(HOST)[1]
    # A slot left behind by a crashed process: its expiry is already in the past.
    await redis_client.zadd(active, {"crashed": 1})
    assert (await attempt(store, "fresh")).granted
    assert await redis_client.zrange(active, 0, -1) == ["fresh"]


async def test_a_crashed_waiter_leaves_the_queue(redis_client: Redis, redis_prefix: str) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    _, _, _, queue, seen = store.keys(HOST)
    # A waiter that queued first and then went silent (its process crashed long ago).
    await redis_client.zadd(queue, {"crashed": 1})
    await redis_client.zadd(seen, {"crashed": 1})
    assert (await attempt(store, "fresh", waiter_ttl=15.0)).granted
    assert await redis_client.zcard(queue) == 0
    assert await redis_client.zcard(seen) == 0


async def test_release_gives_back_the_slot_and_the_queue_place(
    redis_client: Redis, redis_prefix: str
) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    assert (await attempt(store, "holder")).granted
    assert not (await attempt(store, "waiter")).granted
    await store.release(HOST, "waiter")  # gives up or is cancelled
    await store.release(HOST, "holder")
    _, active, _, queue, seen = store.keys(HOST)
    for key in (active, queue, seen):
        assert await redis_client.zcard(key) == 0


async def test_the_crawl_delay_keeps_the_largest_value(
    redis_client: Redis, redis_prefix: str
) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    delay_key = store.keys(HOST)[2]
    await store.raise_crawl_delay(HOST, 3.0, ttl=60)
    assert await redis_client.get(delay_key) == "3000000"
    assert 0 < await redis_client.pttl(delay_key) <= 60_000
    await store.raise_crawl_delay(HOST, 1.0, ttl=60)  # another origin of the host
    assert await redis_client.get(delay_key) == "3000000"
    await store.raise_crawl_delay(HOST, 5.0, ttl=60)
    assert await redis_client.get(delay_key) == "5000000"
    assert (await attempt(store, "t1", interval=1.0)).granted
    await store.release(HOST, "t1")
    grant = await attempt(store, "t2", interval=1.0)
    assert grant.wait is not None
    assert 4.0 < grant.wait <= 5.0  # the delay, not the interval


async def test_every_key_expires(redis_client: Redis, redis_prefix: str) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    assert (await attempt(store, "t", interval=2.0, slot_ttl=5.0, waiter_ttl=8.0)).granted
    assert not (await attempt(store, "w", interval=2.0, slot_ttl=5.0, waiter_ttl=8.0)).granted
    last, active, _, queue, seen = store.keys(HOST)
    assert 0 < await redis_client.pttl(last) <= 3_000
    assert 0 < await redis_client.pttl(active) <= 6_000
    assert 0 < await redis_client.pttl(queue) <= 9_000
    assert 0 < await redis_client.pttl(seen) <= 9_000


async def test_a_held_slot_is_renewed_and_a_released_one_is_not(
    redis_client: Redis, redis_prefix: str
) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    active = store.keys(HOST)[1]
    assert (await attempt(store, "t", slot_ttl=2.0)).granted
    first = await redis_client.zscore(active, "t")
    assert first is not None
    assert await store.renew(HOST, "t", lease=30.0)
    renewed = await redis_client.zscore(active, "t")
    assert renewed is not None
    assert renewed - first >= 27_000_000  # now about 30 s from now, instead of 2 s
    assert 28_000 < await redis_client.pttl(active) <= 31_000  # the key outlives the slot
    await store.release(HOST, "t")
    assert not await store.renew(HOST, "t", lease=30.0)
    assert await redis_client.zscore(active, "t") is None  # a renewal never adds a slot back


async def test_an_expired_slot_cannot_be_renewed(redis_client: Redis, redis_prefix: str) -> None:
    """Once a slot has expired, another request may have taken its place: renewing it then
    would exceed the concurrency cap."""
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    active = store.keys(HOST)[1]
    assert (await attempt(store, "t", slot_ttl=0.05)).granted
    await asyncio.sleep(0.15)
    assert not await store.renew(HOST, "t", lease=30.0)
    assert await redis_client.zscore(active, "t") is None
    assert (await attempt(store, "u")).granted


async def test_a_live_request_keeps_its_slot_for_longer_than_one_lease(
    redis_client: Redis, redis_prefix: str
) -> None:
    config = PolitenessConfig(
        interval=0.0, max_wait=0.5, slot_ttl=30.0, slot_lease=0.6, poll_interval=0.02
    )
    holder = PolitenessGate(RedisSlotStore(redis_client, key_prefix=redis_prefix), config)
    other = PolitenessGate(RedisSlotStore(redis_client, key_prefix=redis_prefix), config)
    async with holder.slot(URL):
        await asyncio.sleep(2.0)  # more than three leases: renewed every 0.2 s
        with pytest.raises(RateLimitedError):
            async with other.slot(URL):
                pass
    async with other.slot(URL):  # released at once when the request ends
        pass


async def test_a_backward_clock_step_costs_one_interval_at_most(
    redis_client: Redis, redis_prefix: str
) -> None:
    """Redis TIME stepped back about 1 s during a Phase 4 run (WSL 2 clock resync). Simulated by
    a last start 10 s in the future: the spacing counts from now instead."""
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    seconds, microseconds = await redis_client.time()
    future = (seconds + 10) * 1_000_000 + microseconds
    await redis_client.set(store.keys(HOST)[0], future, px=60_000)
    grant = await attempt(store, "t", interval=0.2)
    assert not grant.granted
    assert grant.wait is not None
    assert 0.1 < grant.wait <= 0.2  # one interval, not about 10 s
    await asyncio.sleep(grant.wait)
    assert (await attempt(store, "t", interval=0.2)).granted  # and not one interval forever


async def test_the_queue_keeps_arrival_order_across_a_backward_clock_step(
    redis_client: Redis, redis_prefix: str
) -> None:
    store = RedisSlotStore(redis_client, key_prefix=redis_prefix)
    _, _, _, queue, seen = store.keys(HOST)
    seconds, microseconds = await redis_client.time()
    now = seconds * 1_000_000 + microseconds
    # A waiter that arrived "10 s from now": the clock has stepped back since.
    await redis_client.zadd(queue, {"early": now + 10_000_000})
    await redis_client.zadd(seen, {"early": now})
    assert await attempt(store, "late") == Grant(granted=False, wait=None)  # behind "early"
    assert (await attempt(store, "early")).granted


async def test_busy_workers_share_a_host_fairly(redis_client: Redis, redis_prefix: str) -> None:
    """Regression (review): four tasks in a loop on one host, as the driver runs them.

    The old design let the task that had just finished re-reserve first; measured on Redis
    with the default timings, its peers got [71, 1, 1, 1] grants and 6 rate_limited failures.
    Here with short timings: every task gets its turn and none fails.
    """
    config = PolitenessConfig(interval=0.05, max_wait=1.0, poll_interval=0.01)
    gate = PolitenessGate(RedisSlotStore(redis_client, key_prefix=redis_prefix), config)
    grants = [0, 0, 0, 0]
    failures = 0
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + 1.5

    async def worker(index: int) -> None:
        nonlocal failures
        while loop.time() < stop_at:
            try:
                async with gate.slot(URL):
                    grants[index] += 1
                    await asyncio.sleep(0.005)  # the request
            except RateLimitedError:
                failures += 1
            await asyncio.sleep(0.003)  # the driver starts the next task

    await asyncio.gather(*(worker(index) for index in range(4)))
    assert failures == 0
    assert min(grants) >= 4, grants
    assert max(grants) - min(grants) <= 2, grants
