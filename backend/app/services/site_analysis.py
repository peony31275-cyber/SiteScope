"""Read-only analysis of one crawl job: pages, links, broken links, the link graph, statistics.

Phase 8 (ADR-0019). Everything here is computed from stored crawl results in PostgreSQL; nothing
sends a request to a crawl target. Each use case runs one read-only transaction at REPEATABLE
READ, so the lists and counts it returns come from one snapshot, and PostgreSQL stops its
statements after ``timeout_seconds`` (``AnalysisTimeoutError``).

**Link classification.** Every stored link (``page_links``) gets exactly one class, from the
crawl result of its target in the same job (ADR-0019). A target that redirected is judged by
the end of its redirect chain; the chain is followed through the job's own crawl results, never
fetched again. Only a terminal result (fetched, failed, skipped) is a result: a URL waiting for
a retry keeps its last attempt's status, which decides nothing.

==================  ==========================================================================
``out_of_scope``    the link is not internal (another host, or not an approved target), or the
                    target was skipped as not approved or as a blocked destination, or its
                    redirect chain leaves the job's hosts: never requested
``not_followed``    internal, never admitted: the link is ``rel="nofollow"``, or its page is
                    ``nofollow`` (meta robots or ``X-Robots-Tag``)
``beyond_limits``   internal, never admitted (the target, or the end of its redirect chain):
                    one level beyond ``max_depth``, or the job's page budget was used up
``not_crawled``     internal, not requested (yet): pending, queued, in progress or cancelled,
                    or never admitted for a reason the stored results do not record (the job is
                    still running or ended first, the redirect hop limit, a redirect to a URL
                    of the job's host that is not an approved target)
``robots_blocked``  robots.txt disallowed the target, or could not be fetched: never requested
``redirect_error``  the target redirects, but the chain is unusable: a loop, more than
                    ``MAX_REDIRECT_HOPS`` redirects, or a 3xx without a usable ``Location``
``ok``              the final response was 2xx
``broken``          the final response was 404 or 410: the only *confirmed broken* links
``server_error``    the final response was 5xx, 408 or 429, after the crawl's retries: possibly
                    transient
``http_error``      any other final response status (for example 401 or 403): refused, which
                    does not prove the page is missing
``unreachable``     the request ended without a response status: DNS, connection, TLS or
                    protocol errors, timeouts, no politeness slot, the task deadline, attempts
                    used up by crashes or by a poison task message
==================  ==========================================================================

A link is **verified** when an HTTP response decides its class (``ok``, ``broken``,
``server_error``, ``http_error``, ``redirect_error``), and **unverified** otherwise.

**Cost.** The link queries read every link and every crawl URL of the job (the redirect chains
are resolved over the whole job); the job's page budget bounds them (``max_pages``, at most
``SITESCOPE_CRAWL_MAX_PAGES_LIMIT``). Lists use keyset pagination; the graph is cut at
``max_nodes`` and ``max_edges``.
"""

import asyncio
import math
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, NotFoundError
from app.db.models import IndexStatus, UrlState
from app.services.search_index import IndexingProgress, read_job_indexing, sqlstate

QUERY_CANCELED: Final = "57014"  # SQLSTATE of a statement stopped by statement_timeout
# Redirects followed through a job's crawl results before a chain counts as unusable, as the
# most SITESCOPE_FETCH_MAX_REDIRECTS allows. It also bounds the chain query: pages admitted
# through links can form chains of any length (review finding).
MAX_REDIRECT_HOPS: Final = 20
TOP_LINKED_PAGES: Final = 10


class AnalysisTimeoutError(DomainError):
    """An analysis query did not finish within its time limit."""

    status_code = 503
    code = "analysis_timeout"


class LinkClass(StrEnum):
    """The class of a stored link (see the module docstring)."""

    OUT_OF_SCOPE = "out_of_scope"
    NOT_FOLLOWED = "not_followed"
    BEYOND_LIMITS = "beyond_limits"
    NOT_CRAWLED = "not_crawled"
    REDIRECT_ERROR = "redirect_error"
    OK = "ok"
    BROKEN = "broken"
    SERVER_ERROR = "server_error"
    HTTP_ERROR = "http_error"
    ROBOTS_BLOCKED = "robots_blocked"
    UNREACHABLE = "unreachable"


VERIFIED_CLASSES: Final = frozenset(
    {
        LinkClass.OK,
        LinkClass.BROKEN,
        LinkClass.SERVER_ERROR,
        LinkClass.HTTP_ERROR,
        LinkClass.REDIRECT_ERROR,
    }
)


