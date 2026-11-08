"""Response body of the search endpoint (Phase 8), and its page arithmetic."""

import uuid
from datetime import datetime
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.crawl_schemas import CrawlIndexing
from app.core.errors import FieldViolation, InvalidInputError
from app.indexing.queries import MAX_OFFSET, MAX_QUERY_CHARS, QueryError, normalize_query
from app.services.search import SearchHit, SearchResults

# Characters of `q` accepted before normalization (which removes invisible characters and
# repeated whitespace); the normalized query may have at most MAX_QUERY_CHARS.
MAX_RAW_QUERY_CHARS: Final = 4 * MAX_QUERY_CHARS


class HighlightRange(BaseModel):
    """A matched word in the snippet, as a `[start, end)` range of Unicode code points."""

    start: int = Field(ge=0)
    end: int = Field(ge=1)


class SearchResult(BaseModel):
    """One matching page. Only pages whose stored content is indexed are ever returned."""

    page_id: uuid.UUID
    url: str = Field(description="The page's normalized URL.")
    title: str | None
    snippet: str = Field(
        description="Plain text around the matches, from the title, description, headings "
        "or body. Never HTML: render it as text."
    )
    highlights: list[HighlightRange] = Field(
        description="Matched words in `snippet`. Offsets count Unicode code points, not UTF-16 "
        "code units: in JavaScript, index `Array.from(snippet)`, not the string itself."
    )
    score: float = Field(
        description="PostgreSQL `ts_rank` of the page for this query (title and description "
        "weigh more than headings, headings more than the body). It orders the results of "
        "this search; it is not comparable between queries or jobs."
    )
    crawled_at: datetime = Field(description="When the indexed content was fetched.")
    indexed_at: datetime = Field(description="When the page's search vector was built.")

    @classmethod
    def from_hit(cls, hit: SearchHit) -> Self:
        return cls(
            page_id=hit.page_id,
            url=hit.url,
            title=hit.title,
            snippet=hit.snippet.text,
            highlights=[HighlightRange(start=s, end=e) for s, e in hit.snippet.highlights],
            score=hit.rank,
            crawled_at=hit.fetched_at,
            indexed_at=hit.indexed_at,
        )


class SearchResponse(BaseModel):
    """One page of the results of a search of one crawl job."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "consumer groups",
                    "job_id": "0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b",
                    "searchable": True,
                    "total": 1,
                    "page": 1,
                    "page_size": 10,
                    "results": [
                        {
                            "page_id": "0199a1b2-8d4e-7f50-9b0c-1d2e3f4a5b6c",
                            "url": "https://docs.example.com/streams.html",
                            "title": "Redis Streams",
                            "snippet": "Consumer groups let several workers share one stream",
                            "highlights": [{"start": 0, "end": 8}, {"start": 9, "end": 15}],
                            "score": 0.6079271,
                            "crawled_at": "2026-11-07T10:15:02.418Z",
                            "indexed_at": "2026-11-07T10:15:03.977Z",
                        }
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
                }
            ]
        }
    )

    query: str = Field(
        description="The query as it was searched: invisible characters removed, whitespace "
        "collapsed."
    )
    job_id: uuid.UUID
    searchable: bool = Field(
        description="False when the query has no term an index can look up, for example only "
        "stop words (`the and`) or only excluded words (`-redis`): nothing was searched, and "
        "there are no results."
    )
    total: int = Field(description="Indexed pages of the job that match, on every page.")
    page: int
    page_size: int
    results: list[SearchResult] = Field(
        description="Ranked by score, then URL, then page ID; empty beyond the last page."
    )
    indexing: CrawlIndexing = Field(
        description="How much of the job is indexed, counted in the same snapshot as the "
        "results. Pages not indexed yet are not searched: while `status` is `pending` or "
        "`running`, later searches may find more."
    )

    @classmethod
    def from_results(cls, results: SearchResults, *, page: int, page_size: int) -> Self:
        return cls(
            query=results.query,
            job_id=results.job_id,
            searchable=results.searchable,
            total=results.total,
            page=page,
            page_size=page_size,
            results=[SearchResult.from_hit(hit) for hit in results.hits],
            indexing=CrawlIndexing.from_progress(results.indexing),
        )


def search_offset(q: str, page: int, page_size: int) -> int:
    """The offset of ``page`` (1-based), or ``InvalidInputError`` listing every broken rule:
    the query's (``empty_query``, ``query_too_long``: the search service's own rules, checked
    here as well so that both fields are reported together) and the page's
    (``page_out_of_range``, beyond the deepest offset a search serves, ``MAX_OFFSET``)."""
    violations: list[FieldViolation] = []
    try:
        normalize_query(q)
    except QueryError as exc:
        violations.append(FieldViolation(("query", "q"), exc.message, exc.code))
    offset = (page - 1) * page_size
    if offset > MAX_OFFSET:
        last_page = MAX_OFFSET // page_size + 1
        violations.append(
            FieldViolation(
                ("query", "page"),
                f"page must be at most {last_page} for page_size {page_size}: results "
                f"beyond the first {MAX_OFFSET + page_size} are not served",
                "page_out_of_range",
            )
        )
    if violations:
        raise InvalidInputError(violations)
    return offset
