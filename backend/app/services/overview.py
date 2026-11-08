"""Counts over every crawl job, for the dashboard's overview (Phase 9).

The job listing is cursor-paged and carries no totals, so a client cannot count jobs, URLs,
pages or search documents without walking every page. ``system_overview`` counts them in
PostgreSQL instead, in one read-only REPEATABLE READ transaction (``read_only_snapshot``), so the
numbers agree with each other. Nothing is estimated and nothing is cached.

**Cost.** Each count reads every row of its table (``crawl_jobs``, ``crawl_urls``, ``pages``,
``search_documents``): linear in the deployment's data, and bounded by the analysis time limit
(``SITESCOPE_ANALYSIS_TIMEOUT_SECONDS``, ``AnalysisTimeoutError``). The dashboard asks for it
only while its overview page is open.

Discovered URLs (admitted ones plus link targets never admitted) are not counted here: that
needs every stored link of every job. The statistics of one job report them
(``GET /api/v1/crawls/{job_id}/stats``).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IndexStatus, JobStatus, UrlState
from app.services.site_analysis import read_only_snapshot

_NOW = text("SELECT now()")
_JOBS_BY_STATUS = text("SELECT status, count(*) AS n FROM crawl_jobs GROUP BY status")
_URLS_BY_STATE = text("SELECT state, count(*) AS n FROM crawl_urls GROUP BY state")
_PAGES = text("SELECT count(*) FROM pages")
_DOCUMENTS_BY_STATUS = text(
    """
    SELECT status,
           count(*) AS n,
           count(*) FILTER (WHERE status = 'queued' AND lease_expires_at >= clock_timestamp())
               AS active
    FROM search_documents GROUP BY status
    """
)


@dataclass(frozen=True, slots=True)
class SystemOverview:
    """Every job, URL, page and search document, counted in one snapshot."""

    jobs_by_status: dict[JobStatus, int]
    urls_by_state: dict[UrlState, int]  # admitted URLs of every job
    pages_stored: int
    documents_by_status: dict[IndexStatus, int]
    documents_active: int  # queued documents an indexer is working on (live lease)
    counted_at: datetime  # the transaction's start, by the PostgreSQL clock

    @property
    def jobs_total(self) -> int:
        return sum(self.jobs_by_status.values())

    @property
    def urls_total(self) -> int:
        return sum(self.urls_by_state.values())

    @property
    def documents_total(self) -> int:
        return sum(self.documents_by_status.values())


async def system_overview(session: AsyncSession, *, timeout_seconds: float) -> SystemOverview:
    """Count every job by status, every admitted URL by state, the stored pages and the search
    documents by status, in one read-only snapshot (``AnalysisTimeoutError`` after
    ``timeout_seconds``)."""
    async with read_only_snapshot(session, timeout_seconds, subject="overview"):
        counted_at = (await session.execute(_NOW)).scalar_one()
        jobs = {row.status: row.n for row in await session.execute(_JOBS_BY_STATUS)}
        urls = {row.state: row.n for row in await session.execute(_URLS_BY_STATE)}
        pages = (await session.execute(_PAGES)).scalar_one()
        documents: dict[str, int] = {}
        active = 0
        for row in await session.execute(_DOCUMENTS_BY_STATUS):
            documents[row.status] = row.n
            active += row.active
    return SystemOverview(
        jobs_by_status={status: jobs.get(status.value, 0) for status in JobStatus},
        urls_by_state={state: urls.get(state.value, 0) for state in UrlState},
        pages_stored=pages,
        documents_by_status={status: documents.get(status.value, 0) for status in IndexStatus},
        documents_active=active,
        counted_at=counted_at,
    )
