"""Index state: the indexing outbox, counted attempts, versioned index writes (Phase 7).

``search_documents`` is both the full-text index and the outbox of index tasks (ADR-0008):
the crawler's result transaction inserts a page's row as ``pending``, so only committed pages
are ever indexed. The index dispatcher (``app/scheduler/index_dispatcher.py``) and the indexers
(``app/workers/indexer.py``) share the use cases below. Each runs one short transaction
(ADR-0018):

* **I1** ``dispatch_index_tasks``: lock ``pending`` rows (``FOR UPDATE SKIP LOCKED``), publish
  one message per row (``XADD``), then mark them ``queued`` with their entry IDs, in one
  transaction, as for crawl tasks (ADR-0016). A crash or a failed commit after the ``XADD``
  leaves the rows ``pending``: they are published again, and the earlier message finds nothing
  to do. The ``XADD`` round trip is the one network call inside a transaction, bounded by the
  Redis timeout; it is what lets an indexer's attempt wait for a dispatch still in flight.
* **Reconciliation** ``republish_stale_index_tasks``: a ``queued`` row without progress for
  ``older_than`` seconds, and with no live lease, is published again if its stream entry no
  longer exists or was acknowledged (Redis lost it, or it was trimmed).
* **I2** ``start_index_attempt``: count an attempt and take the row's lease, if the row is
  ``queued``, the message's content version is not newer than the page's (an older message
  stands for the same work: I3 indexes the current content), attempts are left, and no other
  attempt holds a live lease. It first locks the row, so it waits for a dispatch of it still in
  flight. Attempts are counted in PostgreSQL before any work, so an attempt that
  crashes its indexer counts too, and ``max_attempts`` bounds the work of every document
  without relying on Redis delivery counts (the Phase 6 lesson).
* **I3** ``index_document``: build the vector *inside PostgreSQL* from the stored page and
  write it with the page's version, in one statement that first locks the page row
  (``FOR SHARE``) and then updates the row only while it is ``queued`` and its indexed
  version is not newer. The vector and its version therefore always come from the same page
  content, and a content change (which sets the row back to ``pending`` in its own
  transaction) either waits for this one or makes it a no-op. A trigger (migration 0004)
  rejects any write that would move ``indexed_content_version`` back. The transaction's
  statements share one budget, the attempt's deadline: each one's ``statement_timeout`` is
  what remains of it, so an attempt ends before its lease does.
* **I4** ``fail_document`` and ``fail_exhausted_document``: ``queued`` -> ``failed`` after a
  deterministic error, or once the attempts are used up and no attempt is running.
* **I5** ``complete_job_indexing``: under the job row lock, a finished job with no
  ``pending`` or ``queued`` row gets ``indexing_completed_at``. ``completable_indexing_jobs``
  finds the candidates: only jobs with nothing left to index, so jobs that still wait never
  hold up the others.
* **I6** ``request_reindex``: under the job row lock, every ``indexed`` and ``failed`` row of
  a job goes back to ``pending``, and ``indexing_completed_at`` is cleared.

**Leases are advisory.** A lease (``lease_owner``, ``lease_expires_at``) keeps duplicate
messages from starting attempts while one runs. It is not a fencing token: I3 reads what it
indexes under its own locks, so an indexer whose lease expired writes the same result as its
successor, or nothing.

**Lock order.** Page row before ``search_documents`` row (I3, and any content change); job
row before ``search_documents`` rows (I6); I1, I2, I4 and the reconciliation lock only
``search_documents`` rows, and I5 only the job row. Inserting an event takes ``FOR KEY SHARE``
on the job row and on its crawl URL row, which no transaction here conflicts with. There is no
cycle.

**Cancelled jobs** (policy, ADR-0018): their pages stay indexed and searchable, and pages
stored before the cancellation took effect are still indexed. Only the crawl stops.
"""

import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import IndexStatus
from app.indexing.documents import (
    BODY_WEIGHT,
    DESCRIPTION_WEIGHT,
    HEADINGS_WEIGHT,
    MAX_ATTEMPTS_EXCEEDED,
    TEXT_SEARCH_CONFIG,
    TITLE_WEIGHT,
    TSVECTOR_TOO_LARGE,
    WORD_LOOKBACK,
    fallback_body_chars,
)
from app.services.crawl_state import (
    MAX_ERROR_CHARS,
    DeliveryCheck,
    ReconcileCursor,
    Reconciliation,
    add_event,
)

logger = logging.getLogger(__name__)

# SQLSTATE of "string is too long for tsvector" (program_limit_exceeded).
PROGRAM_LIMIT_EXCEEDED: Final = "54000"


