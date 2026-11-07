"""Per-host politeness: request spacing and a concurrency cap, shared by every crawler process.

Every request hop to a crawl target first takes a *slot* for its host: the safe fetcher enters
``PolitenessGate.slot`` for each hop, redirect hops and robots.txt requests included. One atomic
Redis Lua script grants slots, and applies three rules per host:

* **First come, first served.** A request joins its host's queue on its first attempt, and only
  the oldest waiters can get a slot. A task that has just released a slot cannot take the next
  one ahead of tasks that were already waiting. An earlier design granted slots for future
  starts, so the releasing task could reserve again before the waiters woke up, and it starved
  them. The Phase 4 review found this.
* **Spacing.** Request starts are at least ``interval`` apart: a slot is granted only once
  ``interval`` has passed since the host's previous start. A robots.txt ``Crawl-delay`` raises
  the interval of its host (``set_crawl_delay``), up to ``max_crawl_delay``.
* **Concurrency.** At most ``max_concurrent`` slots of one host are held at the same time,
  across all processes. A slot is held until its request has finished. It also expires
  ``slot_ttl`` seconds after it was granted, so a crashed process cannot block a host for long.

The waiter at the head of the queue learns exactly how long the spacing still requires, and
sleeps that long. Waiters further back poll every ``poll_interval``. A waiter that stops asking,
because its process crashed, leaves the queue after ``waiter_ttl``. Time comes from Redis
(``TIME``), never from a worker's clock (architecture, section 19): the schedule does not depend
on the workers' clocks, which only time their own sleeps. A request waits at most ``max_wait``
seconds for its slot. Without a slot by then it fails with ``RateLimitedError``
(``rate_limited``), before anything is sent, and leaves the queue.

``TIME`` is the Redis host's wall clock, which can step (WSL 2 resynchronizes its VM clock in
jumps; the Phase 4 tests caught a one-second backward step). After a backward step, the spacing
counts from the current time, so a waiter never waits more than one interval for it, and the
queue keeps its arrival order. A forward step can shorten one gap.

Redis keys, all with a TTL so that idle hosts leave nothing behind (the hash tag keeps one host's
keys in one Redis Cluster slot):

* ``<prefix>:rate:{<host>}:last``: the start of the host's latest request (microseconds of
  Redis time);
* ``<prefix>:rate:{<host>}:active``: held slots (sorted set: slot token -> expiry);
* ``<prefix>:rate:{<host>}:queue``: waiting requests in arrival order (sorted set: slot token ->
  first attempt);
* ``<prefix>:rate:{<host>}:seen``: when each waiter last asked (sorted set: slot token -> time);
* ``<prefix>:rate:{<host>}:delay``: the host's crawl delay (microseconds).

Only the slot store talks to Redis, so the gate itself is tested with a controlled clock and no
real waiting (``tests/politeness_support.py``).
"""

import asyncio
import logging
import math
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.crawler.errors import RateLimitedError
from app.crawler.urls import NormalizedUrl

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

MICROSECONDS: Final = 1_000_000
# A slot outlives the longest request hop (the fetch deadline) by this margin before it expires.
SLOT_TTL_MARGIN_SECONDS: Final = 10.0
# How much longer than its longest sleep a waiter may stay silent before it counts as gone.
WAITER_TTL_MARGIN_SECONDS: Final = 5.0

