"""Crawl jobs against the real PostgreSQL: transactional creation, retrieval and listing.

Service tests call app/services/crawl_jobs.py directly. API tests go through the real
application (TestClient) and then check what PostgreSQL stored.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.errors import PROBLEM_MEDIA_TYPE, InvalidInputError, NotFoundError
from app.db.models import CrawlJob, CrawlUrl, UrlState
from app.db.session import Database
from app.main import create_app
from app.services import crawl_jobs
from tests.integration.helpers import new_job, unused_port

pytestmark = pytest.mark.anyio

SEED = "HTTPS://Docs.Example.com:443/guide/./intro#install"
NORMALIZED_SEED = "https://docs.example.com/guide/intro"


async def create(
    database: Database,
    settings: Settings,
    *,
    seed_url: str = SEED,
    max_depth: int = 3,
    max_pages: int = 500,
) -> CrawlJob:
    async with database.session() as session:
        return await crawl_jobs.create_crawl_job(
            session, settings, seed_url=seed_url, max_depth=max_depth, max_pages=max_pages
        )


async def row_counts(database: Database) -> tuple[int, int]:
    """Rows in (crawl_jobs, crawl_urls)."""
    async with database.session() as session:
        jobs = await session.scalar(select(func.count()).select_from(CrawlJob))
        urls = await session.scalar(select(func.count()).select_from(CrawlUrl))
    return jobs or 0, urls or 0


async def stored(database: Database, job_id: uuid.UUID) -> tuple[CrawlJob, list[CrawlUrl]]:
    async with database.session() as session:
        job = await session.get(CrawlJob, job_id)
        urls = (await session.scalars(select(CrawlUrl).where(CrawlUrl.job_id == job_id))).all()
    assert job is not None
    return job, list(urls)


@pytest.fixture
async def failing_seed_insert(database: Database) -> AsyncIterator[None]:
    """Make PostgreSQL reject every crawl_urls INSERT, as if the seed URL insert failed."""
    async with database.transaction() as session:
        await session.execute(
            text(
                "CREATE FUNCTION test_reject_insert() RETURNS trigger LANGUAGE plpgsql AS"
                " $$ BEGIN RAISE EXCEPTION 'injected failure in the seed URL insert'; END; $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER test_reject_insert BEFORE INSERT ON crawl_urls"
                " FOR EACH ROW EXECUTE FUNCTION test_reject_insert()"
            )
        )
    try:
        yield
    finally:
        async with database.transaction() as session:
            await session.execute(text("DROP TRIGGER test_reject_insert ON crawl_urls"))
            await session.execute(text("DROP FUNCTION test_reject_insert()"))


# --- Creation ------------------------------------------------------------------------------------


async def test_creation_stores_a_queued_job_and_its_pending_seed(
    database: Database, crawl_settings: Settings
) -> None:
    job = await create(database, crawl_settings)

    job, urls = await stored(database, job.id)
    assert job.seed_url == NORMALIZED_SEED
    assert job.allowed_hosts == ["docs.example.com"]
    assert (job.max_depth, job.max_pages) == (3, 500)
    # Waiting for the scheduler: not running, not started, nothing fetched.
    assert (job.status, job.current_level, job.admitted_count) == ("queued", 0, 1)
    assert (job.started_at, job.finished_at, job.failure_reason) == (None, None, None)
    [seed] = urls
    assert (seed.normalized_url, seed.depth, seed.state) == (NORMALIZED_SEED, 0, "pending")
    assert (seed.attempt_count, seed.lease_epoch, seed.lease_owner) == (0, 0, None)
    assert (seed.discovered_from_url_id, seed.dispatched_at, seed.result_epoch) == (None,) * 3
    # now() is the transaction's start time: equal timestamps show one transaction wrote both.
    assert seed.created_at == job.created_at


async def test_a_failed_seed_insert_rolls_back_the_job(
    database: Database, crawl_settings: Settings, failing_seed_insert: None
) -> None:
    with pytest.raises(DBAPIError, match="injected failure in the seed URL insert"):
        await create(database, crawl_settings)

    assert await row_counts(database) == (0, 0)


@pytest.mark.parametrize(
    "request_fields",
    [
        {"seed_url": "https://other.example/"},
        {"seed_url": "ftp://docs.example.com/"},
        {"max_depth": 6},
        {"max_pages": 1001},
    ],
)
async def test_rejected_requests_write_nothing(
    database: Database, crawl_settings: Settings, request_fields: dict[str, Any]
) -> None:
    with pytest.raises(InvalidInputError):
        await create(database, crawl_settings, **request_fields)

    assert await row_counts(database) == (0, 0)


async def test_the_same_seed_may_start_several_jobs(
    database: Database, crawl_settings: Settings
) -> None:
    """URLs are unique per job only (UNIQUE (job_id, normalized_url)), never globally."""
    first = await create(database, crawl_settings)
    second = await create(database, crawl_settings, seed_url=NORMALIZED_SEED)

    assert first.id != second.id
    assert first.seed_url == second.seed_url == NORMALIZED_SEED
    assert await row_counts(database) == (2, 2)


async def test_concurrent_creation_of_jobs_with_the_same_seed(
    database: Database, crawl_settings: Settings
) -> None:
    tasks = 8  # below the pool limit (5 + 5 overflow), so every task holds its own connection
    barrier = asyncio.Barrier(tasks)

    async def create_after_barrier() -> CrawlJob:
        await barrier.wait()
        return await create(database, crawl_settings)

    jobs = await asyncio.gather(*(create_after_barrier() for _ in range(tasks)))

    assert len({job.id for job in jobs}) == tasks
    assert await row_counts(database) == (tasks, tasks)
    for job in jobs:
        stored_job, urls = await stored(database, job.id)
        assert stored_job.admitted_count == 1
        assert [url.normalized_url for url in urls] == [NORMALIZED_SEED]


# --- Retrieval -----------------------------------------------------------------------------------


async def test_get_counts_the_jobs_own_url_rows(
    database: Database, crawl_settings: Settings
) -> None:
    job = await create(database, crawl_settings)
    other = await create(database, crawl_settings, seed_url="https://a.example.org/")
    async with database.transaction() as session:
        session.add_all(
            [
                CrawlUrl(job_id=job.id, normalized_url=f"{NORMALIZED_SEED}/a", depth=1),
                CrawlUrl(
                    job_id=job.id,
                    normalized_url=f"{NORMALIZED_SEED}/b",
                    depth=1,
                    state="cancelled",
                    finished_at=func.now(),
                ),
                CrawlUrl(
                    job_id=job.id,
                    normalized_url=f"{NORMALIZED_SEED}/c",
                    depth=1,
                    state="skipped",
                    result_epoch=0,
                    outcome="robots_disallowed",
                    finished_at=func.now(),
                ),
            ]
        )
        await session.execute(
            update(CrawlJob).where(CrawlJob.id == job.id).values(admitted_count=4)
        )

    async with database.session() as session:
        progress = await crawl_jobs.get_crawl_job(session, job.id)

    assert progress.job.id == job.id
    assert progress.job.admitted_count == 4
    assert progress.urls_by_state == {
        UrlState.PENDING: 2,
        UrlState.QUEUED: 0,
        UrlState.IN_PROGRESS: 0,
        UrlState.FETCHED: 0,
        UrlState.FAILED: 0,
        UrlState.SKIPPED: 1,
        UrlState.CANCELLED: 1,
    }
    assert (progress.urls_total, progress.urls_finished) == (4, 2)
    async with database.session() as session:
        assert (await crawl_jobs.get_crawl_job(session, other.id)).urls_total == 1


async def test_get_an_unknown_job_raises_not_found(database: Database) -> None:
    missing = uuid.uuid4()

    async with database.session() as session:
        with pytest.raises(NotFoundError, match=str(missing)):
            await crawl_jobs.get_crawl_job(session, missing)


# --- Listing -------------------------------------------------------------------------------------


async def page_ids(
    database: Database, *, limit: int, after: crawl_jobs.JobPosition | None = None
) -> tuple[list[uuid.UUID], crawl_jobs.JobPosition | None]:
    async with database.session() as session:
        page = await crawl_jobs.list_crawl_jobs(session, limit=limit, after=after)
    return [job.id for job in page.jobs], page.next_position


async def test_list_pages_through_jobs_newest_first(
    database: Database, crawl_settings: Settings
) -> None:
    created = [(await create(database, crawl_settings)).id for _ in range(5)]
    newest_first = list(reversed(created))

    first, position = await page_ids(database, limit=2)
    second, position = await page_ids(database, limit=2, after=position)
    third, position = await page_ids(database, limit=2, after=position)

    assert [first, second, third] == [newest_first[0:2], newest_first[2:4], newest_first[4:]]
    assert position is None


async def test_jobs_created_while_paging_never_shift_later_pages(
    database: Database, crawl_settings: Settings
) -> None:
    old = [(await create(database, crawl_settings)).id for _ in range(4)]
    first, position = await page_ids(database, limit=2)

    new = [(await create(database, crawl_settings)).id for _ in range(2)]
    second, position = await page_ids(database, limit=2, after=position)

    assert first == [old[3], old[2]]
    assert second == [old[1], old[0]]  # no duplicate, no gap
    assert position is None
    assert (await page_ids(database, limit=2))[0] == [new[1], new[0]]


async def test_jobs_with_the_same_created_at_are_ordered_by_id(database: Database) -> None:
    async with database.transaction() as session:  # now() is the same for all three rows
        jobs = [new_job() for _ in range(3)]
        session.add_all(jobs)
    assert len({job.created_at for job in jobs}) == 1

    first, position = await page_ids(database, limit=1)
    second, position = await page_ids(database, limit=1, after=position)
    third, position = await page_ids(database, limit=1, after=position)

    assert first + second + third == sorted((job.id for job in jobs), reverse=True)
    assert position is None  # the third page is the last


async def test_listing_is_served_by_the_created_at_index(database: Database) -> None:
    """With sequential scans disabled, the keyset query runs as one backward index scan."""
    async with database.transaction() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            (
                await session.execute(
                    text(
                        "EXPLAIN SELECT * FROM crawl_jobs"
                        " WHERE (created_at, id) < (now(), uuidv7())"
                        " ORDER BY created_at DESC, id DESC LIMIT 21"
                    )
                )
            ).scalars()
        )

    assert "Index Scan Backward using ix_crawl_jobs_created_at_id" in plan
    assert "Sort" not in plan


# --- Through the HTTP API ------------------------------------------------------------------------


async def test_post_then_get_and_list_through_the_api(
    crawl_client: TestClient, database: Database
) -> None:
    created = crawl_client.post(
        "/api/v1/crawls", json={"seed_url": SEED, "max_depth": 3, "max_pages": 500}
    )

    assert created.status_code == 201
    body = created.json()
    assert created.headers["Location"] == f"/api/v1/crawls/{body['id']}"
    assert body["status"] == "queued"
    assert body["seed_url"] == NORMALIZED_SEED
    assert (body["admitted_count"], body["current_level"], body["started_at"]) == (1, 0, None)
    assert body["created_at"].endswith("Z")

    detail = crawl_client.get(created.headers["Location"])
    assert detail.status_code == 200
    assert detail.json()["progress"] == {
        "urls_total": 1,
        "urls_finished": 0,
        "urls_by_state": {
            "pending": 1,
            "queued": 0,
            "in_progress": 0,
            "fetched": 0,
            "failed": 0,
            "skipped": 0,
            "cancelled": 0,
        },
    }
    listing = crawl_client.get("/api/v1/crawls").json()
    assert [item["id"] for item in listing["items"]] == [body["id"]]
    assert listing["next_cursor"] is None

    job, urls = await stored(database, uuid.UUID(body["id"]))
    assert (job.seed_url, [url.normalized_url for url in urls]) == (NORMALIZED_SEED, [job.seed_url])


async def test_api_rejects_unapproved_hosts_and_writes_nothing(
    crawl_client: TestClient, database: Database
) -> None:
    response = crawl_client.post(
        "/api/v1/crawls",
        json={"seed_url": "https://example.org/", "max_depth": 1, "max_pages": 10},
    )

    assert response.status_code == 422
    [error] = response.json()["errors"]
    # "*.example.org" approves subdomains only, not example.org itself.
    assert (error["type"], error["msg"]) == (
        "target_not_allowed",
        "host 'example.org' is not an approved crawl target",
    )
    assert await row_counts(database) == (0, 0)


async def test_api_returns_a_404_problem_for_an_unknown_job(crawl_client: TestClient) -> None:
    missing = uuid.uuid4()

    response = crawl_client.get(f"/api/v1/crawls/{missing}")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert (body["code"], body["detail"]) == ("not_found", f"Crawl job {missing} does not exist.")


async def test_api_list_pagination(crawl_client: TestClient) -> None:
    ids = [
        crawl_client.post(
            "/api/v1/crawls",
            json={"seed_url": f"https://docs.example.com/{n}", "max_depth": 0, "max_pages": 1},
        ).json()["id"]
        for n in range(3)
    ]

    first = crawl_client.get("/api/v1/crawls", params={"limit": 2}).json()
    second = crawl_client.get(
        "/api/v1/crawls", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()

    assert [item["id"] for item in first["items"]] == [ids[2], ids[1]]
    assert [item["id"] for item in second["items"]] == [ids[0]]
    assert second["next_cursor"] is None


async def test_a_database_error_becomes_a_generic_500_and_writes_nothing(
    crawl_client: TestClient, database: Database, failing_seed_insert: None
) -> None:
    response = crawl_client.post(
        "/api/v1/crawls", json={"seed_url": SEED, "max_depth": 1, "max_pages": 10}
    )

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "injected failure" not in response.text  # no database error leaks to the client
    assert await row_counts(database) == (0, 0)


async def test_an_unreachable_database_becomes_a_503_problem(crawl_settings: Settings) -> None:
    """A PostgreSQL outage is reported as such, without driver details (about 2 s on Windows)."""
    settings = crawl_settings.model_copy(update={"postgres_port": unused_port()})

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/crawls", json={"seed_url": SEED, "max_depth": 1, "max_pages": 10}
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    body = response.json()
    assert body["code"] == "dependency_unavailable"
    assert str(settings.postgres_port) not in response.text


async def test_jobs_are_created_while_redis_is_down(
    crawl_settings: Settings, database: Database
) -> None:
    """Job creation touches PostgreSQL only: the API never publishes to Redis (ADR-0002)."""
    settings = crawl_settings.model_copy(update={"redis_port": unused_port()})

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/crawls", json={"seed_url": SEED, "max_depth": 1, "max_pages": 10}
        )

    assert response.status_code == 201
    assert await row_counts(database) == (1, 1)
