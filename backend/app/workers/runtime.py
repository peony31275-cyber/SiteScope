"""What every crawl process (a crawler worker, the single-process driver) does the same way."""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable

from redis.asyncio import Redis

from app.core.config import Settings
from app.crawler.fetcher import RequestGate, build_fetcher
from app.crawler.html_parser import ParseLimits
from app.crawler.politeness import build_gate
from app.crawler.robots import build_robots_policy
from app.crawler.urls import NormalizedUrl
from app.db.models import UrlState
from app.db.session import Database
from app.observability.timing import task_timings_var
from app.services.crawl_jobs import target_policy
from app.services.crawl_state import Lease, UrlResult
from app.workers.crawl_task import CrawlContext, crawl_url

DEADLINE_EXCEEDED = "deadline_exceeded"


class TimedGate:
    """A request gate that times each slot's wait (Phase 10): the wait, granted or refused, is
    added to the running task's ``TaskTimings`` (``app.observability.timing``), if it set one.
    Admission itself is the wrapped gate's, unchanged."""

    def __init__(self, gate: RequestGate, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._gate = gate
        self._clock = clock

    @contextlib.asynccontextmanager
    async def slot(self, url: NormalizedUrl) -> AsyncIterator[None]:
        timings = task_timings_var.get()
        started = self._clock()
        granted = False
        try:
            async with self._gate.slot(url):
                granted = True
                if timings is not None:
                    timings.politeness_wait += self._clock() - started
                    timings.slots += 1
                yield
        finally:
            if not granted and timings is not None:
                timings.politeness_wait += self._clock() - started
                timings.slots += 1


def idle_transaction_timeout_ms(settings: Settings) -> int:
    """A crawl process's ``idle_in_transaction_session_timeout``: the lease margin (at least 1 s).

    A crawl process's transactions never wait on the network (claims, results and recoveries
    are a few statements each), so a session that stays idle inside one for longer belongs to a
    frozen process (a VM pause, a suspended process). PostgreSQL then ends the session, which
    rolls the transaction back and releases its row lock. Without this, a worker frozen between
    its claim's lock and its commit blocked every takeover of that URL for as long as it was
    frozen (found by the Phase 5 review).
    """
    return round(max(1.0, settings.crawl_lease_margin_seconds) * 1000)


def worker_database(settings: Settings, *, application_name: str) -> Database:
    """The PostgreSQL access of a crawl process (see ``idle_transaction_timeout_ms``)."""
    return Database(
        settings,
        application_name=application_name,
        server_settings={
            "idle_in_transaction_session_timeout": str(idle_transaction_timeout_ms(settings))
        },
    )


def build_crawl_context(settings: Settings, redis: Redis) -> CrawlContext:
    """The safe fetcher with the shared politeness gate, the robots.txt policy, and limits.

    Politeness slots and the robots.txt cache live in Redis, so every process that crawls
    shares them (ADR-0015).
    """
    gate = build_gate(settings, redis)
    fetcher = build_fetcher(settings, gate=TimedGate(gate))
    return CrawlContext(
        fetcher=fetcher,
        robots=build_robots_policy(settings, fetcher=fetcher, redis=redis, delay_sink=gate),
        targets=target_policy(settings),
        parse_limits=ParseLimits.from_settings(settings),
        crawler_name=settings.crawler_name,
    )


async def crawl_within(context: CrawlContext, lease: Lease, *, deadline: float) -> UrlResult:
    """``crawl_task.crawl_url``, cut off after ``deadline`` seconds.

    The deadline is shorter than the lease (task_deadline < lease_duration, ADR-0003), so a task
    never keeps running on an expired lease; a task cut off fails as ``deadline_exceeded``.
    """
    try:
        async with asyncio.timeout(deadline) as scope:
            return await crawl_url(context, lease)
    except TimeoutError:
        if not scope.expired():  # a timeout of something else: not this task's deadline
            raise
        return UrlResult(
            UrlState.FAILED,
            DEADLINE_EXCEEDED,
            error=f"the task did not finish within {deadline:g} s",
        )
