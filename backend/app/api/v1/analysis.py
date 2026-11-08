"""Website-analysis endpoints of one crawl job: pages, links, broken links, the link graph and
statistics (Phase 8, ADR-0019).

HTTP concerns only: parameters, cursors and response shapes. The queries, their snapshot and
their time limit are in ``app/services/site_analysis.py``. Every endpoint is read-only and
reads stored crawl results from PostgreSQL; none sends a request to a crawl target.
"""

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Path, Query

from app.api.dependencies import SessionDep, SettingsDep
from app.api.problems import problem_responses
from app.api.v1.analysis_schemas import (
    MAX_CURSOR_CHARS,
    BrokenLinkList,
    CrawlStatsResponse,
    LinkGraphResponse,
    LinkList,
    PageList,
    decode_broken_link_cursor,
    decode_link_cursor,
    decode_page_cursor,
)
from app.services import site_analysis
from app.services.site_analysis import LinkClass, PageIndexStatus

router = APIRouter(prefix="/crawls/{job_id}", tags=["analysis"])

DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 200
DEFAULT_MAX_NODES: Final = 500
MAX_NODES: Final = 2000
DEFAULT_MAX_EDGES: Final = 2000
MAX_EDGES: Final = 10_000
MAX_DEPTH_FILTER: Final = 1000  # above any max_depth a job may have

_PROBLEMS: Final = problem_responses(
    404,
    422,
    503,
    descriptions={
        404: "The crawl job does not exist (`not_found`).",
        422: "The request failed validation (`validation_failed`); `errors` lists each field "
        "with its `type`, for example `invalid_cursor` (a cursor of another listing, or "
        "altered) or `uuid_parsing` (`job_id`).",
        503: "The analysis took longer than SITESCOPE_ANALYSIS_TIMEOUT_SECONDS and was stopped "
        "(`analysis_timeout`), or PostgreSQL is unavailable (`dependency_unavailable`, with "
        "`Retry-After`).",
    },
)

JobId = Annotated[uuid.UUID, Path(description="The crawl job.")]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="Items per page.")]
Cursor = Annotated[
    str | None,
    Query(
        max_length=MAX_CURSOR_CHARS,
        description="The `next_cursor` of the previous page of the same listing, with the same "
        "filters.",
    ),
]


@router.get("/pages", summary="List the pages of a crawl job", responses=_PROBLEMS)
async def list_crawl_pages(
    job_id: JobId,
    session: SessionDep,
    settings: SettingsDep,
    limit: PageSize = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
    depth: Annotated[
        int | None,
        Query(ge=0, le=MAX_DEPTH_FILTER, description="Only pages at this BFS depth."),
    ] = None,
    index_status: Annotated[
        PageIndexStatus | None, Query(description="Only pages with this indexing status.")
    ] = None,
) -> PageList:
    """List the job's stored HTML pages in BFS order (depth, then URL), with cursor paging.

    A *page* is an HTML document the crawl fetched (2xx) and stored. Other crawled URLs
    (redirects, errors, other content types) are not pages; the links and statistics endpoints
    report them. Page bodies are never returned.
    """
    after = decode_page_cursor(cursor) if cursor is not None else None
    listing = await site_analysis.list_pages(
        session,
        job_id,
        limit=limit,
        after=after,
        depth=depth,
        index_status=index_status,
        timeout_seconds=settings.analysis_timeout_seconds,
    )
    return PageList.from_listing(listing)


