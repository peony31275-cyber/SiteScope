"""Response bodies of the website-analysis endpoints (Phase 8), and their listing cursors.

The terms (pages, URLs, links, broken and unverified links) are defined in ADR-0019 and in
docs/api-conventions.md; the field descriptions below repeat what a client needs.

A cursor is an opaque, URL-safe string: the base64 of a JSON array naming its listing and the
last item's sort key. Its URLs are normalized URLs the client has already received; a cursor of
one listing is rejected by another (``invalid_cursor``).
"""

import base64
import json
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.crawl_schemas import CrawlIndexing, UrlStateCounts
from app.core.errors import FieldViolation, InvalidInputError
from app.db.models import JobStatus, UrlState
from app.services.site_analysis import (
    BrokenLinkListing,
    BrokenLinkPosition,
    CrawlStats,
    GraphNode,
    LinkClass,
    LinkGraph,
    LinkListing,
    LinkPosition,
    LinkView,
    PageIndexStatus,
    PageListing,
    PagePosition,
    PageSummary,
)

# A cursor holds at most two normalized URLs (2048 bytes each) and its JSON and base64 framing.
MAX_CURSOR_CHARS: Final = 8192
_MAX_URL_CHARS: Final = 2048
_MAX_INTEGER: Final = 2**31 - 1  # the sort keys are PostgreSQL integers

_CLASS_DESCRIPTION: Final = (
    "Why the link is or is not known to work, from the stored crawl results of this job only "
    "(never a new request). `ok`, `broken` (404 or 410), `server_error` (5xx, 408, 429), "
    "`http_error` (another status, such as 401 or 403) and `redirect_error` (a redirect loop, "
    "or a 3xx without a usable Location) are decided by an HTTP response: *verified*. "
    "`out_of_scope`, `not_followed` (nofollow), `beyond_limits` (max_depth or max_pages), "
    "`not_crawled` (not requested, or not finished, yet), `robots_blocked` and `unreachable` "
    "(no response: DNS, connection, TLS, timeouts) are *unverified*. A redirect is judged by "
    "where its chain ends. See ADR-0019."
)


# --- Pages ---------------------------------------------------------------------------------------


class PageItem(BaseModel):
    """A stored HTML page. Page bodies are never returned."""

    page_id: uuid.UUID
    url: str = Field(description="The page's normalized URL.")
    title: str | None
    description: str | None = Field(description="The page's meta description.")
    http_status: int
    content_type: str | None
    depth: int = Field(
        description="BFS depth: link hops from the seed. A redirect target has the depth of "
        "the URL that redirected to it."
    )
    crawled_at: datetime = Field(description="When the page was fetched.")
    noindex: bool = Field(
        description="The page asked not to be indexed (meta robots or X-Robots-Tag); it is "
        "never searchable."
    )
    nofollow: bool = Field(
        description="The page asked that its links not be followed; they are recorded, never "
        "crawled."
    )
    index_status: PageIndexStatus = Field(
        description="`pending` (waiting to be published), `queued` (published to the "
        "indexers, not indexed yet), "
        "`indexed`, `failed` (could not be indexed), or `noindex` (never indexed). A page "
        "whose content changed after indexing is `pending` again."
    )
    indexed_at: datetime | None = Field(
        description="When the page's search vector was last built; null if never."
    )

    @classmethod
    def from_summary(cls, page: PageSummary) -> Self:
        return cls(
            page_id=page.page_id,
            url=page.url,
            title=page.title,
            description=page.description,
            http_status=page.http_status,
            content_type=page.content_type,
            depth=page.depth,
            crawled_at=page.fetched_at,
            noindex=page.noindex,
            nofollow=page.nofollow,
            index_status=page.index_status,
            indexed_at=page.indexed_at,
        )


