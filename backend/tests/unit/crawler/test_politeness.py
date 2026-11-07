"""The per-host politeness gate, with a controlled clock: no Redis, no real waiting."""

import asyncio
import itertools
import logging
from collections.abc import Callable

import pytest

from app.core.config import Settings
from app.crawler.errors import RateLimitedError
from app.crawler.politeness import (
    SLOT_TTL_MARGIN_SECONDS,
    WAITER_TTL_MARGIN_SECONDS,
    PolitenessConfig,
    PolitenessGate,
    RedisSlotStore,
    build_gate,
)
from app.crawler.urls import normalize_url
from tests.politeness_support import FakeClock, MemorySlotStore, VirtualClock

pytestmark = pytest.mark.anyio

A = normalize_url("https://a.example/page")
A_OTHER_PATH = normalize_url("http://a.example:8080/other")
B = normalize_url("https://b.example/")


def gate(
    clock: FakeClock | VirtualClock, store: MemorySlotStore | None = None, **config: float
) -> PolitenessGate:
    settings: dict[str, float] = {"interval": 1.0, "max_wait": 30.0, "poll_interval": 0.1}
    settings.update(config)
    return PolitenessGate(
        store if store is not None else MemorySlotStore(clock),  # type: ignore[arg-type]  # both clocks
        PolitenessConfig(
            interval=settings["interval"],
            max_concurrent=int(settings.get("max_concurrent", 1)),
            max_wait=settings["max_wait"],
            slot_ttl=settings.get("slot_ttl", 70.0),
            max_crawl_delay=settings.get("max_crawl_delay", 10.0),
            poll_interval=settings["poll_interval"],
            slot_lease=settings.get("slot_lease", 15.0),
        ),
        sleep=clock.sleep,
        clock=clock,
    )


def renewing_gate(
    clock: VirtualClock, store: MemorySlotStore, *, slot_ttl: float, slot_lease: float
) -> PolitenessGate:
    """A gate whose waiting *and* slot renewal run on the virtual clock."""
    return PolitenessGate(
        store,
        PolitenessConfig(interval=0.0, slot_ttl=slot_ttl, slot_lease=slot_lease),
        sleep=clock.sleep,
        clock=clock,
        renew_sleep=clock.sleep,
        renew_clock=clock,
    )


async def wait_until(condition: Callable[[], bool]) -> None:
    """Let other tasks run until ``condition()`` holds (no real time passes)."""
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def test_requests_to_one_host_start_one_interval_apart() -> None:
    clock = FakeClock()
    politeness = gate(clock)
    starts = []
    for _ in range(4):
        async with politeness.slot(A):
            starts.append(clock())
    assert starts == [1000.0, 1001.0, 1002.0, 1003.0]
    assert clock.sleeps == [1.0, 1.0, 1.0]  # the first request does not wait


async def test_spacing_is_per_host_not_per_origin() -> None:
    clock = FakeClock()
    politeness = gate(clock, interval=5.0)
    async with politeness.slot(A):
        pass
    async with politeness.slot(A_OTHER_PATH):  # same host, other scheme, port and path
        assert clock() == 1005.0
    async with politeness.slot(B):  # another host does not wait for a.example
        assert clock() == 1005.0


async def test_an_idle_host_is_not_delayed() -> None:
    clock = FakeClock()
    politeness = gate(clock, interval=2.0)
    async with politeness.slot(A):
        pass
    clock.now += 10  # the host was idle for longer than the interval
    async with politeness.slot(A):
        pass
    assert clock.sleeps == []


async def test_concurrent_requests_wait_for_a_free_slot() -> None:
    clock = FakeClock()
    store = MemorySlotStore(clock)
    politeness = gate(clock, store, interval=0.0, max_concurrent=1)
    release_first = asyncio.Event()
    entered: list[str] = []

    async def first() -> None:
        async with politeness.slot(A):
            entered.append("first")
            await release_first.wait()

    async def second() -> None:
        async with politeness.slot(A):
            entered.append("second")

    first_task = asyncio.create_task(first())
    await wait_until(lambda: entered == ["first"])
    second_task = asyncio.create_task(second())
    await wait_until(lambda: sum(not attempt.granted for attempt in store.attempts) >= 3)
    assert entered == ["first"]  # still waiting: one slot per host
    assert store.waiting("a.example") == 1
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert entered == ["first", "second"]
    assert all(seconds == pytest.approx(0.1) for seconds in clock.sleeps)  # polling
    assert (store.held("a.example"), store.waiting("a.example")) == (0, 0)