# KEYS: last start, active slots, crawl delay, queue, seen. ARGV: slot token, interval, max
# concurrent slots, slot TTL, waiter TTL (times in microseconds). Returns {1, 0} for a granted
# slot, {0, wait} when the caller is next in line but the spacing needs ``wait`` more
# microseconds, and {0, -1} when others are ahead or every slot is held (poll again).
# Numbers are formatted with "%.0f" so that microsecond timestamps keep every digit.
ACQUIRE_SCRIPT: Final = """
local function fmt(n) return string.format('%.0f', n) end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000000 + tonumber(clock[2])
local token = ARGV[1]
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', fmt(now))
local gone = redis.call('ZRANGEBYSCORE', KEYS[5], '-inf', fmt(now - tonumber(ARGV[5])))
for _, waiter in ipairs(gone) do
  redis.call('ZREM', KEYS[4], waiter)
  redis.call('ZREM', KEYS[5], waiter)
end
-- Arrival order: after the queue's tail even if the clock stepped back (TIME is a wall clock).
local tail = redis.call('ZRANGE', KEYS[4], -1, -1, 'WITHSCORES')
local arrival = now
if #tail == 2 and tonumber(tail[2]) >= arrival then
  arrival = tonumber(tail[2]) + 1
end
redis.call('ZADD', KEYS[4], 'NX', fmt(arrival), token)
redis.call('ZADD', KEYS[5], fmt(now), token)
local keep = fmt(math.floor(tonumber(ARGV[5]) / 1000) + 1000)
redis.call('PEXPIRE', KEYS[4], keep)
redis.call('PEXPIRE', KEYS[5], keep)
local interval = tonumber(ARGV[2])
local delay = tonumber(redis.call('GET', KEYS[3]) or '0') or 0
if delay > interval then
  interval = delay
end
local free = tonumber(ARGV[3]) - redis.call('ZCARD', KEYS[2])
local rank = redis.call('ZRANK', KEYS[4], token)
if rank >= free then
  return {0, -1}
end
-- A last start "in the future" means the clock stepped back: record now as the last start, so
-- the real gap stays at least one interval and the wait is one interval, not the step.
local last = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
if last > now then
  last = now
  redis.call('SET', KEYS[1], fmt(now), 'PX', fmt(math.floor(interval / 1000) + 1000))
end
local earliest = last + interval
if now < earliest then
  return {0, earliest - now}
end
redis.call('ZREM', KEYS[4], token)
redis.call('ZREM', KEYS[5], token)
redis.call('ZADD', KEYS[2], fmt(now + tonumber(ARGV[4])), token)
local ttl = math.floor(tonumber(ARGV[4]) / 1000) + 1000
if redis.call('PTTL', KEYS[2]) < ttl then
  redis.call('PEXPIRE', KEYS[2], fmt(ttl))
end
redis.call('SET', KEYS[1], fmt(now), 'PX', fmt(math.floor(interval / 1000) + 1000))
return {1, 0}
"""

# KEYS: crawl delay. ARGV: delay, TTL in milliseconds. Keeps the larger of the stored and the
# new delay, so that one origin of a host never lowers the delay another origin asked for.
RAISE_DELAY_SCRIPT: Final = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
if tonumber(ARGV[1]) > current then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
end
return 1
"""


@dataclass(frozen=True, slots=True)
class PolitenessConfig:
    """Per-host politeness rules (settings: ``SITESCOPE_POLITENESS_*``)."""

    interval: float = 1.0  # minimum seconds between two request starts to one host
    max_concurrent: int = 1  # slots of one host held at the same time
    max_wait: float = 30.0  # the longest a request waits for its slot
    slot_ttl: float = 70.0  # a slot never released expires this long after it was granted
    max_crawl_delay: float = 10.0  # cap on a robots.txt Crawl-delay
    poll_interval: float = 0.1  # how often a waiter that is not next in line asks again

    def __post_init__(self) -> None:
        if self.interval < 0 or self.max_crawl_delay < 0:
            raise ValueError("intervals must not be negative")
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if self.max_wait <= 0 or self.slot_ttl <= 0 or self.poll_interval <= 0:
            raise ValueError("max_wait, slot_ttl and poll_interval must be positive")

    @property
    def waiter_ttl(self) -> float:
        """How long a waiter may stay silent before it leaves the queue (its process crashed).

        A live waiter sleeps at most the longest spacing (the interval, or a capped crawl delay)
        or one poll interval between two attempts.
        """
        longest_sleep = max(self.interval, self.max_crawl_delay, self.poll_interval)
        return longest_sleep + WAITER_TTL_MARGIN_SECONDS


@dataclass(frozen=True, slots=True)
class Grant:
    """A slot store's answer to one attempt."""

    granted: bool
    # Not granted: seconds the spacing still requires when the caller is next in line, or None
    # when others are ahead or every slot is held (poll again).
    wait: float | None = None


class SlotStore(Protocol):
    """Shared state of the per-host rules (``RedisSlotStore``; tests use an in-memory double)."""

    async def acquire(
        self,
        host: str,
        token: str,
        *,
        interval: float,
        max_concurrent: int,
        slot_ttl: float,
        waiter_ttl: float,
    ) -> Grant: ...

    async def release(self, host: str, token: str) -> None: ...

    async def raise_crawl_delay(self, host: str, delay: float, *, ttl: float) -> None: ...


