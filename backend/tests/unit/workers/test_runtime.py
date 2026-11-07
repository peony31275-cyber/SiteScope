"""The task deadline (app/workers/runtime.py: crawl_within) with test doubles.

``SlowRobots`` and ``TimingOutRobots`` are test doubles of the robots.txt policy; nothing here
touches the network, PostgreSQL or Redis.
"""

import asyncio
import uuid

import pytest

from app.crawler.html_parser import ParseLimits
from app.crawler.results import FetchResult
from app.crawler.robots import RobotsDecision
from app.crawler.scope import CrawlTargetPolicy
from app.crawler.urls import NormalizedUrl
from app.db.models import UrlState
from app.services.crawl_state import Lease
from app.workers.crawl_task import CrawlContext
from app.workers.runtime import DEADLINE_EXCEEDED, crawl_within

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
