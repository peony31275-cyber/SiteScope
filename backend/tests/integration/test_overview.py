"""The dashboard's overview (Phase 9) against real PostgreSQL: every job, URL, page and search
document counted, in one snapshot, under the analysis time limit."""

import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IndexStatus, JobStatus, UrlState
from app.db.session import Database
from app.services import overview
from app.services.overview import system_overview
from app.services.site_analysis import AnalysisTimeoutError, read_only_snapshot
from tests.analysis_support import add_url
from tests.crawl_support import new_job
from tests.index_support import add_page, execute, finish_job

pytestmark = pytest.mark.anyio

FETCHED, FAILED, SKIPPED = UrlState.FETCHED, UrlState.FAILED, UrlState.SKIPPED


SET_DOCUMENT = {
    "indexed": "UPDATE search_documents SET status = 'indexed', indexed_content_version = 1,"
    " indexed_at = clock_timestamp(), search_vector = to_tsvector('english', 'indexed')"
    " WHERE page_id = :id",
    "failed": "UPDATE search_documents SET status = 'failed', last_error = 'test'"
    " WHERE page_id = :id",
    # Published, and an indexer holds a live lease: an attempt is running.
    "active": "UPDATE search_documents SET status = 'queued', dispatched_at = clock_timestamp(),"
    " lease_owner = 'indexer', lease_expires_at = clock_timestamp() + interval '1 minute'"
    " WHERE page_id = :id",
    "queued": "UPDATE search_documents SET status = 'queued', dispatched_at = clock_timestamp()"
    " WHERE page_id = :id",
}


async def set_document(database: Database, page_id: uuid.UUID, status: str) -> None:
    await execute(database, SET_DOCUMENT[status], id=page_id)


async def a_deployment(database: Database) -> None:
    """Four jobs: running, completed, queued and cancelled, with pages and documents."""
    running, _ = await new_job(database, urls=2)  # seed + 2 pending URLs
    await add_page(database, running, path="r1")
    completed, _ = await new_job(database, host="other.test")
    pages = [await add_page(database, completed, path=f"c{n}", host="other.test") for n in range(4)]
    await add_page(database, completed, path="private", host="other.test", noindex=True)
    await set_document(database, pages[0], "indexed")
    await set_document(database, pages[1], "failed")
    await set_document(database, pages[2], "active")
    await set_document(database, pages[3], "queued")
    await add_url(database, completed, "gone", state=FAILED, outcome="connect_error")
    await add_url(database, completed, "blocked", state=SKIPPED, outcome="robots_disallowed")
    await add_url(
        database, completed, "missing", state=FETCHED, outcome="http_error", http_status=404
    )
    await finish_job(database, completed)
    await new_job(database, status=JobStatus.QUEUED)
    cancelled, _ = await new_job(database)
    await finish_job(database, cancelled, status="cancelled")


async def test_every_job_url_page_and_document_is_counted(database: Database) -> None:
    await a_deployment(database)

    async with database.session() as session:
        counts = await system_overview(session, timeout_seconds=5)

    assert counts.jobs_by_status == {
        JobStatus.QUEUED: 1,
        JobStatus.RUNNING: 1,
        JobStatus.CANCELLING: 0,
        JobStatus.COMPLETED: 1,
        JobStatus.FAILED: 0,
        JobStatus.CANCELLED: 1,
    }
    assert counts.jobs_total == 4
    # Seeds (4) and 2 pending URLs, 6 fetched page URLs, 3 more results.
    assert counts.urls_by_state == {
        UrlState.PENDING: 6,
        UrlState.QUEUED: 0,
        UrlState.IN_PROGRESS: 0,
        UrlState.FETCHED: 7,
        UrlState.FAILED: 1,
        UrlState.SKIPPED: 1,
        UrlState.CANCELLED: 0,
    }
    assert counts.urls_total == 15
    assert counts.pages_stored == 6
    # The noindex page has no document.
    assert counts.documents_by_status == {
        IndexStatus.PENDING: 1,
        IndexStatus.QUEUED: 2,
        IndexStatus.INDEXED: 1,
        IndexStatus.FAILED: 1,
    }
    assert (counts.documents_total, counts.documents_active) == (5, 1)


