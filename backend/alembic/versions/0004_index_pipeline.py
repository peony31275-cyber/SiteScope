"""The asynchronous index pipeline: versions only move forward, index leases.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07

Phase 7 indexes pages asynchronously (ADR-0018). This migration adds what the database itself
must enforce or record for it:

* ``sitescope_index_version_forward`` (on ``search_documents``): once set,
  ``indexed_content_version`` never decreases and never returns to NULL. An index task can
  finish after a newer one; the indexer's guarded update writes only the current version, and
  this rejects any other path (a bug, a manual statement) that would put an older index over a
  newer one, as the fencing triggers do for leases (ADR-0010, ADR-0011).
* ``sitescope_content_version_forward`` (on ``pages``): ``content_version`` never decreases, so
  "the index reflects the current content" (``indexed_content_version = content_version``)
  can never become true again for content that was replaced.
* ``search_documents.lease_owner`` and ``lease_expires_at``: the indexer running the current
  attempt of a queued document, until when (database clock). A lease is advisory: it keeps
  duplicate messages from starting attempts while one runs, so they do not use up the
  document's attempts, and it shows which indexer works on what. It is not a fencing token:
  the index transaction reads the content it indexes under its own row locks, so a late holder
  writes the same result or nothing (ADR-0018). Both columns are set together, and only while
  the document is ``queued``.
* ``ix_crawl_jobs_indexing_open``: the finished jobs whose indexing is not complete yet, and
  ``ix_search_documents_open``: each job's documents still to index (``pending`` or
  ``queued``). The index dispatcher looks for jobs whose indexing can complete on every pass.

The triggers are BEFORE UPDATE OF triggers: rows whose versions do not change pay nothing. The
new columns are nullable without a default, so adding them rewrites no table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION sitescope_index_version_forward() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.indexed_content_version IS NOT NULL
               AND (NEW.indexed_content_version IS NULL
                    OR NEW.indexed_content_version < OLD.indexed_content_version) THEN
                RAISE EXCEPTION
                    'indexed_content_version of page % may not move back (% -> %)',
                    OLD.page_id, OLD.indexed_content_version, NEW.indexed_content_version;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER search_documents_index_version_forward"
        " BEFORE UPDATE OF indexed_content_version ON search_documents"
        " FOR EACH ROW EXECUTE FUNCTION sitescope_index_version_forward()"
    )
    op.execute(
        """
        CREATE FUNCTION sitescope_content_version_forward() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.content_version < OLD.content_version THEN
                RAISE EXCEPTION
                    'content_version of page % may not move back (% -> %)',
                    OLD.id, OLD.content_version, NEW.content_version;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER pages_content_version_forward"
        " BEFORE UPDATE OF content_version ON pages"
        " FOR EACH ROW EXECUTE FUNCTION sitescope_content_version_forward()"
    )

    op.add_column("search_documents", sa.Column("lease_owner", sa.Text(), nullable=True))
    op.add_column(
        "search_documents",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_search_documents_lease_complete"),
        "search_documents",
        "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_search_documents_lease_only_when_queued"),
        "search_documents",
        "status = 'queued' OR lease_owner IS NULL",
    )
    op.create_index(
        "ix_crawl_jobs_indexing_open",
        "crawl_jobs",
        ["finished_at"],
        unique=False,
        postgresql_where=sa.text("finished_at IS NOT NULL AND indexing_completed_at IS NULL"),
    )
    op.create_index(
        "ix_search_documents_open",
        "search_documents",
        ["job_id"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'queued')"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_search_documents_open",
        table_name="search_documents",
        postgresql_where=sa.text("status IN ('pending', 'queued')"),
    )
    op.drop_index(
        "ix_crawl_jobs_indexing_open",
        table_name="crawl_jobs",
        postgresql_where=sa.text("finished_at IS NOT NULL AND indexing_completed_at IS NULL"),
    )
    op.drop_constraint(
        op.f("ck_search_documents_lease_only_when_queued"), "search_documents", type_="check"
    )
    op.drop_constraint(
        op.f("ck_search_documents_lease_complete"), "search_documents", type_="check"
    )
    op.drop_column("search_documents", "lease_expires_at")
    op.drop_column("search_documents", "lease_owner")
    op.execute("DROP TRIGGER pages_content_version_forward ON pages")
    op.execute("DROP FUNCTION sitescope_content_version_forward()")
    op.execute("DROP TRIGGER search_documents_index_version_forward ON search_documents")
    op.execute("DROP FUNCTION sitescope_index_version_forward()")
