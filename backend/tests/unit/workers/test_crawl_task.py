"""The per-URL crawl pipeline (app/workers/crawl_task.py) with test doubles.

``FakeFetcher`` and ``FakeRobots`` are test doubles: they return scripted results, and record
what the task asked for. Nothing here touches the network, PostgreSQL or Redis.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from app.crawler import html_parser
from app.crawler.errors import FailureKind, FetchError
from app.crawler.html_parser import ParseLimits
from app.crawler.results import FetchResult
from app.crawler.robots import RobotsDecision, RobotsStatus
from app.crawler.scope import CrawlTargetPolicy
from app.crawler.urls import NormalizedUrl, normalize_url
from app.db.models import UrlState
from app.services.crawl_state import Lease, LinkRecord
from app.workers import crawl_task
from app.workers.crawl_task import CrawlContext, crawl_url, failure_result, in_job_scope

pytestmark = pytest.mark.anyio

SEED = "https://docs.example.com/guide/"
ALLOWED = RobotsDecision(allowed=True, status=RobotsStatus.PARSED, crawl_delay=None, detail=None)


@dataclass
class FakeFetcher:
    """Test double of the safe fetcher: answers every fetch with ``reply``."""

    reply: FetchResult | FetchError
    calls: list[tuple[str, int | None]] = field(default_factory=list)

    async def fetch(self, url: str, *, max_redirects: int | None = None) -> FetchResult:
        self.calls.append((url, max_redirects))
        if isinstance(self.reply, FetchError):
            raise self.reply
        return self.reply


@dataclass
class FakeRobots:
    """Test double of the robots.txt policy: answers every check with ``decision``."""

    decision: RobotsDecision | FetchError = ALLOWED
    checked: list[str] = field(default_factory=list)

    async def check(self, url: NormalizedUrl) -> RobotsDecision:
        self.checked.append(url.url)
        if isinstance(self.decision, FetchError):
            raise self.decision
        return self.decision


def lease(url: str = SEED, hosts: Sequence[str] = ("docs.example.com",)) -> Lease:
    return Lease(
        url_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        token=1,
        owner="worker",
        url=url,
        depth=0,
        redirect_hops=0,
        attempt=1,
        allowed_hosts=tuple(hosts),
        takeover=False,
    )


def context(
    fetcher: FakeFetcher, robots: FakeRobots | None = None, *, hosts: Sequence[str] = ()
) -> CrawlContext:
    return CrawlContext(
        fetcher=fetcher,
        robots=robots if robots is not None else FakeRobots(),
        targets=CrawlTargetPolicy.from_config(
            hosts=hosts or ("docs.example.com", "*.example.org"),
            schemes=("http", "https"),
            ports=(80, 443),
        ),
        parse_limits=ParseLimits(),
        crawler_name="SiteScopeBot",
    )


def response(
    body: bytes = b"",
    *,
    status: int = 200,
    location: str | None = None,
    robots_tags: tuple[str, ...] = (),
) -> FetchResult:
    return FetchResult(
        requested_url=SEED,
        url=SEED,
        status_code=status,
        content_type="text/html" if location is None else None,
        charset="utf-8",
        headers={},
        body=body,
        redirects=(),
        redirect_location=location,
        remote_address="192.0.2.1",
        wire_bytes=len(body),
        elapsed_seconds=0.25,
        robots_tags=robots_tags,
    )


def error(kind: FailureKind, status: int | None = None) -> FetchError:
    return FetchError(kind, f"{kind.value} happened", url=SEED, http_status=status)


FETCHED = {
    FailureKind.HTTP_ERROR: "http_error",
    FailureKind.UNSUPPORTED_CONTENT_TYPE: "non_html",
    FailureKind.TOO_LARGE: "too_large",
    FailureKind.UNSUPPORTED_CONTENT_ENCODING: "unsupported_content_encoding",
    FailureKind.INVALID_REDIRECT: "invalid_redirect",
}
SKIPPED = {FailureKind.TARGET_NOT_ALLOWED, FailureKind.BLOCKED_DESTINATION}


@pytest.mark.parametrize("kind", list(FailureKind))
def test_every_fetch_failure_has_a_terminal_outcome(kind: FailureKind) -> None:
    result = failure_result(error(kind, 503))
    if kind in FETCHED:
        assert (result.state, result.outcome) == (UrlState.FETCHED, FETCHED[kind])
    elif kind in SKIPPED:
        assert (result.state, result.outcome) == (UrlState.SKIPPED, kind.value)
    else:
        assert (result.state, result.outcome) == (UrlState.FAILED, kind.value)
    assert result.http_status == 503
    assert result.error == f"{kind.value} happened"
    assert result.page is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://docs.example.com/x", True),
        ("http://docs.example.com/x", True),  # the job's scope is the host, not the scheme
        ("https://www.example.org/", False),  # an approved target, but outside the job
        ("https://docs.example.com:8443/", False),  # the job's host on an unapproved port
        ("https://other.test/", False),
    ],
)
def test_the_job_scope(url: str, expected: bool) -> None:
    ctx = context(FakeFetcher(response()))
    assert in_job_scope(ctx, lease(), normalize_url(url)) is expected


async def test_an_unapproved_target_is_skipped_without_any_request() -> None:
    fetcher, robots = FakeFetcher(response()), FakeRobots()
    result = await crawl_url(context(fetcher, robots, hosts=("other.test",)), lease())
    assert (result.state, result.outcome) == (UrlState.SKIPPED, "target_not_allowed")
    assert (fetcher.calls, robots.checked) == ([], [])


@pytest.mark.parametrize(
    ("decision", "outcome", "error_text"),
    [
        (
            RobotsDecision(False, RobotsStatus.PARSED, None, None),
            "robots_disallowed",
            "disallowed by robots.txt",
        ),
        (
            RobotsDecision(
                False, RobotsStatus.UNREACHABLE, None, "robots.txt unreachable (HTTP 503)"
            ),
            "robots_unreachable",
            "robots.txt unreachable (HTTP 503)",
        ),
    ],
)
async def test_robots_txt_refusals_skip_the_url_without_fetching_it(
    decision: RobotsDecision, outcome: str, error_text: str
) -> None:
    fetcher = FakeFetcher(response())
    result = await crawl_url(context(fetcher, FakeRobots(decision)), lease())
    assert (result.state, result.outcome, result.error) == (UrlState.SKIPPED, outcome, error_text)
    assert fetcher.calls == []


async def test_no_politeness_slot_for_robots_txt_fails_the_url() -> None:
    robots = FakeRobots(error(FailureKind.RATE_LIMITED))
    fetcher = FakeFetcher(response())
    result = await crawl_url(context(fetcher, robots), lease())
    assert (result.state, result.outcome) == (UrlState.FAILED, "rate_limited")
    assert fetcher.calls == []


async def test_a_fetch_failure_becomes_the_result() -> None:
    fetcher = FakeFetcher(error(FailureKind.HTTP_ERROR, 404))
    result = await crawl_url(context(fetcher), lease())
    assert (result.state, result.outcome, result.http_status) == (
        UrlState.FETCHED,
        "http_error",
        404,
    )
    assert fetcher.calls == [(SEED, 0)]  # redirects are returned, never followed


@pytest.mark.parametrize(
    ("location", "follow"),
    [
        ("https://docs.example.com/new", True),
        ("http://docs.example.com/new", True),
        ("https://www.example.org/elsewhere", False),
        ("https://unapproved.test/", False),
    ],
)
async def test_a_redirect_is_recorded_with_its_target(location: str, follow: bool) -> None:
    fetcher = FakeFetcher(response(status=301, location=location))
    result = await crawl_url(context(fetcher), lease())
    assert (result.state, result.outcome, result.http_status) == (UrlState.FETCHED, "redirect", 301)
    assert (result.redirect_target, result.follow_redirect) == (location, follow)
    assert result.page is None


async def test_an_html_page_is_parsed_into_a_page_record() -> None:
    body = (
        b"<html><head><title>Guide</title><meta name='description' content='About'>"
        b"<meta name='robots' content='nofollow'></head><body><h1>Intro</h1><p>Text</p>"
        b"<a href='/guide/next'>Next</a>"
        b"<a href='https://www.example.org/x' rel='nofollow'>Other</a>"
        b"<a href='https://docs.example.com:8443/'>Port</a></body></html>"
    )
    fetcher = FakeFetcher(response(body, robots_tags=("otherbot: noindex", "noindex")))
    result = await crawl_url(context(fetcher), lease())
    assert (result.state, result.outcome, result.http_status) == (
        UrlState.FETCHED,
        "html_stored",
        200,
    )
    page = result.page
    assert page is not None
    assert (page.title, page.description, page.headings) == ("Guide", "About", "Intro")
    assert (page.noindex, page.nofollow) == (True, True)
    assert (page.byte_size, page.fetch_duration_ms, page.content_type) == (
        len(body),
        250,
        "text/html",
    )
    assert page.links == (
        LinkRecord("https://docs.example.com/guide/next", 0, "Next", False, True),
        LinkRecord("https://www.example.org/x", 1, "Other", True, False),
        LinkRecord("https://docs.example.com:8443/", 2, "Port", False, False),
    )


async def test_a_parser_failure_fails_only_this_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(crawl_task, "parse_html", broken)
    result = await crawl_url(context(FakeFetcher(response(b"<p>x</p>"))), lease())
    assert (result.state, result.outcome, result.http_status) == (
        UrlState.FAILED,
        "parse_error",
        200,
    )
    assert html_parser.parse_html is not broken  # only the task's reference was replaced