async def test_an_empty_deployment_counts_zero(database: Database) -> None:
    async with database.session() as session:
        counts = await system_overview(session, timeout_seconds=5)

    assert set(counts.jobs_by_status.values()) == {0}
    assert set(counts.urls_by_state.values()) == {0}
    assert set(counts.documents_by_status.values()) == {0}
    assert (counts.pages_stored, counts.documents_active) == (0, 0)


async def test_the_counts_come_from_one_snapshot(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another transaction stores a page (a fetched URL, the page and its document) after the
    URLs were counted: neither the pages nor the documents count it (REPEATABLE READ). At READ
    COMMITTED the later counts would include a page whose URL was not counted."""
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a")

    async with database.session() as session:
        execute_statement = session.execute

        async def store_a_page_after_the_url_counts(statement: Any, *args: Any, **kw: Any) -> Any:
            result = await execute_statement(statement, *args, **kw)
            if statement is overview._URLS_BY_STATE:
                await add_page(database, job_id, path="late")
            return result

        monkeypatch.setattr(session, "execute", store_a_page_after_the_url_counts)
        counts = await system_overview(session, timeout_seconds=5)

    assert (counts.urls_total, counts.pages_stored, counts.documents_total) == (2, 1, 1)
    async with database.session() as session:
        after = await system_overview(session, timeout_seconds=5)
    assert (after.urls_total, after.pages_stored, after.documents_total) == (3, 2, 2)


async def test_the_snapshot_is_read_only_bounded_and_planned_per_call(database: Database) -> None:
    async def setting(session: AsyncSession, name: str) -> str:
        value: str = await session.scalar(text(f"SELECT current_setting('{name}')"))
        return value

    async with database.session() as session:
        async with read_only_snapshot(session, 1.5, subject="overview"):
            assert await setting(session, "transaction_isolation") == "repeatable read"
            assert await setting(session, "transaction_read_only") == "on"
            assert await setting(session, "statement_timeout") == "1500ms"
            assert await setting(session, "plan_cache_mode") == "force_custom_plan"
        assert await setting(session, "plan_cache_mode") == "auto"


async def test_counts_that_wait_too_long_are_stopped(database: Database) -> None:
    """A lock held elsewhere (as a migration may take) makes the counts wait; the statement
    timeout stops them, with the overview's own message."""
    await new_job(database)
    async with database.transaction() as blocker:
        await blocker.execute(text("LOCK TABLE pages IN ACCESS EXCLUSIVE MODE"))
        began = time.monotonic()
        with pytest.raises(AnalysisTimeoutError, match=r"^The overview took longer than 0.3 s"):
            async with database.session() as session:
                await system_overview(session, timeout_seconds=0.3)
        assert time.monotonic() - began < 2.0
    async with database.session() as session:
        assert (await system_overview(session, timeout_seconds=5)).jobs_total == 1


async def test_the_overview_through_http(database: Database, api_client: TestClient) -> None:
    await a_deployment(database)

    response = api_client.get("/api/v1/overview")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["jobs"] == {
        "total": 4,
        "by_status": {
            "queued": 1,
            "running": 1,
            "cancelling": 0,
            "completed": 1,
            "failed": 0,
            "cancelled": 1,
        },
    }
    assert body["urls"]["total"] == 15
    assert body["urls"]["by_state"]["failed"] == 1
    assert body["pages_stored"] == 6
    assert body["documents"] == {
        "total": 5,
        "by_status": {"pending": 1, "queued": 2, "indexed": 1, "failed": 1},
        "active": 1,
    }
    assert body["counted_at"].endswith("Z")
