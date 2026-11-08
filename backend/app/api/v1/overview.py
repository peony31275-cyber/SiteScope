"""The overview of every crawl job, for the dashboard (Phase 9).

HTTP concerns only: the counts and their snapshot are in ``app/services/overview.py``.
"""

from datetime import datetime
from typing import Self

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import SessionDep, SettingsDep
from app.api.problems import problem_responses
from app.api.v1.crawl_schemas import DocumentStatusCounts, UrlStateCounts
from app.db.models import IndexStatus
from app.services.overview import SystemOverview, system_overview

router = APIRouter(tags=["overview"])


class JobStatusCounts(BaseModel):
    """Number of crawl jobs in each status."""

    queued: int
    running: int
    cancelling: int
    completed: int
    failed: int
    cancelled: int


class JobCounts(BaseModel):
    total: int = Field(description="Every crawl job.")
    by_status: JobStatusCounts = Field(
        description="The crawl's status of each job; indexing is counted in `documents`."
    )


class UrlCounts(BaseModel):
    total: int = Field(
        description="URLs admitted by every job, seeds included (the sum of the jobs' "
        "`admitted_count`). Link targets a job never admitted are not counted."
    )
    by_state: UrlStateCounts


class DocumentCounts(BaseModel):
    total: int = Field(description="Search documents: every stored page except `noindex` ones.")
    by_status: DocumentStatusCounts
    active: int = Field(description="Documents an indexer is working on now.")


class OverviewResponse(BaseModel):
    """Every crawl job, admitted URL, stored page and search document, counted in one
    snapshot of PostgreSQL."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "jobs": {
                        "total": 4,
                        "by_status": {
                            "queued": 0,
                            "running": 1,
                            "cancelling": 0,
                            "completed": 2,
                            "failed": 0,
                            "cancelled": 1,
                        },
                    },
                    "urls": {
                        "total": 52,
                        "by_state": {
                            "pending": 3,
                            "queued": 2,
                            "in_progress": 1,
                            "fetched": 40,
                            "failed": 1,
                            "skipped": 2,
                            "cancelled": 3,
                        },
                    },
                    "pages_stored": 36,
                    "documents": {
                        "total": 34,
                        "by_status": {"pending": 1, "queued": 2, "indexed": 31, "failed": 0},
                        "active": 1,
                    },
                    "counted_at": "2026-11-07T10:15:05.120Z",
                }
            ]
        }
    )

    jobs: JobCounts
    urls: UrlCounts
    pages_stored: int = Field(description="Stored HTML pages of every job (2xx HTML responses).")
    documents: DocumentCounts
    counted_at: datetime = Field(description="When the counts were read (database clock).")

    @classmethod
    def from_overview(cls, overview: SystemOverview) -> Self:
        documents = overview.documents_by_status
        return cls(
            jobs=JobCounts(
                total=overview.jobs_total,
                by_status=JobStatusCounts(
                    **{status.value: count for status, count in overview.jobs_by_status.items()}
                ),
            ),
            urls=UrlCounts(
                total=overview.urls_total,
                by_state=UrlStateCounts.from_counts(overview.urls_by_state),
            ),
            pages_stored=overview.pages_stored,
            documents=DocumentCounts(
                total=overview.documents_total,
                by_status=DocumentStatusCounts(
                    pending=documents[IndexStatus.PENDING],
                    queued=documents[IndexStatus.QUEUED],
                    indexed=documents[IndexStatus.INDEXED],
                    failed=documents[IndexStatus.FAILED],
                ),
                active=overview.documents_active,
            ),
            counted_at=overview.counted_at,
        )


@router.get(
    "/overview",
    summary="Get the overview of every crawl job",
    responses=problem_responses(
        503,
        descriptions={
            503: "The counts took longer than SITESCOPE_ANALYSIS_TIMEOUT_SECONDS and were "
            "stopped (`analysis_timeout`), or PostgreSQL is unavailable "
            "(`dependency_unavailable`, with `Retry-After`)."
        },
    ),
)
async def get_overview(session: SessionDep, settings: SettingsDep) -> OverviewResponse:
    """Count every crawl job by status, every admitted URL by state, the stored pages and the
    search documents by status, in one read-only snapshot of PostgreSQL.

    Each count reads every row of its table, so the time grows with the deployment's data; it
    is stopped after SITESCOPE_ANALYSIS_TIMEOUT_SECONDS. Discovered URLs (admitted ones plus
    link targets never admitted) are counted per job, in its statistics.
    """
    overview = await system_overview(session, timeout_seconds=settings.analysis_timeout_seconds)
    return OverviewResponse.from_overview(overview)