async def test_the_concurrency_cap_allows_several_slots() -> None:
    clock = FakeClock()
    store = MemorySlotStore(clock)
    politeness = gate(clock, store, interval=0.0, max_concurrent=3)
    held = asyncio.Event()
    inside = 0

    async def request() -> None:
        nonlocal inside
        async with politeness.slot(A):
            inside += 1
            await held.wait()

    tasks = [asyncio.create_task(request()) for _ in range(3)]
    await wait_until(lambda: inside == 3)
    assert store.held("a.example") == 3
    held.set()
    await asyncio.gather(*tasks)
    assert clock.sleeps == []


async def test_waiters_are_served_first_come_first_served() -> None:
    """Regression: a task that releases its slot cannot overtake tasks that already wait."""
    clock = FakeClock()
    store = MemorySlotStore(clock)
    politeness = gate(clock, store, interval=0.0, max_concurrent=1)
    release = {name: asyncio.Event() for name in "ABCD"}
    order: list[str] = []

    async def request(name: str) -> None:
        async with politeness.slot(A):
            order.append(name)
            await release[name].wait()

    tasks = {"A": asyncio.create_task(request("A"))}
    await wait_until(lambda: order == ["A"])
    for name in "BC":  # B, then C, start waiting while A holds the slot
        tasks[name] = asyncio.create_task(request(name))
        await wait_until(lambda: store.waiting("a.example") == len(tasks) - 1)
    release["A"].set()
    await tasks["A"]
    # A newcomer asks at once, before the waiters' next attempt: it must queue behind them.
    grant = await store.acquire(
        "a.example", "newcomer", interval=0.0, max_concurrent=1, slot_ttl=70.0, waiter_ttl=10.0
    )
    assert not grant.granted
    await store.release("a.example", "newcomer")
    await wait_until(lambda: order == ["A", "B"])
    tasks["D"] = asyncio.create_task(request("D"))
    for name in "BC":
        release[name].set()
        await tasks[name]
    release["D"].set()
    await tasks["D"]
    assert order == ["A", "B", "C", "D"]


async def test_busy_workers_share_a_host_fairly() -> None:
    """Regression for the starvation found in review: four tasks crawl one host in a loop.

    With the old design the task that had just finished re-reserved first, and its peers ran
    out of time (measured on Redis: grants per task [71, 1, 1, 1], 6 rate_limited). In virtual
    time with the default rules, every task must get its turn, and none may fail.
    """
    clock = VirtualClock()
    store = MemorySlotStore(clock)  # type: ignore[arg-type]  # shares the clock protocol
    politeness = gate(clock, store, interval=1.0, max_wait=30.0)
    grants = [0, 0, 0, 0]
    failures = 0
    stop_at = clock() + 70.0

    async def worker(index: int) -> None:
        nonlocal failures
        while clock() < stop_at:
            try:
                async with politeness.slot(A):
                    grants[index] += 1
                    await clock.sleep(0.05)  # the request
            except RateLimitedError:
                failures += 1
            await clock.sleep(0.03)  # the driver starts the next task

    async def main() -> None:
        await asyncio.gather(*(worker(index) for index in range(4)))

    await clock.run(main())
    assert failures == 0
    assert max(grants) - min(grants) <= 1, grants
    starts = [time for _, time in store.grants]
    assert all(later - earlier >= 1.0 - 1e-9 for earlier, later in itertools.pairwise(starts))
    assert len(starts) >= 69  # one request per second for 70 s


async def test_a_backward_clock_step_delays_a_waiter_by_one_interval_at_most() -> None:
    """Redis TIME is a wall clock; WSL 2 stepped it back about 1 s during a Phase 4 test run."""
    clock = FakeClock()
    politeness = gate(clock, interval=1.0)
    async with politeness.slot(A):
        pass
    clock.now -= 50.0  # the clock steps back 50 s
    async with politeness.slot(A):
        pass
    assert clock.sleeps == [1.0]  # one interval, not 51 s


async def test_the_queue_keeps_arrival_order_across_a_backward_clock_step() -> None:
    clock = FakeClock()
    store = MemorySlotStore(clock)

    async def attempt(token: str) -> bool:
        grant = await store.acquire(
            "a.example", token, interval=0.0, max_concurrent=1, slot_ttl=70.0, waiter_ttl=15.0
        )
        return grant.granted

    assert await attempt("holder")
    clock.now += 0.5
    assert not await attempt("early")
    clock.now -= 10.0  # the clock steps back: "late" must still queue behind "early"
    assert not await attempt("late")
    await store.release("a.example", "holder")
    assert not await attempt("late")
    assert await attempt("early")