class PageList(BaseModel):
    """One page of a job's stored pages, in BFS order: depth, then URL."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "items": [
                        {
                            "page_id": "0199a1b2-8d4e-7f50-9b0c-1d2e3f4a5b6c",
                            "url": "https://docs.example.com/",
                            "title": "Example documentation",
                            "description": "Guides and reference for Example.",
                            "http_status": 200,
                            "content_type": "text/html; charset=utf-8",
                            "depth": 0,
                            "crawled_at": "2026-11-07T10:15:01.204Z",
                            "noindex": False,
                            "nofollow": False,
                            "index_status": "indexed",
                            "indexed_at": "2026-11-07T10:15:02.877Z",
                        }
                    ],
                    "next_cursor": "WyJwYWdlcyIsMCwiaHR0cHM6Ly9kb2NzLmV4YW1wbGUuY29tLyJd",
                }
            ]
        }
    )

    items: list[PageItem]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page."
    )

    @classmethod
    def from_listing(cls, listing: PageListing) -> Self:
        position = listing.next_position
        return cls(
            items=[PageItem.from_summary(page) for page in listing.pages],
            next_cursor=_encode("pages", position.depth, position.url) if position else None,
        )


# --- Links ---------------------------------------------------------------------------------------


class LinkItem(BaseModel):
    """A stored link: one per source page and target URL (repeated links on a page are
    stored once, at their first position)."""

    source_page_id: uuid.UUID
    source_url: str
    target_url: str = Field(description="The normalized target URL.")
    anchor_text: str | None
    position: int = Field(description="0-based order of the link's first occurrence on its page.")
    internal: bool = Field(
        description="The target is in the job's scope: one of its hosts, and an approved "
        "crawl target. External links are recorded, never crawled."
    )
    nofollow: bool = Field(description='The link has `rel="nofollow"`.')
    classification: LinkClass = Field(description=_CLASS_DESCRIPTION)
    verified: bool = Field(description="An HTTP response decided `classification`.")
    target_crawled: bool = Field(
        description="The crawl attempted the target URL itself and its result is final "
        "(`target_state` is `fetched` or `failed`)."
    )
    target_state: UrlState | None = Field(
        description="The target URL's crawl state in this job; null if the job never admitted it."
    )
    target_http_status: int | None = Field(
        description="The target URL's own final response status (3xx if it redirected); null "
        "until its result is final, and when it got no response."
    )
    target_page_id: uuid.UUID | None = Field(description="The target's stored page, if any.")
    final_url: str | None = Field(
        description="Where the target's redirects end: the first URL that did not redirect, "
        "or a redirect target the job never requested; for an unusable chain (a loop, or more "
        "than 20 redirects), the last URL followed. Null if the target did not redirect to "
        "another URL (a target redirecting to itself is a `redirect_error` without one)."
    )
    final_http_status: int | None = Field(
        description="The final URL's response status; null if it was never requested or got "
        "no response."
    )
    redirect_hops: int = Field(description="Redirects from the target to `final_url`.")
    checked_at: datetime | None = Field(
        description="When the result that decides `classification` was recorded; null if "
        "there is none."
    )

    @classmethod
    def from_view(cls, link: LinkView) -> Self:
        return cls(
            source_page_id=link.source_page_id,
            source_url=link.source_url,
            target_url=link.target_url,
            anchor_text=link.anchor_text,
            position=link.position,
            internal=link.is_internal,
            nofollow=link.nofollow,
            classification=link.classification,
            verified=link.verified,
            target_crawled=link.target_crawled,
            target_state=link.target_state,
            target_http_status=link.target_http_status,
            target_page_id=link.target_page_id,
            final_url=link.final_url,
            final_http_status=link.final_http_status,
            redirect_hops=link.redirect_hops,
            checked_at=link.checked_at,
        )


class LinkList(BaseModel):
    """One page of a job's stored links, by source page URL, then position on the page."""

    items: list[LinkItem]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page."
    )

    @classmethod
    def from_listing(cls, listing: LinkListing) -> Self:
        position = listing.next_position
        cursor = None
        if position is not None:
            cursor = _encode("links", position.source_url, position.position, position.target_url)
        return cls(items=[LinkItem.from_view(link) for link in listing.links], next_cursor=cursor)


