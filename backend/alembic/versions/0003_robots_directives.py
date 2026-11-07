"""Page-level robots directives and per-link nofollow.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06

Phase 4 acts on robots directives, and must keep two decisions apart (ADR-0014):

* ``pages.robots_noindex``: the page must not be indexed. The crawl stores the page but creates
  no ``search_documents`` row for it, so it never enters the indexing outbox;
* ``pages.robots_nofollow``: none of the page's links may be admitted to the next BFS level;
* ``page_links.nofollow``: this one link carries ``rel="nofollow"`` and is not admitted.

Links are still recorded in every case, for link analysis. Each column is ``NOT NULL`` with a
constant default, which PostgreSQL adds without rewriting the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pages",
        sa.Column("robots_noindex", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "pages",
        sa.Column("robots_nofollow", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "page_links",
        sa.Column("nofollow", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("page_links", "nofollow")
    op.drop_column("pages", "robots_nofollow")
    op.drop_column("pages", "robots_noindex")
