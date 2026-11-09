"""Crawl job endpoints: create, inspect, list and cancel crawl jobs.

HTTP concerns only: request and response shapes, status codes, headers and the listing cursor.
Every rule and every transaction lives in the service layer (``app/services/crawl_jobs.py`` and
``app/services/crawl_cancellation.py``). Creating and cancelling are writes: they follow the
deployment's write access (``app/api/security.py``, ADR-0024); listing and reading are public.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from app.api.dependencies import SessionDep, SettingsDep
from app.api.problems import problem_responses
from app.api.security import require_write_access
from app.api.v1.crawl_schemas import (
    CrawlJobDetail,
    CrawlJobList,
    CrawlJobSummary,
    CreateCrawlJobRequest,
    decode_cursor,
    encode_cursor,
)
from app.services import crawl_cancellation, crawl_jobs

router = APIRouter(prefix="/crawls", tags=["crawls"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@router.post(
    "",
    status_code=201,
    summary="Create a crawl job",
    dependencies=[Depends(require_write_access)],
    responses={
        200: {
            "description": "A replay: an earlier request with the same `Idempotency-Key` and an "
            "equivalent body created this job (header `Idempotent-Replayed: true`).",
            "model": CrawlJobSummary,
        },
        **problem_responses(401, 403, 409, 422, 429, 503),
    },
)
async def create_crawl(
    body: CreateCrawlJobRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            pattern=crawl_jobs.IDEMPOTENCY_KEY_PATTERN,
            max_length=crawl_jobs.IDEMPOTENCY_KEY_MAX_LENGTH,
            description="Optional. 1 to 255 visible ASCII characters. Retrying a request with "
            "the same key and an equivalent body returns the job the first request created "
            "(`200`) instead of creating another; the same key with a different body is a "
            "`409 idempotency_key_reused`. Keys are remembered for "
            "SITESCOPE_IDEMPOTENCY_KEY_TTL_SECONDS.",
        ),
    ] = None,
) -> CrawlJobSummary:
    """Validate the request and store the job with its seed URL, in one transaction.

    The seed URL is normalized and must name an approved crawl target; `max_depth` and
    `max_pages` must be within this server's limits. The job is created as `queued`: it waits
    for the scheduler, and nothing is fetched yet. The `Location` header points to the job.
    """
    submitted = await crawl_jobs.submit_crawl_job(
        session,
        settings,
        seed_url=body.seed_url,
        max_depth=body.max_depth,
        max_pages=body.max_pages,
        idempotency_key=idempotency_key,
    )
    job = submitted.job
    response.headers["Location"] = request.app.url_path_for("get_crawl", job_id=str(job.id))
    if not submitted.created:
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
    return CrawlJobSummary.model_validate(job)


@router.post(
    "/{job_id}/cancel",
    status_code=200,
    summary="Cancel a crawl job",
    dependencies=[Depends(require_write_access)],
    responses={
        202: {
            "description": "The job is `cancelling`: in-flight URLs are still finishing.",
            "model": CrawlJobSummary,
        },
        **problem_responses(401, 403, 404, 422, 503),
    },
)
async def cancel_crawl(
    job_id: uuid.UUID, response: Response, session: SessionDep
) -> CrawlJobSummary:
    """Cancel a job. Idempotent: repeating it, or cancelling a finished job, changes nothing.

    A `queued` job is `cancelled` at once (`200`). A `running` job stops: nothing new is
    dispatched or claimed, and its pending and queued URLs are cancelled at once. If none of its
    URLs is in progress, it is `cancelled` at once (`200`); otherwise it is `cancelling` (`202`)
    until its in-flight URLs are final (workers stop them within seconds; the URLs of a crashed
    worker are cancelled when their lease expires). The response is `202` whenever the job is
    still `cancelling`, and `200` with the unchanged job for a finished one (`completed`,
    `failed`, `cancelled`).
    """
    await crawl_cancellation.cancel_crawl_job(session, job_id)
    progress = await crawl_jobs.get_crawl_job(session, job_id)
    if progress.job.status == "cancelling":
        response.status_code = 202
    return CrawlJobSummary.model_validate(progress.job)


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