class BrokenLinkItem(BaseModel):
    """A confirmed broken link: a stored internal link whose target, after its redirects,
    answered 404 Not Found or 410 Gone during this crawl."""

    source_page_id: uuid.UUID
    source_url: str
    anchor_text: str | None
    position: int = Field(description="0-based order of the link's first occurrence on its page.")
    target_url: str
    status: int = Field(description="The verified final status: 404 or 410.")
    final_url: str | None = Field(
        description="The URL that answered `status` when the target redirected to it; null if "
        "the target itself answered."
    )
    redirect_hops: int
    target_http_status: int = Field(
        description="The target URL's own status: `status`, or the 3xx of its first redirect."
    )
    checked_at: datetime = Field(description="When the crawl recorded the final response.")

    @classmethod
    def from_view(cls, link: LinkView) -> Self:
        status = link.final_http_status if link.final_url is not None else link.target_http_status
        if status is None or link.target_http_status is None or link.checked_at is None:
            raise ValueError("a broken link always has its final response")  # pragma: no cover
        return cls(
            source_page_id=link.source_page_id,
            source_url=link.source_url,
            anchor_text=link.anchor_text,
            position=link.position,
            target_url=link.target_url,
            status=status,
            final_url=link.final_url,
            redirect_hops=link.redirect_hops,
            target_http_status=link.target_http_status,
            checked_at=link.checked_at,
        )


class BrokenLinkList(BaseModel):
    """One page of a job's confirmed broken links, by target URL, then source page URL."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "total": 1,
                    "items": [
                        {
                            "source_page_id": "0199a1b2-8d4e-7f50-9b0c-1d2e3f4a5b6c",
                            "source_url": "https://docs.example.com/about.html",
                            "anchor_text": "Our old team page",
                            "position": 4,
                            "target_url": "https://docs.example.com/missing.html",
                            "status": 404,
                            "final_url": None,
                            "redirect_hops": 0,
                            "target_http_status": 404,
                            "checked_at": "2026-11-07T10:15:04.551Z",
                        }
                    ],
                    "next_cursor": None,
                }
            ]
        }
    )

    total: int = Field(description="Confirmed broken links of the job, on every page.")
    items: list[BrokenLinkItem]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page."
    )

    @classmethod
    def from_listing(cls, listing: BrokenLinkListing) -> Self:
        position = listing.next_position
        cursor = None
        if position is not None:
            cursor = _encode("broken-links", position.target_url, position.source_url)
        return cls(
            total=listing.total,
            items=[BrokenLinkItem.from_view(link) for link in listing.links],
            next_cursor=cursor,
        )


# --- The link graph ------------------------------------------------------------------------------


class NodeKind(StrEnum):
    PAGE = "page"  # a stored HTML page
    CRAWL_URL = "crawl_url"  # admitted by the job, without a stored page
    LINK_TARGET = "link_target"  # only known as the target of internal links


class GraphNodeItem(BaseModel):
    """A URL of the job's internal link graph."""

    id: int = Field(description="The node's index in `nodes`; edges refer to it.")
    url: str
    kind: NodeKind = Field(
        description="`page`: a stored HTML page. `crawl_url`: a URL the job admitted that has "
        "no stored page (not crawled yet, a redirect, an error, another content type, "
        "skipped). `link_target`: a target of internal links that the job never admitted; it "
        "was never requested."
    )
    page_id: uuid.UUID | None
    title: str | None
    depth: int | None = Field(description="BFS depth; null for a `link_target`.")
    crawl_state: UrlState | None = Field(description="Null for a `link_target`.")
    http_status: int | None = Field(
        description="The URL's own final response status; null until its result is final, "
        "and when it got no response."
    )
    in_degree: int = Field(
        description="Stored internal links to this URL, one per linking page. Only links on "
        "stored pages are known: 0 does not mean that nothing links to the URL."
    )
    out_degree: int | None = Field(
        description="Stored internal links on this page; null if the node is not a page."
    )

    @classmethod
    def from_node(cls, index: int, node: GraphNode) -> Self:
        if node.page_id is not None:
            kind = NodeKind.PAGE
        elif node.crawl_state is not None:
            kind = NodeKind.CRAWL_URL
        else:
            kind = NodeKind.LINK_TARGET
        return cls(
            id=index,
            url=node.url,
            kind=kind,
            page_id=node.page_id,
            title=node.title,
            depth=node.depth,
            crawl_state=node.crawl_state,
            http_status=node.http_status,
            in_degree=node.in_degree,
            out_degree=node.out_degree,
        )