class IndexDeadlineError(TimeoutError):
    """An index attempt's deadline passed before its next statement could start."""


@dataclass(frozen=True, slots=True)
class IndexTaskRef:
    """What an index task message names: a page of a job, at a content version."""

    job_id: uuid.UUID
    page_id: uuid.UUID
    content_version: int


# Publishes index tasks to the index stream; returns their entry IDs in order.
IndexPublisher = Callable[[Sequence[IndexTaskRef]], Awaitable[list[str]]]


@dataclass(frozen=True, slots=True)
class IndexAttempt:
    """A started attempt: the attempt's number, and the content version it is for."""

    page_id: uuid.UUID
    job_id: uuid.UUID
    attempt: int  # attempt_count after this start
    content_version: int
    takeover: bool  # it took over the expired lease of another attempt


@dataclass(frozen=True, slots=True)
class DocumentStatus:
    """The authoritative state of a search document and its page (``inspect_document``)."""

    page_id: uuid.UUID
    job_id: uuid.UUID
    status: IndexStatus
    attempt_count: int
    lease_owner: str | None
    lease_live: bool  # an attempt holds a lease that has not expired (database clock)
    content_version: int  # the page's current content version
    indexed_content_version: int | None


class IndexOutcome(StrEnum):
    INDEXED = "indexed"  # the vector was written for the page's current content
    NOT_QUEUED = "not_queued"  # nothing to do: another attempt finished, or it is pending again
    FAILED = "failed"  # a deterministic failure was recorded: the document is failed


@dataclass(frozen=True, slots=True)
class IndexResult:
    outcome: IndexOutcome
    content_version: int | None = None  # INDEXED: the version the vector reflects
    lexemes: int | None = None  # INDEXED: distinct lexemes in the vector
    body_chars: int | None = None  # the page body's length, in characters
    indexed_body_chars: int | None = None  # INDEXED: how much of it was indexed
    status: IndexStatus | None = None  # NOT_QUEUED: the status found
    reason: str | None = None  # FAILED: why

    @property
    def truncated(self) -> bool:
        return (
            self.body_chars is not None
            and self.indexed_body_chars is not None
            and self.body_chars > self.indexed_body_chars
        )


class IndexingState(StrEnum):
    """A job's indexing, derived from its search documents (never stored)."""

    PENDING = "pending"  # nothing published or indexed yet
    RUNNING = "running"  # under way, or waiting for the crawl or the index dispatcher
    INDEXED = "indexed"  # complete: every page of the finished job is indexed
    FAILED = "failed"  # complete, but at least one page could not be indexed


@dataclass(frozen=True, slots=True)
class IndexingProgress:
    """A job's search documents by status, and when its indexing completed."""

    pending: int = 0
    queued: int = 0
    indexed: int = 0
    failed: int = 0
    active: int = 0  # queued documents an indexer is working on (live lease)
    completed_at: datetime | None = None  # crawl_jobs.indexing_completed_at

    @property
    def documents(self) -> int:
        return self.pending + self.queued + self.indexed + self.failed

    @property
    def state(self) -> IndexingState:
        return indexing_state(self)


def indexing_state(progress: IndexingProgress) -> IndexingState:
    """Complete only once the index dispatcher has marked it (``indexing_completed_at``): the
    job is finished, so no page can be added, and no document is pending or queued."""
    if progress.completed_at is not None:
        return IndexingState.FAILED if progress.failed else IndexingState.INDEXED
    if progress.queued or progress.indexed or progress.failed:
        return IndexingState.RUNNING
    return IndexingState.PENDING


@dataclass(frozen=True, slots=True)
class IndexingOverview:
    """Indexing over all jobs, counted from PostgreSQL when it was asked for."""

    pending: int
    queued: int
    indexed: int
    failed: int
    active: int  # queued documents with a live lease
    oldest_pending_seconds: float | None  # how long the oldest pending document has waited
    oldest_queued_seconds: float | None  # time since the oldest queued document was published
    jobs_awaiting_completion: int  # finished jobs whose indexing is not complete yet


# --- SQL ---------------------------------------------------------------------------------------

# I1: lock pending rows, oldest change first (ix_search_documents_pending). The page's content
# version goes into the message.
_DISPATCH_LOCK = text(
    """
    SELECT d.page_id, d.job_id, p.content_version
    FROM search_documents AS d
    JOIN pages AS p ON p.id = d.page_id AND p.job_id = d.job_id
    WHERE d.status = 'pending'
    ORDER BY d.updated_at, d.page_id
    LIMIT :limit
    FOR UPDATE OF d SKIP LOCKED
    """
)

