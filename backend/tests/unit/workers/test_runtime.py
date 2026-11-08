"""The task deadline (app/workers/runtime.py: crawl_within) and the timed politeness gate
(``TimedGate``, Phase 10), with test doubles.

``SlowRobots`` and ``TimingOutRobots`` are test doubles of the robots.txt policy, and
``ScriptedGate`` of the politeness gate; nothing here touches the network, PostgreSQL or Redis.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import pytest

from app.crawler.errors import RateLimitedError
from app.crawler.html_parser import ParseLimits
from app.crawler.results import FetchResult
from app.crawler.robots import RobotsDecision
from app.crawler.scope import CrawlTargetPolicy
from app.crawler.urls import NormalizedUrl, normalize_url
from app.db.models import UrlState
from app.observability.timing import TaskTimings, task_timings_var
from app.services.crawl_state import Lease
from app.workers.crawl_task import CrawlContext
from app.workers.runtime import DEADLINE_EXCEEDED, TimedGate, crawl_within

pytestmark = pytest.mark.anyio


class SlowRobots:
    """Test double: the robots.txt check never finishes (a hung dependency)."""

    async def check(self, url: NormalizedUrl) -> RobotsDecision:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class TimingOutRobots:
    """Test double: the robots.txt check fails with a timeout of its own."""

    async def check(self, url: NormalizedUrl) -> RobotsDecision:
        raise TimeoutError("an inner timeout, not the task deadline")


class UnusedFetcher:
    async def fetch(self, url: str, *, max_redirects: int | None = None) -> FetchResult:
        raise AssertionError("the fetcher must not be called")


def context(robots: SlowRobots | TimingOutRobots) -> CrawlContext:
    return CrawlContext(
        fetcher=UnusedFetcher(),
        robots=robots,
        targets=CrawlTargetPolicy.from_config(
            hosts=("docs.example.com",), schemes=("https",), ports=(443,)
        ),
        parse_limits=ParseLimits(),
        crawler_name="SiteScopeBot",
    )


LEASE = Lease(
    url_id=uuid.uuid4(),
    job_id=uuid.uuid4(),
    token=1,
    owner="crawler-test",
    url="https://docs.example.com/",
    depth=0,
    redirect_hops=0,
    attempt=1,
    allowed_hosts=("docs.example.com",),
    takeover=False,
)


async def test_a_task_past_its_deadline_fails_as_deadline_exceeded() -> None:
    result = await crawl_within(context(SlowRobots()), LEASE, deadline=0.05)

    assert result.state is UrlState.FAILED
    assert result.outcome == DEADLINE_EXCEEDED


async def test_another_timeout_is_not_mistaken_for_the_deadline() -> None:
    """An infrastructure timeout must propagate: the worker then leaves the lease to expire
    instead of recording a terminal failure."""
    with pytest.raises(TimeoutError, match="inner timeout"):
        await crawl_within(context(TimingOutRobots()), LEASE, deadline=10)


# --- TimedGate (Phase 10) ------------------------------------------------------------------------


class ScriptedGate:
    """Test double of the politeness gate: each slot takes ``waits`` seconds of a fake clock
    to be granted, or is refused (``RateLimitedError``) after its wait."""

    def __init__(self, clock: list[float], waits: list[float], *, refuse: bool = False) -> None:
        self.clock = clock
        self.waits = waits
        self.refuse = refuse
        self.held = 0

    @contextlib.asynccontextmanager
    async def slot(self, url: NormalizedUrl) -> AsyncIterator[None]:
        self.clock[0] += self.waits.pop(0)
        if self.refuse:
            raise RateLimitedError("no slot for docs.example.com within 5 s")
        self.held += 1
        try:
            yield
        finally:
            self.held -= 1


URL = normalize_url("https://docs.example.com/page")


async def test_the_timed_gate_adds_each_slots_wait_to_the_task_timings() -> None:
    clock = [100.0]
    gate = ScriptedGate(clock, waits=[0.25, 0.5])
    timed = TimedGate(gate, clock=lambda: clock[0])
    timings = TaskTimings()
    token = task_timings_var.set(timings)
    try:
        for _ in range(2):
            async with timed.slot(URL):
                assert gate.held == 1  # admission is the gate's own
                clock[0] += 10  # the request itself does not count as waiting
    finally:
        task_timings_var.reset(token)

    assert timings.politeness_wait == pytest.approx(0.75)
    assert timings.slots == 2
    assert gate.held == 0


async def test_a_refused_slots_wait_counts_and_the_refusal_propagates() -> None:
    clock = [0.0]
    timed = TimedGate(ScriptedGate(clock, waits=[5.0], refuse=True), clock=lambda: clock[0])
    timings = TaskTimings()
    token = task_timings_var.set(timings)
    try:
        with pytest.raises(RateLimitedError):
            async with timed.slot(URL):
                raise AssertionError("a refused slot runs no request")
    finally:
        task_timings_var.reset(token)

    assert timings.politeness_wait == pytest.approx(5.0)
    assert timings.slots == 1


async def test_without_task_timings_the_timed_gate_only_admits() -> None:
    clock = [0.0]
    gate = ScriptedGate(clock, waits=[1.0])

    async with TimedGate(gate, clock=lambda: clock[0]).slot(URL):
        assert gate.held == 1

    assert task_timings_var.get() is None
