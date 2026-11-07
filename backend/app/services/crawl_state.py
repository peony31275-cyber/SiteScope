"""Crawl state: the URL frontier, leases, fenced results, and level-synchronous BFS (Phase 4).

These are the use cases every crawler process shares: the single-process driver
(``app.workers.local_crawler``) now, the scheduler and the distributed workers in Phase 5.
PostgreSQL is the source of truth. Each function runs one short transaction, and no
transaction is open during network I/O (architecture, section 9):

* **T2** ``activate_job``: ``queued`` -> ``running``, a compare-and-set.
* **T3** ``dispatch_urls``: ``pending`` URLs at the job's current level -> ``queued``, selected
  with ``FOR UPDATE SKIP LOCKED``. Phase 5 publishes each one to Redis Streams inside this
  transaction; here the caller receives the IDs directly.
* **T4** ``claim_url``: ``queued``, or ``in_progress`` with a lease that expired by the database
  clock, -> ``in_progress``. The claim issues a new fencing token (``lease_epoch + 1``,
  ADR-0011) and counts an attempt; a URL with no attempts left is never claimed.
* **T5** ``record_result``: the fenced result. Its first statement is the fenced update of the
  URL row: ``WHERE state = 'in_progress' AND lease_epoch = :token`` and
  ``SET result_epoch = :token``, always the token the claim returned to this worker, never a
  value copied from the row (ADR-0011). If it matches no row, the lease was lost: the
  transaction rolls back, nothing is written, and the result is reported as ``FENCED``.
  Otherwise the same transaction writes the page, its links and its ``search_documents`` row
  (not for ``noindex`` pages), and admits a redirect target at the same depth under the job row
  lock. Any failure rolls all of it back.
* **T6** ``advance_job``: under the job row lock, and only when no URL of the job is unfinished:
  admit the next level (deterministic order, exact budget), or finish the job.
* **Recovery** ``recover_exhausted_url``: a URL whose lease expired after its last allowed
  attempt fails as ``max_attempts_exceeded``. A recovery claim issues a new token first, so the
  failure is a fenced result like any other, and a stalled holder is fenced out.

**Lock order.** ``record_result`` locks the URL row (its fenced update) before the job row (only
to admit a redirect). ``advance_job`` locks the job row first, but then only reads URL rows and
inserts new ones; it never locks an ``in_progress`` row. ``activate_job`` and
``dispatch_urls`` lock one table each. There is no cycle, so there is no deadlock.

**Budget and deduplication.** Every admission (job creation, ``advance_job``, redirect
admission) holds the job row lock, inserts with ``ON CONFLICT DO NOTHING`` on
``UNIQUE (job_id, normalized_url)``, and adds exactly the number of inserted rows to
``admitted_count`` in the same transaction. ``CHECK (admitted_count <= max_pages)`` is the
backstop.
"""

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.crawler.urls import InvalidUrlError, normalize_url
from app.db.models import (
    UNFINISHED_URL_STATES,
    WORKER_OUTCOME_STATES,
    CrawlEvent,
    CrawlJob,
    CrawlUrl,
    JobStatus,
    Page,
    PageLink,
    SearchDocument,
    UrlState,
    sql_in,
)

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS: Final = 2000  # crawl_urls.last_error
EXHAUSTED_OUTCOME: Final = "max_attempts_exceeded"
# How long a recovery claim's lease lasts; it is released in the same transaction.
_RECOVERY_LEASE_SECONDS: Final = 60.0
_OUTCOME = re.compile(r"[a-z][a-z0-9_]*")


# --- Results produced by a crawl task ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Lease:
    """A claimed URL. ``token`` is the fencing token: the ``lease_epoch`` this claim issued."""

    url_id: uuid.UUID
    job_id: uuid.UUID
    token: int
    owner: str
    url: str  # the normalized URL
    depth: int
    redirect_hops: int
    attempt: int  # attempt_count after this claim
    allowed_hosts: tuple[str, ...]  # the job's scope
    takeover: bool  # the claim took over an expired lease