async def test_a_busy_host_fails_after_the_maximum_wait(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.crawler.politeness")
    clock = FakeClock()
    store = MemorySlotStore(clock)
    politeness = gate(clock, store, interval=0.0, max_wait=2.0)
    blocker = asyncio.Event()

    async def hold() -> None:
        async with politeness.slot(A):
            await blocker.wait()

    holder = asyncio.create_task(hold())
    await wait_until(lambda: store.held("a.example") == 1)
    with pytest.raises(RateLimitedError, match=r"a\.example within 2 s"):
        async with politeness.slot(A):
            pytest.fail("the slot must not be granted")
    assert sum(clock.sleeps) <= 2.0 + 1e-9  # never waits past the limit
    assert store.waiting("a.example") == 0  # it left the queue
    blocker.set()
    await holder
    assert [getattr(r, "event", None) for r in caplog.records] == ["politeness.rate_limited"]


async def test_a_spacing_beyond_the_maximum_wait_fails_without_waiting() -> None:
    clock = FakeClock()
    store = MemorySlotStore(clock)
    politeness = gate(clock, store, interval=60.0, max_wait=30.0)
    async with politeness.slot(A):
        pass
    with pytest.raises(RateLimitedError):
        async with politeness.slot(A):
            pass
    assert clock.sleeps == []  # next in line, but the spacing ends after the deadline
    assert store.waiting("a.example") == 0


async def test_the_slot_is_released_when_the_request_fails() -> None:
    clock = FakeClock()
    store = MemorySlotStore(clock)
    politeness = gate(clock, store)
    with pytest.raises(ValueError, match="boom"):
        async with politeness.slot(A):
            raise ValueError("boom")
    assert store.held("a.example") == 0


async def test_a_cancelled_waiter_leaves_the_queue() -> None:
    clock = FakeClock()
    store = MemorySlotStore(clock)
    never = asyncio.Event()

    async def blocked_sleep(seconds: float) -> None:
        await never.wait()

    politeness = PolitenessGate(
        store, PolitenessConfig(interval=5.0), sleep=blocked_sleep, clock=clock
    )
    async with politeness.slot(A):
        pass

    async def second() -> None:
        async with politeness.slot(A):
            pytest.fail("the slot must not be granted")

    waiting = asyncio.create_task(second())
    await wait_until(lambda: len(store.attempts) == 2)
    assert (store.held("a.example"), store.waiting("a.example")) == (0, 1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert (store.held("a.example"), store.waiting("a.example")) == (0, 0)


async def test_a_slot_granted_while_the_caller_is_cancelled_is_released() -> None:
    """Review concern: the store grants the slot, but the caller is cancelled before it reads
    the answer (e.g. the task deadline). The slot must not stay held until it expires."""
    clock = FakeClock()
    store = MemorySlotStore(clock)
    granted_but_unanswered = asyncio.Event()
    never = asyncio.Event()
    acquire = store.acquire

    async def acquire_then_hang(*args: object, **kwargs: object) -> object:
        grant = await acquire(*args, **kwargs)  # type: ignore[arg-type]  # pass-through
        granted_but_unanswered.set()
        await never.wait()  # the answer never arrives
        return grant

    store.acquire = acquire_then_hang  # type: ignore[method-assign, assignment]  # test double
    politeness = gate(clock, store)

    async def request() -> None:
        async with politeness.slot(A):
            pytest.fail("the block must not run")

    task = asyncio.create_task(request())
    await granted_but_unanswered.wait()
    assert store.held("a.example") == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (store.held("a.example"), store.waiting("a.example")) == (0, 0)


async def test_a_failed_release_is_logged_and_left_to_expire(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.crawler.politeness")
    clock = FakeClock()
    store = MemorySlotStore(clock, fail_release=True)
    async with gate(clock, store).slot(A):
        pass
    assert store.held("a.example") == 1  # expires after slot_ttl
    assert [getattr(r, "event", None) for r in caplog.records] == ["politeness.release_failed"]


async def test_an_abandoned_slot_expires_after_its_ttl() -> None:
    clock = FakeClock()
    store = MemorySlotStore(clock, fail_release=True)
    politeness = gate(clock, store, interval=0.0, slot_ttl=5.0, max_wait=30.0)
    async with politeness.slot(A):
        pass  # the release fails: the slot stays held
    store.fail_release = False
    async with politeness.slot(A):
        assert clock() >= 1005.0  # granted once the abandoned slot expired
    assert all(seconds <= 0.1 + 1e-9 for seconds in clock.sleeps)


async def test_a_dead_holders_slot_frees_after_one_lease_not_the_longest_hold() -> None:
    """Phase 6: a crashed or frozen process stops renewing, so its slot frees after one lease
    (6 s here), not after slot_ttl (70 s) as before."""
    clock = FakeClock()
    store = MemorySlotStore(clock, fail_release=True)
    politeness = gate(clock, store, interval=0.0, slot_ttl=70.0, slot_lease=6.0, max_wait=30.0)
    async with politeness.slot(A):
        pass  # no release: as if the process had died while holding the slot
    store.fail_release = False
    async with politeness.slot(A):
        assert 1006.0 <= clock() < 1007.0


async def test_a_held_slot_is_renewed_but_never_beyond_its_longest_hold() -> None:
    clock = VirtualClock()
    store = MemorySlotStore(clock)  # type: ignore[arg-type]  # the virtual clock is a clock
    politeness = renewing_gate(clock, store, slot_ttl=20.0, slot_lease=6.0)
    expiries: list[float] = []

    async def hold() -> None:
        async with politeness.slot(A):
            for _ in range(15):  # a request longer than slot_ttl (impossible with real settings)
                await clock.sleep(2.0)
                expiries.extend(store.state("a.example").active.values())

    await clock.run(hold())
    # A renewal every lease/3 = 2 s; each extends the slot to now + min(lease, what is left of
    # slot_ttl), so the slot is held continuously, and never past grant + slot_ttl = 1020.
    assert [at for _, at, _ in store.renewals] == [1000.0 + 2 * n for n in range(1, 10)]
    assert [lease for _, _, lease in store.renewals] == [6.0] * 7 + [4.0, 2.0]
    assert max(expiries) == 1020.0
    # Observed every 2 s from 1002 to 1018: always still held.
    observed = zip(expiries[:9], range(1002, 1020, 2), strict=True)
    assert all(expiry > at for expiry, at in observed), expiries
    assert store.held("a.example") == 0  # released at the end


async def test_a_failed_renewal_is_logged_and_tried_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.crawler.politeness")
    clock = VirtualClock()
    store = MemorySlotStore(clock, fail_renew=True)  # type: ignore[arg-type]  # virtual clock
    politeness = renewing_gate(clock, store, slot_ttl=70.0, slot_lease=6.0)

    async def hold() -> str:
        async with politeness.slot(A):
            await clock.sleep(3.0)  # the renewal at 1002 fails: Redis is down
            store.fail_renew = False
            await clock.sleep(2.0)  # the one at 1004 works
        return "the request is unaffected"

    assert await clock.run(hold()) == "the request is unaffected"
    assert [(at, lease) for _, at, lease in store.renewals] == [(1004.0, 6.0)]
    assert [getattr(r, "event", None) for r in caplog.records] == ["politeness.renew_failed"]


async def test_a_lost_slot_is_logged_and_no_longer_renewed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.crawler.politeness")
    clock = VirtualClock()
    store = MemorySlotStore(clock)  # type: ignore[arg-type]  # the virtual clock is a clock
    politeness = renewing_gate(clock, store, slot_ttl=70.0, slot_lease=6.0)

    async def hold() -> None:
        async with politeness.slot(A):
            # The process stalled past its lease: the slot expired and may be someone else's.
            store.state("a.example").active.clear()
            await clock.sleep(10.0)

    await clock.run(hold())
    assert store.renewals == []
    assert [getattr(r, "event", None) for r in caplog.records] == ["politeness.slot_lost"]


async def test_cancelling_the_request_stops_the_renewal_and_releases_the_slot() -> None:
    clock = VirtualClock()
    store = MemorySlotStore(clock)  # type: ignore[arg-type]  # the virtual clock is a clock
    politeness = renewing_gate(clock, store, slot_ttl=70.0, slot_lease=6.0)
    holding = asyncio.Event()

    async def request() -> None:
        async with politeness.slot(A):
            holding.set()
            await asyncio.Event().wait()  # until cancelled

    async def scenario() -> None:
        task = asyncio.create_task(request())
        await holding.wait()
        await clock.sleep(5.0)  # one renewal, at 1002
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await clock.sleep(10.0)  # no renewal after the release

    await clock.run(scenario())
    assert [at for _, at, _ in store.renewals] == [1002.0, 1004.0]
    assert store.held("a.example") == 0


async def test_a_crashed_waiter_stops_blocking_the_queue() -> None:
    clock = FakeClock()
    store = MemorySlotStore(clock)
    config = PolitenessConfig(interval=0.0, max_concurrent=1)
    politeness = PolitenessGate(store, config, sleep=clock.sleep, clock=clock)
    # A waiter of a process that crashed: queued once, never seen again.
    await store.acquire(
        "a.example",
        "crashed",
        interval=0.0,
        max_concurrent=0,  # refused, so it stays in the queue
        slot_ttl=70.0,
        waiter_ttl=config.waiter_ttl,
    )
    assert store.waiting("a.example") == 1
    clock.now += 0.001  # a later arrival (equal times would be ordered by token)
    async with politeness.slot(A):
        # Granted once the silent waiter has been dropped, after waiter_ttl.
        assert clock() >= 1000.0 + config.waiter_ttl
    assert store.waiting("a.example") == 0


async def test_a_crawl_delay_raises_the_interval_up_to_the_cap() -> None:
    clock = FakeClock()
    store = MemorySlotStore(clock)
    politeness = gate(clock, store, interval=1.0, max_crawl_delay=4.0)
    await politeness.set_crawl_delay("a.example", 25.0, ttl=60)
    assert store.state("a.example").delay == 4.0  # capped
    for _ in range(2):
        async with politeness.slot(A):
            pass
    assert clock.sleeps == [4.0]
    # A host keeps the largest delay of its origins: smaller or missing ones change nothing.
    await politeness.set_crawl_delay("a.example", 2.0, ttl=60)
    await politeness.set_crawl_delay("a.example", None, ttl=60)
    assert store.state("a.example").delay == 4.0


@pytest.mark.parametrize(
    "config",
    [
        {"interval": -1.0},
        {"max_concurrent": 0},
        {"max_wait": 0.0},
        {"slot_ttl": 0.0},
        {"poll_interval": 0.0},
        {"max_crawl_delay": -1.0},
        {"slot_lease": 0.0},
    ],
)
def test_invalid_configurations_are_rejected(config: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"must"):
        PolitenessConfig(**config)  # type: ignore[arg-type]  # deliberately invalid values


def test_a_waiter_may_stay_silent_for_its_longest_sleep() -> None:
    config = PolitenessConfig(interval=1.0, max_crawl_delay=10.0, poll_interval=0.1)
    assert config.waiter_ttl == 10.0 + WAITER_TTL_MARGIN_SECONDS


def test_the_first_lease_never_exceeds_the_longest_hold() -> None:
    assert PolitenessConfig(slot_ttl=70.0, slot_lease=15.0).first_lease == 15.0
    assert PolitenessConfig(slot_ttl=5.0, slot_lease=15.0).first_lease == 5.0


def test_build_gate_uses_the_settings(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "politeness_interval_seconds": 2.5,
            "politeness_max_concurrent_per_host": 3,
            "politeness_max_wait_seconds": 20.0,
            "robots_max_crawl_delay_seconds": 7.0,
            "fetch_deadline_seconds": 45.0,
            "politeness_slot_lease_seconds": 9.0,
        }
    )

    class FakeRedis:
        def register_script(self, script: str) -> object:
            return object()

    built = build_gate(configured, FakeRedis())  # type: ignore[arg-type]  # only scripts used
    assert built.config == PolitenessConfig(
        interval=2.5,
        max_concurrent=3,
        max_wait=20.0,
        slot_ttl=45.0 + SLOT_TTL_MARGIN_SECONDS,
        max_crawl_delay=7.0,
        slot_lease=9.0,
    )


def test_redis_keys_share_a_hash_tag_per_host() -> None:
    class FakeRedis:
        def register_script(self, script: str) -> object:
            return object()

    store = RedisSlotStore(FakeRedis(), key_prefix="test")  # type: ignore[arg-type]  # keys only
    assert store.keys("a.example") == (
        "test:rate:{a.example}:last",
        "test:rate:{a.example}:active",
        "test:rate:{a.example}:delay",
        "test:rate:{a.example}:queue",
        "test:rate:{a.example}:seen",
    )
