"""Read-only views of crawl and index state, for operators (``app.scheduler.status``)."""

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.search_index import IndexingOverview, read_indexing_overview

_JOBS_BY_STATUS = text("SELECT status, count(*) AS n FROM crawl_jobs GROUP BY status")

_ACTIVE_JOBS = text(
    """
    SELECT id, status, current_level, max_depth, admitted_count, max_pages
    FROM crawl_jobs WHERE status IN ('running', 'cancelling')
    ORDER BY created_at, id LIMIT :limit
    """
)

_URLS_BY_STATE = text(
    """
    SELECT job_id, state, count(*) AS n FROM crawl_urls
    WHERE job_id = ANY(CAST(:ids AS uuid[])) GROUP BY job_id, state
    """
)

_LEASES = text(
    """
    SELECT lease_owner,
           count(*) FILTER (WHERE lease_expires_at >= clock_timestamp()) AS live,
           count(*) FILTER (WHERE lease_expires_at < clock_timestamp()) AS expired
    FROM crawl_urls WHERE state = 'in_progress'
    GROUP BY lease_owner ORDER BY lease_owner
    """
)

# Index attempts running now (Phase 7): queued documents with a live lease, per indexer.
_INDEX_LEASES = text(
    """
    SELECT lease_owner, count(*) AS live
    FROM search_documents
    WHERE status = 'queued' AND lease_expires_at >= clock_timestamp()
    GROUP BY lease_owner ORDER BY lease_owner
    """
)

_OLDEST_QUEUED = text(
    """
    SELECT extract(epoch FROM clock_timestamp() - min(dispatched_at))
    FROM crawl_urls WHERE state = 'queued'
    """
)


@dataclass(frozen=True, slots=True)
class ActiveJob:
    job_id: str
    status: str
    current_level: int
    max_depth: int
    admitted_count: int
    max_pages: int
    urls_by_state: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CrawlOverview:
    jobs_by_status: dict[str, int]
    active_jobs: list[ActiveJob]  # running or cancelling, oldest first
    live_leases_by_owner: dict[str, int]  # URLs in progress per worker, unexpired leases
    expired_leases: int  # in progress, lease expired: waiting for a takeover
    oldest_queued_seconds: float | None  # how long the oldest queued URL has waited


async def crawl_overview(session: AsyncSession, *, limit: int = 20) -> CrawlOverview:
    """Jobs, URL states and leases, read in one read-only transaction."""
    async with session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        jobs_by_status = {row.status: row.n for row in await session.execute(_JOBS_BY_STATUS)}
        jobs = [
            ActiveJob(
                job_id=str(row.id),
                status=row.status,
                current_level=row.current_level,
                max_depth=row.max_depth,
                admitted_count=row.admitted_count,
                max_pages=row.max_pages,
            )
            for row in await session.execute(_ACTIVE_JOBS, {"limit": limit})
        ]
        if jobs:
            by_id = {job.job_id: job for job in jobs}
            for row in await session.execute(_URLS_BY_STATE, {"ids": list(by_id)}):
                by_id[str(row.job_id)].urls_by_state[row.state] = row.n
        live: dict[str, int] = {}
        expired = 0
        for row in await session.execute(_LEASES):
            if row.live:
                live[row.lease_owner] = row.live
            expired += row.expired
        oldest = await session.scalar(_OLDEST_QUEUED)
    return CrawlOverview(
        jobs_by_status=jobs_by_status,
        active_jobs=jobs,
        live_leases_by_owner=live,
        expired_leases=expired,
        oldest_queued_seconds=float(oldest) if oldest is not None else None,
    )


@dataclass(frozen=True, slots=True)
class IndexOverview:
    """Search documents over all jobs (Phase 7), and the index attempts running now."""

    documents: IndexingOverview
    live_leases_by_indexer: dict[str, int]  # queued documents per indexer, unexpired leases


async def index_overview(session: AsyncSession) -> IndexOverview:
    """Index statistics, read in one read-only transaction."""
    async with session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        documents = await read_indexing_overview(session)
        leases = {row.lease_owner: row.live for row in await session.execute(_INDEX_LEASES)}
    return IndexOverview(documents=documents, live_leases_by_indexer=leases)
