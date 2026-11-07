"""Static checks of the ORM metadata (no database needed). Behaviour is tested in integration."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.db.base import Base
from app.db.models import JobStatus, normalized_url_check, sql_in

EXPECTED_TABLES = {
    "crawl_jobs",
    "crawl_urls",
    "pages",
    "page_links",
    "search_documents",
    "crawl_events",
}


def constraint_names(table_name: str, kind: type) -> set[str]:
    table = Base.metadata.tables[table_name]
    return {str(c.name) for c in table.constraints if isinstance(c, kind)}


def test_metadata_defines_exactly_the_approved_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_crawl_urls_enforces_dedup_and_fencing_constraints() -> None:
    assert "uq_crawl_urls_job_id_normalized_url" in constraint_names("crawl_urls", UniqueConstraint)
    assert {
        "ck_crawl_urls_result_epoch_is_current",
        "ck_crawl_urls_result_epoch_cleared_by_claim",
        "ck_crawl_urls_lease_iff_in_progress",
    } <= constraint_names("crawl_urls", CheckConstraint)


def test_job_scoped_children_use_composite_foreign_keys() -> None:
    """Every reference between job-scoped tables includes job_id (job isolation)."""
    for table_name in ("crawl_urls", "pages", "page_links", "search_documents", "crawl_events"):
        table = Base.metadata.tables[table_name]
        for fk in table.constraints:
            if not isinstance(fk, ForeignKeyConstraint):
                continue
            if fk.referred_table.name == "crawl_jobs":
                continue  # the job itself
            assert "job_id" in fk.column_keys, f"{fk.name} must include job_id"


def test_sql_helpers_render_expected_expressions() -> None:
    rendered = sql_in("status", [JobStatus.QUEUED, JobStatus.RUNNING])

    assert rendered == "status IN ('queued', 'running')"
    assert normalized_url_check("u") == (
        "u ~ '^https?://[\\x21-\\x7e]+$' AND octet_length(u) <= 2048"
    )