class GraphEdgeItem(BaseModel):
    """A stored internal link between two returned nodes."""

    source: int = Field(description="The `id` of the linking page.")
    target: int = Field(description="The `id` of the target.")
    nofollow: bool


class LinkGraphResponse(BaseModel):
    """The job's internal link graph, possibly cut at `max_nodes` and `max_edges`."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "job_id": "0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b",
                    "nodes": [
                        {
                            "id": 0,
                            "url": "https://docs.example.com/",
                            "kind": "page",
                            "page_id": "0199a1b2-8d4e-7f50-9b0c-1d2e3f4a5b6c",
                            "title": "Example documentation",
                            "depth": 0,
                            "crawl_state": "fetched",
                            "http_status": 200,
                            "in_degree": 0,
                            "out_degree": 1,
                        },
                        {
                            "id": 1,
                            "url": "https://docs.example.com/missing.html",
                            "kind": "crawl_url",
                            "page_id": None,
                            "title": None,
                            "depth": 1,
                            "crawl_state": "fetched",
                            "http_status": 404,
                            "in_degree": 1,
                            "out_degree": None,
                        },
                    ],
                    "edges": [{"source": 0, "target": 1, "nofollow": False}],
                    "total_nodes": 2,
                    "total_edges": 1,
                    "max_nodes": 500,
                    "max_edges": 2000,
                    "truncated": False,
                }
            ]
        }
    )

    job_id: uuid.UUID
    nodes: list[GraphNodeItem] = Field(
        description="Every URL the job admitted and every target of an internal link, in BFS "
        "order: depth, then URL; never-admitted targets last. Cut at `max_nodes`."
    )
    edges: list[GraphEdgeItem] = Field(
        description="Internal links between returned nodes, by source URL and position. Cut "
        "at `max_edges`. Redirects are not edges."
    )
    total_nodes: int = Field(description="Nodes of the whole graph (`urls_discovered`).")
    total_edges: int = Field(description="Stored internal links of the job.")
    max_nodes: int
    max_edges: int
    truncated: bool = Field(
        description="Some nodes or edges were cut off. Degrees always count every stored link."
    )

    @classmethod
    def from_graph(cls, job_id: uuid.UUID, graph: LinkGraph) -> Self:
        return cls(
            job_id=job_id,
            nodes=[GraphNodeItem.from_node(index, node) for index, node in enumerate(graph.nodes)],
            edges=[
                GraphEdgeItem(source=edge.source, target=edge.target, nofollow=edge.nofollow)
                for edge in graph.edges
            ],
            total_nodes=graph.total_nodes,
            total_edges=graph.total_edges,
            max_nodes=graph.max_nodes,
            max_edges=graph.max_edges,
            truncated=graph.truncated,
        )


# --- Statistics ----------------------------------------------------------------------------------


class CrawlCounts(BaseModel):
    """The crawl's progress, from the job's URL rows."""

    urls_discovered: int = Field(
        description="Distinct URLs the job knows: every admitted URL, plus every target of an "
        "internal link that was never admitted."
    )
    urls_admitted: int = Field(
        description="URLs admitted to the crawl, seed included (the job's `admitted_count`)."
    )
    urls_by_state: UrlStateCounts = Field(
        description="Admitted URLs by crawl state. `fetched`: the response was recorded (any "
        "status); `failed`: the request ended without a response, or its response could not "
        "be used (for example a page that could not be parsed); `skipped`: never requested "
        "(robots.txt, not an approved target)."
    )
    pages_stored: int = Field(description="Stored HTML pages (2xx HTML responses).")
    current_depth: int = Field(description="The BFS level being crawled.")
    max_depth: int
    max_pages: int


class HttpStatusCounts(BaseModel):
    """Final response statuses of the URLs whose request ended (state `fetched` or `failed`),
    each URL counted once with its own status: a redirect counts as 3xx, not as its target's
    status. The six numbers add up to those URLs; skipped and unfinished URLs are not counted."""

    informational_1xx: int = Field(
        description="A 1xx status as the final response (for example a `101` the request did "
        "not ask for): rare, and an HTTP error for the crawl."
    )
    success_2xx: int
    redirect_3xx: int
    client_error_4xx: int
    server_error_5xx: int
    no_response: int = Field(
        description="Requests that ended without an HTTP response: DNS, connection, TLS or "
        "protocol errors, timeouts, attempts used up."
    )


class DepthCountItem(BaseModel):
    """The admitted URLs at one BFS depth."""

    depth: int
    urls: int = Field(description="URLs admitted at this depth.")
    fetched: int = Field(
        description="Of those, URLs in state `fetched` (their response was recorded)."
    )
    pages: int = Field(description="Of those, stored HTML pages.")


class LinkClassCounts(BaseModel):
    """Stored links by classification (see `classification` of the links endpoint)."""

    ok: int
    broken: int
    server_error: int
    http_error: int
    redirect_error: int
    out_of_scope: int
    not_followed: int
    beyond_limits: int
    not_crawled: int
    robots_blocked: int
    unreachable: int


class LinkCounts(BaseModel):
    """The job's stored links: one per source page and target URL."""

    total: int
    internal: int
    external: int
    internal_targets: int = Field(description="Distinct target URLs of internal links.")
    broken: int = Field(description="Confirmed broken links (404 or 410).")
    verified: int = Field(description="Links whose classification an HTTP response decided.")
    unverified: int = Field(description="All other links: never requested, or no response.")
    by_class: LinkClassCounts