class PageIndexStatus(StrEnum):
    """A stored page's indexing: its search document's status, or ``noindex`` (none)."""

    PENDING = IndexStatus.PENDING.value
    QUEUED = IndexStatus.QUEUED.value
    INDEXED = IndexStatus.INDEXED.value
    FAILED = IndexStatus.FAILED.value
    NOINDEX = "noindex"


# --- Results -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PagePosition:
    """Where a page listing stopped: the last page's BFS depth and URL."""

    depth: int
    url: str


@dataclass(frozen=True, slots=True)
class PageSummary:
    page_id: uuid.UUID
    url: str
    title: str | None
    description: str | None
    http_status: int
    content_type: str | None
    depth: int
    fetched_at: datetime
    noindex: bool
    nofollow: bool
    index_status: PageIndexStatus
    indexed_at: datetime | None


@dataclass(frozen=True, slots=True)
class PageListing:
    pages: list[PageSummary]
    next_position: PagePosition | None


@dataclass(frozen=True, slots=True)
class LinkPosition:
    """Where a link listing stopped: the last link's source URL, position on its page, and
    target URL (positions are distinct on a page, but no constraint enforces it)."""

    source_url: str
    position: int
    target_url: str


@dataclass(frozen=True, slots=True)
class LinkView:
    source_page_id: uuid.UUID
    source_url: str
    target_url: str
    anchor_text: str | None
    position: int
    is_internal: bool
    nofollow: bool
    target_state: UrlState | None  # the target URL's own crawl state; None: never admitted
    # The target URL's own response status, once its result is final (fetched or failed); None
    # before that, and when the request got no response.
    target_http_status: int | None
    target_page_id: uuid.UUID | None  # the target's stored page, if any
    # Where the target's redirect chain ends: the first URL that did not redirect, or the
    # redirect target the job never requested; for a loop, the last URL before it repeats.
    # None: the target did not redirect (to another URL).
    final_url: str | None
    # The final URL's response status, once final (None: never requested, or no response).
    final_http_status: int | None
    redirect_hops: int  # redirects from the target to final_url
    # When the result that decides the class was recorded: the final URL's terminal result
    # (None: no final result, or the chain's end was never requested).
    checked_at: datetime | None
    classification: LinkClass

    @property
    def target_crawled(self) -> bool:
        """The crawl attempted the target URL itself, and its result is final."""
        return self.target_state in (UrlState.FETCHED, UrlState.FAILED)

    @property
    def verified(self) -> bool:
        """An HTTP response decided the class (module docstring)."""
        return self.classification in VERIFIED_CLASSES


@dataclass(frozen=True, slots=True)
class LinkListing:
    links: list[LinkView]
    next_position: LinkPosition | None


@dataclass(frozen=True, slots=True)
class BrokenLinkPosition:
    """Where a broken-link listing stopped: the last link's target and source URL."""

    target_url: str
    source_url: str


@dataclass(frozen=True, slots=True)
class BrokenLinkListing:
    total: int  # confirmed broken links of the job
    links: list[LinkView]
    next_position: BrokenLinkPosition | None


@dataclass(frozen=True, slots=True)
class GraphNode:
    url: str
    page_id: uuid.UUID | None  # None: not a stored page
    title: str | None
    depth: int | None  # None: never admitted
    crawl_state: UrlState | None
    http_status: int | None
    in_degree: int  # stored internal links to this URL, from distinct pages
    out_degree: int | None  # stored internal links on this page; None: not a stored page


@dataclass(frozen=True, slots=True)
class GraphEdge:
    source: int  # index into the nodes
    target: int
    nofollow: bool


@dataclass(frozen=True, slots=True)
class LinkGraph:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    total_nodes: int
    total_edges: int
    max_nodes: int
    max_edges: int

    @property
    def truncated(self) -> bool:
        return self.total_nodes > len(self.nodes) or self.total_edges > len(self.edges)


@dataclass(frozen=True, slots=True)
class DepthCount:
    depth: int
    urls: int  # crawl URLs admitted at this depth
    fetched: int  # of those, requested and answered (state fetched)
    pages: int  # of those, stored HTML pages


@dataclass(frozen=True, slots=True)
class TopLinkedPage:
    url: str
    page_id: uuid.UUID | None
    in_links: int


@dataclass(frozen=True, slots=True)
class CrawlStats:
    job_id: uuid.UUID
    status: str
    current_level: int
    max_depth: int
    max_pages: int
    urls_discovered: int  # admitted URLs plus internal link targets never admitted
    urls_admitted: int
    urls_by_state: dict[UrlState, int]
    pages_stored: int
    status_1xx: int
    status_2xx: int
    status_3xx: int
    status_4xx: int
    status_5xx: int
    no_response: int
    depths: list[DepthCount]
    indexing: IndexingProgress
    links_total: int
    links_internal: int
    links_external: int
    internal_targets: int
    links_by_class: dict[LinkClass, int]
    top_linked: list[TopLinkedPage] = field(default_factory=list)

    @property
    def links_broken(self) -> int:
        return self.links_by_class[LinkClass.BROKEN]

    @property
    def links_verified(self) -> int:
        return sum(self.links_by_class[item] for item in VERIFIED_CLASSES)

    @property
    def links_unverified(self) -> int:
        return self.links_total - self.links_verified


