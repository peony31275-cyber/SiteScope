"""SiteScope's persistent data model (PostgreSQL is the source of truth, ADR-0001).

Tables and their roles (details and rationale: docs/architecture.md, section 5):

* ``crawl_jobs``       one row per crawl request: configuration, lifecycle, BFS level, page budget.
* ``crawl_urls``       the URL frontier and each URL's lifecycle. It is also the outbox for crawl
                       tasks (ADR-0002): the scheduler publishes committed ``pending`` rows.
* ``pages``            content extracted from fetched HTML documents; at most one per crawl URL.
* ``page_links``       the crawl graph: every http(s) link found on a page.
* ``search_documents`` per-page full-text index state. It is also the outbox for index tasks
                       (ADR-0008).
* ``crawl_events``     append-only audit log of important lifecycle events.

Integrity rules enforced by the database itself:

* Job isolation: child rows carry ``job_id``, and composite foreign keys ``(parent_id, job_id)``
  make cross-job references impossible.
* Deduplication: ``UNIQUE (job_id, normalized_url)`` on ``crawl_urls`` (ADR-0009).
* Page budget: ``CHECK (admitted_count BETWEEN 0 AND max_pages)`` on ``crawl_jobs``.
* Fencing (ADR-0003, ADR-0010, ADR-0011): ``result_epoch`` must equal the current
  ``lease_epoch``, so a stale worker's result violates a CHECK constraint and its whole
  transaction is rolled back. Triggers created by the migrations (not visible in these models)
  keep ``lease_epoch`` from decreasing (0001), and let it change only in a claim, by exactly
  one, with every lease grant requiring that new token (0002).
* Index versions (ADR-0018): triggers keep ``pages.content_version`` and
  ``search_documents.indexed_content_version`` from moving back (0004), so an older index can
  never replace a newer one.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UrlState(StrEnum):
    PENDING = "pending"  # admitted; waits for dispatch (or for next_attempt_at)
    QUEUED = "queued"  # published to the crawl stream
    IN_PROGRESS = "in_progress"  # claimed by a worker that holds a lease
    FETCHED = "fetched"  # an HTTP response was received and recorded
    FAILED = "failed"  # no usable response after bounded retries
    SKIPPED = "skipped"  # not requested, by policy (robots.txt, blocked destination, ...)
    CANCELLED = "cancelled"  # the job was cancelled before this URL was processed


class IndexStatus(StrEnum):
    PENDING = "pending"  # (re)indexing needed; not yet published to the index stream
    QUEUED = "queued"  # published to the index stream
    INDEXED = "indexed"  # search_vector reflects indexed_content_version
    FAILED = "failed"  # gave up after bounded attempts


TERMINAL_JOB_STATUSES = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
STARTED_JOB_STATUSES = (
    JobStatus.RUNNING,
    JobStatus.CANCELLING,
    JobStatus.COMPLETED,
    JobStatus.FAILED,
)
ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCELLING)
UNFINISHED_URL_STATES = (UrlState.PENDING, UrlState.QUEUED, UrlState.IN_PROGRESS)
WORKER_OUTCOME_STATES = (UrlState.FETCHED, UrlState.FAILED, UrlState.SKIPPED)
TERMINAL_URL_STATES = (*WORKER_OUTCOME_STATES, UrlState.CANCELLED)

# Normalized URLs are printable ASCII (percent-encoded, IDNA hosts) and at most 2048 bytes, so
# B-tree index entries on them stay well below PostgreSQL's ~2.7 kB limit.
MAX_URL_BYTES = 2048


def sql_in(column: str, values: Iterable[StrEnum]) -> str:
    """Render ``column IN ('a', 'b')`` for CHECK constraints and partial-index predicates."""
    return f"{column} IN ({', '.join(f"'{value.value}'" for value in values)})"


def normalized_url_check(column: str) -> str:
    """CHECK expression: an http(s) URL of printable ASCII, at most MAX_URL_BYTES bytes."""
    return f"{column} ~ '^https?://[\\x21-\\x7e]+$' AND octet_length({column}) <= {MAX_URL_BYTES}"


# Redis stream entry IDs look like "1730880000000-0".
STREAM_ENTRY_ID_CHECK = "stream_entry_id IS NULL OR stream_entry_id ~ '^[0-9]+-[0-9]+$'"


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    seed_url: Mapped[str] = mapped_column(Text(collation="C"))
    # Crawl scope: the hosts this job may fetch (exact match); always within the deployment
    # allowlist (ADR-0006).
    allowed_hosts: Mapped[list[str]] = mapped_column(ARRAY(Text))
    max_depth: Mapped[int] = mapped_column(SmallInteger)
    max_pages: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    # The BFS level being crawled; only URLs at this depth are dispatched (ADR-0004).
    current_level: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    # URLs admitted so far (seed included). Never exceeds max_pages (CHECK below).
    admitted_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Job-creation idempotency: the same key with the same request returns the existing job.
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    request_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), server_onupdate=FetchedValue()
    )
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    cancel_requested_at: Mapped[datetime | None]
    # Set once every page of a finished job has been indexed (Phase 7).
    indexing_completed_at: Mapped[datetime | None]

    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 - SQLAlchemy declarative API
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint(sql_in("status", JobStatus), name="status_valid"),
        CheckConstraint(normalized_url_check("seed_url"), name="seed_url_normalized"),
        CheckConstraint("cardinality(allowed_hosts) >= 1", name="allowed_hosts_not_empty"),
        CheckConstraint("max_depth >= 0", name="max_depth_non_negative"),
        CheckConstraint("max_pages >= 1", name="max_pages_positive"),
        CheckConstraint("current_level BETWEEN 0 AND max_depth", name="current_level_in_range"),
        CheckConstraint("admitted_count BETWEEN 0 AND max_pages", name="admitted_within_budget"),
        CheckConstraint(
            f"(finished_at IS NOT NULL) = ({sql_in('status', TERMINAL_JOB_STATUSES)})",
            name="finished_iff_terminal",
        ),
        CheckConstraint(
            f"NOT ({sql_in('status', STARTED_JOB_STATUSES)}) OR started_at IS NOT NULL",
            name="started_at_when_started",
        ),
        CheckConstraint(
            "status <> 'failed' OR failure_reason IS NOT NULL", name="failure_reason_when_failed"
        ),
        CheckConstraint(
            "status NOT IN ('cancelling', 'cancelled') OR cancel_requested_at IS NOT NULL",
            name="cancel_requested_when_cancelled",
        ),
        CheckConstraint(
            "indexing_completed_at IS NULL OR finished_at IS NOT NULL",
            name="indexing_completed_after_finish",
        ),
        CheckConstraint(
            "(started_at IS NULL OR started_at >= created_at)"
            " AND (finished_at IS NULL OR finished_at >= created_at)",
            name="timestamps_ordered",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL) = (request_fingerprint IS NULL)",
            name="idempotency_key_with_fingerprint",
        ),
        CheckConstraint(
            "request_fingerprint IS NULL OR octet_length(request_fingerprint) = 32",
            name="request_fingerprint_sha256",
        ),
        Index("ix_crawl_jobs_created_at_id", "created_at", "id"),
        Index(
            "ix_crawl_jobs_active",
            "created_at",
            postgresql_where=text(sql_in("status", ACTIVE_JOB_STATUSES)),
        ),
        # Index dispatcher (Phase 7): finished jobs whose indexing is not complete yet.
        Index(
            "ix_crawl_jobs_indexing_open",
            "finished_at",
            postgresql_where=text("finished_at IS NOT NULL AND indexing_completed_at IS NULL"),
        ),
    )


class CrawlUrl(Base):
    __tablename__ = "crawl_urls"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crawl_jobs.id", ondelete="CASCADE"))
    normalized_url: Mapped[str] = mapped_column(Text(collation="C"))
    # BFS distance from the seed (redirect targets inherit the redirecting URL's depth).
    depth: Mapped[int] = mapped_column(SmallInteger)
    # BFS parent: the first page that linked here (or the URL that redirected here).
    discovered_from_url_id: Mapped[uuid.UUID | None]
    redirect_hops: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    state: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    # Number of claims so far; bounded by the worker's max_attempts (Phase 6).
    attempt_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Fencing token: incremented by exactly one by every claim, and changed by nothing else
    # (triggers in migrations 0001 and 0002).
    lease_epoch: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None]
    # The lease_epoch of the claim that recorded the current outcome; must equal lease_epoch.
    result_epoch: Mapped[int | None] = mapped_column(BigInteger)
    # Task publication: when, and as which Redis stream entry, the task was last published.
    dispatched_at: Mapped[datetime | None]
    stream_entry_id: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    outcome: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    redirect_target: Mapped[str | None] = mapped_column(Text(collation="C"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), server_onupdate=FetchedValue()
    )
    finished_at: Mapped[datetime | None]

    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 - SQLAlchemy declarative API
    __table_args__ = (
        # Deduplication (ADR-0009): one row per URL per job; INSERT ... ON CONFLICT DO NOTHING.
        UniqueConstraint("job_id", "normalized_url"),
        # Targets for composite foreign keys that keep child rows inside the same job.
        UniqueConstraint("id", "job_id"),
        UniqueConstraint("id", "job_id", "normalized_url"),
        ForeignKeyConstraint(
            ["discovered_from_url_id", "job_id"], ["crawl_urls.id", "crawl_urls.job_id"]
        ),
        CheckConstraint(sql_in("state", UrlState), name="state_valid"),
        CheckConstraint(normalized_url_check("normalized_url"), name="normalized_url_format"),
        CheckConstraint(
            f"redirect_target IS NULL OR ({normalized_url_check('redirect_target')})",
            name="redirect_target_format",
        ),
        CheckConstraint("depth >= 0", name="depth_non_negative"),
        CheckConstraint("redirect_hops >= 0", name="redirect_hops_non_negative"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("lease_epoch >= 0", name="lease_epoch_non_negative"),
        CheckConstraint(
            "discovered_from_url_id IS NULL OR discovered_from_url_id <> id",
            name="not_discovered_from_self",
        ),
        # A lease (owner + expiry) exists exactly while the URL is in progress.
        CheckConstraint(
            "((state = 'in_progress') = (lease_owner IS NOT NULL))"
            " AND ((state = 'in_progress') = (lease_expires_at IS NOT NULL))",
            name="lease_iff_in_progress",
        ),
        # Fencing: an outcome can only be recorded under the current lease epoch.
        CheckConstraint(
            "result_epoch IS NULL OR result_epoch = lease_epoch", name="result_epoch_is_current"
        ),
        # A claim must clear the previous outcome marker.
        CheckConstraint(
            "state <> 'in_progress' OR result_epoch IS NULL", name="result_epoch_cleared_by_claim"
        ),
        # Every worker-produced outcome names the lease epoch that produced it.
        CheckConstraint(
            f"NOT ({sql_in('state', WORKER_OUTCOME_STATES)}) OR result_epoch IS NOT NULL",
            name="worker_outcome_has_result_epoch",
        ),
        CheckConstraint(
            "state <> 'queued' OR dispatched_at IS NOT NULL", name="dispatched_when_queued"
        ),
        CheckConstraint(STREAM_ENTRY_ID_CHECK, name="stream_entry_id_format"),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599", name="http_status_range"
        ),
        CheckConstraint("outcome IS NULL OR outcome ~ '^[a-z][a-z0-9_]*$'", name="outcome_format"),
        CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 2000", name="last_error_length"
        ),
        CheckConstraint(
            f"(finished_at IS NOT NULL) = ({sql_in('state', TERMINAL_URL_STATES)})",
            name="finished_iff_terminal",
        ),
        # Dispatcher: pending URLs of a job at a given BFS level, oldest retry time first.
        Index(
            "ix_crawl_urls_dispatch",
            "job_id",
            "depth",
            "next_attempt_at",
            postgresql_where=text("state = 'pending'"),
        ),
        # Level advancer / finalizer: does a job still have unfinished URLs?
        Index(
            "ix_crawl_urls_unfinished",
            "job_id",
            postgresql_where=text(sql_in("state", UNFINISHED_URL_STATES)),
        ),
        # Recovery: expired leases and stale publications.
        Index(
            "ix_crawl_urls_lease_expiry",
            "lease_expires_at",
            postgresql_where=text("state = 'in_progress'"),
        ),
        Index(
            "ix_crawl_urls_queued_since",
            "dispatched_at",
            postgresql_where=text("state = 'queued'"),
        ),
        # Supports the self-referencing foreign key (e.g. when a job's URLs are deleted).
        Index(
            "ix_crawl_urls_discovered_from",
            "discovered_from_url_id",
            "job_id",
            postgresql_where=text("discovered_from_url_id IS NOT NULL"),
        ),
    )


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    job_id: Mapped[uuid.UUID]
    crawl_url_id: Mapped[uuid.UUID]
    # Copy of the crawl URL's normalized URL; the composite foreign key guarantees it matches.
    normalized_url: Mapped[str] = mapped_column(Text(collation="C"))
    http_status: Mapped[int] = mapped_column(SmallInteger)
    content_type: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    meta_description: Mapped[str | None] = mapped_column(Text)
    headings: Mapped[str] = mapped_column(Text, server_default=text("''"))
    extracted_text: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # SHA-256 of the extracted content.
    content_hash: Mapped[bytes] = mapped_column(LargeBinary)
    # Incremented whenever the stored content changes; search_documents tracks what was indexed.
    content_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    byte_size: Mapped[int] = mapped_column(Integer)
    fetch_duration_ms: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime]
    # Robots directives (meta robots, X-Robots-Tag; migration 0003, ADR-0014): a noindex page gets
    # no search_documents row; the links of a nofollow page are recorded but never admitted.
    robots_noindex: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    robots_nofollow: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), server_onupdate=FetchedValue()
    )

    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 - SQLAlchemy declarative API
    __table_args__ = (
        # At most one page per crawl URL: repeated or stale writes cannot create duplicates.
        UniqueConstraint("crawl_url_id"),
        UniqueConstraint("id", "job_id"),
        ForeignKeyConstraint(
            ["crawl_url_id", "job_id", "normalized_url"],
            ["crawl_urls.id", "crawl_urls.job_id", "crawl_urls.normalized_url"],
            ondelete="CASCADE",
        ),
        CheckConstraint("http_status BETWEEN 100 AND 599", name="http_status_range"),
        CheckConstraint("octet_length(content_hash) = 32", name="content_hash_sha256"),
        CheckConstraint("content_version >= 1", name="content_version_positive"),
        CheckConstraint("byte_size >= 0", name="byte_size_non_negative"),
        CheckConstraint(
            "fetch_duration_ms IS NULL OR fetch_duration_ms >= 0",
            name="fetch_duration_non_negative",
        ),
        CheckConstraint(
            "char_length(coalesce(title, '')) <= 1000"
            " AND char_length(coalesce(meta_description, '')) <= 2000"
            " AND char_length(headings) <= 20000"
            " AND char_length(extracted_text) <= 1000000",
            name="text_lengths",
        ),
        Index("ix_pages_job_id", "job_id"),
    )


class PageLink(Base):
    """A link from a stored page to a target URL.

    Uniqueness policy: one row per (source page, normalized target URL). Repeated links to the same
    target within one page collapse to the first occurrence (lowest ``position``). Targets do not
    need to exist in ``crawl_urls``: links beyond the depth limit, outside the scope, or never
    admitted are still recorded for link analysis.
    """

    __tablename__ = "page_links"

    source_page_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    target_normalized_url: Mapped[str] = mapped_column(Text(collation="C"), primary_key=True)
    job_id: Mapped[uuid.UUID]
    anchor_text: Mapped[str | None] = mapped_column(Text)
    # Order of the first occurrence within the source document (deterministic BFS admission).
    position: Mapped[int] = mapped_column(Integer)
    # Whether the target is inside the job's scope and an approved crawl target (when stored).
    is_internal: Mapped[bool] = mapped_column(Boolean)
    # rel="nofollow" on this link: recorded, never admitted (migration 0003).
    nofollow: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_page_id", "job_id"], ["pages.id", "pages.job_id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            normalized_url_check("target_normalized_url"), name="target_normalized_url_format"
        ),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint(
            "anchor_text IS NULL OR char_length(anchor_text) <= 500", name="anchor_text_length"
        ),
        # Link analysis and BFS expansion: links pointing at a URL within a job.
        Index("ix_page_links_job_id_target", "job_id", "target_normalized_url"),
    )


class SearchDocument(Base):
    """Full-text index state of one page; also the outbox for index tasks (ADR-0008, ADR-0018).

    A page needs (re)indexing while ``status`` is ``pending``. The index dispatcher publishes
    pending rows to the index stream and marks them ``queued``; an indexer counts an attempt and
    takes the row's lease, then writes ``search_vector`` and ``indexed_content_version`` and
    marks it ``indexed`` (or ``failed`` after bounded attempts). When a page's content changes,
    the same transaction that increments ``pages.content_version`` sets ``status`` back to
    ``pending``. Triggers (migration 0004) keep both versions from moving back.
    """

    __tablename__ = "search_documents"

    page_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    job_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    indexed_content_version: Mapped[int | None] = mapped_column(Integer)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR)
    # Indexing attempts since the row was last set to pending; counted before each attempt's
    # work starts, so crashed attempts count too (Phase 7).
    attempt_count: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    dispatched_at: Mapped[datetime | None]
    stream_entry_id: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime | None]
    # The indexer running the current attempt, and until when (database clock; migration 0004).
    # Advisory, not a fencing token: it keeps duplicate messages from starting attempts while
    # one runs. Set only while the row is queued.
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), server_onupdate=FetchedValue()
    )

    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 - SQLAlchemy declarative API
    __table_args__ = (
        ForeignKeyConstraint(
            ["page_id", "job_id"], ["pages.id", "pages.job_id"], ondelete="CASCADE"
        ),
        CheckConstraint(sql_in("status", IndexStatus), name="status_valid"),
        CheckConstraint(
            "status <> 'indexed' OR (search_vector IS NOT NULL"
            " AND indexed_content_version IS NOT NULL AND indexed_at IS NOT NULL)",
            name="indexed_state_complete",
        ),
        CheckConstraint(
            "indexed_content_version IS NULL OR indexed_content_version >= 1",
            name="indexed_content_version_positive",
        ),
        CheckConstraint(
            "status <> 'queued' OR dispatched_at IS NOT NULL", name="dispatched_when_queued"
        ),
        CheckConstraint(STREAM_ENTRY_ID_CHECK, name="stream_entry_id_format"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 2000", name="last_error_length"
        ),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)", name="lease_complete"
        ),
        CheckConstraint("status = 'queued' OR lease_owner IS NULL", name="lease_only_when_queued"),
        Index("ix_search_documents_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_search_documents_job_id", "job_id"),
        # Index relay: pending documents, oldest change first.
        Index(
            "ix_search_documents_pending",
            "updated_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_search_documents_queued_since",
            "dispatched_at",
            postgresql_where=text("status = 'queued'"),
        ),
        # Job indexing completion (Phase 7): a job's documents still to index.
        Index(
            "ix_search_documents_open",
            "job_id",
            postgresql_where=text("status IN ('pending', 'queued')"),
        ),
    )


class CrawlEvent(Base):
    """Append-only audit log of important lifecycle events (a trigger rejects UPDATE).

    ``event_type`` uses dotted lowercase names. The initial vocabulary: ``job.created``,
    ``job.started``, ``job.level_advanced``, ``job.cancel_requested``, ``job.completed``,
    ``job.failed``, ``job.cancelled``, ``url.retry_scheduled``, ``url.lease_expired``,
    ``url.recovered``, ``url.failed``, ``url.dead_lettered``, ``worker.error``.
    """

    __tablename__ = "crawl_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crawl_jobs.id", ondelete="CASCADE"))
    crawl_url_id: Mapped[uuid.UUID | None]
    event_type: Mapped[str] = mapped_column(Text)
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(Text)
    lease_epoch: Mapped[int | None] = mapped_column(BigInteger)
    # Structured context. Never secrets or page bodies.
    detail: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["crawl_url_id", "job_id"],
            ["crawl_urls.id", "crawl_urls.job_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            r"event_type ~ '^[a-z][a-z_]*(\.[a-z][a-z_]*)+$'", name="event_type_format"
        ),
        CheckConstraint("jsonb_typeof(detail) = 'object'", name="detail_is_object"),
        CheckConstraint("lease_epoch IS NULL OR lease_epoch >= 0", name="lease_epoch_non_negative"),
        # A job's events in insertion order.
        Index("ix_crawl_events_job_id_id", "job_id", "id"),
        Index(
            "ix_crawl_events_crawl_url_id",
            "crawl_url_id",
            postgresql_where=text("crawl_url_id IS NOT NULL"),
        ),
    )