@dataclass(frozen=True, slots=True)
class LinkRecord:
    url: str  # normalized
    position: int
    anchor_text: str | None
    nofollow: bool  # rel="nofollow"
    is_internal: bool  # in the job's scope and an approved crawl target


@dataclass(frozen=True, slots=True)
class PageRecord:
    """What a fetched HTML page stores. Text is sanitized and hashed by ``record_result``."""

    http_status: int
    content_type: str | None
    title: str | None
    description: str | None
    headings: str
    text: str
    byte_size: int  # decoded body bytes
    fetch_duration_ms: int | None
    noindex: bool
    nofollow: bool
    links: tuple[LinkRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class UrlResult:
    """The outcome of one crawl task: a terminal worker state and what to store with it."""

    state: UrlState  # fetched, skipped or failed
    outcome: str  # e.g. html_stored, redirect, http_error, robots_disallowed, read_timeout
    http_status: int | None = None
    error: str | None = None  # safe to store and show: never a full URL or a page body
    redirect_target: str | None = None  # the normalized Location of a redirect
    # The redirect target is in the job's scope: admit it at the same depth if the redirect hop
    # limit and the page budget allow.
    follow_redirect: bool = False
    page: PageRecord | None = None

    def __post_init__(self) -> None:
        if self.state not in WORKER_OUTCOME_STATES:
            raise ValueError(f"{self.state} is not a worker outcome")
        if not _OUTCOME.fullmatch(self.outcome):
            raise ValueError(f"invalid outcome {self.outcome!r}")
        if self.page is not None and self.state is not UrlState.FETCHED:
            raise ValueError("only a fetched URL stores a page")


class RecordStatus(StrEnum):
    RECORDED = "recorded"
    FENCED = "fenced"  # the lease was lost: nothing was written


@dataclass(frozen=True, slots=True)
class RecordedResult:
    status: RecordStatus
    page_id: uuid.UUID | None = None
    redirect_url_id: uuid.UUID | None = None  # the redirect target admitted with this result


class AdvanceAction(StrEnum):
    NOT_RUNNING = "not_running"  # the job is not running: nothing to do
    LEVEL_IN_PROGRESS = "level_in_progress"  # some URL is still pending, queued or in progress
    ADVANCED = "advanced"  # the next level was admitted
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    action: AdvanceAction
    status: JobStatus  # the job's status after the call
    level: int  # the job's current level after the call
    admitted: int = 0  # URLs admitted by this call


@dataclass(frozen=True, slots=True)
class FrontierUrl:
    """A URL of the current level that a worker may act on now (``find_actionable_urls``)."""

    url_id: uuid.UUID
    exhausted: bool  # its lease expired after its last allowed attempt: recover, do not claim


class _FencedOutError(Exception):
    """The fenced update matched no row; raised inside the transaction to roll it back."""


# --- SQL ---------------------------------------------------------------------------------------

# Job timestamps never precede created_at (ck_crawl_jobs_timestamps_ordered), even if the
# database host's clock steps back: the Phase 4 tests caught a one-second backward step of the
# WSL 2 VM clock between a job's creation and its end.
_ACTIVATE = text(
    """
    UPDATE crawl_jobs SET status = 'running', started_at = GREATEST(clock_timestamp(), created_at)
    WHERE id = :job_id AND status = 'queued'
    RETURNING id
    """
)

_DISPATCH = text(
    """
    UPDATE crawl_urls AS u
    SET state = 'queued', dispatched_at = clock_timestamp()
    WHERE u.state = 'pending' AND u.id IN (
        SELECT c.id
        FROM crawl_urls AS c
        JOIN crawl_jobs AS j ON j.id = c.job_id
        WHERE c.job_id = :job_id AND j.status = 'running'
          AND c.state = 'pending' AND c.depth = j.current_level
          AND c.next_attempt_at <= clock_timestamp()
        ORDER BY c.next_attempt_at, c.id
        LIMIT :limit
        FOR UPDATE OF c SKIP LOCKED
    )
    RETURNING u.id
    """
)

# T4 (architecture, section 9). The token is the incremented lease_epoch, returned to the
# claimant; old.* (PostgreSQL 18) tells a takeover from a first claim.
_CLAIM = text(
    """
    UPDATE crawl_urls AS u
    SET state = 'in_progress',
        lease_epoch = u.lease_epoch + 1,
        attempt_count = u.attempt_count + 1,
        lease_owner = :owner,
        lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds),
        result_epoch = NULL
    FROM crawl_jobs AS j
    WHERE u.id = :url_id
      AND j.id = u.job_id AND j.status = 'running'
      AND u.attempt_count < :max_attempts
      AND (u.state = 'queued'
           OR (u.state = 'in_progress' AND u.lease_expires_at < clock_timestamp()))
    RETURNING u.lease_epoch, u.job_id, u.normalized_url, u.depth, u.redirect_hops,
              u.attempt_count, j.allowed_hosts,
              old.state AS previous_state, old.lease_owner AS previous_owner
    """
)

# A recovery claim: takes over an expired lease whose URL has no attempt left, without counting
# an attempt. The new token then records the failure through the normal fenced update.
_RECOVERY_CLAIM = text(
    """
    UPDATE crawl_urls
    SET lease_epoch = lease_epoch + 1,
        lease_owner = :owner,
        lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds),
        result_epoch = NULL
    WHERE id = :url_id AND state = 'in_progress'
      AND lease_expires_at < clock_timestamp()
      AND attempt_count >= :max_attempts
    RETURNING lease_epoch, job_id, normalized_url, depth, redirect_hops, attempt_count,
              old.lease_owner AS previous_owner
    """
)

_ACTIONABLE = text(
    """
    SELECT u.id, (u.state = 'in_progress' AND u.attempt_count >= :max_attempts) AS exhausted
    FROM crawl_urls AS u
    JOIN crawl_jobs AS j ON j.id = u.job_id
    WHERE u.job_id = :job_id AND j.status = 'running' AND u.depth = j.current_level
      AND (u.state = 'queued'
           OR (u.state = 'in_progress' AND u.lease_expires_at < clock_timestamp()))
    ORDER BY u.id
    LIMIT :limit
    """
)

# Served by the partial index ix_crawl_urls_unfinished. sql_in renders constants from the model.
_HAS_UNFINISHED = text(
    "SELECT EXISTS (SELECT 1 FROM crawl_urls"  # noqa: S608 - only model constants are inserted
    f" WHERE job_id = :job_id AND {sql_in('state', UNFINISHED_URL_STATES)})"
)

# T6 admission. Candidates: links of the level's stored pages that are internal and followable,
# whose targets the job does not know yet. Each target keeps its first occurrence in BFS order
# (source URL, then link position); the first :remaining of them in that order are admitted.
_ADMIT_NEXT_LEVEL = text(
    """
    WITH candidates AS (
        SELECT DISTINCT ON (l.target_normalized_url)
               l.target_normalized_url AS url,
               s.id AS source_id,
               s.normalized_url AS source_url,
               l.position AS link_position
        FROM crawl_urls AS s
        JOIN pages AS p ON p.crawl_url_id = s.id AND p.job_id = s.job_id
        JOIN page_links AS l ON l.source_page_id = p.id AND l.job_id = p.job_id
        WHERE s.job_id = :job_id AND s.depth = :level AND s.state = 'fetched'
          AND NOT p.robots_nofollow AND l.is_internal AND NOT l.nofollow
          AND NOT EXISTS (
              SELECT 1 FROM crawl_urls AS e
              WHERE e.job_id = :job_id AND e.normalized_url = l.target_normalized_url
          )
        ORDER BY l.target_normalized_url, s.normalized_url, l.position
    ),
    chosen AS (
        SELECT url, source_id, source_url, link_position
        FROM candidates
        ORDER BY source_url, link_position
        LIMIT :remaining
    )
    INSERT INTO crawl_urls (job_id, normalized_url, depth, discovered_from_url_id, state)
    SELECT :job_id, url, :next_level, source_id, 'pending'
    FROM chosen
    ORDER BY source_url, link_position
    ON CONFLICT (job_id, normalized_url) DO NOTHING
    RETURNING id
    """
)


# --- Use cases ---------------------------------------------------------------------------------


async def activate_job(session: AsyncSession, job_id: uuid.UUID) -> bool:
    """T2: move a ``queued`` job to ``running``. False if it was not queued (a no-op)."""
    async with session.begin():
        activated = (await session.execute(_ACTIVATE, {"job_id": job_id})).scalar_one_or_none()
        if activated is not None:
            _add_event(session, job_id, "job.started", from_state="queued", to_state="running")
    if activated is not None:
        logger.info("Crawl job started", extra={"event": "job.started", "job_id": str(job_id)})
    return activated is not None


async def dispatch_urls(session: AsyncSession, job_id: uuid.UUID, *, limit: int) -> list[uuid.UUID]:
    """T3: mark up to ``limit`` due ``pending`` URLs of the job's current level ``queued``.

    Only a ``running`` job is dispatched, and only at its ``current_level`` (ADR-0004). Rows
    locked by a concurrent dispatcher are skipped, so two dispatchers never take the same URL.
    """
    if limit <= 0:
        return []
    async with session.begin():
        rows = await session.execute(_DISPATCH, {"job_id": job_id, "limit": limit})
        return [row.id for row in rows]


async def claim_url(
    session: AsyncSession,
    url_id: uuid.UUID,
    *,
    owner: str,
    lease_seconds: float,
    max_attempts: int,
) -> Lease | None:
    """T4: lease a URL to ``owner``. None if it is not claimable now (a no-op).

    Claimable: the job is running, the URL has attempts left, and it is ``queued`` or its
    lease expired by the database clock (a takeover). ``owner`` must be unique per process
    start (ADR-0011). A takeover is recorded as a ``url.recovered`` event.
    """
    async with session.begin():
        row = (
            await session.execute(
                _CLAIM,
                {
                    "url_id": url_id,
                    "owner": owner,
                    "lease_seconds": lease_seconds,
                    "max_attempts": max_attempts,
                },
            )
        ).one_or_none()
        if row is None:
            return None
        takeover = row.previous_state == UrlState.IN_PROGRESS.value
        if takeover:
            _add_event(
                session,
                row.job_id,
                "url.recovered",
                crawl_url_id=url_id,
                from_state="in_progress",
                to_state="in_progress",
                worker_id=owner,
                lease_epoch=row.lease_epoch,
                detail={"previous_owner": row.previous_owner, "attempt": row.attempt_count},
            )
    lease = Lease(
        url_id=url_id,
        job_id=row.job_id,
        token=row.lease_epoch,
        owner=owner,
        url=row.normalized_url,
        depth=row.depth,
        redirect_hops=row.redirect_hops,
        attempt=row.attempt_count,
        allowed_hosts=tuple(row.allowed_hosts),
        takeover=takeover,
    )
    if takeover:
        logger.warning(
            "Took over an expired lease",
            extra={
                "event": "lease.taken_over",
                "job_id": str(lease.job_id),
                "url_id": str(url_id),
                "worker_id": owner,
                "lease_epoch": lease.token,
                "attempt": lease.attempt,
            },
        )
    return lease


async def record_result(
    session: AsyncSession, lease: Lease, result: UrlResult, *, max_redirect_hops: int
) -> RecordedResult:
    """T5: commit a task's result under its lease, all or nothing. See the module docstring.

    Returns ``FENCED`` if the lease was lost (another claim took the URL over, or the URL is no
    longer in progress); nothing was written then, and the caller must discard the result.
    """
    try:
        async with session.begin():
            recorded = await _record(session, lease, result, max_redirect_hops=max_redirect_hops)
    except _FencedOutError:
        logger.warning(
            "Result rejected: the lease was lost",
            extra={
                "event": "task.fenced",
                "job_id": str(lease.job_id),
                "url_id": str(lease.url_id),
                "worker_id": lease.owner,
                "lease_epoch": lease.token,
            },
        )
        return RecordedResult(status=RecordStatus.FENCED)
    logger.debug(
        "Result recorded",
        extra={
            "event": "task.committed",
            "job_id": str(lease.job_id),
            "url_id": str(lease.url_id),
            "lease_epoch": lease.token,
            "outcome": result.outcome,
        },
    )
    return recorded


async def find_actionable_urls(
    session: AsyncSession, job_id: uuid.UUID, *, max_attempts: int, limit: int
) -> list[FrontierUrl]:
    """URLs of the current level that need a claim now: ``queued`` ones, and ``in_progress``
    ones whose lease expired by the database clock (the in-process stand-in for stream
    redelivery and ``XAUTOCLAIM``, Phases 5 and 6)."""
    if limit <= 0:
        return []
    async with session.begin():
        rows = await session.execute(
            _ACTIONABLE, {"job_id": job_id, "max_attempts": max_attempts, "limit": limit}
        )
        return [FrontierUrl(url_id=row.id, exhausted=row.exhausted) for row in rows]


async def recover_exhausted_url(
    session: AsyncSession,
    url_id: uuid.UUID,
    *,
    owner: str,
    max_attempts: int,
) -> bool:
    """Fail a URL whose lease expired after its last allowed attempt (``max_attempts_exceeded``).

    One transaction: a recovery claim issues a new token (no attempt is counted), and that
    token records the failure through the fenced update. Only an expired lease, by the
    database clock, is ever recovered. False if the URL does not qualify (a no-op).
    """
    try:
        async with session.begin():
            row = (
                await session.execute(
                    _RECOVERY_CLAIM,
                    {
                        "url_id": url_id,
                        "owner": owner,
                        "lease_seconds": _RECOVERY_LEASE_SECONDS,
                        "max_attempts": max_attempts,
                    },
                )
            ).one_or_none()
            if row is None:
                return False
            lease = Lease(
                url_id=url_id,
                job_id=row.job_id,
                token=row.lease_epoch,
                owner=owner,
                url=row.normalized_url,
                depth=row.depth,
                redirect_hops=row.redirect_hops,
                attempt=row.attempt_count,
                allowed_hosts=(),
                takeover=True,
            )
            await _record(
                session,
                lease,
                UrlResult(
                    state=UrlState.FAILED,
                    outcome=EXHAUSTED_OUTCOME,
                    error=f"the lease expired after attempt {row.attempt_count} of {max_attempts}",
                ),
                max_redirect_hops=0,
            )
            _add_event(
                session,
                row.job_id,
                "url.failed",
                crawl_url_id=url_id,
                from_state="in_progress",
                to_state="failed",
                worker_id=owner,
                lease_epoch=lease.token,
                detail={
                    "reason": EXHAUSTED_OUTCOME,
                    "attempts": row.attempt_count,
                    "previous_owner": row.previous_owner,
                },
            )
    except _FencedOutError:  # pragma: no cover - the recovery claim holds the row lock
        return False
    logger.warning(
        "Failed a URL after its last attempt",
        extra={
            "event": "url.failed",
            "job_id": str(lease.job_id),
            "url_id": str(url_id),
            "worker_id": owner,
            "lease_epoch": lease.token,
            "attempt": row.attempt_count,
        },
    )
    return True


async def advance_job(session: AsyncSession, job_id: uuid.UUID) -> AdvanceResult:
    """T6: admit the next BFS level, or finish the job, once its current level is done.

    Under the job row lock, and only when no URL of the job is pending, queued or in progress:

    * if ``current_level < max_depth`` and budget remains, admit the new internal, followable
      link targets of the level's stored pages at ``current_level + 1``: the first ones in BFS
      order (source URL, then link position), at most the remaining budget;
    * if nothing was admitted, finish: ``completed`` if the seed URL was fetched, otherwise
      ``failed`` with the seed's outcome as the reason.

    Raises ``NotFoundError`` for an unknown job. Safe to call repeatedly and concurrently: the
    job row lock serializes callers, and each one re-checks the state it locked.
    """
    async with session.begin():
        # Plain columns, not the ORM entity: a session that already holds the CrawlJob object
        # would return it unrefreshed (identity map), with a stale level and budget, although
        # the row itself is locked and current (Phase 4 review).
        job = (
            await session.execute(
                select(
                    CrawlJob.id,
                    CrawlJob.seed_url,
                    CrawlJob.status,
                    CrawlJob.current_level,
                    CrawlJob.max_depth,
                    CrawlJob.max_pages,
                    CrawlJob.admitted_count,
                )
                .where(CrawlJob.id == job_id)
                .with_for_update()
            )
        ).one_or_none()
        if job is None:
            raise NotFoundError(f"Crawl job {job_id} does not exist.")
        status = JobStatus(job.status)
        level = job.current_level
        if status is not JobStatus.RUNNING:
            return AdvanceResult(AdvanceAction.NOT_RUNNING, status, level)
        if await session.scalar(_HAS_UNFINISHED, {"job_id": job_id}):
            return AdvanceResult(AdvanceAction.LEVEL_IN_PROGRESS, status, level)

        remaining = job.max_pages - job.admitted_count
        admitted = 0
        if level < job.max_depth and remaining > 0:
            rows = await session.execute(
                _ADMIT_NEXT_LEVEL,
                {
                    "job_id": job_id,
                    "level": level,
                    "next_level": level + 1,
                    "remaining": remaining,
                },
            )
            admitted = len(rows.all())
        if admitted:
            await session.execute(
                update(CrawlJob)
                .where(CrawlJob.id == job_id)
                .values(
                    current_level=CrawlJob.current_level + 1,
                    admitted_count=CrawlJob.admitted_count + admitted,
                )
                .execution_options(synchronize_session=False)
            )
            _add_event(
                session,
                job_id,
                "job.level_advanced",
                detail={
                    "level": level + 1,
                    "admitted": admitted,
                    "budget_left": remaining - admitted,
                },
            )
            result = AdvanceResult(AdvanceAction.ADVANCED, status, level + 1, admitted)
        else:
            result = await _finish_job(
                session,
                job_id,
                seed_url=job.seed_url,
                level=level,
                admitted_count=job.admitted_count,
            )

    _log_advance(job_id, result)
    return result


# --- Internals ---------------------------------------------------------------------------------


async def _record(
    session: AsyncSession, lease: Lease, result: UrlResult, *, max_redirect_hops: int
) -> RecordedResult:
    """The body of T5, inside the caller's transaction. Raises ``_FencedOutError``."""
    finished = (
        await session.execute(
            update(CrawlUrl)
            .where(
                CrawlUrl.id == lease.url_id,
                CrawlUrl.job_id == lease.job_id,
                CrawlUrl.state == UrlState.IN_PROGRESS.value,
                # The fencing check, with the worker's own token (ADR-0003, ADR-0011).
                CrawlUrl.lease_epoch == lease.token,
            )
            .values(
                state=result.state.value,
                outcome=result.outcome,
                http_status=_http_status(result.http_status),
                last_error=_db_text(result.error, MAX_ERROR_CHARS),
                redirect_target=result.redirect_target,
                # Never "result_epoch = lease_epoch": that would accept any lease's result.
                result_epoch=lease.token,
                lease_owner=None,
                lease_expires_at=None,
                finished_at=func.clock_timestamp(),
            )
            .returning(CrawlUrl.normalized_url, CrawlUrl.depth, CrawlUrl.redirect_hops)
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()
    if finished is None:
        raise _FencedOutError

    page_id = None
    if result.page is not None:
        page_id = await _store_page(session, lease, finished.normalized_url, result.page)

    redirect_url_id = None
    if (
        result.redirect_target is not None
        and result.follow_redirect
        and finished.redirect_hops < max_redirect_hops
    ):
        redirect_url_id = await _admit_redirect(
            session,
            lease,
            target=result.redirect_target,
            depth=finished.depth,
            hops=finished.redirect_hops + 1,
        )
    return RecordedResult(RecordStatus.RECORDED, page_id=page_id, redirect_url_id=redirect_url_id)


async def _store_page(
    session: AsyncSession, lease: Lease, normalized_url: str, page: PageRecord
) -> uuid.UUID:
    title = _db_text(page.title)
    description = _db_text(page.description)
    headings = _db_text(page.headings) or ""
    body_text = _db_text(page.text) or ""
    # No ON CONFLICT: a second page for one crawl URL would break an invariant (fencing makes it
    # impossible), so the unique violation must roll the whole result back.
    inserted = await session.execute(
        insert(Page)
        .values(
            job_id=lease.job_id,
            crawl_url_id=lease.url_id,
            normalized_url=normalized_url,
            http_status=page.http_status,
            content_type=page.content_type,
            title=title,
            meta_description=description,
            headings=headings,
            extracted_text=body_text,
            content_hash=_content_hash(title, description, headings, body_text),
            byte_size=page.byte_size,
            fetch_duration_ms=page.fetch_duration_ms,
            fetched_at=func.now(),
            robots_noindex=page.noindex,
            robots_nofollow=page.nofollow,
        )
        .returning(Page.id)
    )
    page_id: uuid.UUID = inserted.scalar_one()
    if page.links:
        await session.execute(
            insert(PageLink),
            [
                {
                    "source_page_id": page_id,
                    "target_normalized_url": link.url,
                    "job_id": lease.job_id,
                    "anchor_text": _db_text(link.anchor_text),
                    "position": link.position,
                    "is_internal": link.is_internal,
                    "nofollow": link.nofollow,
                }
                for link in page.links
            ],
        )
    if not page.noindex:
        # The indexing outbox (ADR-0008): Phase 7's relay publishes pending rows.
        await session.execute(insert(SearchDocument).values(page_id=page_id, job_id=lease.job_id))
    return page_id


async def _admit_redirect(
    session: AsyncSession, lease: Lease, *, target: str, depth: int, hops: int
) -> uuid.UUID | None:
    """Admit a redirect target at the redirecting URL's depth, under the job row lock."""
    job = (
        await session.execute(
            select(
                CrawlJob.status, CrawlJob.allowed_hosts, CrawlJob.max_pages, CrawlJob.admitted_count
            )
            .where(CrawlJob.id == lease.job_id)
            .with_for_update()
        )
    ).one()
    if job.status != JobStatus.RUNNING.value or job.admitted_count >= job.max_pages:
        return None
    try:
        host = normalize_url(target).host
    except InvalidUrlError:
        return None
    if host not in job.allowed_hosts:  # the job's scope, checked again under the lock
        return None
    # A known target is skipped before inserting. INSERT ... ON CONFLICT would otherwise wait for
    # any transaction that is updating the target's row (another task recording its result),
    # while this one holds the job row lock: if that task then needs the job row lock to admit a
    # redirect of its own, the two deadlock. A plain SELECT never waits. Every admission holds
    # the job row lock, so no other transaction can insert the target in between; ON CONFLICT
    # remains the backstop (ADR-0009).
    known = await session.scalar(
        select(CrawlUrl.id).where(
            CrawlUrl.job_id == lease.job_id, CrawlUrl.normalized_url == target
        )
    )
    if known is not None:
        return None
    url_id: uuid.UUID | None = await session.scalar(
        pg_insert(CrawlUrl)
        .values(
            job_id=lease.job_id,
            normalized_url=target,
            depth=depth,
            discovered_from_url_id=lease.url_id,
            redirect_hops=hops,
            state=UrlState.PENDING.value,
        )
        .on_conflict_do_nothing(index_elements=[CrawlUrl.job_id, CrawlUrl.normalized_url])
        .returning(CrawlUrl.id)
    )
    if url_id is None:  # already known to the job: dedup by constraint (ADR-0009)
        return None
    await session.execute(
        update(CrawlJob)
        .where(CrawlJob.id == lease.job_id)
        .values(admitted_count=CrawlJob.admitted_count + 1)
        .execution_options(synchronize_session=False)
    )
    return url_id


async def _finish_job(
    session: AsyncSession, job_id: uuid.UUID, *, seed_url: str, level: int, admitted_count: int
) -> AdvanceResult:
    """Complete or fail the job that ``advance_job`` has locked (values read under the lock)."""
    seed = (
        await session.execute(
            select(CrawlUrl.state, CrawlUrl.outcome).where(
                CrawlUrl.job_id == job_id, CrawlUrl.normalized_url == seed_url
            )
        )
    ).one()
    pages = await session.scalar(
        select(func.count()).select_from(Page).where(Page.job_id == job_id)
    )
    if seed.state == UrlState.FETCHED.value:
        status, reason = JobStatus.COMPLETED, None
    else:
        status = JobStatus.FAILED
        reason = f"the seed URL was not fetched ({seed.state}: {seed.outcome})"
    await session.execute(
        update(CrawlJob)
        .where(CrawlJob.id == job_id, CrawlJob.status == JobStatus.RUNNING.value)
        .values(
            status=status.value,
            # Never before created_at, even if the clock stepped back (see _ACTIVATE).
            finished_at=func.greatest(func.clock_timestamp(), CrawlJob.created_at),
            failure_reason=reason,
        )
        .execution_options(synchronize_session=False)
    )
    _add_event(
        session,
        job_id,
        f"job.{status.value}",
        from_state="running",
        to_state=status.value,
        detail={
            "levels": level + 1,
            "admitted": admitted_count,
            "pages": pages,
            **({"reason": reason} if reason else {}),
        },
    )
    action = AdvanceAction.COMPLETED if status is JobStatus.COMPLETED else AdvanceAction.FAILED
    return AdvanceResult(action, status, level)


def _add_event(
    session: AsyncSession,
    job_id: uuid.UUID,
    event_type: str,
    *,
    crawl_url_id: uuid.UUID | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    worker_id: str | None = None,
    lease_epoch: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append a crawl_events row in the caller's transaction (written at its commit)."""
    session.add(
        CrawlEvent(
            job_id=job_id,
            crawl_url_id=crawl_url_id,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            detail=detail or {},
        )
    )


def _log_advance(job_id: uuid.UUID, result: AdvanceResult) -> None:
    if result.action is AdvanceAction.ADVANCED:
        logger.info(
            "Next crawl level admitted",
            extra={
                "event": "job.level_advanced",
                "job_id": str(job_id),
                "level": result.level,
                "admitted": result.admitted,
            },
        )
    elif result.action in (AdvanceAction.COMPLETED, AdvanceAction.FAILED):
        logger.info(
            "Crawl job finished",
            extra={"event": f"job.{result.status.value}", "job_id": str(job_id)},
        )


def _db_text(value: str | None, limit: int | None = None) -> str | None:
    """Text PostgreSQL accepts: NUL characters, which ``text`` cannot store, become U+FFFD."""
    if value is None:
        return None
    value = value.replace("\x00", "�")
    return value[:limit] if limit is not None else value


def _http_status(status: int | None) -> int | None:
    """The status if the column can store it (100-599); servers may send 600-999."""
    return status if status is not None and 100 <= status <= 599 else None


def _content_hash(title: str | None, description: str | None, headings: str, body: str) -> bytes:
    """SHA-256 of the extracted content (pages.content_hash)."""
    canonical = json.dumps([title, description, headings, body], ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).digest()