# --- SQL -----------------------------------------------------------------------------------------

_SNAPSHOT = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
_STATEMENT_TIMEOUT = text("SELECT set_config('statement_timeout', :timeout, true)")
# Plan every execution with its parameters. asyncpg prepares a statement once per connection, and
# after five executions PostgreSQL may switch to a generic plan, which cannot drop the optional
# filters ("CAST(:x AS ...) IS NULL OR ..."): the link listing was measured 3 to 8 times slower
# with it (benchmarks/results/phase8-analysis.md).
_CUSTOM_PLANS = text("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
_JOB = text(
    """
    SELECT id, status, current_level, max_depth, max_pages, admitted_count
    FROM crawl_jobs WHERE id = :job_id
    """
)

_PAGES = text(
    """
    SELECT p.id, p.normalized_url, p.title, p.meta_description, p.http_status, p.content_type,
           u.depth, p.fetched_at, p.robots_noindex, p.robots_nofollow,
           coalesce(d.status, 'noindex') AS index_status, d.indexed_at
    FROM pages AS p
    JOIN crawl_urls AS u ON u.id = p.crawl_url_id AND u.job_id = p.job_id
    LEFT JOIN search_documents AS d ON d.page_id = p.id
    WHERE p.job_id = :job_id
      AND (CAST(:depth AS integer) IS NULL OR u.depth = CAST(:depth AS integer))
      AND (CAST(:index_status AS text) IS NULL
           OR coalesce(d.status, 'noindex') = CAST(:index_status AS text))
      AND (CAST(:after_depth AS integer) IS NULL
           OR (u.depth, p.normalized_url)
              > (CAST(:after_depth AS integer), CAST(:after_url AS text)))
    ORDER BY u.depth, p.normalized_url
    LIMIT :limit
    """
)


def _redirect_chains(start: str = "") -> str:
    """The ``chain`` and ``final`` CTEs: the job's crawl URLs (``start``, a constant condition on
    ``u``, may narrow them) with the end of each one's redirect chain. A chain is followed through
    the job's crawl results only, never fetched again; a URL already seen ends it (a loop), and so
    does the ``MAX_REDIRECT_HOPS``-th redirect: either makes the chain ``unusable``."""
    return f"""
    chain AS (
        SELECT u.normalized_url AS start_url, u.normalized_url AS url, u.state, u.outcome,
               u.http_status, u.redirect_target, u.finished_at, 0 AS hops,
               ARRAY[u.normalized_url::text] AS seen
        FROM crawl_urls AS u
        WHERE u.job_id = :job_id{start}
        UNION ALL
        SELECT c.start_url, n.normalized_url, n.state, n.outcome, n.http_status,
               n.redirect_target, n.finished_at, c.hops + 1, c.seen || n.normalized_url::text
        FROM chain AS c
        JOIN crawl_urls AS n ON n.job_id = :job_id AND n.normalized_url = c.redirect_target
        WHERE c.state = 'fetched' AND c.outcome = 'redirect' AND c.hops < {MAX_REDIRECT_HOPS}
          AND NOT (n.normalized_url::text = ANY(c.seen))
    ),
    final AS MATERIALIZED (
        SELECT DISTINCT ON (start_url)
               start_url, url AS last_url, state, outcome, http_status, redirect_target,
               finished_at, hops,
               coalesce(outcome = 'redirect'
                        AND (redirect_target::text = ANY(seen) OR hops >= {MAX_REDIRECT_HOPS}),
                        false) AS unusable
        FROM chain
        ORDER BY start_url, hops DESC
    )"""  # noqa: S608 - only constant conditions and a constant number are inserted


# The stored links of the job, with what their class needs from the linking page. Statements
# append conditions on l (the link) and sp (its page).
_LINK_ROWS = """
        SELECT l.source_page_id, sp.normalized_url AS source_url,
               l.target_normalized_url AS target_url, l.anchor_text, l.position, l.is_internal,
               l.nofollow, sp.robots_nofollow AS source_nofollow, su.depth AS source_depth
        FROM page_links AS l
        JOIN pages AS sp ON sp.id = l.source_page_id AND sp.job_id = l.job_id
        JOIN crawl_urls AS su ON su.id = sp.crawl_url_id AND su.job_id = sp.job_id
        WHERE l.job_id = :job_id"""

_LINK_FILTERS = """
          AND (CAST(:source_page_id AS uuid) IS NULL
               OR l.source_page_id = CAST(:source_page_id AS uuid))
          AND (CAST(:internal AS boolean) IS NULL OR l.is_internal = CAST(:internal AS boolean))"""

# The class of a link (the module docstring's table). Aliases: c the link (a ``candidates``
# row), j the job, f the final crawl result of its target. The state comes first: a URL waiting
# for a retry keeps its last attempt's status (crawl_state._RETRY), which is not a final
# response. Only terminal results carry an outcome.
_CLASSIFICATION = r"""
    CASE
        WHEN NOT c.is_internal THEN 'out_of_scope'
        WHEN f.start_url IS NULL THEN
            CASE WHEN c.nofollow OR c.source_nofollow THEN 'not_followed'
                 WHEN c.source_depth >= j.max_depth OR j.admitted_count >= j.max_pages
                     THEN 'beyond_limits'
                 ELSE 'not_crawled' END
        WHEN f.state NOT IN ('fetched', 'failed', 'skipped') THEN 'not_crawled'
        WHEN f.state = 'skipped' THEN
            CASE WHEN f.outcome IN ('robots_disallowed', 'robots_unreachable')
                     THEN 'robots_blocked'
                 ELSE 'out_of_scope' END
        WHEN f.outcome = 'redirect' THEN
            CASE WHEN f.unusable THEN 'redirect_error'
                 WHEN NOT coalesce(
                          substring(f.redirect_target
                                    FROM '^[a-z][a-z0-9+.-]*://(\[[^]]*\]|[^/:?#]+)')
                          = ANY(j.allowed_hosts), false)
                     THEN 'out_of_scope'
                 WHEN j.admitted_count >= j.max_pages THEN 'beyond_limits'
                 ELSE 'not_crawled' END
        WHEN f.outcome = 'invalid_redirect' THEN 'redirect_error'
        WHEN f.http_status BETWEEN 200 AND 299 THEN 'ok'
        WHEN f.http_status IN (404, 410) THEN 'broken'
        WHEN f.http_status >= 500 OR f.http_status IN (408, 429) THEN 'server_error'
        WHEN f.http_status IS NOT NULL THEN 'http_error'
        ELSE 'unreachable'
    END"""

# The ``candidates`` links with their target's crawl facts and their class.
_CLASSIFIED = f"""
    classified AS (
        SELECT c.source_page_id, c.source_url, c.target_url, c.anchor_text, c.position,
               c.is_internal, c.nofollow, t.state AS target_state,
               CASE WHEN t.state IN ('fetched', 'failed') THEN t.http_status END
                   AS target_http_status,
               tp.id AS target_page_id,
               CASE WHEN f.outcome = 'redirect' AND NOT f.unusable THEN f.redirect_target
                    WHEN f.hops > 0 THEN f.last_url END AS final_url,
               CASE WHEN f.outcome = 'redirect' AND NOT f.unusable THEN NULL
                    WHEN f.hops > 0 AND f.state IN ('fetched', 'failed') THEN f.http_status
               END AS final_http_status,
               CASE WHEN f.outcome = 'redirect' AND NOT f.unusable THEN f.hops + 1
                    ELSE coalesce(f.hops, 0) END AS redirect_hops,
               CASE WHEN f.outcome = 'redirect' AND NOT f.unusable THEN NULL
                    WHEN f.state IN ('fetched', 'failed', 'skipped') THEN f.finished_at
               END AS checked_at,
               {_CLASSIFICATION} AS classification
        FROM candidates AS c
        JOIN crawl_jobs AS j ON j.id = :job_id
        LEFT JOIN crawl_urls AS t ON t.job_id = :job_id AND t.normalized_url = c.target_url
        LEFT JOIN pages AS tp ON tp.crawl_url_id = t.id AND tp.job_id = t.job_id
        LEFT JOIN final AS f ON f.start_url = c.target_url
    )"""  # noqa: S608 - only the classification CASE above is inserted

_LINK_COLUMNS = """
    source_page_id, source_url, target_url, anchor_text, position, is_internal, nofollow,
    target_state, target_http_status, target_page_id, final_url, final_http_status,
    redirect_hops, checked_at, classification"""

# A page of links without a class filter: the page is cut first (ix_pages_job_id_normalized_url
# gives the pages in order; the redundant ">=" lets a cursor start the scan), then only its
# links are classified, and only their targets' redirect chains are followed.
_LINKS_PAGE = text(
    f"""
    WITH RECURSIVE
    candidates AS MATERIALIZED (
        {_LINK_ROWS}{_LINK_FILTERS}
          AND (CAST(:after_url AS text) IS NULL
               OR (sp.normalized_url >= CAST(:after_url AS text)
                   AND (sp.normalized_url, l.position, l.target_normalized_url)
                       > (CAST(:after_url AS text), CAST(:after_position AS integer),
                          CAST(:after_target AS text))))
        ORDER BY sp.normalized_url, l.position, l.target_normalized_url
        LIMIT :limit
    ),
    {_redirect_chains(" AND u.normalized_url IN (SELECT target_url FROM candidates)")},
    {_CLASSIFIED}
    SELECT {_LINK_COLUMNS} FROM classified ORDER BY source_url, position, target_url
    """  # noqa: S608 - only the SQL fragments above are inserted
)

# The verified classes, as an SQL list (constants of this module).
_VERIFIED_SQL = ", ".join(f"'{item.value}'" for item in sorted(VERIFIED_CLASSES))

# A page of the links that match a class, verification or redirect filter: every link (with the
# other filters) is classified first. "Redirected": the target itself answered with a 3xx.
_LINKS_FILTERED = text(
    f"""
    WITH RECURSIVE {_redirect_chains()},
    candidates AS ({_LINK_ROWS}{_LINK_FILTERS}
    ),
    {_CLASSIFIED}
    SELECT {_LINK_COLUMNS}
    FROM classified
    WHERE (CAST(:classification AS text) IS NULL
           OR classification = CAST(:classification AS text))
      AND (CAST(:verified AS boolean) IS NULL
           OR (classification IN ({_VERIFIED_SQL})) = CAST(:verified AS boolean))
      AND (CAST(:redirected AS boolean) IS NULL
           OR coalesce(target_http_status BETWEEN 300 AND 399, false)
              = CAST(:redirected AS boolean))
      AND (CAST(:after_url AS text) IS NULL
           OR (source_url, position, target_url)
              > (CAST(:after_url AS text), CAST(:after_position AS integer),
                 CAST(:after_target AS text)))
    ORDER BY source_url, position, target_url
    LIMIT :limit
    """  # noqa: S608 - only the SQL fragments above are inserted
)

# Only a link whose target's chain ends in a 404 or 410 response can be broken: those links are
# looked up by their target, then classified like every other link (the class decides).
_BROKEN_LINKS = text(
    f"""
    WITH RECURSIVE {_redirect_chains()},
    candidates AS ({_LINK_ROWS}
          AND l.is_internal
          AND l.target_normalized_url IN (
              SELECT start_url FROM final WHERE http_status IN (404, 410))
    ),
    {_CLASSIFIED},
    broken AS MATERIALIZED (SELECT * FROM classified WHERE classification = 'broken')
    SELECT counted.total, page.*
    FROM (SELECT count(*) AS total FROM broken) AS counted
    LEFT JOIN LATERAL (
        SELECT {_LINK_COLUMNS}
        FROM broken
        WHERE CAST(:after_target AS text) IS NULL
           OR (target_url, source_url)
              > (CAST(:after_target AS text), CAST(:after_source AS text))
        ORDER BY target_url, source_url
        LIMIT :limit
    ) AS page ON true
    """  # noqa: S608 - only the SQL fragments above are inserted
)

_LINK_CLASS_COUNTS = text(
    f"""
    WITH RECURSIVE {_redirect_chains()},
    candidates AS ({_LINK_ROWS}
    ),
    {_CLASSIFIED}
    SELECT classification, count(*) AS n FROM classified GROUP BY classification
    """  # noqa: S608 - only the SQL fragments above are inserted
)

# The internal link graph. Nodes: every URL the job admitted (its crawl URLs, which include
# every stored page), and every target of an internal link. Edges: the stored internal links.
# The returned nodes are cut first; their degrees are counted from the indexes.
_GRAPH_NODES = text(
    """
    WITH node_urls AS (
        SELECT normalized_url AS url FROM crawl_urls WHERE job_id = :job_id
        UNION
        SELECT target_normalized_url FROM page_links WHERE job_id = :job_id AND is_internal
    ),
    returned AS MATERIALIZED (
        SELECT n.url, u.id AS url_id, u.depth, u.state, u.http_status,
               count(*) OVER () AS total_nodes
        FROM node_urls AS n
        LEFT JOIN crawl_urls AS u ON u.job_id = :job_id AND u.normalized_url = n.url
        ORDER BY u.depth NULLS LAST, n.url
        LIMIT :max_nodes
    )
    SELECT r.url, p.id AS page_id, p.title, r.depth, r.state,
           CASE WHEN r.state IN ('fetched', 'failed') THEN r.http_status END AS http_status,
           (SELECT count(*) FROM page_links AS l
            WHERE l.job_id = :job_id AND l.target_normalized_url = r.url AND l.is_internal)
               AS in_degree,
           CASE WHEN p.id IS NOT NULL THEN
               (SELECT count(*) FROM page_links AS l
                WHERE l.source_page_id = p.id AND l.is_internal)
           END AS out_degree,
           r.total_nodes
    FROM returned AS r
    LEFT JOIN pages AS p ON p.crawl_url_id = r.url_id AND p.job_id = :job_id
    ORDER BY r.depth NULLS LAST, r.url
    """
)

# The internal links from the returned pages to returned nodes, through each page's links.
_GRAPH_EDGES = text(
    """
    WITH targets AS MATERIALIZED (SELECT url FROM unnest(CAST(:urls AS text[])) AS t(url))
    SELECT sp.normalized_url AS source_url, l.target_normalized_url AS target_url, l.nofollow
    FROM pages AS sp
    JOIN page_links AS l ON l.source_page_id = sp.id AND l.job_id = sp.job_id
    WHERE sp.job_id = :job_id AND sp.id = ANY(CAST(:page_ids AS uuid[]))
      AND l.is_internal AND l.target_normalized_url IN (SELECT url FROM targets)
    ORDER BY sp.normalized_url, l.position, l.target_normalized_url
    LIMIT :max_edges
    """
)

_GRAPH_EDGE_TOTAL = text("SELECT count(*) FROM page_links WHERE job_id = :job_id AND is_internal")

_URL_STATES = text(
    "SELECT state, count(*) AS n FROM crawl_urls WHERE job_id = :job_id GROUP BY state"
)

# Over the URLs whose request finished (fetched or failed): each URL's own response status, in
# exactly one bucket (a CHECK constraint keeps statuses within 100-599; a 101 can be final).
_HTTP_CLASSES = text(
    """
    SELECT count(*) FILTER (WHERE http_status BETWEEN 100 AND 199) AS s1,
           count(*) FILTER (WHERE http_status BETWEEN 200 AND 299) AS s2,
           count(*) FILTER (WHERE http_status BETWEEN 300 AND 399) AS s3,
           count(*) FILTER (WHERE http_status BETWEEN 400 AND 499) AS s4,
           count(*) FILTER (WHERE http_status BETWEEN 500 AND 599) AS s5,
           count(*) FILTER (WHERE http_status IS NULL) AS none
    FROM crawl_urls
    WHERE job_id = :job_id AND state IN ('fetched', 'failed')
    """
)

_DEPTHS = text(
    """
    SELECT u.depth, count(*) AS urls,
           count(*) FILTER (WHERE u.state = 'fetched') AS fetched,
           count(p.id) AS pages
    FROM crawl_urls AS u
    LEFT JOIN pages AS p ON p.crawl_url_id = u.id AND p.job_id = u.job_id
    WHERE u.job_id = :job_id
    GROUP BY u.depth
    ORDER BY u.depth
    """
)

_LINK_TOTALS = text(
    """
    SELECT count(*) AS total,
           count(*) FILTER (WHERE is_internal) AS internal,
           count(DISTINCT target_normalized_url) FILTER (WHERE is_internal) AS internal_targets,
           (SELECT count(*) FROM pages WHERE job_id = :job_id) AS pages,
           (SELECT count(*) FROM (
                SELECT normalized_url FROM crawl_urls WHERE job_id = :job_id
                UNION
                SELECT target_normalized_url FROM page_links
                WHERE job_id = :job_id AND is_internal
           ) AS known) AS discovered
    FROM page_links
    WHERE job_id = :job_id
    """
)

_TOP_LINKED = text(
    """
    SELECT l.target_normalized_url AS url, p.id AS page_id, count(*) AS in_links
    FROM page_links AS l
    LEFT JOIN crawl_urls AS u
        ON u.job_id = l.job_id AND u.normalized_url = l.target_normalized_url
    LEFT JOIN pages AS p ON p.crawl_url_id = u.id AND p.job_id = u.job_id
    WHERE l.job_id = :job_id AND l.is_internal
    GROUP BY l.target_normalized_url, p.id
    ORDER BY in_links DESC, url
    LIMIT :limit
    """
)


# --- Use cases -----------------------------------------------------------------------------------


async def list_pages(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    limit: int,
    after: PagePosition | None = None,
    depth: int | None = None,
    index_status: PageIndexStatus | None = None,
    timeout_seconds: float = 10.0,
) -> PageListing:
    """The job's stored pages in BFS order (depth, then URL), ``limit`` at a time.

    A page is an HTML document that was fetched and stored (``html_stored``); other crawled URLs
    have no page. No page body is returned.
    """
    async with _read_only(session, job_id, timeout_seconds):
        rows = (
            await session.execute(
                _PAGES,
                {
                    "job_id": job_id,
                    "depth": depth,
                    "index_status": index_status.value if index_status else None,
                    "after_depth": after.depth if after else None,
                    "after_url": after.url if after else None,
                    "limit": limit + 1,
                },
            )
        ).all()
    pages = [
        PageSummary(
            page_id=row.id,
            url=row.normalized_url,
            title=row.title,
            description=row.meta_description,
            http_status=row.http_status,
            content_type=row.content_type,
            depth=row.depth,
            fetched_at=row.fetched_at,
            noindex=row.robots_noindex,
            nofollow=row.robots_nofollow,
            index_status=PageIndexStatus(row.index_status),
            indexed_at=row.indexed_at,
        )
        for row in rows[:limit]
    ]
    more = len(rows) > limit
    return PageListing(pages, PagePosition(pages[-1].depth, pages[-1].url) if more else None)


async def list_links(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    limit: int,
    after: LinkPosition | None = None,
    source_page_id: uuid.UUID | None = None,
    internal: bool | None = None,
    classification: LinkClass | None = None,
    verified: bool | None = None,
    redirected: bool | None = None,
    timeout_seconds: float = 10.0,
) -> LinkListing:
    """The job's stored links, by source page URL and position on the page, with their class.

    ``verified`` keeps the links whose class an HTTP response decided (or, False, the others);
    ``redirected`` the links whose target itself answered with a 3xx (or the others).
    """
    params: dict[str, Any] = {
        "job_id": job_id,
        "source_page_id": source_page_id,
        "internal": internal,
        "after_url": after.source_url if after else None,
        "after_position": after.position if after else None,
        "after_target": after.target_url if after else None,
        "limit": limit + 1,
    }
    statement = _LINKS_PAGE
    if classification is not None or verified is not None or redirected is not None:
        statement = _LINKS_FILTERED
        params["classification"] = classification.value if classification else None
        params["verified"] = verified
        params["redirected"] = redirected
    async with _read_only(session, job_id, timeout_seconds):
        rows = (await session.execute(statement, params)).all()
    links = [_link(row) for row in rows[:limit]]
    more = len(rows) > limit
    last = links[-1] if more else None
    position = LinkPosition(last.source_url, last.position, last.target_url) if last else None
    return LinkListing(links, position)


async def list_broken_links(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    limit: int,
    after: BrokenLinkPosition | None = None,
    timeout_seconds: float = 10.0,
) -> BrokenLinkListing:
    """The job's confirmed broken links (class ``broken``), by target URL then source URL."""
    async with _read_only(session, job_id, timeout_seconds):
        rows = (
            await session.execute(
                _BROKEN_LINKS,
                {
                    "job_id": job_id,
                    "after_target": after.target_url if after else None,
                    "after_source": after.source_url if after else None,
                    "limit": limit + 1,
                },
            )
        ).all()
    total = rows[0].total if rows else 0
    found = [row for row in rows if row.source_page_id is not None]
    links = [_link(row) for row in found[:limit]]
    more = len(found) > limit
    last = links[-1] if more else None
    position = BrokenLinkPosition(last.target_url, last.source_url) if last else None
    return BrokenLinkListing(total, links, position)


async def link_graph(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    max_nodes: int,
    max_edges: int,
    timeout_seconds: float = 10.0,
) -> LinkGraph:
    """The job's internal link graph, cut at ``max_nodes`` and ``max_edges``.

    Nodes are every URL the job admitted and every target of an internal link, in BFS order
    (depth, then URL; URLs never admitted last). Edges are the internal links between returned
    nodes, by source URL, position and target URL. Degrees count every stored internal link,
    also those cut off. Redirects are not edges: a link graph holds links only.
    """
    if max_nodes < 1:
        raise ValueError("max_nodes must be at least 1")
    if max_edges < 0:
        raise ValueError("max_edges must not be negative")
    async with _read_only(session, job_id, timeout_seconds):
        node_rows = (
            await session.execute(_GRAPH_NODES, {"job_id": job_id, "max_nodes": max_nodes})
        ).all()
        edge_rows = (
            await session.execute(
                _GRAPH_EDGES,
                {
                    "job_id": job_id,
                    "urls": [row.url for row in node_rows],
                    "page_ids": [row.page_id for row in node_rows if row.page_id is not None],
                    "max_edges": max_edges,
                },
            )
        ).all()
        total_edges = (await session.execute(_GRAPH_EDGE_TOTAL, {"job_id": job_id})).scalar_one()
    nodes = [
        GraphNode(
            url=row.url,
            page_id=row.page_id,
            title=row.title,
            depth=row.depth,
            crawl_state=UrlState(row.state) if row.state is not None else None,
            http_status=row.http_status,
            in_degree=row.in_degree,
            out_degree=row.out_degree,
        )
        for row in node_rows
    ]
    index = {node.url: position for position, node in enumerate(nodes)}
    edges = [
        GraphEdge(source=index[row.source_url], target=index[row.target_url], nofollow=row.nofollow)
        for row in edge_rows
    ]
    total_nodes = node_rows[0].total_nodes if node_rows else 0
    return LinkGraph(nodes, edges, total_nodes, total_edges, max_nodes, max_edges)


async def crawl_stats(
    session: AsyncSession, job_id: uuid.UUID, *, timeout_seconds: float = 10.0
) -> CrawlStats:
    """Statistics of one job, all from one snapshot (see ``CrawlStats`` and ADR-0019)."""
    async with _read_only(session, job_id, timeout_seconds) as job:
        params = {"job_id": job_id}
        states = {UrlState(row.state): row.n for row in await session.execute(_URL_STATES, params)}
        http = (await session.execute(_HTTP_CLASSES, params)).one()
        depths = [
            DepthCount(row.depth, row.urls, row.fetched, row.pages)
            for row in await session.execute(_DEPTHS, params)
        ]
        indexing = await read_job_indexing(session, job_id)
        totals = (await session.execute(_LINK_TOTALS, params)).one()
        classes = {
            LinkClass(row.classification): row.n
            for row in await session.execute(_LINK_CLASS_COUNTS, params)
        }
        top = [
            TopLinkedPage(row.url, row.page_id, row.in_links)
            for row in await session.execute(_TOP_LINKED, {**params, "limit": TOP_LINKED_PAGES})
        ]
    if indexing is None:  # pragma: no cover - the job row was read in the same snapshot
        raise NotFoundError(f"Crawl job {job_id} does not exist.")
    return CrawlStats(
        job_id=job_id,
        status=job.status,
        current_level=job.current_level,
        max_depth=job.max_depth,
        max_pages=job.max_pages,
        urls_discovered=totals.discovered,
        urls_admitted=sum(states.values()),
        urls_by_state={state: states.get(state, 0) for state in UrlState},
        pages_stored=totals.pages,
        status_1xx=http.s1,
        status_2xx=http.s2,
        status_3xx=http.s3,
        status_4xx=http.s4,
        status_5xx=http.s5,
        no_response=http.none,
        depths=depths,
        indexing=indexing,
        links_total=totals.total,
        links_internal=totals.internal,
        links_external=totals.total - totals.internal,
        internal_targets=totals.internal_targets,
        links_by_class={item: classes.get(item, 0) for item in LinkClass},
        top_linked=top,
    )


# --- Helpers -------------------------------------------------------------------------------------


@asynccontextmanager
async def _read_only(
    session: AsyncSession, job_id: uuid.UUID, timeout_seconds: float
) -> AsyncIterator[Any]:
    """A read-only REPEATABLE READ transaction for one job that must exist (``NotFoundError``
    otherwise); yields the job row. It has ``timeout_seconds`` in all: PostgreSQL stops each
    statement after that long, and the transaction is abandoned when the time is up; either
    becomes ``AnalysisTimeoutError``. Any other ``TimeoutError`` (connecting to PostgreSQL, for
    example) propagates: the API reports it as an outage, ``dependency_unavailable``."""
    deadline = asyncio.timeout(timeout_seconds)
    try:
        async with deadline, session.begin():
            await session.execute(_SNAPSHOT)
            await session.execute(
                _STATEMENT_TIMEOUT, {"timeout": str(max(1, math.ceil(timeout_seconds * 1000)))}
            )
            await session.execute(_CUSTOM_PLANS)
            job = (await session.execute(_JOB, {"job_id": job_id})).one_or_none()
            if job is None:
                raise NotFoundError(f"Crawl job {job_id} does not exist.")
            yield job
    except (DBAPIError, TimeoutError) as exc:
        stopped = (
            deadline.expired() if isinstance(exc, TimeoutError) else sqlstate(exc) == QUERY_CANCELED
        )
        if stopped:
            raise AnalysisTimeoutError(
                f"The analysis took longer than {timeout_seconds:g} s and was stopped."
            ) from None
        raise


def _link(row: Any) -> LinkView:
    return LinkView(
        source_page_id=row.source_page_id,
        source_url=row.source_url,
        target_url=row.target_url,
        anchor_text=row.anchor_text,
        position=row.position,
        is_internal=row.is_internal,
        nofollow=row.nofollow,
        target_state=UrlState(row.target_state) if row.target_state is not None else None,
        target_http_status=row.target_http_status,
        target_page_id=row.target_page_id,
        final_url=row.final_url,
        final_http_status=row.final_http_status,
        redirect_hops=row.redirect_hops,
        checked_at=row.checked_at,
        classification=LinkClass(row.classification),
    )
