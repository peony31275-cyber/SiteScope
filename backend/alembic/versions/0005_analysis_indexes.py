"""Indexes for the website-analysis queries: two indexes replaced by wider ones.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-07

Phase 8 reads a job's pages and links for its analysis endpoints (ADR-0019). Two existing
indexes are replaced by wider ones, so the number of indexes does not change. Each was measured
with ``benchmarks/analysis.py`` on a 10,000-page job with 500,000 links
(``benchmarks/results/phase8-analysis.md``):

* ``ix_pages_job_id`` (job_id) becomes ``ix_pages_job_id_normalized_url``
  (job_id, normalized_url). The links listing is ordered by the linking page's URL: with this
  index, PostgreSQL reads a job's pages in that order and stops after one page of links, instead
  of joining and sorting every link of the job. Every query that used the old index (its first
  column) can use this one.
* ``ix_page_links_job_id_target`` (job_id, target_normalized_url) gains
  ``INCLUDE (is_internal)``. The statistics and the link graph count and group a job's internal
  links by target; with the flag in the index, these become index-only scans wherever the
  visibility map allows, instead of one heap visit per link.

Both are plain ``CREATE INDEX`` statements in the migration's transaction, as in the earlier
migrations: writes to the two tables wait while the indexes are built.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_pages_job_id_normalized_url", "pages", ["job_id", "normalized_url"])
    op.drop_index("ix_pages_job_id", table_name="pages")
    op.drop_index("ix_page_links_job_id_target", table_name="page_links")
    op.create_index(
        "ix_page_links_job_id_target",
        "page_links",
        ["job_id", "target_normalized_url"],
        postgresql_include=["is_internal"],
    )


def downgrade() -> None:
    op.drop_index("ix_page_links_job_id_target", table_name="page_links")
    op.create_index(
        "ix_page_links_job_id_target", "page_links", ["job_id", "target_normalized_url"]
    )
    op.create_index("ix_pages_job_id", "pages", ["job_id"])
    op.drop_index("ix_pages_job_id_normalized_url", table_name="pages")