_DISPATCH_MARK = text(
    """
    UPDATE search_documents AS d
    SET status = 'queued', dispatched_at = clock_timestamp(), stream_entry_id = e.entry_id
    FROM unnest(CAST(:ids AS uuid[]), CAST(:entries AS text[])) AS e(page_id, entry_id)
    WHERE d.page_id = e.page_id AND d.status = 'pending'
    RETURNING d.page_id
    """
)

_QUEUED_COUNT = text("SELECT count(*) FROM search_documents WHERE status = 'queued'")

# Reconciliation: queued rows without progress, and without a live attempt. Keyset pagination
# on (dispatched_at, page_id) lets successive calls cover every stale row.
_STALE_LOCK = text(
    """
    SELECT d.page_id, d.job_id, d.stream_entry_id, d.dispatched_at, p.content_version
    FROM search_documents AS d
    JOIN pages AS p ON p.id = d.page_id AND p.job_id = d.job_id
    WHERE d.status = 'queued'
      AND d.dispatched_at < clock_timestamp() - make_interval(secs => :older_than)
      AND (d.lease_expires_at IS NULL OR d.lease_expires_at < clock_timestamp())
      AND (CAST(:after_at AS timestamptz) IS NULL
           OR (d.dispatched_at, d.page_id)
              > (CAST(:after_at AS timestamptz), CAST(:after_id AS uuid)))
    ORDER BY d.dispatched_at, d.page_id
    LIMIT :limit
    FOR UPDATE OF d SKIP LOCKED
    """
)

_STALE_MARK = text(
    """
    UPDATE search_documents AS d
    SET dispatched_at = clock_timestamp(), stream_entry_id = e.entry_id
    FROM unnest(CAST(:ids AS uuid[]), CAST(:entries AS text[])) AS e(page_id, entry_id)
    WHERE d.page_id = e.page_id AND d.status = 'queued'
    RETURNING d.page_id
    """
)

# I2. The lock statement waits for a dispatch of the row still in flight (the attempt's UPDATE
# alone would not: a row that is still pending in its snapshot does not match, so it would not
# wait). old.* (PostgreSQL 18) tells a takeover from a first attempt.
_ATTEMPT_LOCK = text("SELECT 1 FROM search_documents WHERE page_id = :page_id FOR NO KEY UPDATE")

_ATTEMPT = text(
    """
    UPDATE search_documents AS d
    SET attempt_count = d.attempt_count + 1,
        lease_owner = :owner,
        lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds)
    FROM pages AS p
    WHERE d.page_id = :page_id AND d.job_id = :job_id
      AND p.id = d.page_id AND p.job_id = d.job_id
      AND d.status = 'queued'
      AND d.attempt_count < :max_attempts
      AND (d.lease_expires_at IS NULL OR d.lease_expires_at < clock_timestamp())
      AND p.content_version >= :content_version
    RETURNING d.attempt_count, p.content_version, old.lease_owner AS previous_owner
    """
)

_INSPECT = text(
    """
    SELECT d.job_id, d.status, d.attempt_count, d.lease_owner,
           coalesce(d.lease_expires_at >= clock_timestamp(), false) AS lease_live,
           p.content_version, d.indexed_content_version
    FROM search_documents AS d
    JOIN pages AS p ON p.id = d.page_id AND p.job_id = d.job_id
    WHERE d.page_id = :page_id
    """
)

# I3. The page row is locked first (the MATERIALIZED CTE, FOR SHARE) and returned in its latest
# committed version; the vector, its version and the length come from that one row. A body longer
# than the limit is cut at the last whitespace before it, found within WORD_LOOKBACK characters,
# so no partial word is indexed (a longer run of non-space characters is left as it is: the
# parser ignores words that long). Only the configuration and the body limit are parameters; the
# weights and the lookback are constants of app/indexing/documents.py.
_INDEX = text(
    f"""
    WITH locked AS MATERIALIZED (
        SELECT id, job_id, content_version, title, meta_description, headings,
               left(extracted_text, :max_body_chars) AS cut,
               substr(extracted_text, :max_body_chars + 1, 1) AS after_cut,
               char_length(extracted_text) AS body_chars
        FROM pages
        WHERE id = :page_id AND job_id = :job_id
        FOR SHARE
    ),
    page AS (
        SELECT locked.*,
               CASE WHEN after_cut = '' OR after_cut ~ '\\s' THEN cut
                    WHEN right(cut, {WORD_LOOKBACK}) !~ '\\s' THEN cut
                    ELSE left(cut, char_length(cut) - coalesce(char_length(
                             substring(right(cut, {WORD_LOOKBACK}) FROM '\\S+$')), 0))
               END AS body
        FROM locked
    )
    UPDATE search_documents AS d
    SET search_vector =
            setweight(to_tsvector(CAST(:config AS regconfig), coalesce(page.title, '')),
                      '{TITLE_WEIGHT}')
         || setweight(to_tsvector(CAST(:config AS regconfig),
                                  coalesce(page.meta_description, '')), '{DESCRIPTION_WEIGHT}')
         || setweight(to_tsvector(CAST(:config AS regconfig), page.headings),
                      '{HEADINGS_WEIGHT}')
         || setweight(to_tsvector(CAST(:config AS regconfig), page.body), '{BODY_WEIGHT}'),
        indexed_content_version = page.content_version,
        status = 'indexed',
        indexed_at = clock_timestamp(),
        last_error = NULL,
        lease_owner = NULL,
        lease_expires_at = NULL
    FROM page
    WHERE d.page_id = page.id AND d.job_id = page.job_id
      AND d.status = 'queued'
      AND (d.indexed_content_version IS NULL
           OR d.indexed_content_version <= page.content_version)
    RETURNING page.content_version, length(d.search_vector) AS lexemes, page.body_chars,
              char_length(page.body) AS indexed_body_chars
    """  # noqa: S608 - only constants of app/indexing/documents.py are inserted
)

