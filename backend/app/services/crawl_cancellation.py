"""Crawl job cancellation: T7 and the finalizer (architecture sections 7.1 and 8.6; ADR-0017).

* **T7** ``cancel_crawl_job`` (``POST /api/v1/crawls/{id}/cancel``): under the job row lock, a
  ``queued`` job becomes ``cancelled`` at once, and a ``running`` job becomes ``cancelling``. The
  same transaction cancels the job's ``pending`` and ``queued`` URLs; if none of its URLs is in
  progress, the job is then ``cancelled`` at once too. It never locks an ``in_progress`` URL: a
  transaction that locks the job row first must not lock a URL row that a worker may hold (lock
  order, architecture section 9). Cancelling a job that is cancelling already, or finished,
  changes nothing: the call is idempotent and reports the current state.
* **Afterwards nothing new starts.** Dispatch, claims, redirect admission, level advancement and
  reconciliation all require a ``running`` job, so a stale or duplicate message can only meet a
  refused claim (and is acknowledged).
* **In-flight tasks end either way.** A worker that sees the cancellation stops its task and
  records the URL ``cancelled`` under its own token (``cancel_claimed_url``, a fenced update); a
  task that finishes first records its result normally.
* **The finalizer** ``settle_cancellation`` (the scheduler, for each ``cancelling`` job, every
  pass) runs three short transactions:

  1. cancel ``pending`` and ``queued`` URLs again: a task may have scheduled a retry meanwhile;
  2. cancel ``in_progress`` URLs whose lease expired (their worker is gone; a stale worker is
     then fenced out, because its fenced update needs ``in_progress``); rows a worker holds are
     skipped (``SKIP LOCKED``), and no job row lock is held;
  3. under the job row lock, if no URL is unfinished: ``cancelling`` -> ``cancelled``.

  The job is therefore ``cancelled`` only once every URL is final. Without workers, that takes at
  most one lease duration after the cancel request.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import CrawlJob, JobStatus
from app.services.crawl_state import Lease, add_event

logger = logging.getLogger(__name__)

CANCELLED_OUTCOME: Final = "job_cancelled"

# FOR NO KEY UPDATE, not FOR UPDATE: it still serializes T7 with every other writer of the job
# row (advance_job, redirect admission and activation take FOR UPDATE; another T7 or the
# finalizer NO KEY UPDATE), but not with foreign-key checks (FOR KEY SHARE). A quarantine
# locks its queued URL row and then inserts an event, whose foreign key checks the job row;
# T7 waits for that URL row. With FOR UPDATE here the two deadlocked (Phase 6 review).
_LOCK_JOB = text("SELECT status FROM crawl_jobs WHERE id = :job_id FOR NO KEY UPDATE")

_CANCEL_QUEUED_JOB = text(
    """
    UPDATE crawl_jobs
    SET status = 'cancelled',
        cancel_requested_at = clock_timestamp(),
        finished_at = GREATEST(clock_timestamp(), created_at)
    WHERE id = :job_id AND status = 'queued'
    """
)

_CANCEL_RUNNING_JOB = text(
    """
    UPDATE crawl_jobs SET status = 'cancelling', cancel_requested_at = clock_timestamp()
    WHERE id = :job_id AND status = 'running'
    """
)

# pending and queued URLs carry no lease: no worker holds them.
_CANCEL_UNCLAIMED = text(
    """
    UPDATE crawl_urls
    SET state = 'cancelled', outcome = :outcome, finished_at = clock_timestamp()
    WHERE job_id = :job_id AND state IN ('pending', 'queued')
    RETURNING id
    """
)

_CANCEL_EXPIRED_LEASES = text(
    """
    WITH expired AS MATERIALIZED (
        SELECT id FROM crawl_urls
        WHERE job_id = :job_id AND state = 'in_progress'
          AND lease_expires_at < clock_timestamp()
        FOR UPDATE SKIP LOCKED
    )
    UPDATE crawl_urls AS u
    SET state = 'cancelled', outcome = :outcome, finished_at = clock_timestamp(),
        lease_owner = NULL, lease_expires_at = NULL
    FROM expired
    WHERE u.id = expired.id AND u.state = 'in_progress'
    RETURNING u.id
    """
)

_FINALIZE = text(
    """
    UPDATE crawl_jobs
    SET status = 'cancelled', finished_at = GREATEST(clock_timestamp(), created_at)
    WHERE id = :job_id AND status = 'cancelling'
      AND NOT EXISTS (
          SELECT 1 FROM crawl_urls
          WHERE job_id = :job_id AND state IN ('pending', 'queued', 'in_progress')
      )
    RETURNING id
    """
)

# The worker's own cancellation of the URL it holds: the fenced update, with its token.
_CANCEL_CLAIMED = text(
    """
    UPDATE crawl_urls AS u
    SET state = 'cancelled', outcome = :outcome, finished_at = clock_timestamp(),
        lease_owner = NULL, lease_expires_at = NULL, result_epoch = :token
    FROM crawl_jobs AS j
    WHERE u.id = :url_id AND u.job_id = :job_id
      AND u.state = 'in_progress' AND u.lease_epoch = :token
      AND j.id = u.job_id AND j.status = 'cancelling'
    RETURNING u.id
    """
)

_CANCELLING_JOBS = text(
    """
    SELECT id FROM crawl_jobs WHERE status = 'cancelling'
    ORDER BY cancel_requested_at, id LIMIT :limit
    """
)


class CancelAction(StrEnum):
    CANCELLED = "cancelled"  # a queued job, cancelled at once
    CANCELLING = "cancelling"  # a running job: cancelling until its in-flight URLs are final
    UNCHANGED = "unchanged"  # cancelling already, or finished: nothing to do


@dataclass(frozen=True, slots=True)
class CancelResult:
    action: CancelAction
    status: JobStatus  # the job's status after the call
    cancelled_urls: int = 0


@dataclass(frozen=True, slots=True)
class Settlement:
    """One ``settle_cancellation`` call."""

    cancelled_urls: int  # pending or queued URLs cancelled
    cancelled_leases: int  # expired in-progress URLs cancelled
    finalized: bool  # the job is now cancelled


async def cancel_crawl_job(session: AsyncSession, job_id: uuid.UUID) -> CancelResult:
    """T7: cancel a job (see the module docstring). Raises ``NotFoundError``."""
    async with session.begin():
        status_text = await session.scalar(_LOCK_JOB, {"job_id": job_id})
        if status_text is None:
            raise NotFoundError(f"Crawl job {job_id} does not exist.")
        status = JobStatus(status_text)
        if status is JobStatus.QUEUED:
            await session.execute(_CANCEL_QUEUED_JOB, {"job_id": job_id})
            action, new_status = CancelAction.CANCELLED, JobStatus.CANCELLED
        elif status is JobStatus.RUNNING:
            await session.execute(_CANCEL_RUNNING_JOB, {"job_id": job_id})
            action, new_status = CancelAction.CANCELLING, JobStatus.CANCELLING
        else:
            return CancelResult(CancelAction.UNCHANGED, status)
        cancelled = len(
            (
                await session.execute(
                    _CANCEL_UNCLAIMED, {"job_id": job_id, "outcome": CANCELLED_OUTCOME}
                )
            ).all()
        )
        add_event(
            session,
            job_id,
            "job.cancel_requested",
            from_state=status.value,
            to_state=new_status.value,
            detail={"cancelled_urls": cancelled},
        )
        if new_status is JobStatus.CANCELLING:
            # Nothing in flight (no URL in progress): the job is cancelled at once. Otherwise
            # the finalizer finishes it later. Reading in-progress rows takes no lock.
            if (await session.execute(_FINALIZE, {"job_id": job_id})).one_or_none() is not None:
                action, new_status = CancelAction.CANCELLED, JobStatus.CANCELLED
                add_event(
                    session, job_id, "job.cancelled", from_state="cancelling", to_state="cancelled"
                )
        else:
            add_event(session, job_id, "job.cancelled", from_state="queued", to_state="cancelled")
    logger.info(
        "Crawl job cancellation requested",
        extra={
            "event": "job.cancel_requested",
            "job_id": str(job_id),
            "status": new_status.value,
            "cancelled_urls": cancelled,
        },
    )
    return CancelResult(action, new_status, cancelled)


async def cancel_claimed_url(session: AsyncSession, lease: Lease) -> bool:
    """Record a URL ``cancelled`` under its lease, because its job is being cancelled.

    The fenced update: only with the worker's own token, only while the URL is in progress,
    and only if the job is ``cancelling``. False if any of that does not hold (nothing written):
    the lease was lost, as for a fenced result.
    """
    async with session.begin():
        row = (
            await session.execute(
                _CANCEL_CLAIMED,
                {
                    "url_id": lease.url_id,
                    "job_id": lease.job_id,
                    "token": lease.token,
                    "outcome": CANCELLED_OUTCOME,
                },
            )
        ).one_or_none()
    if row is None:
        logger.warning(
            "Cancellation not recorded: the lease was lost",
            extra={
                "event": "task.fenced",
                "job_id": str(lease.job_id),
                "url_id": str(lease.url_id),
                "worker_id": lease.owner,
                "lease_epoch": lease.token,
            },
        )
        return False
    logger.info(
        "URL cancelled with its job",
        extra={
            "event": "url.cancelled",
            "job_id": str(lease.job_id),
            "url_id": str(lease.url_id),
            "worker_id": lease.owner,
            "lease_epoch": lease.token,
        },
    )
    return True


async def jobs_being_cancelled(
    session: AsyncSession, job_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """Of ``job_ids``, those that are ``cancelling`` or ``cancelled`` (the workers' check).

    Other statuses do not stop a task. A task whose job has completed or failed holds a stale
    lease (its URL was finished by a takeover); it runs to its end and is fenced out as usual.
    """
    if not job_ids:
        return set()
    async with session.begin():
        rows = await session.execute(
            select(CrawlJob.id).where(
                CrawlJob.id.in_(list(job_ids)),
                CrawlJob.status.in_([JobStatus.CANCELLING.value, JobStatus.CANCELLED.value]),
            )
        )
        return {row.id for row in rows}


async def cancelling_jobs(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Jobs waiting for their cancellation to finish, oldest request first."""
    async with session.begin():
        return [row.id for row in await session.execute(_CANCELLING_JOBS, {"limit": limit})]


async def settle_cancellation(session: AsyncSession, job_id: uuid.UUID) -> Settlement:
    """The finalizer for one ``cancelling`` job (see the module docstring)."""
    async with session.begin():
        unclaimed = len(
            (
                await session.execute(
                    _CANCEL_UNCLAIMED, {"job_id": job_id, "outcome": CANCELLED_OUTCOME}
                )
            ).all()
        )
    async with session.begin():
        expired = len(
            (
                await session.execute(
                    _CANCEL_EXPIRED_LEASES, {"job_id": job_id, "outcome": CANCELLED_OUTCOME}
                )
            ).all()
        )
    async with session.begin():
        finalized = (await session.execute(_FINALIZE, {"job_id": job_id})).one_or_none()
        if finalized is not None:
            add_event(
                session, job_id, "job.cancelled", from_state="cancelling", to_state="cancelled"
            )
    if finalized is not None:
        logger.info("Crawl job cancelled", extra={"event": "job.cancelled", "job_id": str(job_id)})
    return Settlement(unclaimed, expired, finalized is not None)