class MostLinkedItem(BaseModel):
    url: str
    page_id: uuid.UUID | None = Field(description="The URL's stored page; null if none.")
    in_links: int = Field(description="Stored internal links to the URL (one per page).")


class CrawlStatsResponse(BaseModel):
    """Statistics of one crawl job, all counted in one snapshot of PostgreSQL."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "job_id": "0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b",
                    "status": "completed",
                    "crawl": {
                        "urls_discovered": 15,
                        "urls_admitted": 14,
                        "urls_by_state": {
                            "pending": 0,
                            "queued": 0,
                            "in_progress": 0,
                            "fetched": 13,
                            "failed": 0,
                            "skipped": 1,
                            "cancelled": 0,
                        },
                        "pages_stored": 12,
                        "current_depth": 3,
                        "max_depth": 3,
                        "max_pages": 100,
                    },
                    "http_status": {
                        "informational_1xx": 0,
                        "success_2xx": 12,
                        "redirect_3xx": 0,
                        "client_error_4xx": 1,
                        "server_error_5xx": 0,
                        "no_response": 0,
                    },
                    "depths": [
                        {"depth": 0, "urls": 1, "fetched": 1, "pages": 1},
                        {"depth": 1, "urls": 5, "fetched": 4, "pages": 4},
                    ],
                    "indexing": {
                        "status": "indexed",
                        "documents_total": 11,
                        "documents_by_status": {
                            "pending": 0,
                            "queued": 0,
                            "indexed": 11,
                            "failed": 0,
                        },
                        "active": 0,
                        "completed_at": "2026-11-07T10:15:05.120Z",
                    },
                    "links": {
                        "total": 40,
                        "internal": 38,
                        "external": 2,
                        "internal_targets": 14,
                        "broken": 1,
                        "verified": 34,
                        "unverified": 6,
                        "by_class": {
                            "ok": 33,
                            "broken": 1,
                            "server_error": 0,
                            "http_error": 0,
                            "redirect_error": 0,
                            "out_of_scope": 2,
                            "not_followed": 1,
                            "beyond_limits": 2,
                            "not_crawled": 0,
                            "robots_blocked": 1,
                            "unreachable": 0,
                        },
                    },
                    "most_linked": [
                        {
                            "url": "https://docs.example.com/",
                            "page_id": "0199a1b2-8d4e-7f50-9b0c-1d2e3f4a5b6c",
                            "in_links": 11,
                        }
                    ],
                }
            ]
        }
    )

    job_id: uuid.UUID
    status: JobStatus = Field(description="The crawl's status (see the job resource).")
    crawl: CrawlCounts
    http_status: HttpStatusCounts
    depths: list[DepthCountItem] = Field(
        description="Admitted URLs per BFS depth, from depth 0; depths without URLs are absent."
    )
    indexing: CrawlIndexing
    links: LinkCounts
    most_linked: list[MostLinkedItem] = Field(
        description="The 10 URLs with the most stored internal links to them, most first, then "
        "by URL. A URL need not be a stored page."
    )

    @classmethod
    def from_stats(cls, stats: CrawlStats) -> Self:
        return cls(
            job_id=stats.job_id,
            status=JobStatus(stats.status),
            crawl=CrawlCounts(
                urls_discovered=stats.urls_discovered,
                urls_admitted=stats.urls_admitted,
                urls_by_state=UrlStateCounts.from_counts(stats.urls_by_state),
                pages_stored=stats.pages_stored,
                current_depth=stats.current_level,
                max_depth=stats.max_depth,
                max_pages=stats.max_pages,
            ),
            http_status=HttpStatusCounts(
                informational_1xx=stats.status_1xx,
                success_2xx=stats.status_2xx,
                redirect_3xx=stats.status_3xx,
                client_error_4xx=stats.status_4xx,
                server_error_5xx=stats.status_5xx,
                no_response=stats.no_response,
            ),
            depths=[
                DepthCountItem(depth=row.depth, urls=row.urls, fetched=row.fetched, pages=row.pages)
                for row in stats.depths
            ],
            indexing=CrawlIndexing.from_progress(stats.indexing),
            links=LinkCounts(
                total=stats.links_total,
                internal=stats.links_internal,
                external=stats.links_external,
                internal_targets=stats.internal_targets,
                broken=stats.links_broken,
                verified=stats.links_verified,
                unverified=stats.links_unverified,
                by_class=LinkClassCounts(
                    **{item.value: count for item, count in stats.links_by_class.items()}
                ),
            ),
            most_linked=[
                MostLinkedItem(url=top.url, page_id=top.page_id, in_links=top.in_links)
                for top in stats.top_linked
            ],
        )


# --- Cursors -------------------------------------------------------------------------------------


def decode_page_cursor(cursor: str) -> PagePosition:
    depth, url = _decode(cursor, "pages", (int, str))
    return PagePosition(depth=depth, url=url)


def decode_link_cursor(cursor: str) -> LinkPosition:
    source_url, position, target_url = _decode(cursor, "links", (str, int, str))
    return LinkPosition(source_url=source_url, position=position, target_url=target_url)


def decode_broken_link_cursor(cursor: str) -> BrokenLinkPosition:
    target_url, source_url = _decode(cursor, "broken-links", (str, str))
    return BrokenLinkPosition(target_url=target_url, source_url=source_url)


def _encode(kind: str, *key: str | int) -> str:
    raw = json.dumps([kind, *key], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _decode(cursor: str, kind: str, shape: tuple[type, ...]) -> list[Any]:
    """The sort key in a cursor from ``_encode(kind, ...)``, checked against ``shape``: integers
    within PostgreSQL's integer range, strings of printable ASCII (as normalized URLs are)."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii"))
        if not isinstance(value, list) or len(value) != len(shape) + 1 or value[0] != kind:
            raise ValueError("not a cursor of this listing")
        key = value[1:]
        for item, expected in zip(key, shape, strict=True):
            if type(item) is not expected:  # bool is an int subclass: compare types exactly
                raise ValueError("unexpected cursor value")
            if isinstance(item, int) and not 0 <= item <= _MAX_INTEGER:
                raise ValueError("cursor integer out of range")
            if isinstance(item, str) and not (
                len(item) <= _MAX_URL_CHARS and item.isascii() and item.isprintable()
            ):
                raise ValueError("cursor text is not a normalized URL")
        return key
    # ValueError includes binascii.Error, UnicodeError and json.JSONDecodeError; deeply nested
    # JSON raises RecursionError.
    except (ValueError, RecursionError):
        raise InvalidInputError(
            [
                FieldViolation(
                    ("query", "cursor"),
                    "cursor is not valid for this listing; use the next_cursor value of a "
                    "previous page",
                    "invalid_cursor",
                )
            ]
        ) from None
