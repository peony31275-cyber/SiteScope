"""Test doubles for the politeness gate: a controlled clock and an in-memory slot store.

``MemorySlotStore`` applies the same rules as the Redis Lua script in
``app.crawler.politeness`` (a first-come-first-served queue, spacing between starts, the
concurrency cap, expiring slots and waiters, crawl delays) to a ``FakeClock``, so that the
gate's behavior can be tested without Redis and without real waiting. The Redis store itself is
tested against real Redis (tests/integration/test_politeness_redis.py).
"""

import asyncio
import heapq
import itertools
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TypeVar

from redis.exceptions import ConnectionError as RedisConnectionError

from app.crawler.politeness import Grant

T = TypeVar("T")


class FakeClock:
    """A monotonic clock that only moves when a test (or ``sleep``) moves it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Advance the clock instead of waiting; yield once so other tasks can run."""
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class VirtualClock:
    """Virtual time for concurrent sleepers (a small discrete-event simulation).

    ``sleep`` parks the caller until virtual time reaches its wake-up time; ``run`` lets every
    runnable task proceed, then jumps to the earliest wake-up, and repeats until the given
    coroutine finishes. Unlike ``FakeClock``, concurrent sleeps overlap as they would in real
    time. Tasks must only wait on this clock (or on each other), never on real I/O.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self._timers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._sequence = itertools.count()

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        wake = asyncio.get_running_loop().create_future()
        heapq.heappush(self._timers, (self.now + max(seconds, 0.0), next(self._sequence), wake))
        await wake

    async def run(self, main: Awaitable[T]) -> T:
        task = asyncio.ensure_future(main)
        while not task.done():
            for _ in range(20):  # let every runnable task reach its next sleep
                await asyncio.sleep(0)
            if task.done():
                break
            if not self._timers:
                raise AssertionError("deadlock: every task waits, and no timer is pending")
            when, _, wake = heapq.heappop(self._timers)
            self.now = max(self.now, when)
            if not wake.cancelled():
                wake.set_result(None)
        return task.result()


@dataclass
class Attempt:
    host: str
    token: str
    granted: bool
    wait: float | None
    at: float  # FakeClock time of the attempt


@dataclass
class HostState:
    last_start: float | None = None
    active: dict[str, float] = field(default_factory=dict)  # token -> expiry
    queue: dict[str, float] = field(default_factory=dict)  # token -> first attempt (FIFO)
    seen: dict[str, float] = field(default_factory=dict)  # token -> latest attempt
    delay: float = 0.0


@dataclass
class MemorySlotStore:
    """In-memory double of ``RedisSlotStore``, on a ``FakeClock``."""

    clock: FakeClock
    hosts: dict[str, HostState] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)
    grants: list[tuple[str, float]] = field(default_factory=list)  # (token, time)
    fail_release: bool = False

    def state(self, host: str) -> HostState:
        return self.hosts.setdefault(host, HostState())

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
        now = self.clock()
        state = self.state(host)
        state.active = {t: expiry for t, expiry in state.active.items() if expiry > now}
        for gone in [t for t, seen in state.seen.items() if seen <= now - waiter_ttl]:
            state.queue.pop(gone, None)
            state.seen.pop(gone, None)
        if token not in state.queue:
            # After the queue's tail even if the clock stepped back.
            tail = max(state.queue.values(), default=None)
            state.queue[token] = now if tail is None or tail < now else tail + 1e-6
        state.seen[token] = now
        interval = max(interval, state.delay)
        order = sorted(state.queue, key=lambda t: (state.queue[t], t))
        rank = order.index(token)
        if rank >= max_concurrent - len(state.active):
            grant = Grant(granted=False, wait=None)
        elif state.last_start is not None and now < min(state.last_start, now) + interval:
            # A last start in the future: the clock stepped back. Record now instead.
            state.last_start = min(state.last_start, now)
            grant = Grant(granted=False, wait=state.last_start + interval - now)
        else:
            del state.queue[token]
            del state.seen[token]
            state.active[token] = now + slot_ttl
            state.last_start = now
            self.grants.append((token, now))
            grant = Grant(granted=True)
        self.attempts.append(Attempt(host, token, grant.granted, grant.wait, now))
        return grant

    async def release(self, host: str, token: str) -> None:
        if self.fail_release:
            raise RedisConnectionError("Redis is unavailable")
        state = self.state(host)
        state.active.pop(token, None)
        state.queue.pop(token, None)
        state.seen.pop(token, None)

    async def raise_crawl_delay(self, host: str, delay: float, *, ttl: float) -> None:
        state = self.state(host)
        state.delay = max(state.delay, delay)

    def held(self, host: str) -> int:
        return len(self.state(host).active)

    def waiting(self, host: str) -> int:
        return len(self.state(host).queue)