class RedisSlotStore:
    """The slot store in Redis: one Lua script makes each attempt atomic."""

    def __init__(self, redis: Redis, *, key_prefix: str) -> None:
        self._redis = redis
        self._prefix = key_prefix
        self._acquire = redis.register_script(ACQUIRE_SCRIPT)
        self._raise_delay = redis.register_script(RAISE_DELAY_SCRIPT)

    def keys(self, host: str) -> tuple[str, str, str, str, str]:
        """The last-start, active, crawl-delay, queue and seen keys of a host."""
        base = f"{self._prefix}:rate:{{{host}}}"
        return f"{base}:last", f"{base}:active", f"{base}:delay", f"{base}:queue", f"{base}:seen"

    async def acquire(
        self,
        host: str,
        token: str,
        *,
        interval: float,
        max_concurrent: int,
        slot_ttl: float,
        waiter_ttl: float,
    ) -> Grant:
        reply = await self._acquire(
            keys=list(self.keys(host)),
            args=[
                token,
                _microseconds(interval),
                max_concurrent,
                _microseconds(slot_ttl),
                _microseconds(waiter_ttl),
            ],
        )
        granted, wait = cast(list[int], reply)
        if granted:
            return Grant(granted=True)
        return Grant(granted=False, wait=None if wait < 0 else wait / MICROSECONDS)

    async def release(self, host: str, token: str) -> None:
        """Give the slot back, or leave the queue (a waiter that gives up or is cancelled)."""
        _, active, _, queue, seen = self.keys(host)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zrem(active, token)
            pipe.zrem(queue, token)
            pipe.zrem(seen, token)
            await pipe.execute()

    async def raise_crawl_delay(self, host: str, delay: float, *, ttl: float) -> None:
        await self._raise_delay(
            keys=[self.keys(host)[2]],
            args=[_microseconds(delay), max(1, math.ceil(ttl * 1000))],
        )


class PolitenessGate:
    """The fetcher's ``RequestGate``: one slot per request hop, per host. See the module doc."""

    def __init__(
        self,
        store: SlotStore,
        config: PolitenessConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._config = config
        # Injected so that tests control time; production waits with asyncio.sleep. The local
        # clock only bounds how long this process waits; the schedule itself is Redis time.
        self._sleep = sleep
        self._clock = clock

    @property
    def config(self) -> PolitenessConfig:
        return self._config

    @asynccontextmanager
    async def slot(self, url: NormalizedUrl) -> AsyncIterator[None]:
        """Wait for a slot of the URL's host and hold it for the block; see the module doc.

        Raises ``RateLimitedError`` if no slot is granted within ``max_wait``. Whatever happens
        (success, failure, cancellation), the slot or the place in the queue is given back.
        """
        host = url.host
        token = secrets.token_hex(8)
        try:
            await self._acquire(host, token)
            yield
        finally:
            await self._release(host, token)

    async def set_crawl_delay(self, host: str, delay: float | None, *, ttl: float) -> None:
        """Apply a robots.txt Crawl-delay (capped) to ``host`` for ``ttl`` seconds.

        A host's delay is the largest one found in the robots.txt of any of its origins during
        the cache time; a robots.txt without Crawl-delay changes nothing.
        """
        if delay is None or delay <= 0:
            return
        await self._store.raise_crawl_delay(host, min(delay, self._config.max_crawl_delay), ttl=ttl)

    async def _acquire(self, host: str, token: str) -> None:
        config = self._config
        started = self._clock()
        deadline = started + config.max_wait
        while True:
            grant = await self._store.acquire(
                host,
                token,
                interval=config.interval,
                max_concurrent=config.max_concurrent,
                slot_ttl=config.slot_ttl,
                waiter_ttl=config.waiter_ttl,
            )
            if grant.granted:
                return
            remaining = deadline - self._clock()
            if remaining <= 0 or (grant.wait is not None and grant.wait > remaining):
                break  # next in line, but the spacing ends after the deadline: give up now
            wait = config.poll_interval if grant.wait is None else grant.wait
            await self._sleep(min(wait, remaining))
        logger.info(
            "No politeness slot within the maximum wait",
            extra={
                "event": "politeness.rate_limited",
                "host": host,
                "waited_ms": round((self._clock() - started) * 1000, 1),
            },
        )
        raise RateLimitedError(
            f"no request slot for {host} within {config.max_wait:g} s (politeness limit)"
        )

    async def _release(self, host: str, token: str) -> None:
        try:
            await self._store.release(host, token)
        except RedisError:
            # The slot expires after slot_ttl, a queue place after waiter_ttl.
            logger.warning(
                "Could not release a politeness slot",
                extra={"event": "politeness.release_failed", "host": host},
            )


def build_gate(settings: "Settings", redis: Redis) -> PolitenessGate:
    """The politeness gate configured from the application settings."""
    return PolitenessGate(
        RedisSlotStore(redis, key_prefix=settings.redis_key_prefix),
        PolitenessConfig(
            interval=settings.politeness_interval_seconds,
            max_concurrent=settings.politeness_max_concurrent_per_host,
            max_wait=settings.politeness_max_wait_seconds,
            slot_ttl=settings.fetch_deadline_seconds + SLOT_TTL_MARGIN_SECONDS,
            max_crawl_delay=settings.robots_max_crawl_delay_seconds,
        ),
    )


def _microseconds(seconds: float) -> int:
    return max(0, round(seconds * MICROSECONDS))
