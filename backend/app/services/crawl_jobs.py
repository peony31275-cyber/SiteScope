"""Crawl job use cases: create a job, inspect one, list them.

**Creation** validates the request, then runs one PostgreSQL transaction that inserts:

* the ``crawl_jobs`` row: status ``queued``, ``current_level`` 0, and ``admitted_count`` 1,
  because the seed counts against the page budget;
* its depth-0 seed URL in ``crawl_urls``, in state ``pending``.

If anything fails, both rows roll back together. Nothing is published to Redis: request handlers
never publish (ADR-0002), so creating a job works even while Redis is down. The job stays
``queued`` until a scheduler exists (Phase 5); it is never reported as running before then.

**How the scheduler will discover committed work** (Phase 5), by querying PostgreSQL:

1. The activator selects ``queued`` jobs, oldest first (index ``ix_crawl_jobs_active``), and moves
   each one to ``running`` with a conditional update.
2. The dispatcher selects ``pending`` URLs of running jobs at the job's ``current_level`` whose
   ``next_attempt_at`` has passed (index ``ix_crawl_urls_dispatch``), publishes them, and marks
   them ``queued`` (the crawl-task outbox).

The job and its seed commit together, so the scheduler never sees one without the other.

**Idempotent creation** (``Idempotency-Key``, Phase 6, ADR-0017). The key and a fingerprint of
the *normalized* request (SHA-256 of the seed URL after normalization, ``max_depth`` and
``max_pages``) are stored on the job row, in the same transaction, with
``INSERT ... ON CONFLICT (idempotency_key) DO NOTHING``:

* a new key creates the job;
* a known key with the same fingerprint returns the job it created (a replay; nothing new is
  written);
* a known key with another fingerprint is refused (``IdempotencyKeyReusedError``, 409);
* concurrent requests with one key serialize on the unique index: one inserts, the others wait
  for its commit and then replay its job (or insert, if it rolled back);
* a key older than ``SITESCOPE_IDEMPOTENCY_KEY_TTL_SECONDS`` is forgotten: it is cleared from its
  job (the job itself is unchanged) and creates a new job. The scheduler also clears expired keys
  in batches (``expire_idempotency_keys``).

PostgreSQL holds the keys, so they survive restarts, and a request that fails rolls the key back
with the job.

**Reads** run in their own short transactions, and the job detail is a single SQL statement, so
the job row and its URL counts come from the same snapshot.
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import func, select, text, true, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, FieldViolation, InvalidInputError, NotFoundError
from app.crawler.scope import CrawlTargetPolicy, TargetNotAllowedError
from app.crawler.urls import InvalidUrlError, NormalizedUrl, normalize_url
from app.db.models import TERMINAL_URL_STATES, CrawlJob, CrawlUrl, JobStatus, UrlState

logger = logging.getLogger(__name__)

# Idempotency-Key values: 1 to 255 visible ASCII characters (no spaces).
IDEMPOTENCY_KEY_PATTERN: Final = r"^[\x21-\x7e]{1,255}$"
IDEMPOTENCY_KEY_MAX_LENGTH: Final = 255
_INSERT_ATTEMPTS: Final = 3

# Forget an expired key: the job keeps everything else (the CHECK requires both to be NULL).
_FREE_EXPIRED_KEY = text(
    """
    UPDATE crawl_jobs SET idempotency_key = NULL, request_fingerprint = NULL
    WHERE idempotency_key = :key
      AND created_at < clock_timestamp() - make_interval(secs => :ttl)
    """
)

_EXPIRE_KEYS = text(
    """
    WITH expired AS MATERIALIZED (
        SELECT id FROM crawl_jobs
        WHERE idempotency_key IS NOT NULL
          AND created_at < clock_timestamp() - make_interval(secs => :ttl)
        ORDER BY created_at, id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE crawl_jobs AS j SET idempotency_key = NULL, request_fingerprint = NULL
    FROM expired WHERE j.id = expired.id
    RETURNING j.id
    """
)


class IdempotencyKeyReusedError(ConflictError):
    """An Idempotency-Key that created a job for a different request."""

    code = "idempotency_key_reused"


@dataclass(frozen=True, slots=True)
class SubmittedCrawlJob:
    job: CrawlJob
    created: bool  # False: a replay, the job an earlier request with this key created


@dataclass(frozen=True, slots=True)
class CrawlJobProgress:
    """A job and how many of its URLs are in each state, read in one snapshot."""

    job: CrawlJob
    urls_by_state: dict[UrlState, int]

    @property
    def urls_total(self) -> int:
        return sum(self.urls_by_state.values())

    @property
    def urls_finished(self) -> int:
        return sum(self.urls_by_state[state] for state in TERMINAL_URL_STATES)


@dataclass(frozen=True, slots=True)
class JobPosition:
    """A position in the listing order (newest first): the sort key of a page's last job."""

    created_at: datetime
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class CrawlJobPage:
    jobs: list[CrawlJob]
    # Where the next page starts, or None if this is the last page.
    next_position: JobPosition | None


def target_policy(settings: Settings) -> CrawlTargetPolicy:
    """The approved crawl targets of this deployment."""
    return CrawlTargetPolicy.from_config(
        hosts=settings.crawl_allowed_hosts,
        schemes=settings.crawl_allowed_schemes,
        ports=settings.crawl_allowed_ports,
    )


async def create_crawl_job(
    session: AsyncSession, settings: Settings, *, seed_url: str, max_depth: int, max_pages: int
) -> CrawlJob:
    """Validate a crawl request, then create the job and its seed URL in one transaction.

    Raises ``InvalidInputError`` for an invalid or unapproved seed URL, or for limits above this
    deployment's caps. Validation does no DNS lookup and no network I/O.
    """
    submitted = await submit_crawl_job(
        session, settings, seed_url=seed_url, max_depth=max_depth, max_pages=max_pages
    )
    return submitted.job


async def submit_crawl_job(
    session: AsyncSession,
    settings: Settings,
    *,
    seed_url: str,
    max_depth: int,
    max_pages: int,
    idempotency_key: str | None = None,
) -> SubmittedCrawlJob:
    """``create_crawl_job``, idempotent when ``idempotency_key`` is given (module docstring).

    Raises ``InvalidInputError`` as ``create_crawl_job`` does, and ``IdempotencyKeyReusedError``
    if the key created a job for a different request.
    """
    seed = _validate_request(settings, seed_url=seed_url, max_depth=max_depth, max_pages=max_pages)
    fingerprint = request_fingerprint(seed, max_depth=max_depth, max_pages=max_pages)
    for _ in range(_INSERT_ATTEMPTS):
        async with session.begin():
            if idempotency_key is not None:
                await session.execute(
                    _FREE_EXPIRED_KEY,
                    {"key": idempotency_key, "ttl": settings.idempotency_key_ttl_seconds},
                )
            job_id = await session.scalar(
                pg_insert(CrawlJob)
                .values(
                    seed_url=seed.url,
                    allowed_hosts=[seed.host],
                    max_depth=max_depth,
                    max_pages=max_pages,
                    status=JobStatus.QUEUED.value,
                    current_level=0,
                    admitted_count=1,  # the seed URL
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint if idempotency_key is not None else None,
                )
                .on_conflict_do_nothing(index_elements=[CrawlJob.idempotency_key])
                .returning(CrawlJob.id)
            )
            if job_id is not None:
                session.add(
                    CrawlUrl(
                        job_id=job_id,
                        normalized_url=seed.url,
                        depth=0,
                        state=UrlState.PENDING.value,
                    )
                )
                await session.flush()
                job = await session.get_one(CrawlJob, job_id)
                created = True
            else:
                existing = await session.scalar(
                    select(CrawlJob).where(CrawlJob.idempotency_key == idempotency_key)
                )
                if existing is None:  # the key was just freed by another request: try again
                    continue
                if existing.request_fingerprint != fingerprint:
                    raise IdempotencyKeyReusedError(
                        "This Idempotency-Key was used for a different crawl request."
                    )
                job, created = existing, False
        break
    else:  # pragma: no cover - needs three conflicting expiries in a row
        raise ConflictError("The crawl request conflicted with concurrent requests; retry it.")
    # Logged only after the commit. The query string may hold tokens, so the URL is not logged.
    logger.info(
        "Crawl job created" if created else "Crawl job request replayed",
        extra={
            "event": "job.created" if created else "job.replayed",
            "job_id": str(job.id),
            "seed_host": seed.host,
            "max_depth": max_depth,
            "max_pages": max_pages,
            "idempotent": idempotency_key is not None,
        },
    )
    return SubmittedCrawlJob(job, created)


def request_fingerprint(seed: NormalizedUrl, *, max_depth: int, max_pages: int) -> bytes:
    """SHA-256 of the normalized request: equivalent requests have the same fingerprint."""
    canonical = json.dumps(
        {"seed_url": seed.url, "max_depth": max_depth, "max_pages": max_pages},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


async def expire_idempotency_keys(session: AsyncSession, *, ttl: int, limit: int) -> int:
    """Forget up to ``limit`` keys older than ``ttl`` seconds (the jobs stay); returns how many."""
    async with session.begin():
        return len((await session.execute(_EXPIRE_KEYS, {"ttl": ttl, "limit": limit})).all())


async def get_crawl_job(session: AsyncSession, job_id: uuid.UUID) -> CrawlJobProgress:
    """Return a job with its URL counts per state, or raise ``NotFoundError``."""
    counts = (
        select(
            *(
                func.count().filter(CrawlUrl.state == state.value).label(state.value)
                for state in UrlState
            )
        )
        .where(CrawlUrl.job_id == CrawlJob.id)
        .lateral("url_counts")
    )
    statement = select(CrawlJob, *counts.c).join(counts, true()).where(CrawlJob.id == job_id)
    async with session.begin():
        row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise NotFoundError(f"Crawl job {job_id} does not exist.")
    job, *state_counts = row
    return CrawlJobProgress(job=job, urls_by_state=dict(zip(UrlState, state_counts, strict=True)))


async def list_crawl_jobs(
    session: AsyncSession, *, limit: int, after: JobPosition | None = None
) -> CrawlJobPage:
    """Return up to ``limit`` jobs, newest first, starting after ``after`` (keyset pagination).

    The order ``(created_at DESC, id DESC)`` is total, because ``id`` breaks ties between jobs
    created at the same instant, and the index ``ix_crawl_jobs_created_at_id`` serves it.

    A walk through the pages never returns a job twice, and returns every job that existed when
    the walk began. Jobs created meanwhile do not shift later pages: they normally sort before the
    first page. ``created_at`` is the creating transaction's start time, so a job whose
    transaction was still open when a page was read can sort into pages already returned; that
    walk misses it.
    """
    statement = (
        select(CrawlJob)
        .order_by(CrawlJob.created_at.desc(), CrawlJob.id.desc())
        .limit(limit + 1)  # one extra row tells whether another page follows
    )
    if after is not None:
        statement = statement.where(
            tuple_(CrawlJob.created_at, CrawlJob.id) < tuple_(after.created_at, after.id)
        )
    async with session.begin():
        jobs = list((await session.scalars(statement)).all())
    if len(jobs) <= limit:
        return CrawlJobPage(jobs=jobs, next_position=None)
    jobs = jobs[:limit]
    return CrawlJobPage(jobs=jobs, next_position=JobPosition(jobs[-1].created_at, jobs[-1].id))


def _validate_request(
    settings: Settings, *, seed_url: str, max_depth: int, max_pages: int
) -> NormalizedUrl:
    """Check every field and report all violations at once."""
    violations: list[FieldViolation] = []
    seed: NormalizedUrl | None = None
    try:
        seed = normalize_url(seed_url)
        target_policy(settings).check(seed)
    except InvalidUrlError as exc:
        violations.append(FieldViolation(("body", "seed_url"), str(exc), "invalid_url"))
    except TargetNotAllowedError as exc:
        violations.append(FieldViolation(("body", "seed_url"), str(exc), "target_not_allowed"))
    if max_depth > settings.crawl_max_depth_limit:
        violations.append(
            FieldViolation(
                ("body", "max_depth"),
                f"max_depth must be at most {settings.crawl_max_depth_limit} on this server",
                "limit_exceeded",
            )
        )
    if max_pages > settings.crawl_max_pages_limit:
        violations.append(
            FieldViolation(
                ("body", "max_pages"),
                f"max_pages must be at most {settings.crawl_max_pages_limit} on this server",
                "limit_exceeded",
            )
        )
    if violations or seed is None:
        logger.info(
            "Crawl request rejected",
            extra={
                "event": "job.rejected",
                "reasons": sorted({violation.type for violation in violations}),
            },
        )
        raise InvalidInputError(violations)
    return seed