_STATUS = text("SELECT status FROM search_documents WHERE page_id = :page_id")

_STATEMENT_TIMEOUT = text("SELECT set_config('statement_timeout', :timeout, true)")

# I4 after a deterministic error of the running attempt.
_FAIL = text(
    """
    UPDATE search_documents AS d
    SET status = 'failed', last_error = :reason, lease_owner = NULL, lease_expires_at = NULL
    FROM pages AS p
    WHERE d.page_id = :page_id AND d.job_id = :job_id
      AND p.id = d.page_id AND p.job_id = d.job_id
      AND d.status = 'queued'
    RETURNING d.attempt_count, p.crawl_url_id, p.content_version
    """
)

# I4 once the attempts are used up and no attempt is running; the last attempt's error is kept.
_FAIL_EXHAUSTED = text(
    """
    UPDATE search_documents AS d
    SET status = 'failed',
        last_error = left(:reason || coalesce(' (last error: ' || d.last_error || ')', ''),
                          :max_error_chars),
        lease_owner = NULL,
        lease_expires_at = NULL
    FROM pages AS p
    WHERE d.page_id = :page_id AND d.job_id = :job_id
      AND p.id = d.page_id AND p.job_id = d.job_id
      AND d.status = 'queued'
      AND d.attempt_count >= :max_attempts
      AND (d.lease_expires_at IS NULL OR d.lease_expires_at < clock_timestamp())
    RETURNING d.attempt_count, p.crawl_url_id, p.content_version
    """
)

# An attempt that will be retried: keep its error for diagnostics, release its lease.
_NOTE_ATTEMPT_ERROR = text(
    """
    UPDATE search_documents
    SET last_error = :reason, lease_owner = NULL, lease_expires_at = NULL
    WHERE page_id = :page_id AND status = 'queued' AND lease_owner = :owner
    """
)

# I5. Candidates: finished jobs whose indexing is not complete (ix_crawl_jobs_indexing_open)
# and that have nothing left to index (ix_search_documents_open), so jobs that still wait never
# hold up the others. Each candidate is then checked again under its row lock.
_COMPLETABLE_JOBS = text(
    """
    SELECT j.id
    FROM crawl_jobs AS j
    WHERE j.finished_at IS NOT NULL AND j.indexing_completed_at IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM search_documents AS d
          WHERE d.job_id = j.id AND d.status IN ('pending', 'queued')
      )
    ORDER BY j.finished_at, j.id
    LIMIT :limit
    """
)

_LOCK_FINISHED_JOB = text(
    """
    SELECT id FROM crawl_jobs
    WHERE id = :job_id AND finished_at IS NOT NULL AND indexing_completed_at IS NULL
    FOR NO KEY UPDATE
    """
)

_JOB_DOCUMENT_COUNTS = text(
    """
    SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
           count(*) FILTER (WHERE status = 'queued') AS queued,
           count(*) FILTER (WHERE status = 'indexed') AS indexed,
           count(*) FILTER (WHERE status = 'failed') AS failed,
           count(*) FILTER (WHERE status = 'queued'
                            AND lease_expires_at >= clock_timestamp()) AS active
    FROM search_documents
    WHERE job_id = :job_id
    """
)

_COMPLETE_JOB = text(
    """
    UPDATE crawl_jobs
    SET indexing_completed_at = GREATEST(clock_timestamp(), finished_at)
    WHERE id = :job_id AND finished_at IS NOT NULL AND indexing_completed_at IS NULL
    RETURNING indexing_completed_at
    """
)

