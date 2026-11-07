"""Idempotent job creation (``Idempotency-Key``) against real PostgreSQL (Phase 6, ADR-0017).

PostgreSQL is the only store of keys: they are written with the job, in its transaction, and a
unique constraint decides between concurrent requests.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.core.config import Settings
from app.crawler.urls import normalize_url
from app.db.models import CrawlJob, CrawlUrl
from app.db.session import Database
from app.main import create_app
from app.services.crawl_jobs import (
    IdempotencyKeyReusedError,
    SubmittedCrawlJob,
    expire_idempotency_keys,
    request_fingerprint,
    submit_crawl_job,
)
from tests.crawl_support import execute

pytestmark = pytest.mark.anyio

SEED = "https://docs.example.com/guide"
KEY = "order-7f3a-2026-11-07"


async def submit(
    database: Database,
    settings: Settings,
    *,
    key: str | None = KEY,
    seed_url: str = SEED,
    max_depth: int = 2,
    max_pages: int = 50,
) -> SubmittedCrawlJob:
    async with database.session() as session:
        return await submit_crawl_job(
            session,
            settings,
            seed_url=seed_url,
            max_depth=max_depth,
            max_pages=max_pages,
            idempotency_key=key,
        )


async def job_count(database: Database) -> int:
    async with database.session() as session:
        return int(await session.scalar(select(func.count()).select_from(CrawlJob)) or 0)


async def seed_count(database: Database) -> int:
    async with database.session() as session:
        return int(await session.scalar(select(func.count()).select_from(CrawlUrl)) or 0)


async def age_job(database: Database, job_id: uuid.UUID, seconds: int) -> None:
    """Make a job look ``seconds`` older (as if the key's TTL had passed)."""
    await execute(
        database,
        "UPDATE crawl_jobs SET created_at = created_at - make_interval(secs => :s) WHERE id = :id",
        id=job_id,
        s=seconds,
    )


@pytest.fixture
async def failing_seed_insert(database: Database) -> AsyncIterator[None]:
    """Make PostgreSQL reject every crawl_urls INSERT, as if the seed URL insert failed."""
    async with database.transaction() as session:
        await session.execute(
            text(
                "CREATE FUNCTION test_reject_seed() RETURNS trigger LANGUAGE plpgsql AS"
                " $$ BEGIN RAISE EXCEPTION 'injected failure in the seed URL insert'; END; $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER test_reject_seed BEFORE INSERT ON crawl_urls"
                " FOR EACH ROW EXECUTE FUNCTION test_reject_seed()"
            )
        )
    try:
        yield
    finally:
        async with database.transaction() as session:
            await session.execute(text("DROP TRIGGER test_reject_seed ON crawl_urls"))
            await session.execute(text("DROP FUNCTION test_reject_seed()"))


# --- The service ---------------------------------------------------------------------------------


async def test_a_key_is_stored_with_its_job(database: Database, crawl_settings: Settings) -> None:
    submitted = await submit(database, crawl_settings)

    assert submitted.created
    job = submitted.job
    assert job.idempotency_key == KEY
    seed = normalize_url(SEED)
    assert job.request_fingerprint == request_fingerprint(seed, max_depth=2, max_pages=50)
    assert len(job.request_fingerprint) == 32


async def test_the_same_key_and_request_return_the_same_job(
    database: Database, crawl_settings: Settings
) -> None:
    first = await submit(database, crawl_settings)
    # An equivalent request: the same seed after normalization.
    again = await submit(database, crawl_settings, seed_url="HTTPS://Docs.Example.com:443/guide#x")

    assert (first.created, again.created) == (True, False)
    assert again.job.id == first.job.id
    assert (await job_count(database), await seed_count(database)) == (1, 1)


@pytest.mark.parametrize(
    "change",
    [{"seed_url": "https://docs.example.com/other"}, {"max_depth": 3}, {"max_pages": 51}],
)
async def test_the_same_key_with_another_request_is_a_conflict(
    database: Database, crawl_settings: Settings, change: dict[str, str | int]
) -> None:
    await submit(database, crawl_settings)

    with pytest.raises(IdempotencyKeyReusedError) as raised:
        await submit(database, crawl_settings, **change)  # type: ignore[arg-type]  # parametrized

    assert raised.value.code == "idempotency_key_reused"
    assert raised.value.status_code == 409
    assert await job_count(database) == 1


async def test_without_a_key_every_request_creates_a_job(
    database: Database, crawl_settings: Settings
) -> None:
    first = await submit(database, crawl_settings, key=None)
    second = await submit(database, crawl_settings, key=None)

    assert first.created
    assert second.created
    assert first.job.id != second.job.id
    assert (first.job.idempotency_key, first.job.request_fingerprint) == (None, None)


async def test_concurrent_identical_requests_create_one_job(
    database: Database, crawl_settings: Settings
) -> None:
    results = await asyncio.gather(*(submit(database, crawl_settings) for _ in range(8)))

    assert sum(result.created for result in results) == 1
    assert len({result.job.id for result in results}) == 1
    assert (await job_count(database), await seed_count(database)) == (1, 1)


async def test_concurrent_conflicting_requests_create_one_job(
    database: Database, crawl_settings: Settings
) -> None:
    outcomes = await asyncio.gather(
        *(submit(database, crawl_settings, max_pages=50 + index) for index in range(6)),
        return_exceptions=True,
    )

    created = [item for item in outcomes if isinstance(item, SubmittedCrawlJob)]
    conflicts = [item for item in outcomes if isinstance(item, IdempotencyKeyReusedError)]
    assert (len(created), len(conflicts)) == (1, 5)
    assert created[0].created
    assert await job_count(database) == 1


async def test_a_failed_creation_leaves_no_key_behind(
    database: Database, crawl_settings: Settings, failing_seed_insert: None
) -> None:
    with pytest.raises(Exception, match="injected failure"):
        await submit(database, crawl_settings)
    assert await job_count(database) == 0


async def test_the_key_works_again_after_a_failed_creation(
    database: Database, crawl_settings: Settings
) -> None:
    async with database.transaction() as session:
        await session.execute(
            text(
                "CREATE FUNCTION test_reject_once() RETURNS trigger LANGUAGE plpgsql AS"
                " $$ BEGIN RAISE EXCEPTION 'injected failure'; END; $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER test_reject_once BEFORE INSERT ON crawl_urls"
                " FOR EACH ROW EXECUTE FUNCTION test_reject_once()"
            )
        )
    try:
        with pytest.raises(Exception, match="injected failure"):
            await submit(database, crawl_settings)
    finally:
        async with database.transaction() as session:
            await session.execute(text("DROP TRIGGER test_reject_once ON crawl_urls"))
            await session.execute(text("DROP FUNCTION test_reject_once()"))

    retried = await submit(database, crawl_settings)

    assert retried.created  # the key was rolled back with the failed job
    assert (await job_count(database), await seed_count(database)) == (1, 1)


async def test_keys_survive_a_restart(
    database: Database, crawl_settings: Settings, test_database: Settings
) -> None:
    """A new process (a new engine and connection pool) still knows the key."""
    first = await submit(database, crawl_settings)
    restarted = Database(test_database, application_name="sitescope-tests-restarted")
    try:
        again = await submit(restarted, crawl_settings)
    finally:
        await restarted.dispose()

    assert (again.created, again.job.id) == (False, first.job.id)


async def test_an_expired_key_creates_a_new_job_and_the_old_job_stays(
    database: Database, crawl_settings: Settings
) -> None:
    ttl = crawl_settings.idempotency_key_ttl_seconds
    first = await submit(database, crawl_settings)
    await age_job(database, first.job.id, ttl + 5)

    second = await submit(database, crawl_settings, max_pages=99)  # another request is fine now

    assert second.created
    assert second.job.id != first.job.id
    async with database.session() as session:
        old = await session.get(CrawlJob, first.job.id)
    assert old is not None
    assert (old.idempotency_key, old.request_fingerprint) == (None, None)
    assert await job_count(database) == 2


async def test_a_key_within_its_ttl_is_kept(database: Database, crawl_settings: Settings) -> None:
    first = await submit(database, crawl_settings)
    await age_job(database, first.job.id, crawl_settings.idempotency_key_ttl_seconds - 60)

    again = await submit(database, crawl_settings)

    assert (again.created, again.job.id) == (False, first.job.id)


async def test_concurrent_requests_with_an_expired_key_create_one_new_job(
    database: Database, crawl_settings: Settings
) -> None:
    first = await submit(database, crawl_settings)
    await age_job(database, first.job.id, crawl_settings.idempotency_key_ttl_seconds + 5)

    results = await asyncio.gather(*(submit(database, crawl_settings) for _ in range(6)))

    assert sum(result.created for result in results) == 1
    assert len({result.job.id for result in results}) == 1
    assert results[0].job.id != first.job.id
    assert await job_count(database) == 2


async def test_the_scheduler_forgets_expired_keys_in_batches(
    database: Database, crawl_settings: Settings
) -> None:
    ttl = crawl_settings.idempotency_key_ttl_seconds
    jobs = [await submit(database, crawl_settings, key=f"key-{n}") for n in range(5)]
    for submitted in jobs[:3]:
        await age_job(database, submitted.job.id, ttl + 1)

    async with database.session() as session:
        assert await expire_idempotency_keys(session, ttl=ttl, limit=2) == 2
    async with database.session() as session:
        assert await expire_idempotency_keys(session, ttl=ttl, limit=2) == 1
    async with database.session() as session:
        assert await expire_idempotency_keys(session, ttl=ttl, limit=2) == 0
        keys = set(await session.scalars(select(CrawlJob.idempotency_key)))
    assert keys == {None, "key-3", "key-4"}
    assert await job_count(database) == 5  # the jobs stay


# --- The API ---------------------------------------------------------------------------------


BODY = {"seed_url": SEED, "max_depth": 2, "max_pages": 50}


async def test_the_api_replays_a_request_with_the_same_key(
    crawl_client: TestClient, database: Database
) -> None:
    created = crawl_client.post("/api/v1/crawls", json=BODY, headers={"Idempotency-Key": KEY})
    replayed = crawl_client.post("/api/v1/crawls", json=BODY, headers={"Idempotency-Key": KEY})

    assert created.status_code == 201
    assert "Idempotent-Replayed" not in created.headers
    assert replayed.status_code == 200
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert replayed.headers["Location"] == created.headers["Location"]
    assert replayed.json() == created.json()
    assert await job_count(database) == 1


async def test_the_api_refuses_a_key_reused_for_another_request(
    crawl_client: TestClient, database: Database
) -> None:
    crawl_client.post("/api/v1/crawls", json=BODY, headers={"Idempotency-Key": KEY})

    conflict = crawl_client.post(
        "/api/v1/crawls", json={**BODY, "max_depth": 3}, headers={"Idempotency-Key": KEY}
    )

    assert conflict.status_code == 409
    assert conflict.headers["content-type"] == "application/problem+json"
    problem = conflict.json()
    assert problem["code"] == "idempotency_key_reused"
    assert KEY not in conflict.text  # the key is not echoed
    assert await job_count(database) == 1


@pytest.mark.parametrize(
    "key",
    ["", "has space", "tab\there", "x" * 256, "café"],
    ids=["empty", "space", "tab", "too-long", "non-ascii"],
)
async def test_the_api_rejects_invalid_keys(
    crawl_client: TestClient, database: Database, key: str
) -> None:
    response = crawl_client.post(
        "/api/v1/crawls",
        json=BODY,
        headers={b"Idempotency-Key": key.encode("utf-8")},  # raw bytes: UTF-8 on the wire
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert await job_count(database) == 0


async def test_the_longest_valid_key_is_accepted(
    crawl_client: TestClient, database: Database
) -> None:
    key = "".join(chr(33 + index % 94) for index in range(255))
    assert (
        crawl_client.post("/api/v1/crawls", json=BODY, headers={"Idempotency-Key": key}).status_code
        == 201
    )
    assert (
        crawl_client.post("/api/v1/crawls", json=BODY, headers={"Idempotency-Key": key}).status_code
        == 200
    )


async def test_the_api_without_a_key_behaves_as_before(
    crawl_client: TestClient, database: Database
) -> None:
    statuses = [crawl_client.post("/api/v1/crawls", json=BODY).status_code for _ in range(2)]
    assert statuses == [201, 201]
    assert await job_count(database) == 2


async def test_a_replay_survives_an_api_restart(
    crawl_settings: Settings, database: Database
) -> None:
    with TestClient(create_app(crawl_settings)) as first_process:
        created = first_process.post("/api/v1/crawls", json=BODY, headers={"Idempotency-Key": KEY})
    with TestClient(create_app(crawl_settings)) as second_process:
        replayed = second_process.post(
            "/api/v1/crawls", json=BODY, headers={"Idempotency-Key": KEY}
        )
    assert (created.status_code, replayed.status_code) == (201, 200)
    assert replayed.json()["id"] == created.json()["id"]
