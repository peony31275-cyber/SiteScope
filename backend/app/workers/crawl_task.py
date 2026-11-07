"""One crawl task: what happens to a leased URL between its claim and its fenced result.

``crawl_url`` runs the per-URL pipeline and returns a ``UrlResult``. It writes nothing; the
caller records the result under its lease (``services.crawl_state.record_result``). The
single-process driver uses it now, and the distributed workers will in Phase 5.

1. **Scope.** The deployment's approved targets (syntactic). The safe fetcher checks them again
   before every request.
2. **robots.txt** of the URL's origin (``RobotsPolicy``, RFC 9309). A disallowed URL, or any URL
   of an origin whose robots.txt is unreachable, is ``skipped`` without a request.
3. **One request** through the safe fetcher, with redirects returned instead of followed
   (``max_redirects=0``; architecture section 8.5). The politeness gate spaces requests per
   host, and the fetcher validates DNS answers and pins the connection.
4. **A redirect** is ``fetched`` with outcome ``redirect`` and its target. The result
   transaction admits the target at the same depth when it is in the job's scope (the seed's
   host, and an approved target) and the hop limit and the budget allow. The target then goes
   through this whole pipeline itself, so a redirect bypasses no check.
5. **An HTML page** is parsed in a worker thread (``asyncio.to_thread``). Its meta robots tags
   and ``X-Robots-Tag`` headers decide ``noindex`` and ``nofollow``. Every link is recorded,
   marked internal if it is in the job's scope; the scheduler admits links later (ADR-0004).

Outcomes (``crawl_urls.state`` / ``outcome``):

=====================================================  =========  ==============================
Result                                                 State      Outcome
=====================================================  =========  ==============================
2xx HTML                                               fetched    ``html_stored``
3xx with a usable Location                             fetched    ``redirect``
other HTTP status                                      fetched    ``http_error``
2xx that is not HTML; too large; unknown coding;       fetched    ``non_html``, ``too_large``,
3xx without a usable Location                                     ``unsupported_content_encoding``,
                                                                  ``invalid_redirect``
not an approved target; blocked address                skipped    ``target_not_allowed``,
                                                                  ``blocked_destination``
robots.txt disallows; robots.txt unreachable           skipped    ``robots_disallowed``,
                                                                  ``robots_unreachable``
network, TLS and protocol failures; no politeness      failed     the ``FailureKind`` value, e.g.
slot in time; the task deadline; a parser failure                 ``read_timeout``,
                                                                  ``rate_limited``,
                                                                  ``deadline_exceeded``,
                                                                  ``parse_error``
=====================================================  =========  ==============================

Retryable failures (timeouts, connection errors, HTTP 429 and 5xx, ``rate_limited``) are
terminal in Phase 4. Durable retries with backoff arrive in Phase 6.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Final, Protocol

from app.crawler.directives import page_directives
from app.crawler.errors import FailureKind, FetchError
from app.crawler.html_parser import ParseLimits, parse_html
from app.crawler.results import FetchResult
from app.crawler.robots import RobotsDecision, RobotsStatus
from app.crawler.scope import CrawlTargetPolicy, TargetNotAllowedError
from app.crawler.urls import InvalidUrlError, NormalizedUrl, normalize_url
from app.db.models import UrlState
from app.services.crawl_state import Lease, LinkRecord, PageRecord, UrlResult

logger = logging.getLogger(__name__)

# A response was received: the URL is "fetched", with one of these outcomes.
_FETCHED_FAILURES: Final = {
    FailureKind.HTTP_ERROR: "http_error",
    FailureKind.UNSUPPORTED_CONTENT_TYPE: "non_html",
    FailureKind.TOO_LARGE: "too_large",
    FailureKind.UNSUPPORTED_CONTENT_ENCODING: "unsupported_content_encoding",
    FailureKind.INVALID_REDIRECT: "invalid_redirect",
}
# Policy refusals: no request was sent.
_SKIPPED_FAILURES: Final = frozenset(
    {FailureKind.TARGET_NOT_ALLOWED, FailureKind.BLOCKED_DESTINATION}
)
PARSE_ERROR: Final = "parse_error"


class PageFetcher(Protocol):
    """What a task needs from ``fetcher.SafeFetcher``."""

    async def fetch(self, url: str, *, max_redirects: int | None = None) -> FetchResult: ...


class RobotsChecker(Protocol):
    """What a task needs from ``robots.RobotsPolicy``."""

    async def check(self, url: NormalizedUrl) -> RobotsDecision: ...


@dataclass(frozen=True, slots=True)
class CrawlContext:
    """What every crawl task of a process shares."""

    fetcher: PageFetcher  # the safe fetcher, with the politeness gate
    robots: RobotsChecker  # the robots.txt policy
    targets: CrawlTargetPolicy  # the deployment's approved crawl targets
    parse_limits: ParseLimits
    crawler_name: str


async def crawl_url(context: CrawlContext, lease: Lease) -> UrlResult:
    """Process one leased URL and return its result. See the module docstring.

    Raises only for infrastructure failures (for example Redis, while reading the robots.txt
    cache); the caller then leaves the lease to expire.
    """
    try:
        url = normalize_url(lease.url)
    except InvalidUrlError as exc:  # stored URLs are normalized (a CHECK constraint)
        return UrlResult(UrlState.FAILED, FailureKind.INVALID_URL.value, error=str(exc))
    try:
        context.targets.check(url)
    except TargetNotAllowedError as exc:
        return UrlResult(UrlState.SKIPPED, FailureKind.TARGET_NOT_ALLOWED.value, error=str(exc))

    try:
        decision = await context.robots.check(url)
    except FetchError as exc:  # rate_limited while fetching robots.txt; nothing was cached
        return failure_result(exc)
    if not decision.allowed:
        if decision.status is RobotsStatus.UNREACHABLE:
            return UrlResult(UrlState.SKIPPED, "robots_unreachable", error=decision.detail)
        return UrlResult(UrlState.SKIPPED, "robots_disallowed", error="disallowed by robots.txt")

    try:
        response = await context.fetcher.fetch(url.url, max_redirects=0)
    except FetchError as exc:
        return failure_result(exc)
    if response.redirect_location is not None:
        return _redirect_result(context, lease, response, response.redirect_location)
    return await _page_result(context, lease, response)


def failure_result(error: FetchError) -> UrlResult:
    """The terminal result of a failed fetch (see the outcome table in the module docstring)."""
    if error.kind in _FETCHED_FAILURES:
        return UrlResult(
            UrlState.FETCHED,
            _FETCHED_FAILURES[error.kind],
            http_status=error.http_status,
            error=error.message,
        )
    state = UrlState.SKIPPED if error.kind in _SKIPPED_FAILURES else UrlState.FAILED
    return UrlResult(state, error.kind.value, http_status=error.http_status, error=error.message)


def in_job_scope(context: CrawlContext, lease: Lease, url: NormalizedUrl) -> bool:
    """The job's scope: the seed's host (``allowed_hosts``), and an approved crawl target."""
    if url.host not in lease.allowed_hosts:
        return False
    try:
        context.targets.check(url)
    except TargetNotAllowedError:
        return False
    return True


def _redirect_result(
    context: CrawlContext, lease: Lease, response: FetchResult, location: str
) -> UrlResult:
    target = normalize_url(location)  # already normalized by the fetcher
    return UrlResult(
        UrlState.FETCHED,
        "redirect",
        http_status=response.status_code,
        redirect_target=target.url,
        follow_redirect=in_job_scope(context, lease, target),
    )


async def _page_result(context: CrawlContext, lease: Lease, response: FetchResult) -> UrlResult:
    try:
        # BeautifulSoup builds the whole tree: CPU-bound, so off the event loop.
        page = await asyncio.to_thread(
            parse_html,
            response.body,
            url=response.url,
            declared_charset=response.charset,
            limits=context.parse_limits,
            crawler_name=context.crawler_name,
        )
    except Exception:  # hostile markup must fail one URL, never the whole crawl
        logger.exception(
            "Could not parse a fetched page",
            extra={"event": "task.parse_failed", "url_id": str(lease.url_id)},
        )
        return UrlResult(
            UrlState.FAILED,
            PARSE_ERROR,
            http_status=response.status_code,
            error="the page could not be parsed",
        )
    directives = page_directives(
        page.robots, response.robots_tags, crawler_name=context.crawler_name
    )
    links = tuple(
        LinkRecord(
            url=link.url,
            position=link.position,
            anchor_text=link.anchor_text,
            nofollow=link.nofollow,
            is_internal=in_job_scope(context, lease, normalize_url(link.url)),
        )
        for link in page.links
    )
    return UrlResult(
        UrlState.FETCHED,
        "html_stored",
        http_status=response.status_code,
        page=PageRecord(
            http_status=response.status_code,
            content_type=response.content_type,
            title=page.title,
            description=page.description,
            headings=page.headings,
            text=page.text,
            byte_size=len(response.body),
            fetch_duration_ms=round(response.elapsed_seconds * 1000),
            noindex=directives.noindex,
            nofollow=directives.nofollow,
            links=links,
        ),
    )
