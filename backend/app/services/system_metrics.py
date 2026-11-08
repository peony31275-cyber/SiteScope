"""What ``GET /metrics`` reports (Phase 10, ADR-0021): state counted at the source, and processes.

Three sources, each read once per request and reported separately, so one outage hides only
its own part:

* **PostgreSQL** (``read_system_state``): crawl jobs by status, admitted URLs by state, URLs
  waiting for a retry, expired leases waiting for a takeover, the oldest queued task's wait, and
  search documents by status with the oldest waiting ones, in one read-only REPEATABLE READ
  snapshot under the analysis time limit. These are the authoritative counts: every process's
  work ends in these rows, so counting them once can neither miss a process nor count one twice.
  Each count reads its whole table (like the dashboard's overview).
* **Redis streams**: length, entries not yet delivered (the group's lag), pending entries,
  consumers and dead letters of the crawl and index streams (approximate, ADR-0016).
* **Heartbeats** (``app.observability.heartbeat``): the live processes, their state and their
  metric snapshots.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.process import error_type
from app.db.models import JobStatus, UrlState
from app.db.session import Database
from app.observability.heartbeat import Heartbeat, read_heartbeats
from app.queue.streams import CrawlStream, IndexStream, TaskStream
from app.services.search_index import IndexingOverview, read_indexing_overview
from app.services.site_analysis import read_only_snapshot

_NOW = text("SELECT now()")
_JOBS_BY_STATUS = text("SELECT status, count(*) AS n FROM crawl_jobs GROUP BY status")
_URLS = text(
    """
    SELECT state, count(*) AS n,
           count(*) FILTER (WHERE state = 'pending' AND attempt_count > 0) AS retry_waiting,
           count(*) FILTER (WHERE state = 'in_progress'
                            AND lease_expires_at < clock_timestamp()) AS expired,
           extract(epoch FROM clock_timestamp()
                   - min(dispatched_at) FILTER (WHERE state = 'queued')) AS oldest_queued
    FROM crawl_urls GROUP BY state
    """
)


@dataclass(frozen=True, slots=True)
class SystemState:
    """Crawl and index state over every job, counted in one snapshot."""

    jobs_by_status: dict[JobStatus, int]
    urls_by_state: dict[UrlState, int]
    urls_waiting_for_retry: int  # pending after a failed attempt (their next_attempt_at)
    expired_leases: int  # in progress, lease expired: waiting for a takeover
    oldest_queued_seconds: float | None  # dispatched, not claimed yet; None if none is queued
    documents: IndexingOverview
    counted_at: datetime


async def read_system_state(session: AsyncSession, *, timeout_seconds: float) -> SystemState:
    """The PostgreSQL part (``AnalysisTimeoutError`` after ``timeout_seconds``)."""
    async with read_only_snapshot(session, timeout_seconds, subject="metrics"):
        counted_at = (await session.execute(_NOW)).scalar_one()
        jobs = {row.status: row.n for row in await session.execute(_JOBS_BY_STATUS)}
        urls: dict[str, int] = {}
        retry_waiting = expired = 0
        oldest: float | None = None
        for row in await session.execute(_URLS):
            urls[row.state] = row.n
            retry_waiting += row.retry_waiting
            expired += row.expired
            if row.oldest_queued is not None:
                oldest = max(0.0, float(row.oldest_queued))
        documents = await read_indexing_overview(session)
    return SystemState(
        jobs_by_status={status: jobs.get(status.value, 0) for status in JobStatus},
        urls_by_state={state: urls.get(state.value, 0) for state in UrlState},
        urls_waiting_for_retry=retry_waiting,
        expired_leases=expired,
        oldest_queued_seconds=oldest,
        documents=documents,
        counted_at=counted_at,
    )


@dataclass(frozen=True, slots=True)
class StreamStats:
    name: str  # "crawl" or "index"
    exists: bool
    length: int = 0
    undelivered: int | None = None  # the group's lag; None if Redis cannot tell
    pending: int = 0
    consumers: int = 0
    dead_letters: int = 0


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    """Everything ``GET /metrics`` renders; a source that failed has its error type instead."""

    state: SystemState | None = None
    state_error: str | None = None
    streams: list[StreamStats] = field(default_factory=list)
    heartbeats: list[Heartbeat] = field(default_factory=list)
    redis_error: str | None = None


async def stream_stats(name: str, stream: TaskStream[Any]) -> StreamStats:
    described = await stream.describe()
    if not described.get("exists"):
        return StreamStats(name=name, exists=False)
    group = described.get("group") or {}
    lag = group.get("lag")
    return StreamStats(
        name=name,
        exists=True,
        length=int(described.get("length", 0)),
        undelivered=int(lag) if isinstance(lag, int) else None,
        pending=int(group.get("pending", 0)),
        consumers=len(described.get("consumers") or []),
        dead_letters=int(described.get("dead_letters", 0)),
    )


async def collect_system_metrics(
    database: Database, redis: Redis, *, key_prefix: str, timeout_seconds: float
) -> SystemMetrics:
    """Read every source; a failed source is reported by its error type, never raised."""
    state: SystemState | None = None
    state_error: str | None = None
    try:
        async with database.session() as session:
            state = await read_system_state(session, timeout_seconds=timeout_seconds)
    except Exception as exc:  # reported as sitescope_source_up{source="postgres"} 0
        state_error = error_type(exc)
    streams: list[StreamStats] = []
    heartbeats: list[Heartbeat] = []
    redis_error: str | None = None
    try:
        streams = [
            await stream_stats("crawl", CrawlStream(redis, key_prefix=key_prefix)),
            await stream_stats("index", IndexStream(redis, key_prefix=key_prefix)),
        ]
        heartbeats = await read_heartbeats(redis, key_prefix=key_prefix)
    except Exception as exc:  # reported as sitescope_source_up{source="redis"} 0
        redis_error = error_type(exc)
    return SystemMetrics(
        state=state,
        state_error=state_error,
        streams=streams,
        heartbeats=heartbeats,
        redis_error=redis_error,
    )
