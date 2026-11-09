"""The active-jobs cap (Phase 11): a new crawl job is refused while
SITESCOPE_CRAWL_MAX_ACTIVE_JOBS jobs are queued, running or cancelling; a replay of an
Idempotency-Key is always answered; concurrent creations never exceed the cap (they serialize
on an advisory lock)."""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.db.session import Database
from app.main import create_app
from app.services.crawl_cancellation import cancel_crawl_job
from app.services.crawl_jobs import CREATION_LOCK, TooManyActiveJobsError, submit_crawl_job

pytestmark = pytest.mark.anyio


async def submit(
    database: Database, settings: Settings, number: int, key: str | None = None
) -> bool:
    """True if the job was created (or replayed), False if the cap refused it."""
    async with database.session() as session:
        try:
            await submit_crawl_job(
                session,
                settings,
                seed_url=f"https://docs.example.com/{number}",
                max_depth=1,
                max_pages=10,
                idempotency_key=key,
            )
        except TooManyActiveJobsError:
            return False
    return True


async def active_jobs(database: Database) -> int:
    async with database.session() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM crawl_jobs"
                    " WHERE status IN ('queued', 'running', 'cancelling')"
                )
            )
            or 0
        )


async def test_the_cap_refuses_new_jobs_but_answers_replays(
    database: Database, crawl_settings: Settings
) -> None:
    settings = crawl_settings.model_copy(update={"crawl_max_active_jobs": 2})
    assert await submit(database, settings, 1, key="first")
    assert await submit(database, settings, 2)

    assert not await submit(database, settings, 3)
    # The same request again with its key creates nothing, so the cap does not apply.
    assert await submit(database, settings, 1, key="first")
    assert await active_jobs(database) == 2

    # A job that ends makes room: a queued job is cancelled at once.
    async with database.session() as session:
        job_id = await session.scalar(
            text("SELECT id FROM crawl_jobs WHERE idempotency_key = 'first'")
        )
    async with database.session() as session:
        await cancel_crawl_job(session, job_id)
    assert await submit(database, settings, 3)
    assert await active_jobs(database) == 2


async def test_the_api_answers_429_with_retry_after(
    database: Database, crawl_settings: Settings
) -> None:
    settings = crawl_settings.model_copy(update={"crawl_max_active_jobs": 1})
    body = {"seed_url": "https://docs.example.com/", "max_depth": 1, "max_pages": 10}
    with TestClient(create_app(settings)) as client:
        first = client.post("/api/v1/crawls", json=body)
        second = client.post(
            "/api/v1/crawls", json={**body, "seed_url": "https://docs.example.com/b"}
        )

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "60"
    assert second.json()["code"] == "too_many_active_jobs"


async def test_concurrent_creations_never_exceed_the_cap(
    database: Database, crawl_settings: Settings
) -> None:
    settings = crawl_settings.model_copy(update={"crawl_max_active_jobs": 3})

    results = await asyncio.gather(*(submit(database, settings, number) for number in range(8)))

    assert sorted(results) == [False] * 5 + [True] * 3
    assert await active_jobs(database) == 3


# --- Races found by the Phase 11 review --------------------------------------------------------


async def _holder(database: Database) -> Any:
    """A raw connection of the test's own, for locks held across the code under test."""
    return await database.engine.connect()


async def _wait_for_waiters(database: Database, locktype: str, count: int = 1) -> None:
    """Until ``count`` lock requests of ``locktype`` wait (a backend is blocked on them)."""
    for _ in range(300):
        async with database.session() as session:
            waiting = int(
                await session.scalar(
                    text("SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype = :t"),
                    {"t": locktype},
                )
                or 0
            )
        if waiting >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {locktype} lock request waited")


async def test_a_key_expiring_meanwhile_cannot_create_an_uncounted_job(
    database: Database, crawl_settings: Settings
) -> None:
    """The cap is decided after the insert: a request that expected a replay, whose key the
    scheduler's maintenance freed in between, creates a job and must count it."""
    settings = crawl_settings.model_copy(update={"crawl_max_active_jobs": 1})
    assert await submit(database, settings, 1, key="k")  # the one active job
    holder = await _holder(database)
    try:
        # The maintenance pass, caught mid-transaction: the key cleared, not committed yet.
        await holder.execute(
            text(
                "UPDATE crawl_jobs SET idempotency_key = NULL, request_fingerprint = NULL"
                " WHERE idempotency_key = 'k'"
            )
        )
        request = asyncio.create_task(submit(database, settings, 1, key="k"))
        await _wait_for_waiters(database, "transactionid")  # its insert waits for the clearing
        await holder.commit()
        created = await request
    finally:
        await holder.close()

    assert not created  # refused by the cap, not created beside the active job
    assert await active_jobs(database) == 1


async def test_same_key_requests_around_its_expiry_do_not_deadlock(
    database: Database, crawl_settings: Settings
) -> None:
    """A creation that holds the creation lock while its insert waits for the expired key's
    row, and another request that freed that row and waits for the lock: no lock cycle,
    because a request takes the creation lock before it frees an expired key."""
    settings = crawl_settings.model_copy(update={"idempotency_key_ttl_seconds": 60})
    assert await submit(database, settings, 1, key="k")
    async with database.session() as session, session.begin():
        await session.execute(
            text(
                "UPDATE crawl_jobs SET created_at = now() - interval '1 day'"
                " WHERE idempotency_key = 'k'"
            )
        )
    holder = await _holder(database)
    try:
        # Another creation, holding the lock (as submit_crawl_job does) for its insert.
        await holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CREATION_LOCK})
        request = asyncio.create_task(submit(database, settings, 2, key="k"))
        await _wait_for_waiters(database, "advisory")  # the request waits for the lock
        # The holder's insert of the same key: it must not wait for the request.
        await holder.execute(
            text(
                "INSERT INTO crawl_jobs (seed_url, allowed_hosts, max_depth, max_pages,"
                " idempotency_key, request_fingerprint)"
                " VALUES ('https://docs.example.com/x', ARRAY['docs.example.com'], 1, 10, 'k',"
                " decode(repeat('00', 32), 'hex')) ON CONFLICT (idempotency_key) DO NOTHING"
            )
        )
        await holder.commit()
        created = await asyncio.wait_for(request, 30)
    finally:
        await holder.close()

    assert created  # the expired key was freed and the request created its job