@router.get("/links", summary="List the links of a crawl job", responses=_PROBLEMS)
async def list_crawl_links(
    job_id: JobId,
    session: SessionDep,
    settings: SettingsDep,
    limit: PageSize = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
    source_page_id: Annotated[
        uuid.UUID | None, Query(description="Only links on this page of the job.")
    ] = None,
    internal: Annotated[
        bool | None, Query(description="Only internal (`true`) or external (`false`) links.")
    ] = None,
    classification: Annotated[
        LinkClass | None, Query(description="Only links with this classification.")
    ] = None,
    verified: Annotated[
        bool | None,
        Query(
            description="Only links whose classification an HTTP response decided (`true`), or "
            "only the unverified ones (`false`: never requested, or no response)."
        ),
    ] = None,
    redirected: Annotated[
        bool | None,
        Query(
            description="Only links whose target itself answered with a redirect, a 3xx "
            "(`true`), or only the others (`false`)."
        ),
    ] = None,
) -> LinkList:
    """List the job's stored links, by source page URL and position, with their classification.

    Every link found on a stored page is recorded, internal or not, followed or not. A link's
    classification comes from this job's stored crawl result for its target (after the
    target's redirects); a target that was never requested is never reported as working or
    broken. Nothing is fetched to check a link.
    """
    after = decode_link_cursor(cursor) if cursor is not None else None
    listing = await site_analysis.list_links(
        session,
        job_id,
        limit=limit,
        after=after,
        source_page_id=source_page_id,
        internal=internal,
        classification=classification,
        verified=verified,
        redirected=redirected,
        timeout_seconds=settings.analysis_timeout_seconds,
    )
    return LinkList.from_listing(listing)


@router.get(
    "/broken-links", summary="List the confirmed broken links of a crawl job", responses=_PROBLEMS
)
async def list_crawl_broken_links(
    job_id: JobId,
    session: SessionDep,
    settings: SettingsDep,
    limit: PageSize = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> BrokenLinkList:
    """List the job's confirmed broken links, by target URL, then source page URL.

    A link is *confirmed broken* when it is internal and its target, after following its
    redirects through this job's crawl results, answered 404 Not Found or 410 Gone during this
    crawl. Nothing else is: not a 5xx, 408 or 429 (possibly transient), not another 4xx such as
    401 or 403, not a link the crawl never requested (out of scope, nofollow, beyond the depth
    or page limits, blocked by robots.txt, not crawled yet), and not one that got no response.
    Those are in the links listing, by classification.

    One item per stored link: a page that links to a missing URL twice is listed once; a URL
    linked from three pages is listed three times. `total` counts every item.
    """
    after = decode_broken_link_cursor(cursor) if cursor is not None else None
    listing = await site_analysis.list_broken_links(
        session,
        job_id,
        limit=limit,
        after=after,
        timeout_seconds=settings.analysis_timeout_seconds,
    )
    return BrokenLinkList.from_listing(listing)


@router.get(
    "/link-graph", summary="Get the internal link graph of a crawl job", responses=_PROBLEMS
)
async def get_crawl_link_graph(
    job_id: JobId,
    session: SessionDep,
    settings: SettingsDep,
    max_nodes: Annotated[
        int, Query(ge=1, le=MAX_NODES, description="Most nodes to return.")
    ] = DEFAULT_MAX_NODES,
    max_edges: Annotated[
        int, Query(ge=0, le=MAX_EDGES, description="Most edges to return.")
    ] = DEFAULT_MAX_EDGES,
) -> LinkGraphResponse:
    """Return the job's internal link graph: its known URLs, and the stored internal links
    between them.

    Nodes are every URL the job admitted (pages, and URLs without a page) and every target of
    an internal link the job never admitted, in BFS order; `kind` tells them apart. The graph
    is cut at `max_nodes` and `max_edges` in a stable order, and `truncated` says so.

    The graph holds only what the crawl stored: links on pages it did not fetch are unknown, so
    a node without inbound links is not necessarily an orphan page.
    """
    graph = await site_analysis.link_graph(
        session,
        job_id,
        max_nodes=max_nodes,
        max_edges=max_edges,
        timeout_seconds=settings.analysis_timeout_seconds,
    )
    return LinkGraphResponse.from_graph(job_id, graph)


@router.get("/stats", summary="Get the statistics of a crawl job", responses=_PROBLEMS)
async def get_crawl_stats(
    job_id: JobId, session: SessionDep, settings: SettingsDep
) -> CrawlStatsResponse:
    """Return the job's crawl, HTTP status, depth, indexing and link statistics.

    Every number is counted from stored rows in one snapshot of PostgreSQL: nothing is
    estimated, and the numbers agree with each other and with the listings at that moment.
    """
    stats = await site_analysis.crawl_stats(
        session, job_id, timeout_seconds=settings.analysis_timeout_seconds
    )
    return CrawlStatsResponse.from_stats(stats)