# I6.
_LOCK_JOB = text("SELECT id FROM crawl_jobs WHERE id = :job_id FOR NO KEY UPDATE")

_REINDEX = text(
    """
    UPDATE search_documents
    SET status = 'pending', attempt_count = 0, last_error = NULL
    WHERE job_id = :job_id AND status IN ('indexed', 'failed')
    RETURNING page_id
    """
)

_REOPEN_JOB = text("UPDATE crawl_jobs SET indexing_completed_at = NULL WHERE id = :job_id")

_JOB_INDEXING = text(
    """
    SELECT j.indexing_completed_at, c.*
    FROM crawl_jobs AS j
    CROSS JOIN LATERAL (
        SELECT count(*) FILTER (WHERE d.status = 'pending') AS pending,
               count(*) FILTER (WHERE d.status = 'queued') AS queued,
               count(*) FILTER (WHERE d.status = 'indexed') AS indexed,
               count(*) FILTER (WHERE d.status = 'failed') AS failed,
               count(*) FILTER (WHERE d.status = 'queued'
                                AND d.lease_expires_at >= clock_timestamp()) AS active
        FROM search_documents AS d
        WHERE d.job_id = j.id
    ) AS c
    WHERE j.id = :job_id
    """
)

_OVERVIEW = text(
    """
    SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
           count(*) FILTER (WHERE status = 'queued') AS queued,
           count(*) FILTER (WHERE status = 'indexed') AS indexed,
           count(*) FILTER (WHERE status = 'failed') AS failed,
           count(*) FILTER (WHERE status = 'queued'
                            AND lease_expires_at >= clock_timestamp()) AS active,
           extract(epoch FROM clock_timestamp()
                   - min(updated_at) FILTER (WHERE status = 'pending')) AS oldest_pending,
           extract(epoch FROM clock_timestamp()
                   - min(dispatched_at) FILTER (WHERE status = 'queued')) AS oldest_queued,
           (SELECT count(*) FROM crawl_jobs
            WHERE finished_at IS NOT NULL AND indexing_completed_at IS NULL) AS awaiting
    FROM search_documents
    """
)


# --- Use cases ---------------------------------------------------------------------------------


async def dispatch_index_tasks(
    session: AsyncSession, *, limit: int, publish: IndexPublisher
) -> list[uuid.UUID]:
    """I1: publish up to ``limit`` pending documents, oldest change first, and mark them queued.

    Rows locked by a concurrent dispatcher are skipped, so two dispatchers never publish the
    same row in one pass. If publishing or the commit fails, the rows stay ``pending`` and are
    published again later; a message that was published meanwhile finds nothing to do.
    """
    if limit <= 0:
        return []
    async with session.begin():
        rows = (await session.execute(_DISPATCH_LOCK, {"limit": limit})).all()
        if not rows:
            return []
        tasks = [IndexTaskRef(row.job_id, row.page_id, row.content_version) for row in rows]
        entries = await publish(tasks)
        if len(entries) != len(tasks):  # pragma: no cover - the publisher returns one per task
            raise RuntimeError("the publisher returned a different number of entry IDs")
        marked = await session.execute(
            _DISPATCH_MARK, {"ids": [task.page_id for task in tasks], "entries": entries}
        )
        dispatched = [row.page_id for row in marked]
    logger.info(
        "Index tasks dispatched", extra={"event": "index.dispatched", "count": len(dispatched)}
    )
    return dispatched


async def queued_index_tasks(session: AsyncSession) -> int:
    """Documents published and not yet indexed or failed (the dispatcher's backpressure)."""
    async with session.begin():
        return int((await session.execute(_QUEUED_COUNT)).scalar_one())


async def republish_stale_index_tasks(
    session: AsyncSession,
    *,
    older_than: float,
    limit: int,
    publish: IndexPublisher,
    delivering: DeliveryCheck,
    after: ReconcileCursor | None = None,
) -> Reconciliation:
    """Publish again the queued documents whose stream message is gone.

    Examines up to ``limit`` documents queued for at least ``older_than`` seconds without a
    live attempt. One whose stream entry is still delivering (``delivering``: undelivered, or
    pending for ``XAUTOCLAIM``) is left alone, so a pause without indexers publishes nothing;
    the others (entry acknowledged, trimmed, or lost with Redis data) are published again, with
    the page's current content version. The rows stay locked meanwhile (``SKIP LOCKED``), so
    concurrent dispatchers never republish the same document, and an attempt waits for this
    transaction. Pass ``resume_after`` of the previous call as ``after`` to continue the scan.
    """
    if limit <= 0:
        return Reconciliation(0, 0, after)
    async with session.begin():
        rows = (
            await session.execute(
                _STALE_LOCK,
                {
                    "older_than": older_than,
                    "limit": limit,
                    "after_at": after[0] if after else None,
                    "after_id": after[1] if after else None,
                },
            )
        ).all()
        if not rows:
            return Reconciliation(0, 0, None)
        alive = await delivering([row.stream_entry_id for row in rows])
        lost = [row for row, live in zip(rows, alive, strict=True) if not live]
        republished = 0
        if lost:
            entries = await publish(
                [IndexTaskRef(row.job_id, row.page_id, row.content_version) for row in lost]
            )
            marked = await session.execute(
                _STALE_MARK, {"ids": [row.page_id for row in lost], "entries": entries}
            )
            republished = len(marked.all())
    if republished:
        logger.warning(
            "Index tasks whose stream message was gone were published again",
            extra={"event": "index.republished", "count": republished, "examined": len(rows)},
        )
    last = rows[-1]
    cursor = (last.dispatched_at, last.page_id) if len(rows) == limit else None
    return Reconciliation(len(rows), republished, cursor)


