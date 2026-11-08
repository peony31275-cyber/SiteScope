"""Read-only views of crawl and index state, for operators (``app.scheduler.status``).

Since Phase 10, ``assess`` turns those views and the processes' heartbeats into answers to an
operator's questions (is PostgreSQL or Redis down, are tasks progressing, are workers working,
is indexing falling behind, are failures repeating, has the system recovered).
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.heartbeat import Heartbeat
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


# --- Health assessment (Phase 10) ----------------------------------------------------------------

OK: Final = "ok"
WARNING: Final = "warning"
CRITICAL: Final = "critical"
UNKNOWN: Final = "unknown"


@dataclass(frozen=True, slots=True)
class Finding:
    status: str  # ok, warning, critical, unknown
    detail: str


def _number(value: object) -> float | None:
    """A finite number from a heartbeat (written by another process, maybe another version)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


# Heartbeat state fields that are positive while a process keeps failing: workers back off
# after stream or task errors; pass loops after failed passes, or name the jobs whose step failed
# in their last pass (one broken job fails every pass while the pass itself succeeds).
_FAILURE_FIELDS: Final = (
    "stream_failures",
    "task_failures",
    "consecutive_failures",
    "failed_jobs_last_pass",
)


@dataclass(frozen=True, slots=True)
class ProcessView:
    """A live process as its heartbeat describes it (``app.observability.heartbeat``).

    Ages are as of now: the heartbeat's own age is added to the ages it reports.
    """

    role: str
    name: str
    heartbeat_age_seconds: float
    state: Mapping[str, Any]
    uptime_seconds: float | None = None  # as of the heartbeat

    @classmethod
    def from_heartbeat(cls, heartbeat: Heartbeat) -> "ProcessView":
        return cls(
            role=heartbeat.role,
            name=heartbeat.name,
            heartbeat_age_seconds=heartbeat.age_seconds,
            state=heartbeat.state,
            uptime_seconds=_number(heartbeat.payload.get("uptime_seconds")),
        )

    def _age(self, key: str) -> float | None:
        """Seconds since the moment ``key`` reports; since the start if it never happened."""
        value = _number(self.state.get(key))
        if value is None:
            value = self.uptime_seconds
        return value + self.heartbeat_age_seconds if value is not None else None

    @property
    def progress_age_seconds(self) -> float | None:
        """Seconds since it last finished a task (workers, indexers) or did work in a pass
        (scheduler, index dispatcher); since it started if it never did."""
        return self._age("last_progress_seconds_ago")

    @property
    def pass_age_seconds(self) -> float | None:
        """Pass loops only: seconds since a pass last completed (since the start if none has).
        A pass that hangs leaves the heartbeat fresh, since another task writes it."""
        return self._age("last_pass_seconds_ago") if "passes" in self.state else None

    @property
    def failing(self) -> bool:
        """Backing off after errors (an outage, or a bug), or a job's step failed in its last
        pass, as of its last heartbeat."""
        return any(
            (value := _number(self.state.get(key))) is not None and value > 0
            for key in _FAILURE_FIELDS
        )

    @property
    def recovered_seconds_ago(self) -> float | None:
        value = _number(self.state.get("last_recovered_seconds_ago"))
        return value + self.heartbeat_age_seconds if value is not None else None


def _stale_passes(processes: Sequence[ProcessView], stall_seconds: float) -> list[str]:
    return [
        process.name
        for process in processes
        if (age := process.pass_age_seconds) is not None and age > stall_seconds
    ]


