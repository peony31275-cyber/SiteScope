"""Initial schema: crawl jobs, URL frontier, pages, links, search documents, crawl events.

Revision ID: 0001
Revises:
Create Date: 2026-11-06

Creates the six tables of the approved data model (docs/architecture.md, section 5). The tables,
columns, constraints and indexes mirror app/db/models.py; an integration test compares the
migrated schema with the models. Database-side rules the models cannot express are added here:

* ``sitescope_set_updated_at``: ``updated_at`` is maintained by the database on every UPDATE;
* ``sitescope_lease_epoch_monotonic``: a crawl URL's fencing token (``lease_epoch``) can never
  decrease;
* ``sitescope_reject_update``: ``crawl_events`` is append-only.

The migration is self-contained (it does not import application code), so later model changes
cannot alter what it does.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPDATED_AT_TABLES = ("crawl_jobs", "crawl_urls", "pages", "search_documents")


def upgrade() -> None:
    _create_functions()

    op.create_table(
        "crawl_jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("seed_url", sa.Text(collation="C"), nullable=False),
        sa.Column("allowed_hosts", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("max_depth", sa.SmallInteger(), nullable=False),
        sa.Column("max_pages", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("current_level", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("admitted_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("request_fingerprint", sa.LargeBinary(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexing_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(finished_at IS NOT NULL) = (status IN ('completed', 'failed', 'cancelled'))",
            name=op.f("ck_crawl_jobs_finished_iff_terminal"),
        ),
        sa.CheckConstraint(
            "NOT (status IN ('running', 'cancelling', 'completed', 'failed'))"
            " OR started_at IS NOT NULL",
            name=op.f("ck_crawl_jobs_started_at_when_started"),
        ),
        sa.CheckConstraint(
            "seed_url ~ '^https?://[\\x21-\\x7e]+$' AND octet_length(seed_url) <= 2048",
            name=op.f("ck_crawl_jobs_seed_url_normalized"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR failure_reason IS NOT NULL",
            name=op.f("ck_crawl_jobs_failure_reason_when_failed"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'cancelling', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_crawl_jobs_status_valid"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('cancelling', 'cancelled') OR cancel_requested_at IS NOT NULL",
            name=op.f("ck_crawl_jobs_cancel_requested_when_cancelled"),
        ),
        sa.CheckConstraint(
            "(idempotency_key IS NULL) = (request_fingerprint IS NULL)",
            name=op.f("ck_crawl_jobs_idempotency_key_with_fingerprint"),
        ),
        sa.CheckConstraint(
            "(started_at IS NULL OR started_at >= created_at)"
            " AND (finished_at IS NULL OR finished_at >= created_at)",
            name=op.f("ck_crawl_jobs_timestamps_ordered"),
        ),
        sa.CheckConstraint(
            "admitted_count BETWEEN 0 AND max_pages",
            name=op.f("ck_crawl_jobs_admitted_within_budget"),
        ),
        sa.CheckConstraint(
            "cardinality(allowed_hosts) >= 1", name=op.f("ck_crawl_jobs_allowed_hosts_not_empty")
        ),
        sa.CheckConstraint(
            "current_level BETWEEN 0 AND max_depth",
            name=op.f("ck_crawl_jobs_current_level_in_range"),
        ),
        sa.CheckConstraint(
            "indexing_completed_at IS NULL OR finished_at IS NOT NULL",
            name=op.f("ck_crawl_jobs_indexing_completed_after_finish"),
        ),
        sa.CheckConstraint("max_depth >= 0", name=op.f("ck_crawl_jobs_max_depth_non_negative")),
        sa.CheckConstraint("max_pages >= 1", name=op.f("ck_crawl_jobs_max_pages_positive")),
        sa.CheckConstraint(
            "request_fingerprint IS NULL OR octet_length(request_fingerprint) = 32",
            name=op.f("ck_crawl_jobs_request_fingerprint_sha256"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawl_jobs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_crawl_jobs_idempotency_key")),
    )
    op.create_index(
        "ix_crawl_jobs_active",
        "crawl_jobs",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('queued', 'running', 'cancelling')"),
    )
    op.create_index("ix_crawl_jobs_created_at_id", "crawl_jobs", ["created_at", "id"], unique=False)

    op.create_table(
        "crawl_urls",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_url", sa.Text(collation="C"), nullable=False),
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        sa.Column("discovered_from_url_id", sa.Uuid(), nullable=True),
        sa.Column("redirect_hops", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempt_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_epoch", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_epoch", sa.BigInteger(), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stream_entry_id", sa.Text(), nullable=True),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("redirect_target", sa.Text(collation="C"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "((state = 'in_progress') = (lease_owner IS NOT NULL))"
            " AND ((state = 'in_progress') = (lease_expires_at IS NOT NULL))",
            name=op.f("ck_crawl_urls_lease_iff_in_progress"),
        ),
        sa.CheckConstraint(
            "(finished_at IS NOT NULL) = (state IN ('fetched', 'failed', 'skipped', 'cancelled'))",
            name=op.f("ck_crawl_urls_finished_iff_terminal"),
        ),
        sa.CheckConstraint(
            "NOT (state IN ('fetched', 'failed', 'skipped')) OR result_epoch IS NOT NULL",
            name=op.f("ck_crawl_urls_worker_outcome_has_result_epoch"),
        ),
        sa.CheckConstraint(
            "normalized_url ~ '^https?://[\\x21-\\x7e]+$' AND octet_length(normalized_url) <= 2048",
            name=op.f("ck_crawl_urls_normalized_url_format"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome ~ '^[a-z][a-z0-9_]*$'",
            name=op.f("ck_crawl_urls_outcome_format"),
        ),
        sa.CheckConstraint(
            "redirect_target IS NULL OR (redirect_target ~ '^https?://[\\x21-\\x7e]+$'"
            " AND octet_length(redirect_target) <= 2048)",
            name=op.f("ck_crawl_urls_redirect_target_format"),
        ),
        sa.CheckConstraint(
            "state <> 'in_progress' OR result_epoch IS NULL",
            name=op.f("ck_crawl_urls_result_epoch_cleared_by_claim"),
        ),
        sa.CheckConstraint(
            "state <> 'queued' OR dispatched_at IS NOT NULL",
            name=op.f("ck_crawl_urls_dispatched_when_queued"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'queued', 'in_progress', 'fetched', 'failed', 'skipped',"
            " 'cancelled')",
            name=op.f("ck_crawl_urls_state_valid"),
        ),
        sa.CheckConstraint(
            "stream_entry_id IS NULL OR stream_entry_id ~ '^[0-9]+-[0-9]+$'",
            name=op.f("ck_crawl_urls_stream_entry_id_format"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_crawl_urls_attempt_count_non_negative")
        ),
        sa.CheckConstraint("depth >= 0", name=op.f("ck_crawl_urls_depth_non_negative")),
        sa.CheckConstraint(
            "discovered_from_url_id IS NULL OR discovered_from_url_id <> id",
            name=op.f("ck_crawl_urls_not_discovered_from_self"),
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name=op.f("ck_crawl_urls_http_status_range"),
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 2000",
            name=op.f("ck_crawl_urls_last_error_length"),
        ),
        sa.CheckConstraint("lease_epoch >= 0", name=op.f("ck_crawl_urls_lease_epoch_non_negative")),
        sa.CheckConstraint(
            "redirect_hops >= 0", name=op.f("ck_crawl_urls_redirect_hops_non_negative")
        ),
        sa.CheckConstraint(
            "result_epoch IS NULL OR result_epoch = lease_epoch",
            name=op.f("ck_crawl_urls_result_epoch_is_current"),
        ),
        sa.ForeignKeyConstraint(
            ["discovered_from_url_id", "job_id"],
            ["crawl_urls.id", "crawl_urls.job_id"],
            name=op.f("fk_crawl_urls_discovered_from_url_id_job_id"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["crawl_jobs.id"], name=op.f("fk_crawl_urls_job_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawl_urls")),
        sa.UniqueConstraint(
            "id", "job_id", "normalized_url", name=op.f("uq_crawl_urls_id_job_id_normalized_url")
        ),
        sa.UniqueConstraint("id", "job_id", name=op.f("uq_crawl_urls_id_job_id")),
        sa.UniqueConstraint(
            "job_id", "normalized_url", name=op.f("uq_crawl_urls_job_id_normalized_url")
        ),
    )
    op.create_index(
        "ix_crawl_urls_discovered_from",
        "crawl_urls",
        ["discovered_from_url_id", "job_id"],
        unique=False,
        postgresql_where=sa.text("discovered_from_url_id IS NOT NULL"),
    )
    op.create_index(
        "ix_crawl_urls_dispatch",
        "crawl_urls",
        ["job_id", "depth", "next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_crawl_urls_lease_expiry",
        "crawl_urls",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("state = 'in_progress'"),
    )
    op.create_index(
        "ix_crawl_urls_queued_since",
        "crawl_urls",
        ["dispatched_at"],
        unique=False,
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.create_index(
        "ix_crawl_urls_unfinished",
        "crawl_urls",
        ["job_id"],
        unique=False,
        postgresql_where=sa.text("state IN ('pending', 'queued', 'in_progress')"),
    )

    op.create_table(
        "crawl_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("crawl_url_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type ~ '^[a-z][a-z_]*(\\.[a-z][a-z_]*)+$'",
            name=op.f("ck_crawl_events_event_type_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(detail) = 'object'", name=op.f("ck_crawl_events_detail_is_object")
        ),
        sa.CheckConstraint(
            "lease_epoch IS NULL OR lease_epoch >= 0",
            name=op.f("ck_crawl_events_lease_epoch_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["crawl_url_id", "job_id"],
            ["crawl_urls.id", "crawl_urls.job_id"],
            name=op.f("fk_crawl_events_crawl_url_id_job_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["crawl_jobs.id"], name=op.f("fk_crawl_events_job_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawl_events")),
    )
    op.create_index(
        "ix_crawl_events_crawl_url_id",
        "crawl_events",
        ["crawl_url_id"],
        unique=False,
        postgresql_where=sa.text("crawl_url_id IS NOT NULL"),
    )
    op.create_index("ix_crawl_events_job_id_id", "crawl_events", ["job_id", "id"], unique=False)

    op.create_table(
        "pages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("crawl_url_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_url", sa.Text(collation="C"), nullable=False),
        sa.Column("http_status", sa.SmallInteger(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("headings", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("extracted_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("content_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("fetch_duration_ms", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(coalesce(title, '')) <= 1000"
            " AND char_length(coalesce(meta_description, '')) <= 2000"
            " AND char_length(headings) <= 20000 AND char_length(extracted_text) <= 1000000",
            name=op.f("ck_pages_text_lengths"),
        ),
        sa.CheckConstraint("byte_size >= 0", name=op.f("ck_pages_byte_size_non_negative")),
        sa.CheckConstraint("content_version >= 1", name=op.f("ck_pages_content_version_positive")),
        sa.CheckConstraint(
            "fetch_duration_ms IS NULL OR fetch_duration_ms >= 0",
            name=op.f("ck_pages_fetch_duration_non_negative"),
        ),
        sa.CheckConstraint(
            "http_status BETWEEN 100 AND 599", name=op.f("ck_pages_http_status_range")
        ),
        sa.CheckConstraint(
            "octet_length(content_hash) = 32", name=op.f("ck_pages_content_hash_sha256")
        ),
        sa.ForeignKeyConstraint(
            ["crawl_url_id", "job_id", "normalized_url"],
            ["crawl_urls.id", "crawl_urls.job_id", "crawl_urls.normalized_url"],
            name=op.f("fk_pages_crawl_url_id_job_id_normalized_url"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pages")),
        sa.UniqueConstraint("crawl_url_id", name=op.f("uq_pages_crawl_url_id")),
        sa.UniqueConstraint("id", "job_id", name=op.f("uq_pages_id_job_id")),
    )
    op.create_index("ix_pages_job_id", "pages", ["job_id"], unique=False)

    op.create_table(
        "page_links",
        sa.Column("source_page_id", sa.Uuid(), nullable=False),
        sa.Column("target_normalized_url", sa.Text(collation="C"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("anchor_text", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_normalized_url ~ '^https?://[\\x21-\\x7e]+$'"
            " AND octet_length(target_normalized_url) <= 2048",
            name=op.f("ck_page_links_target_normalized_url_format"),
        ),
        sa.CheckConstraint(
            "anchor_text IS NULL OR char_length(anchor_text) <= 500",
            name=op.f("ck_page_links_anchor_text_length"),
        ),
        sa.CheckConstraint("position >= 0", name=op.f("ck_page_links_position_non_negative")),
        sa.ForeignKeyConstraint(
            ["source_page_id", "job_id"],
            ["pages.id", "pages.job_id"],
            name=op.f("fk_page_links_source_page_id_job_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "source_page_id", "target_normalized_url", name=op.f("pk_page_links")
        ),
    )
    op.create_index(
        "ix_page_links_job_id_target",
        "page_links",
        ["job_id", "target_normalized_url"],
        unique=False,
    )

    op.create_table(
        "search_documents",
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("indexed_content_version", sa.Integer(), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("attempt_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stream_entry_id", sa.Text(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status <> 'indexed' OR (search_vector IS NOT NULL"
            " AND indexed_content_version IS NOT NULL AND indexed_at IS NOT NULL)",
            name=op.f("ck_search_documents_indexed_state_complete"),
        ),
        sa.CheckConstraint(
            "status <> 'queued' OR dispatched_at IS NOT NULL",
            name=op.f("ck_search_documents_dispatched_when_queued"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'queued', 'indexed', 'failed')",
            name=op.f("ck_search_documents_status_valid"),
        ),
        sa.CheckConstraint(
            "stream_entry_id IS NULL OR stream_entry_id ~ '^[0-9]+-[0-9]+$'",
            name=op.f("ck_search_documents_stream_entry_id_format"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_search_documents_attempt_count_non_negative")
        ),
        sa.CheckConstraint(
            "indexed_content_version IS NULL OR indexed_content_version >= 1",
            name=op.f("ck_search_documents_indexed_content_version_positive"),
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 2000",
            name=op.f("ck_search_documents_last_error_length"),
        ),
        sa.ForeignKeyConstraint(
            ["page_id", "job_id"],
            ["pages.id", "pages.job_id"],
            name=op.f("fk_search_documents_page_id_job_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("page_id", name=op.f("pk_search_documents")),
    )
    op.create_index("ix_search_documents_job_id", "search_documents", ["job_id"], unique=False)
    op.create_index(
        "ix_search_documents_pending",
        "search_documents",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_search_documents_queued_since",
        "search_documents",
        ["dispatched_at"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_search_documents_search_vector",
        "search_documents",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )

    _create_triggers()


def downgrade() -> None:
    # Dropping a table also drops its indexes, constraints and triggers.
    for table in (
        "search_documents",
        "page_links",
        "pages",
        "crawl_events",
        "crawl_urls",
        "crawl_jobs",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION sitescope_reject_update()")
    op.execute("DROP FUNCTION sitescope_lease_epoch_monotonic()")
    op.execute("DROP FUNCTION sitescope_set_updated_at()")


def _create_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION sitescope_set_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sitescope_lease_epoch_monotonic() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.lease_epoch < OLD.lease_epoch THEN
                RAISE EXCEPTION 'lease_epoch of crawl URL % cannot decrease (% -> %)',
                    OLD.id, OLD.lease_epoch, NEW.lease_epoch
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sitescope_reject_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only; UPDATE is not allowed', TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )


def _create_triggers() -> None:
    for table in _UPDATED_AT_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION sitescope_set_updated_at()"
        )
    op.execute(
        "CREATE TRIGGER crawl_urls_lease_epoch_monotonic BEFORE UPDATE OF lease_epoch"
        " ON crawl_urls FOR EACH ROW EXECUTE FUNCTION sitescope_lease_epoch_monotonic()"
    )
    op.execute(
        "CREATE TRIGGER crawl_events_append_only BEFORE UPDATE ON crawl_events"
        " FOR EACH ROW EXECUTE FUNCTION sitescope_reject_update()"
    )