async def start_index_attempt(
    session: AsyncSession,
    task: IndexTaskRef,
    *,
    owner: str,
    lease_seconds: float,
    max_attempts: int,
) -> IndexAttempt | None:
    """I2: count an attempt and take the document's lease; None if it cannot start now.

    The document must be ``queued`` in the message's job, the message's content version must not
    be newer than the page's (an older message stands for the same work, since I3 indexes the
    current content), an attempt must be left, and no other attempt may hold a live lease.
    ``inspect_document`` tells why an attempt could not start.
    """
    async with session.begin():
        await session.execute(_ATTEMPT_LOCK, {"page_id": task.page_id})
        row = (
            await session.execute(
                _ATTEMPT,
                {
                    "page_id": task.page_id,
                    "job_id": task.job_id,
                    "content_version": task.content_version,
                    "owner": owner,
                    "lease_seconds": lease_seconds,
                    "max_attempts": max_attempts,
                },
            )
        ).one_or_none()
    if row is None:
        return None
    takeover = row.previous_owner is not None and row.previous_owner != owner
    if takeover:
        logger.warning(
            "Took over the expired lease of an index attempt",
            extra={
                "event": "index.lease_taken_over",
                "page_id": str(task.page_id),
                "previous_owner": row.previous_owner,
                "attempt": row.attempt_count,
            },
        )
    return IndexAttempt(
        page_id=task.page_id,
        job_id=task.job_id,
        attempt=row.attempt_count,
        content_version=row.content_version,
        takeover=takeover,
    )


async def inspect_document(session: AsyncSession, page_id: uuid.UUID) -> DocumentStatus | None:
    """The authoritative state of a search document, or None if there is none for the page."""
    async with session.begin():
        row = (await session.execute(_INSPECT, {"page_id": page_id})).one_or_none()
    if row is None:
        return None
    return DocumentStatus(
        page_id=page_id,
        job_id=row.job_id,
        status=IndexStatus(row.status),
        attempt_count=row.attempt_count,
        lease_owner=row.lease_owner,
        lease_live=row.lease_live,
        content_version=row.content_version,
        indexed_content_version=row.indexed_content_version,
    )


