"""Search one crawl job's pages (Phase 7: the internal service; the public API is Phase 8).

``search_job`` runs one read-only transaction at REPEATABLE READ, so the results and the
indexing progress reported with them come from one snapshot:

1. The query is normalized (``app/indexing/queries.py``); an empty or overlong query, or a
   paging value out of range, is refused (``InvalidInputError``) before any SQL runs.
2. ``websearch_to_tsquery`` parses it with the index's configuration (``english``). If it has
   no term an index can look up (only stop words, or only excluded words), nothing is searched
   and the result says so (``searchable = False``): such a query would match every page.
3. Matching uses the GIN index (``search_vector @@ query``) within the job, and only documents
   that satisfy the **visibility rule**: ``indexed_content_version = pages.content_version``,
   the index reflects the page's current content. A page that was never indexed, or whose
   content changed after it was indexed, is not returned, so search never shows a page for
   words it no longer contains.
4. Ranking is ``ts_rank`` with the default weights (title 1.0, description 0.4, headings 0.2,
   body 0.1) and no length normalization. Ties are broken by URL (byte order), then page ID,
   so the order is total and pages are stable.
5. Every match is counted and ranked; ``ts_headline`` then runs only for the returned page of
   results, on at most ``SNIPPET_SOURCE_CHARS`` characters of each page's body.

A search has ``timeout_seconds`` in all (``SearchTimeoutError``): PostgreSQL stops each statement
after that long (``statement_timeout``), and the service abandons the transaction when the time
is up, whichever comes first. The cost of a search grows with the number of matching documents,
because each one is ranked; ``MAX_OFFSET`` bounds how deep a client can page.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, FieldViolation, InvalidInputError, NotFoundError
from app.indexing.documents import TEXT_SEARCH_CONFIG
from app.indexing.queries import (
    DEFAULT_LIMIT,
    HEADLINE_OPTIONS,
    SNIPPET_SOURCE_CHARS,
    QueryError,
    Snippet,
    check_paging,
    decode_snippet,
    normalize_query,
)
from app.services.search_index import IndexingProgress, read_job_indexing, sqlstate

QUERY_CANCELED: Final = "57014"  # SQLSTATE of a statement cut off by statement_timeout


class SearchTimeoutError(DomainError):
    """The search did not finish within its time limit."""

    status_code = 503
    code = "search_timeout"


@dataclass(frozen=True, slots=True)
class SearchHit:
    page_id: uuid.UUID
    url: str  # the page's normalized URL, as stored
    title: str | None
    rank: float
    snippet: Snippet
    fetched_at: datetime  # when the page's content was fetched
    indexed_at: datetime  # when its search vector was built
    content_version: int  # the content version the vector reflects (the current one)


@dataclass(frozen=True, slots=True)
class SearchResults:
    job_id: uuid.UUID
    query: str  # the normalized query that was searched
    searchable: bool  # False: the query has no term to look up, and nothing was searched
    total: int  # visible documents that match
    limit: int
    offset: int
    hits: list[SearchHit]
    indexing: IndexingProgress  # how much of the job is indexed, in the same snapshot


_SNAPSHOT = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
_STATEMENT_TIMEOUT = text("SELECT set_config('statement_timeout', :timeout, true)")

# A query an index can look up: it has nodes (numnode > 0; only stop words leave none), and
# querytree(), the part an index can use, is not 'T' (only negated words: "-python").
_QUERY_TERMS = text(
    """
    SELECT numnode(q) > 0 AND querytree(q) <> 'T'
    FROM (SELECT websearch_to_tsquery(CAST(:config AS regconfig), :query) AS q) AS parsed
    """
)

_SEARCH = text(
    """
    WITH matches AS MATERIALIZED (
        SELECT d.page_id, p.normalized_url,
               ts_rank(d.search_vector,
                       websearch_to_tsquery(CAST(:config AS regconfig), :query)) AS rank
        FROM search_documents AS d
        JOIN pages AS p ON p.id = d.page_id AND p.job_id = d.job_id
        WHERE d.job_id = :job_id
          AND d.search_vector @@ websearch_to_tsquery(CAST(:config AS regconfig), :query)
          AND d.indexed_content_version = p.content_version
    )
    SELECT t.total, r.page_id, r.normalized_url, r.title, r.rank, r.fetched_at, r.indexed_at,
           r.content_version, r.headline
    FROM (SELECT count(*) AS total FROM matches) AS t
    LEFT JOIN LATERAL (
        SELECT top.page_id, top.normalized_url, top.rank, p.title, p.fetched_at,
               d.indexed_at, d.indexed_content_version AS content_version,
               ts_headline(
                   CAST(:config AS regconfig),
                   translate(
                       coalesce(nullif(left(p.extracted_text, :snippet_chars), ''),
                                p.meta_description, ''),
                       chr(2) || chr(3), ''),
                   websearch_to_tsquery(CAST(:config AS regconfig), :query),
                   :headline_options) AS headline
        FROM (
            SELECT page_id, normalized_url, rank FROM matches
            ORDER BY rank DESC, normalized_url, page_id
            LIMIT :limit OFFSET :offset
        ) AS top
        JOIN pages AS p ON p.id = top.page_id
        JOIN search_documents AS d ON d.page_id = top.page_id
    ) AS r ON true
    ORDER BY r.rank DESC, r.normalized_url, r.page_id
    """
)


async def search_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    timeout_seconds: float = 5.0,
) -> SearchResults:
    """Search the indexed pages of one job (see the module docstring).

    Raises ``InvalidInputError`` for an empty or overlong query or paging out of range,
    ``NotFoundError`` for an unknown job, and ``SearchTimeoutError`` if the search took longer
    than ``timeout_seconds`` (stopped by PostgreSQL or by the service).
    """
    try:
        normalized = normalize_query(query)
        check_paging(limit, offset)
    except QueryError as exc:
        field = {"invalid_limit": "limit", "invalid_offset": "offset"}.get(exc.code, "q")
        raise InvalidInputError([FieldViolation(("query", field), exc.message, exc.code)]) from None
    params = {"config": TEXT_SEARCH_CONFIG, "query": normalized, "job_id": job_id}
    try:
        async with asyncio.timeout(timeout_seconds), session.begin():
            await session.execute(_SNAPSHOT)
            await session.execute(
                _STATEMENT_TIMEOUT, {"timeout": str(max(1, round(timeout_seconds * 1000)))}
            )
            indexing = await read_job_indexing(session, job_id)
            if indexing is None:
                raise NotFoundError(f"Crawl job {job_id} does not exist.")
            searchable = bool((await session.execute(_QUERY_TERMS, params)).scalar_one())
            if not searchable:
                return SearchResults(job_id, normalized, False, 0, limit, offset, [], indexing)
            rows = (
                await session.execute(
                    _SEARCH,
                    {
                        **params,
                        "limit": limit,
                        "offset": offset,
                        "snippet_chars": SNIPPET_SOURCE_CHARS,
                        "headline_options": HEADLINE_OPTIONS,
                    },
                )
            ).all()
    except (DBAPIError, TimeoutError) as exc:
        if isinstance(exc, TimeoutError) or sqlstate(exc) == QUERY_CANCELED:
            raise SearchTimeoutError(
                f"The search took longer than {timeout_seconds:g} s and was stopped."
            ) from None
        raise
    total = rows[0].total if rows else 0
    hits = [
        SearchHit(
            page_id=row.page_id,
            url=row.normalized_url,
            title=row.title,
            rank=float(row.rank),
            snippet=decode_snippet(row.headline),
            fetched_at=row.fetched_at,
            indexed_at=row.indexed_at,
            content_version=row.content_version,
        )
        for row in rows
        if row.page_id is not None
    ]
    return SearchResults(job_id, normalized, True, total, limit, offset, hits, indexing)
