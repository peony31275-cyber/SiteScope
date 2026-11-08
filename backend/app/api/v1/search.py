"""Search endpoint: full-text search of one crawl job's indexed pages (Phase 8).

HTTP concerns only. The search itself, its validation, its visibility rule and its time limit
are the Phase 7 search service's (``app/services/search.py``, ADR-0018); this module maps
``page`` and ``page_size`` to its offset and limit, and its results to the response.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import SessionDep, SettingsDep
from app.api.problems import problem_responses
from app.api.v1.search_schemas import MAX_RAW_QUERY_CHARS, SearchResponse, search_offset
from app.indexing.queries import DEFAULT_LIMIT, MAX_LIMIT, MAX_OFFSET, MAX_QUERY_CHARS
from app.services.search import search_job

router = APIRouter(prefix="/search", tags=["search"])


@router.get(
    "",
    summary="Search the pages of one crawl job",
    responses=problem_responses(
        404,
        422,
        503,
        descriptions={
            404: "The crawl job does not exist (`not_found`).",
            422: "The request failed validation (`validation_failed`); `errors` lists each "
            "field with its `type`: `empty_query` (nothing left after removing whitespace and "
            "invisible characters), `query_too_long`, `page_out_of_range` (reported together "
            "with a query error), or a schema rule (for example `uuid_parsing` for `job_id`, "
            "`missing` for a missing parameter).",
            503: "The search took longer than SITESCOPE_SEARCH_TIMEOUT_SECONDS and was "
            "stopped (`search_timeout`), or PostgreSQL is unavailable "
            "(`dependency_unavailable`, with `Retry-After`).",
        },
    ),
)
async def search_pages(
    session: SessionDep,
    settings: SettingsDep,
    job_id: Annotated[
        uuid.UUID,
        Query(description="The crawl job to search. A search never covers another job."),
    ],
    q: Annotated[
        str,
        Query(
            max_length=MAX_RAW_QUERY_CHARS,
            description="The query, in web-search syntax: words (all must match), quoted "
            '`"exact phrases"`, `or` between alternatives, and `-word` to exclude a word. '
            f"At most {MAX_QUERY_CHARS} characters after invisible characters are removed "
            "and whitespace is collapsed. Words are matched by their English stem "
            "(`crawling` finds `crawled`); stop words such as `the` are ignored.",
            examples=["consumer groups"],
        ),
    ],
    page: Annotated[
        int,
        Query(
            ge=1,
            description=f"1-based page number. A search serves results up to offset "
            f"{MAX_OFFSET}: a page with `(page - 1) * page_size` above it is refused "
            "(`page_out_of_range`).",
        ),
    ] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=MAX_LIMIT, description="Results per page.")
    ] = DEFAULT_LIMIT,
) -> SearchResponse:
    """Search the indexed pages of one crawl job, ranked by relevance.

    Only pages whose current content is indexed are found: a page that is not indexed yet, or
    whose index is older than its content, is not returned, and nothing else (pages of other
    jobs, unindexed text) is ever searched instead. While the job is still crawling or
    indexing (`indexing.status` is `pending` or `running`), a later search may find more.

    Results are ordered by score, then URL, then page ID, so pages do not overlap or skip
    results while the index is unchanged. `total` counts every match. Every match is ranked
    before a page is cut, which is why results are served up to offset 1000 only.
    """
    offset = search_offset(q, page, page_size)
    results = await search_job(
        session,
        job_id,
        q,
        limit=page_size,
        offset=offset,
        timeout_seconds=settings.search_timeout_seconds,
    )
    return SearchResponse.from_results(results, page=page, page_size=page_size)