async def index_document(
    session: AsyncSession,
    task: IndexTaskRef,
    *,
    max_body_chars: int,
    budget_seconds: float,
    worker_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> IndexResult:
    """I3: build and store the document's vector from the page's current content.

    The transaction's statements share ``budget_seconds``: before each one, ``statement_timeout``
    is set to what remains, so PostgreSQL stops the work once the budget is spent (SQLSTATE
    57014, which propagates); a budget spent before a statement starts raises
    ``IndexDeadlineError``. Either way the attempt failed, nothing was written, and it will be
    retried. A vector that exceeds PostgreSQL's size limit is built again once from a quarter of
    the body; if it still does not fit, the document fails as ``tsvector_too_large``, in the same
    transaction.
    """
    deadline = clock() + budget_seconds
    async with session.begin():
        for body_limit in (max_body_chars, fallback_body_chars(max_body_chars)):
            await _within(session, deadline, clock)
            try:
                async with session.begin_nested():
                    row = (
                        await session.execute(
                            _INDEX,
                            {
                                "page_id": task.page_id,
                                "job_id": task.job_id,
                                "config": TEXT_SEARCH_CONFIG,
                                "max_body_chars": body_limit,
                            },
                        )
                    ).one_or_none()
            except DBAPIError as exc:
                if _sqlstate(exc) != PROGRAM_LIMIT_EXCEEDED:
                    raise
                logger.warning(
                    "A search vector was too large; indexing less of the body",
                    extra={
                        "event": "index.vector_too_large",
                        "page_id": str(task.page_id),
                        "max_body_chars": body_limit,
                    },
                )
                continue
            if row is None:
                await _within(session, deadline, clock)
                found = (await session.execute(_STATUS, {"page_id": task.page_id})).scalar()
                return IndexResult(
                    IndexOutcome.NOT_QUEUED,
                    status=IndexStatus(found) if found is not None else None,
                )
            return IndexResult(
                IndexOutcome.INDEXED,
                content_version=row.content_version,
                lexemes=row.lexemes,
                body_chars=row.body_chars,
                indexed_body_chars=row.indexed_body_chars,
            )
        await _within(session, deadline, clock)
        failed = await _fail(session, task, reason=TSVECTOR_TOO_LARGE, worker_id=worker_id)
    if not failed:
        return IndexResult(IndexOutcome.NOT_QUEUED)
    return IndexResult(IndexOutcome.FAILED, reason=TSVECTOR_TOO_LARGE)


async def fail_document(
    session: AsyncSession, task: IndexTaskRef, *, reason: str, worker_id: str | None = None
) -> bool:
    """I4: a queued document fails for ``reason`` (a stable token, never content).

    False if it is not queued (another attempt indexed or failed it, or it is pending again).
    """
    async with session.begin():
        return await _fail(session, task, reason=reason, worker_id=worker_id)


async def fail_exhausted_document(
    session: AsyncSession,
    task: IndexTaskRef,
    *,
    max_attempts: int,
    worker_id: str | None = None,
) -> bool:
    """I4: a queued document whose attempts are used up, and that no attempt holds, fails as
    ``max_attempts_exceeded`` (keeping the last attempt's error). False if it changed."""
    async with session.begin():
        row = (
            await session.execute(
                _FAIL_EXHAUSTED,
                {
                    "page_id": task.page_id,
                    "job_id": task.job_id,
                    "reason": MAX_ATTEMPTS_EXCEEDED,
                    "max_attempts": max_attempts,
                    "max_error_chars": MAX_ERROR_CHARS,
                },
            )
        ).one_or_none()
        if row is not None:
            _failed_event(session, task, row, reason=MAX_ATTEMPTS_EXCEEDED, worker_id=worker_id)
    if row is None:
        return False
    _log_failed(task, row.attempt_count, MAX_ATTEMPTS_EXCEEDED)
    return True


async def note_attempt_error(
    session: AsyncSession, page_id: uuid.UUID, *, owner: str, reason: str
) -> bool:
    """Record why this owner's attempt failed (it will be retried) and release its lease."""
    async with session.begin():
        result = await session.execute(
            _NOTE_ATTEMPT_ERROR, {"page_id": page_id, "owner": owner, "reason": reason}
        )
    return bool(getattr(result, "rowcount", 0))


async def completable_indexing_jobs(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Finished jobs whose indexing is not marked complete although none of their documents is
    ``pending`` or ``queued``: the candidates of ``complete_job_indexing``, oldest finish first."""
    async with session.begin():
        return [row.id for row in await session.execute(_COMPLETABLE_JOBS, {"limit": limit})]


async def complete_job_indexing(session: AsyncSession, job_id: uuid.UUID) -> bool:
    """I5: mark a finished job's indexing complete if none of its documents is pending or queued.

    Under the job row lock, which a reindex (I6) also takes. A finished job gets no new pages:
    its URLs are all terminal, and only a fenced result of a URL in progress stores a page.
    """
    async with session.begin():
        locked = (await session.execute(_LOCK_FINISHED_JOB, {"job_id": job_id})).one_or_none()
        if locked is None:
            return False
        # A fresh statement after the lock: it sees every commit made before the lock was taken.
        counts = (await session.execute(_JOB_DOCUMENT_COUNTS, {"job_id": job_id})).one()
        if counts.pending or counts.queued:
            return False
        completed_at = (await session.execute(_COMPLETE_JOB, {"job_id": job_id})).scalar_one()
        add_event(
            session,
            job_id,
            "job.indexing_completed",
            detail={"indexed": counts.indexed, "failed": counts.failed},
        )
    logger.info(
        "Job indexing completed",
        extra={
            "event": "job.indexing_completed",
            "job_id": str(job_id),
            "indexed": counts.indexed,
            "failed": counts.failed,
            "completed_at": completed_at.isoformat(),
        },
    )
    return True


async def request_reindex(session: AsyncSession, job_id: uuid.UUID) -> int:
    """I6: index every page of a job again; returns the documents set back to ``pending``.

    Pending and queued documents are on their way already and are left alone. Until a document
    is indexed again, search keeps serving its previous vector: the content did not change, so
    it still satisfies the visibility rule. Raises ``NotFoundError`` for an unknown job.
    """
    async with session.begin():
        if (await session.execute(_LOCK_JOB, {"job_id": job_id})).one_or_none() is None:
            raise NotFoundError(f"Crawl job {job_id} does not exist.")
        reset = len((await session.execute(_REINDEX, {"job_id": job_id})).all())
        await session.execute(_REOPEN_JOB, {"job_id": job_id})
        add_event(session, job_id, "job.reindex_requested", detail={"documents": reset})
    logger.info(
        "Reindexing requested",
        extra={"event": "job.reindex_requested", "job_id": str(job_id), "documents": reset},
    )
    return reset


async def job_indexing(session: AsyncSession, job_id: uuid.UUID) -> IndexingProgress:
    """A job's indexing progress; raises ``NotFoundError`` for an unknown job."""
    async with session.begin():
        progress = await read_job_indexing(session, job_id)
    if progress is None:
        raise NotFoundError(f"Crawl job {job_id} does not exist.")
    return progress


async def read_job_indexing(session: AsyncSession, job_id: uuid.UUID) -> IndexingProgress | None:
    """A job's indexing progress, read in the caller's transaction; None for an unknown job."""
    row = (await session.execute(_JOB_INDEXING, {"job_id": job_id})).one_or_none()
    return None if row is None else progress_from_row(row)


def progress_from_row(row: Any) -> IndexingProgress:
    """An ``IndexingProgress`` from a row with the counts and ``indexing_completed_at``."""
    return IndexingProgress(
        pending=row.pending,
        queued=row.queued,
        indexed=row.indexed,
        failed=row.failed,
        active=row.active,
        completed_at=row.indexing_completed_at,
    )


async def indexing_overview(session: AsyncSession) -> IndexingOverview:
    """Indexing statistics over every job (a full scan of ``search_documents``)."""
    async with session.begin():
        return await read_indexing_overview(session)


async def read_indexing_overview(session: AsyncSession) -> IndexingOverview:
    """``indexing_overview``, read in the caller's transaction."""
    row = (await session.execute(_OVERVIEW)).one()
    return IndexingOverview(
        pending=row.pending,
        queued=row.queued,
        indexed=row.indexed,
        failed=row.failed,
        active=row.active,
        oldest_pending_seconds=_seconds(row.oldest_pending),
        oldest_queued_seconds=_seconds(row.oldest_queued),
        jobs_awaiting_completion=row.awaiting,
    )


# --- Helpers -----------------------------------------------------------------------------------


async def _within(session: AsyncSession, deadline: float, clock: Callable[[], float]) -> None:
    """Give the next statement what remains of the attempt's budget (``statement_timeout``)."""
    remaining = deadline - clock()
    if remaining <= 0:
        raise IndexDeadlineError("the index attempt's deadline passed")
    await session.execute(_STATEMENT_TIMEOUT, {"timeout": str(max(1, math.ceil(remaining * 1000)))})


async def _fail(
    session: AsyncSession, task: IndexTaskRef, *, reason: str, worker_id: str | None
) -> bool:
    row = (
        await session.execute(
            _FAIL, {"page_id": task.page_id, "job_id": task.job_id, "reason": reason}
        )
    ).one_or_none()
    if row is None:
        return False
    _failed_event(session, task, row, reason=reason, worker_id=worker_id)
    _log_failed(task, row.attempt_count, reason)
    return True


def _failed_event(
    session: AsyncSession, task: IndexTaskRef, row: Any, *, reason: str, worker_id: str | None
) -> None:
    add_event(
        session,
        task.job_id,
        "page.index_failed",
        crawl_url_id=row.crawl_url_id,
        from_state=IndexStatus.QUEUED.value,
        to_state=IndexStatus.FAILED.value,
        worker_id=worker_id,
        detail={
            "page_id": str(task.page_id),
            "reason": reason,
            "attempts": row.attempt_count,
            "content_version": row.content_version,
        },
    )


def _log_failed(task: IndexTaskRef, attempts: int, reason: str) -> None:
    logger.warning(
        "A page could not be indexed",
        extra={
            "event": "index.failed",
            "job_id": str(task.job_id),
            "page_id": str(task.page_id),
            "reason": reason,
            "attempts": attempts,
        },
    )


def _sqlstate(error: DBAPIError) -> str | None:
    original = getattr(error, "orig", None)
    code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return str(code) if code is not None else None


def sqlstate(error: BaseException) -> str | None:
    """The SQLSTATE of a database error, or None."""
    return _sqlstate(error) if isinstance(error, DBAPIError) else None


def _seconds(value: Any) -> float | None:
    return None if value is None else round(float(value), 3)