def assess(
    *,
    crawl: CrawlOverview | None,
    index: IndexOverview | None,
    redis_ok: bool,
    processes: Sequence[ProcessView],
    stall_seconds: float = 120.0,
) -> dict[str, Finding]:
    """Answers to the operator's questions, from authoritative data only: counts read from
    PostgreSQL, and processes from their own heartbeats (never from configured counts).

    ``stall_seconds``: how long work may wait, a busy process may go without progress, or a
    scheduler or index dispatcher without finishing a pass, before it is reported. ``crawl`` or
    ``index`` is None when PostgreSQL could not be read; heartbeats are empty when Redis could
    not be read, and then a finding that would depend on them is ``unknown``, never ``ok``.
    """
    by_role: dict[str, list[ProcessView]] = {}
    for process in processes:
        by_role.setdefault(process.role, []).append(process)

    def live(role: str) -> list[ProcessView]:
        return by_role.get(role, [])

    findings: dict[str, Finding] = {}
    apis = live("api")
    findings["api"] = (
        Finding(OK, f"{len(apis)} API process(es) with a live heartbeat")
        if apis
        else Finding(
            WARNING if redis_ok else UNKNOWN,
            "no API heartbeat: the API is down, or runs without heartbeats"
            if redis_ok
            else "unknown: Redis could not be read",
        )
    )
    findings["postgres"] = (
        Finding(OK, "reachable") if crawl is not None else Finding(CRITICAL, "not reachable")
    )
    findings["redis"] = Finding(OK, "reachable") if redis_ok else Finding(CRITICAL, "not reachable")

    if crawl is None:
        findings["crawl_progress"] = Finding(UNKNOWN, "PostgreSQL could not be read")
    else:
        running = crawl.jobs_by_status.get("running", 0) + crawl.jobs_by_status.get("cancelling", 0)
        has_jobs = running + crawl.jobs_by_status.get("queued", 0) > 0
        problems: list[str] = []
        status = OK
        schedulers = live("scheduler")
        if running and redis_ok and not schedulers:
            status, problems = (
                CRITICAL,
                [*problems, "no scheduler is running: nothing is dispatched"],
            )
        if stale := _stale_passes(schedulers, stall_seconds):
            # A pass that hangs (a lock wait, a slow statement) starts, dispatches and
            # advances nothing; critical if every scheduler is in that state while jobs wait.
            every = len(stale) == len(schedulers)
            status = CRITICAL if status == CRITICAL or (has_jobs and every) else WARNING
            problems.append(
                f"no scheduler pass finished for {stall_seconds:.0f} s: {', '.join(stale)}"
            )
        if running and redis_ok and not live("crawler"):
            status, problems = CRITICAL, [*problems, "no crawler worker is running"]
        oldest = crawl.oldest_queued_seconds
        if oldest is not None and oldest > stall_seconds:
            status = CRITICAL if status == CRITICAL else WARNING
            problems.append(f"the oldest queued task has waited {oldest:.0f} s")
        if crawl.expired_leases:
            status = CRITICAL if status == CRITICAL else WARNING
            problems.append(f"{crawl.expired_leases} expired lease(s) wait for a takeover")
        if status == OK and not redis_ok and has_jobs:
            # Dispatch needs Redis: URLs it cannot publish stay pending, which no rule above
            # counts, and without heartbeats nothing shows that tasks move. Never "ok".
            status = UNKNOWN
            problems.append("unknown whether tasks move: Redis could not be read")
        summary = f"{running} running job(s)"
        findings["crawl_progress"] = Finding(
            status, summary + ("; " + "; ".join(problems) if problems else "")
        )

    crawlers = live("crawler")
    if not crawlers:
        findings["crawler_workers"] = Finding(
            WARNING if redis_ok else UNKNOWN,
            "no crawler worker heartbeat" if redis_ok else "unknown: Redis could not be read",
        )
    else:
        working = sum(1 for process in crawlers if process.state.get("in_flight"))
        stalled = [
            process.name
            for process in crawlers
            if process.state.get("in_flight")
            and (age := process.progress_age_seconds) is not None
            and age > stall_seconds
        ]
        findings["crawler_workers"] = Finding(
            WARNING if stalled else OK,
            f"{len(crawlers)} live, {working} running tasks"
            + (
                f"; no task finished for {stall_seconds:.0f} s: {', '.join(stalled)}"
                if stalled
                else ""
            ),
        )

    if index is None:
        findings["indexing"] = Finding(UNKNOWN, "PostgreSQL could not be read")
    else:
        documents = index.documents
        waiting = documents.pending + documents.queued
        oldest_waiting = max(
            documents.oldest_pending_seconds or 0.0, documents.oldest_queued_seconds or 0.0
        )
        problems = []
        status = OK
        dispatchers = live("index_dispatcher")
        if waiting and redis_ok and not live("indexer"):
            status, problems = CRITICAL, ["no indexer is running"]
        if documents.pending and redis_ok and not dispatchers:
            status = CRITICAL
            problems.append("no index dispatcher is running: nothing is published")
        if stale := _stale_passes(dispatchers, stall_seconds):
            every = len(stale) == len(dispatchers)
            status = CRITICAL if status == CRITICAL or (documents.pending and every) else WARNING
            problems.append(
                f"no index dispatcher pass finished for {stall_seconds:.0f} s: {', '.join(stale)}"
            )
        if waiting and oldest_waiting > stall_seconds:
            status = CRITICAL if status == CRITICAL else WARNING
            problems.append(f"the oldest waiting document has waited {oldest_waiting:.0f} s")
        if status == OK and not redis_ok and waiting:
            status = UNKNOWN
            problems.append("unknown whether documents move: Redis could not be read")
        findings["indexing"] = Finding(
            status,
            f"{waiting} document(s) waiting, {documents.failed} failed"
            + ("; " + "; ".join(problems) if problems else ""),
        )

    if not redis_ok:
        unknown = Finding(UNKNOWN, "unknown: the heartbeats in Redis could not be read")
        findings["repeated_failures"] = unknown
        findings["recovery"] = unknown
        return findings
    failing = [process.name for process in processes if process.failing]
    findings["repeated_failures"] = Finding(
        WARNING if failing else OK,
        f"failing (backing off after errors, or a job's step failed in the last pass): "
        f"{', '.join(failing)}"
        if failing
        else "no process is backing off after errors or failing a job",
    )
    recovered = [
        f"{process.name} ({age:.0f} s ago)"
        for process in processes
        if (age := process.recovered_seconds_ago) is not None and age <= 3600
    ]
    if failing:
        findings["recovery"] = Finding(WARNING, f"not recovered yet: {', '.join(failing)}")
    elif recovered:
        findings["recovery"] = Finding(OK, f"recovered from errors: {', '.join(recovered)}")
    else:
        findings["recovery"] = Finding(OK, "no recovery in the last hour")
    return findings
