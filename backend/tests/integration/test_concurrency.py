"""Basic concurrency safety: duplicate discovery, competing claims, and the page budget.

Each task uses its own session and connection. A barrier releases them at the same moment, so
the contended statements really run concurrently in PostgreSQL.
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrawlJob, CrawlUrl, JobStatus
from app.db.session import Database
from tests.integration.helpers import add_job_with_seed
from tests.integration.test_leases import CLAIM, queued_url

pytestmark = pytest.mark.anyio

# Stays below the pool limit (pool size 5 + overflow 5) so every task holds its own connection.
TASKS = 8


async def run_concurrently[T](
    database: Database, operation: Callable[[AsyncSession], Awaitable[T]]
) -> list[T | BaseException]:
    barrier = asyncio.Barrier(TASKS)

    async def task() -> T:
        async with database.transaction() as session:
            await session.execute(text("SELECT 1"))  # check out a connection first
            await barrier.wait()
            return await operation(session)

    return await asyncio.gather(*(task() for _ in range(TASKS)), return_exceptions=True)


async def test_concurrent_discovery_of_one_url_creates_exactly_one_row(database: Database) -> None:
    async with database.transaction() as session:
        job, _ = await add_job_with_seed(session)
    statement = (
        insert(CrawlUrl)
        .values(job_id=job.id, normalized_url="https://example.test/popular", depth=1)
        .on_conflict_do_nothing(index_elements=["job_id", "normalized_url"])
        .returning(CrawlUrl.id)
    )

    async def admit(session: AsyncSession) -> object:
        return (await session.execute(statement)).scalar_one_or_none()

    results = await run_concurrently(database, admit)

    assert not [r for r in results if isinstance(r, BaseException)]
    assert sum(r is not None for r in results) == 1
    async with database.session() as session:
        rows = (
            await session.execute(
                select(func.count())
                .select_from(CrawlUrl)
                .where(CrawlUrl.normalized_url == "https://example.test/popular")
            )
        ).scalar_one()
    assert rows == 1


async def test_competing_claims_have_exactly_one_winner(database: Database) -> None:
    url = await queued_url(database)

    async def compete(session: AsyncSession) -> object:
        parameters = {"url_id": url.id, "owner": "worker", "lease_seconds": 90}
        return (await session.execute(CLAIM, parameters)).scalar_one_or_none()

    results = await run_concurrently(database, compete)

    assert not [r for r in results if isinstance(r, BaseException)]
    assert [r for r in results if r is not None] == [1]
    async with database.session() as session:
        stored = await session.get(CrawlUrl, url.id)
    assert stored is not None
    assert (stored.lease_epoch, stored.attempt_count) == (1, 1)


async def test_concurrent_admissions_never_exceed_the_page_budget(database: Database) -> None:
    async with database.transaction() as session:
        job, _ = await add_job_with_seed(session, status=JobStatus.RUNNING, max_pages=3)

    async def admit_one(session: AsyncSession) -> None:
        await session.execute(
            update(CrawlJob)
            .where(CrawlJob.id == job.id)
            .values(admitted_count=CrawlJob.admitted_count + 1)
        )

    results = await run_concurrently(database, admit_one)

    succeeded = [r for r in results if not isinstance(r, BaseException)]
    rejected = [r for r in results if isinstance(r, IntegrityError)]
    assert len(succeeded) == 2  # 1 (seed) + 2 = max_pages
    assert len(rejected) == TASKS - 2
    assert all("ck_crawl_jobs_admitted_within_budget" in str(r) for r in rejected)
    async with database.session() as session:
        stored = await session.get(CrawlJob, job.id)
    assert stored is not None
    assert stored.admitted_count == stored.max_pages == 3
