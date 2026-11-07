"""Database-enforced integrity: foreign keys, uniqueness, CHECK rules, isolation, cascades.

Each test names the constraint it expects, so a test cannot pass because some other constraint
happened to reject the row.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.db.models import CrawlEvent, CrawlJob, CrawlUrl, Page, PageLink, SearchDocument
from app.db.session import Database
from tests.integration.helpers import (
    APP_TABLES,
    add_job_with_seed,
    new_job,
    new_page,
    new_url,
)

pytestmark = pytest.mark.anyio


def link(page: Page, target: str, *, job_id: uuid.UUID | None = None) -> PageLink:
    return PageLink(
        source_page_id=page.id,
        target_normalized_url=target,
        job_id=job_id or page.job_id,
        position=0,
        is_internal=True,
    )


async def table_counts(database: Database) -> dict[str, int]:
    async with database.session() as session:
        return {
            # Table names come from a constant tuple, never from input.
            table: (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            for table in APP_TABLES
        }


# --- Referential integrity and deduplication ----------------------------------------------------


async def test_crawl_url_requires_an_existing_job(database: Database) -> None:
    with pytest.raises(IntegrityError, match="fk_crawl_urls_job_id"):
        async with database.transaction() as session:
            session.add(new_url(uuid.uuid4()))


async def test_duplicate_url_in_the_same_job_is_rejected(database: Database) -> None:
    async with database.transaction() as session:
        job, _ = await add_job_with_seed(session)

    with pytest.raises(IntegrityError, match="uq_crawl_urls_job_id_normalized_url"):
        async with database.transaction() as session:
            session.add(new_url(job.id))


async def test_on_conflict_do_nothing_makes_duplicate_admission_a_no_op(database: Database) -> None:
    async with database.transaction() as session:
        job, _ = await add_job_with_seed(session)
    statement = (
        insert(CrawlUrl)
        .values(job_id=job.id, normalized_url="https://example.test/a", depth=1)
        .on_conflict_do_nothing(index_elements=["job_id", "normalized_url"])
        .returning(CrawlUrl.id)
    )

    async with database.transaction() as session:
        first = (await session.execute(statement)).scalar_one_or_none()
        second = (await session.execute(statement)).scalar_one_or_none()

    assert first is not None
    assert second is None  # the duplicate was ignored, not inserted
    assert (await table_counts(database))["crawl_urls"] == 2  # seed + one admitted URL


async def test_the_same_url_may_appear_in_different_jobs(database: Database) -> None:
    async with database.transaction() as session:
        await add_job_with_seed(session)
        await add_job_with_seed(session)

    assert (await table_counts(database))["crawl_urls"] == 2


async def test_at_most_one_page_per_crawl_url(database: Database) -> None:
    async with database.transaction() as session:
        job, seed = await add_job_with_seed(session)
        session.add(new_page(job.id, seed))

    with pytest.raises(IntegrityError, match="uq_pages_crawl_url_id"):
        async with database.transaction() as session:
            session.add(new_page(job.id, seed, text_value="a second copy"))


async def test_page_url_must_match_its_crawl_url(database: Database) -> None:
    async with database.transaction() as session:
        job, seed = await add_job_with_seed(session)

    page = new_page(job.id, seed)
    page.normalized_url = "https://example.test/somewhere-else"
    with pytest.raises(IntegrityError, match="fk_pages_crawl_url_id_job_id_normalized_url"):
        async with database.transaction() as session:
            session.add(page)


async def test_link_targets_do_not_need_to_exist(database: Database) -> None:
    async with database.transaction() as session:
        job, seed = await add_job_with_seed(session)
        page = new_page(job.id, seed)
        session.add(page)
        await session.flush()
        # Neither target is a crawl URL of this job, e.g. out of scope or beyond max_depth.
        session.add_all(
            [link(page, "https://example.test/deep"), link(page, "https://other.test/")]
        )

    assert (await table_counts(database))["page_links"] == 2


async def test_duplicate_link_from_the_same_page_is_rejected(database: Database) -> None:
    async with database.transaction() as session:
        job, seed = await add_job_with_seed(session)
        page = new_page(job.id, seed)
        session.add(page)
        await session.flush()
        session.add(link(page, "https://example.test/a"))

    with pytest.raises(IntegrityError, match="pk_page_links"):
        async with database.transaction() as session:
            session.add(link(page, "https://example.test/a"))


# --- Job isolation ------------------------------------------------------------------------------


async def test_page_cannot_reference_a_crawl_url_of_another_job(database: Database) -> None:
    async with database.transaction() as session:
        _, seed_a = await add_job_with_seed(session)
        job_b, _ = await add_job_with_seed(session)

    with pytest.raises(IntegrityError, match="fk_pages_crawl_url_id_job_id_normalized_url"):
        async with database.transaction() as session:
            session.add(new_page(job_b.id, seed_a))


async def test_link_cannot_cross_jobs(database: Database) -> None:
    async with database.transaction() as session:
        job_a, seed_a = await add_job_with_seed(session)
        job_b, _ = await add_job_with_seed(session)
        page_a = new_page(job_a.id, seed_a)
        session.add(page_a)

    with pytest.raises(IntegrityError, match="fk_page_links_source_page_id_job_id"):
        async with database.transaction() as session:
            session.add(link(page_a, "https://example.test/x", job_id=job_b.id))


async def test_search_document_cannot_cross_jobs(database: Database) -> None:
    async with database.transaction() as session:
        job_a, seed_a = await add_job_with_seed(session)
        job_b, _ = await add_job_with_seed(session)
        page_a = new_page(job_a.id, seed_a)
        session.add(page_a)

    with pytest.raises(IntegrityError, match="fk_search_documents_page_id_job_id"):
        async with database.transaction() as session:
            session.add(SearchDocument(page_id=page_a.id, job_id=job_b.id))


async def test_event_cannot_reference_a_url_of_another_job(database: Database) -> None:
    async with database.transaction() as session:
        _, seed_a = await add_job_with_seed(session)
        job_b, _ = await add_job_with_seed(session)

    with pytest.raises(IntegrityError, match="fk_crawl_events_crawl_url_id_job_id"):
        async with database.transaction() as session:
            session.add(
                CrawlEvent(job_id=job_b.id, crawl_url_id=seed_a.id, event_type="url.failed")
            )


async def test_queries_scoped_by_job_see_only_that_job(database: Database) -> None:
    async with database.transaction() as session:
        job_a, _ = await add_job_with_seed(session)
        job_b, _ = await add_job_with_seed(session)
        session.add(new_url(job_b.id, "https://example.test/only-in-b", depth=1))

    async with database.session() as session:
        urls_a = (
            (
                await session.execute(
                    select(CrawlUrl.normalized_url).where(CrawlUrl.job_id == job_a.id)
                )
            )
            .scalars()
            .all()
        )

    assert urls_a == ["https://example.test/"]


# --- CHECK rules --------------------------------------------------------------------------------


async def test_admitted_count_cannot_exceed_max_pages(database: Database) -> None:
    async with database.transaction() as session:
        job = new_job(max_pages=2)
        session.add(job)

    with pytest.raises(IntegrityError, match="ck_crawl_jobs_admitted_within_budget"):
        async with database.transaction() as session:
            await session.execute(
                update(CrawlJob).where(CrawlJob.id == job.id).values(admitted_count=3)
            )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/file",
        "javascript:alert(1)",
        "https://example.test/with space",
        "https://example.test/café",
        "https://example.test/" + "a" * 2100,
    ],
    ids=["ftp-scheme", "javascript", "whitespace", "non-ascii", "too-long"],
)
async def test_rejects_urls_that_are_not_normalized(database: Database, url: str) -> None:
    async with database.transaction() as session:
        job, _ = await add_job_with_seed(session)

    with pytest.raises(IntegrityError, match="ck_crawl_urls_normalized_url_format"):
        async with database.transaction() as session:
            session.add(new_url(job.id, url, depth=1))


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"status": "completed"}, "ck_crawl_jobs_finished_iff_terminal"),
        (
            {"status": "failed", "finished_at": func.now()},
            "ck_crawl_jobs_failure_reason_when_failed",
        ),
        ({"status": "running"}, "ck_crawl_jobs_started_at_when_started"),
        ({"status": "cancelling", "started_at": func.now()}, "ck_crawl_jobs_cancel_requested"),
        ({"status": "paused"}, "ck_crawl_jobs_status_valid"),
        ({"current_level": 4}, "ck_crawl_jobs_current_level_in_range"),
    ],
    ids=[
        "terminal-needs-finish",
        "failed-needs-reason",
        "running-needs-start",
        "cancel-needs-request",
        "unknown-status",
        "level-beyond-max-depth",
    ],
)
async def test_job_lifecycle_rules(
    database: Database, changes: dict[str, Any], constraint: str
) -> None:
    async with database.transaction() as session:
        job = new_job(max_depth=3)
        session.add(job)

    with pytest.raises(IntegrityError, match=constraint):
        async with database.transaction() as session:
            await session.execute(update(CrawlJob).where(CrawlJob.id == job.id).values(**changes))


async def test_deleting_a_job_cascades_to_all_of_its_data(database: Database) -> None:
    async with database.transaction() as session:
        job, seed = await add_job_with_seed(session)
        page = new_page(job.id, seed)
        session.add(page)
        await session.flush()
        session.add_all(
            [
                link(page, "https://example.test/a"),
                SearchDocument(page_id=page.id, job_id=job.id),
                CrawlEvent(job_id=job.id, crawl_url_id=seed.id, event_type="job.created"),
            ]
        )
    assert all(count == 1 for count in (await table_counts(database)).values())

    async with database.transaction() as session:
        await session.execute(delete(CrawlJob).where(CrawlJob.id == job.id))

    assert all(count == 0 for count in (await table_counts(database)).values())


# --- Events and search documents ------------------------------------------------------------------


async def test_crawl_events_are_append_only(database: Database) -> None:
    async with database.transaction() as session:
        job = new_job()
        session.add(job)
        await session.flush()
        session.add(CrawlEvent(job_id=job.id, event_type="job.created"))

    with pytest.raises(IntegrityError, match="append-only"):
        async with database.transaction() as session:
            await session.execute(update(CrawlEvent).values(event_type="job.completed"))


async def test_event_types_are_dotted_lowercase_names(database: Database) -> None:
    async with database.transaction() as session:
        job = new_job()
        session.add(job)

    with pytest.raises(IntegrityError, match="ck_crawl_events_event_type_format"):
        async with database.transaction() as session:
            session.add(CrawlEvent(job_id=job.id, event_type="JobCreated"))


async def test_indexed_document_requires_a_vector_and_version(database: Database) -> None:
    async with database.transaction() as session:
        job, seed = await add_job_with_seed(session)
        page = new_page(job.id, seed)
        session.add(page)
        await session.flush()
        session.add(SearchDocument(page_id=page.id, job_id=job.id))

    with pytest.raises(IntegrityError, match="ck_search_documents_indexed_state_complete"):
        async with database.transaction() as session:
            await session.execute(
                update(SearchDocument)
                .where(SearchDocument.page_id == page.id)
                .values(status="indexed")
            )


async def test_full_text_search_over_the_gin_indexed_vector(database: Database) -> None:
    async with database.transaction() as session:
        job, seed = await add_job_with_seed(session)
        page = new_page(job.id, seed)
        session.add(page)
        await session.flush()
        session.add(SearchDocument(page_id=page.id, job_id=job.id))
        await session.flush()
        await session.execute(
            text(
                "UPDATE search_documents SET status = 'indexed', indexed_content_version = 1,"
                " indexed_at = now(),"
                " search_vector = to_tsvector('english', 'Distributed crawling and search')"
                " WHERE page_id = :page_id"
            ),
            {"page_id": page.id},
        )

    async with database.session() as session:
        matches = (
            (
                await session.execute(
                    text(
                        "SELECT page_id FROM search_documents"
                        " WHERE job_id = :job_id"
                        " AND search_vector @@ websearch_to_tsquery('english', 'crawl search')"
                    ),
                    {"job_id": job.id},
                )
            )
            .scalars()
            .all()
        )
        index_definition = (
            await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes"
                    " WHERE indexname = 'ix_search_documents_search_vector'"
                )
            )
        ).scalar_one()

    assert matches == [page.id]  # stemming: "crawling" matches "crawl"
    assert "USING gin (search_vector)" in index_definition
