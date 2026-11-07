"""Crawl job endpoints: create, inspect and list crawl jobs.

HTTP concerns only: request and response shapes, status codes, headers and the listing cursor.
Every rule and every transaction lives in the service layer (``app/services/crawl_jobs.py``).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from app.api.dependencies import SessionDep, SettingsDep
from app.api.problems import problem_responses
from app.api.v1.crawl_schemas import (
    CrawlJobDetail,
    CrawlJobList,
    CrawlJobSummary,
    CreateCrawlJobRequest,
    decode_cursor,
    encode_cursor,
)
from app.services import crawl_jobs

router = APIRouter(prefix="/crawls", tags=["crawls"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@router.post(
    "",
    status_code=201,
    summary="Create a crawl job",
    responses=problem_responses(422, 503),
)
async def create_crawl(
    body: CreateCrawlJobRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> CrawlJobSummary:
    """Validate the request and store the job with its seed URL, in one transaction.

    The seed URL is normalized and must name an approved crawl target; `max_depth` and
    `max_pages` must be within this server's limits. The job is created as `queued`: it waits
    for the scheduler, and nothing is fetched yet. The `Location` header points to the new job.
    """
    job = await crawl_jobs.create_crawl_job(
        session,
        settings,
        seed_url=body.seed_url,
        max_depth=body.max_depth,
        max_pages=body.max_pages,
    )
    response.headers["Location"] = request.app.url_path_for("get_crawl", job_id=str(job.id))
    return CrawlJobSummary.model_validate(job)


@router.get(
    "",
    summary="List crawl jobs",
    responses=problem_responses(422, 503),
)
async def list_crawls(
    session: SessionDep,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Jobs per page.")
    ] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None,
        Query(max_length=256, description="The `next_cursor` of the previous page."),
    ] = None,
) -> CrawlJobList:
    """List crawl jobs, newest first, with cursor (keyset) pagination.

    Paging never returns a job twice, and returns every job that existed when paging started.
    Jobs created meanwhile do not shift later pages.
    """
    after = decode_cursor(cursor) if cursor is not None else None
    page = await crawl_jobs.list_crawl_jobs(session, limit=limit, after=after)
    return CrawlJobList(
        items=[CrawlJobSummary.model_validate(job) for job in page.jobs],
        next_cursor=encode_cursor(page.next_position) if page.next_position else None,
    )


@router.get(
    "/{job_id}",
    summary="Get a crawl job",
    responses=problem_responses(404, 422, 503),
)
async def get_crawl(job_id: uuid.UUID, session: SessionDep) -> CrawlJobDetail:
    """Return the job, with progress counted from its URL rows in PostgreSQL."""
    return CrawlJobDetail.from_progress(await crawl_jobs.get_crawl_job(session, job_id))
