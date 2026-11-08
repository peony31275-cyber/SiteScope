"""Search and analysis time limits: only the call's own deadline (or PostgreSQL's statement
timeout) is a timeout of the call. Another ``TimeoutError``, such as asyncpg's connect timeout
(5 s by default, shorter than both limits), is an outage: it propagates, and the API reports it
as ``503 dependency_unavailable`` with ``Retry-After`` (Phase 8 review). No database is needed:
the sessions here are test doubles."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import overview, site_analysis
from app.services.search import SearchTimeoutError, search_job
from app.services.site_analysis import AnalysisTimeoutError

pytestmark = pytest.mark.anyio


class UnreachableDatabase:
    """A test double of a session whose connection attempt times out."""

    def begin(self) -> "UnreachableDatabase":
        return self

    async def __aenter__(self) -> None:
        raise TimeoutError("timed out connecting to PostgreSQL")

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class StalledDatabase(UnreachableDatabase):
    """A test double of a session whose connection attempt never finishes."""

    async def __aenter__(self) -> None:
        await asyncio.sleep(30)


def session_of(double: UnreachableDatabase) -> AsyncSession:
    return cast(AsyncSession, double)


Call = Callable[[AsyncSession, float], Awaitable[Any]]
JOB = uuid.UUID("0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b")
ANALYSES: dict[str, Call] = {
    "pages": lambda s, t: site_analysis.list_pages(s, JOB, limit=10, timeout_seconds=t),
    "links": lambda s, t: site_analysis.list_links(s, JOB, limit=10, timeout_seconds=t),
    "broken links": lambda s, t: site_analysis.list_broken_links(
        s, JOB, limit=10, timeout_seconds=t
    ),
    "link graph": lambda s, t: site_analysis.link_graph(
        s, JOB, max_nodes=10, max_edges=10, timeout_seconds=t
    ),
    "statistics": lambda s, t: site_analysis.crawl_stats(s, JOB, timeout_seconds=t),
    # The dashboard's overview (Phase 9) reads through the same snapshot helper.
    "overview": lambda s, t: overview.system_overview(s, timeout_seconds=t),
}


@pytest.mark.parametrize("name", list(ANALYSES))
async def test_a_connect_timeout_is_an_outage_not_an_analysis_timeout(name: str) -> None:
    with pytest.raises(TimeoutError, match="connecting to PostgreSQL"):
        await ANALYSES[name](session_of(UnreachableDatabase()), 10.0)


@pytest.mark.parametrize("name", list(ANALYSES))
async def test_an_analysis_past_its_own_deadline_is_an_analysis_timeout(name: str) -> None:
    with pytest.raises(AnalysisTimeoutError, match=r"longer than 0.05 s"):
        await ANALYSES[name](session_of(StalledDatabase()), 0.05)


async def test_a_connect_timeout_is_an_outage_not_a_search_timeout() -> None:
    with pytest.raises(TimeoutError, match="connecting to PostgreSQL"):
        await search_job(session_of(UnreachableDatabase()), JOB, "redis", timeout_seconds=10.0)


async def test_a_search_past_its_own_deadline_is_a_search_timeout() -> None:
    with pytest.raises(SearchTimeoutError, match=r"longer than 0.05 s"):
        await search_job(session_of(StalledDatabase()), JOB, "redis", timeout_seconds=0.05)
