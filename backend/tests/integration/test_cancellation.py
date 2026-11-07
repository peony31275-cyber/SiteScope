"""Crawl job cancellation against real PostgreSQL: T7, the workers' fenced cancellation, the
finalizer, races, lock order, and ``POST /api/v1/crawls/{job_id}/cancel`` (Phase 6, ADR-0017).
"""

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.errors import NotFoundError
from app.db.models import JobStatus, UrlState
from app.db.session import Database
from app.services.crawl_cancellation import (
    CANCELLED_OUTCOME,
    CancelAction,
    cancel_claimed_url,
    cancel_crawl_job,
    cancelling_jobs,
    jobs_being_cancelled,
    settle_cancellation,
)
from app.services.crawl_state import (
    AdvanceAction,
    Lease,
    RecordStatus,
    UrlResult,
    advance_job,
    claim_url,
    dispatch_urls,
    record_result,
)
from tests.crawl_support import (
    events,
    execute,
    expire_lease,
    html_result,
    job_row,
    new_job,
    states,
    url_row,
)

pytestmark = pytest.mark.anyio


async def cancel(database: Database, job_id: uuid.UUID) -> Any:
    async with database.session() as session:
        return await cancel_crawl_job(session, job_id)


async def settle(database: Database, job_id: uuid.UUID) -> Any:
    async with database.session() as session:
        return await settle_cancellation(session, job_id)


async def dispatch(database: Database, job_id: uuid.UUID, **kwargs: Any) -> list[uuid.UUID]:
    async with database.session() as session:
        return await dispatch_urls(session, job_id, limit=100, **kwargs)


async def claim(database: Database, url_id: uuid.UUID, owner: str = "worker-a") -> Lease | None:
    async with database.session() as session:
        return await claim_url(session, url_id, owner=owner, lease_seconds=60.0, max_attempts=3)


async def record(
    database: Database, lease: Lease, result: UrlResult, *, retry_delay: float | None = None
) -> Any:
    async with database.session() as session:
        return await record_result(
            session, lease, result, max_redirect_hops=5, retry_delay=retry_delay
        )


async def cancel_claimed(database: Database, lease: Lease) -> bool:
    async with database.session() as session:
        return await cancel_claimed_url(session, lease)


async def in_flight_job(
    database: Database, urls: int = 2
) -> tuple[uuid.UUID, list[uuid.UUID], Lease]:
    """A running job: its seed in progress (leased to worker-a), ``urls`` more URLs queued."""
    job_id, url_ids = await new_job(database, urls=urls)
    await dispatch(database, job_id)
    lease = await claim(database, url_ids[0])
    assert lease is not None
    return job_id, url_ids, lease


async def until_a_session_waits_for_a_lock(database: Database, within: float = 5.0) -> None:
    """Until PostgreSQL shows a session of the test database blocked on a lock: proof that a
    transaction is waiting, rather than merely not scheduled yet."""
    async with asyncio.timeout(within):
        while True:
            async with database.session() as session:
                waiting = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            if waiting:
                return
            await asyncio.sleep(0.02)


def timeout() -> UrlResult:
    return UrlResult(UrlState.FAILED, "read_timeout", error="no data within 10 s")


# --- T7 ------------------------------------------------------------------------------------------


async def test_a_queued_job_is_cancelled_at_once(database: Database) -> None:
    job_id, [seed] = await new_job(database, status=JobStatus.QUEUED)

    result = await cancel(database, job_id)

    assert (result.action, result.status, result.cancelled_urls) == (
        CancelAction.CANCELLED,
        JobStatus.CANCELLED,
        1,
    )
    job = await job_row(database, job_id)
    assert job.status == "cancelled"
    assert job.cancel_requested_at is not None
    assert job.finished_at is not None
    seed_row = await url_row(database, seed)
    assert (seed_row.state, seed_row.outcome) == ("cancelled", CANCELLED_OUTCOME)
    assert seed_row.finished_at is not None
    [requested] = await events(database, job_id, "job.cancel_requested")
    assert (requested.from_state, requested.to_state) == ("queued", "cancelled")
    assert len(await events(database, job_id, "job.cancelled")) == 1


async def test_a_running_job_with_nothing_in_flight_is_cancelled_at_once(
    database: Database,
) -> None:
    job_id, _ = await new_job(database, urls=2)
    await dispatch(database, job_id)  # queued, but not claimed yet

    result = await cancel(database, job_id)

    assert (result.action, result.status, result.cancelled_urls) == (
        CancelAction.CANCELLED,
        JobStatus.CANCELLED,
        3,
    )
    assert await states(database, job_id) == {"cancelled": 3}
    [done] = await events(database, job_id, "job.cancelled")
    assert (done.from_state, done.to_state) == ("cancelling", "cancelled")


async def test_a_running_job_waits_for_its_in_flight_urls(database: Database) -> None:
    job_id, [seed, *others], lease = await in_flight_job(database)

    result = await cancel(database, job_id)

    assert (result.action, result.status, result.cancelled_urls) == (
        CancelAction.CANCELLING,
        JobStatus.CANCELLING,
        2,
    )
    # The in-progress URL is untouched: its worker holds the lease.
    seed_row = await url_row(database, seed)
    assert (seed_row.state, seed_row.lease_owner, seed_row.lease_epoch) == (
        "in_progress",
        "worker-a",
        lease.token,
    )
    assert {(await url_row(database, url_id)).state for url_id in others} == {"cancelled"}
    assert (await job_row(database, job_id)).finished_at is None
    # Not settled while the lease is live.
    assert (await settle(database, job_id)).finalized is False
    assert (await job_row(database, job_id)).status == "cancelling"


async def test_cancelling_is_idempotent(database: Database) -> None:
    job_id, _, _ = await in_flight_job(database)
    await cancel(database, job_id)

    again = await asyncio.gather(*(cancel(database, job_id) for _ in range(3)))

    assert {(item.action, item.status) for item in again} == {
        (CancelAction.UNCHANGED, JobStatus.CANCELLING)
    }
    assert len(await events(database, job_id, "job.cancel_requested")) == 1


@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
async def test_cancelling_a_finished_job_changes_nothing(
    database: Database, status: JobStatus
) -> None:
    job_id, [seed] = await new_job(database)
    await execute(
        database,
        "UPDATE crawl_urls SET state = 'cancelled', outcome = 'test', finished_at = now()"
        " WHERE id = :id",
        id=seed,
    )
    await execute(
        database,
        "UPDATE crawl_jobs SET status = :status, finished_at = now(),"
        " cancel_requested_at = CASE WHEN :status = 'cancelled' THEN now() END,"
        " failure_reason = CASE WHEN :status = 'failed' THEN 'test' END WHERE id = :id",
        id=job_id,
        status=status.value,
    )

    result = await cancel(database, job_id)

    assert (result.action, result.status) == (CancelAction.UNCHANGED, status)
    assert (await job_row(database, job_id)).status == status.value
    assert await events(database, job_id, "job.cancel_requested") == []


async def test_cancelling_an_unknown_job_is_an_error(database: Database) -> None:
    with pytest.raises(NotFoundError):
        await cancel(database, uuid.uuid4())


async def test_concurrent_cancellations_request_once(database: Database) -> None:
    job_id, _, _ = await in_flight_job(database)

    results = await asyncio.gather(*(cancel(database, job_id) for _ in range(6)))

    actions = sorted(result.action.value for result in results)
    assert actions == ["cancelling"] + ["unchanged"] * 5
    assert len(await events(database, job_id, "job.cancel_requested")) == 1


# --- After the request: nothing new starts ---------------------------------------------------


async def test_a_cancelling_job_dispatches_claims_and_advances_nothing(database: Database) -> None:
    job_id, [seed, other, _], _lease = await in_flight_job(database)
    await cancel(database, job_id)

    assert await dispatch(database, job_id) == []
    assert await claim(database, other, "worker-b") is None  # a stale message
    await expire_lease(database, seed)
    assert await claim(database, seed, "worker-b") is None  # no takeover for a new attempt
    async with database.session() as session:
        assert (await advance_job(session, job_id)).action is AdvanceAction.NOT_RUNNING
    assert (await url_row(database, other)).state == "cancelled"


async def test_an_in_flight_result_is_kept_but_admits_no_redirect(database: Database) -> None:
    job_id, [_seed, *_], lease = await in_flight_job(database)
    await cancel(database, job_id)

    redirect = UrlResult(
        state=UrlState.FETCHED,
        outcome="redirect",
        http_status=301,
        redirect_target="https://example.test/moved",
        follow_redirect=True,
    )
    recorded = await record(database, lease, redirect)

    assert recorded.status is RecordStatus.RECORDED
    assert recorded.redirect_url_id is None  # admission needs a running job
    assert (await settle(database, job_id)).finalized
    assert await states(database, job_id) == {"fetched": 1, "cancelled": 2}
    assert (await job_row(database, job_id)).status == "cancelled"


async def test_a_retry_scheduled_during_cancellation_is_cancelled(database: Database) -> None:
    job_id, [seed, *_], lease = await in_flight_job(database)
    await cancel(database, job_id)

    assert (await record(database, lease, timeout(), retry_delay=0.0)).status is (
        RecordStatus.RETRY_SCHEDULED
    )
    assert (await url_row(database, seed)).state == "pending"
    settled = await settle(database, job_id)

    assert (settled.cancelled_urls, settled.finalized) == (1, True)
    assert await states(database, job_id) == {"cancelled": 3}


# --- In-flight URLs end under fencing ------------------------------------------------------


async def test_a_worker_records_its_own_url_cancelled_with_its_token(database: Database) -> None:
    job_id, [seed, *_], lease = await in_flight_job(database)
    await cancel(database, job_id)

    assert await cancel_claimed(database, lease)

    row = await url_row(database, seed)
    assert (row.state, row.outcome, row.lease_owner, row.result_epoch) == (
        "cancelled",
        CANCELLED_OUTCOME,
        None,
        lease.token,
    )
    assert not await cancel_claimed(database, lease)  # once
    assert (await record(database, lease, html_result())).status is RecordStatus.FENCED
    assert (await settle(database, job_id)).finalized
    assert (await job_row(database, job_id)).status == "cancelled"


async def test_a_stale_worker_cannot_cancel_a_url_it_lost(database: Database) -> None:
    job_id, [seed, *_], stale = await in_flight_job(database)
    await expire_lease(database, seed)
    current = await claim(database, seed, "worker-b")
    assert current is not None
    await cancel(database, job_id)

    assert not await cancel_claimed(database, stale)

    row = await url_row(database, seed)
    assert (row.state, row.lease_owner, row.lease_epoch) == ("in_progress", "worker-b", 2)
    assert await cancel_claimed(database, current)


async def test_a_url_is_cancelled_only_while_its_job_is_cancelling(database: Database) -> None:
    _job_id, [seed, *_], lease = await in_flight_job(database)
    assert not await cancel_claimed(database, lease)  # the job is running
    assert (await url_row(database, seed)).state == "in_progress"


async def test_a_crashed_workers_url_is_cancelled_when_its_lease_expires(
    database: Database,
) -> None:
    job_id, [seed, *_], lease = await in_flight_job(database)
    await cancel(database, job_id)
    assert not (await settle(database, job_id)).finalized

    await expire_lease(database, seed)
    settled = await settle(database, job_id)

    assert (settled.cancelled_leases, settled.finalized) == (1, True)
    row = await url_row(database, seed)
    assert (row.state, row.lease_owner, row.lease_expires_at) == ("cancelled", None, None)
    # If the worker was only frozen, its late result is fenced out.
    assert (await record(database, lease, html_result())).status is RecordStatus.FENCED
    assert (await job_row(database, job_id)).status == "cancelled"


async def test_concurrent_finalizers_finish_the_job_once(database: Database) -> None:
    job_id, _, lease = await in_flight_job(database)
    await cancel(database, job_id)
    await cancel_claimed(database, lease)

    results = await asyncio.gather(*(settle(database, job_id) for _ in range(5)))

    assert sum(result.finalized for result in results) == 1
    assert len(await events(database, job_id, "job.cancelled")) == 1


async def test_the_scheduler_finds_jobs_being_cancelled(database: Database) -> None:
    running, _, _ = await in_flight_job(database)
    cancelling, _, _ = await in_flight_job(database)
    await cancel(database, cancelling)

    async with database.session() as session:
        assert await cancelling_jobs(session, limit=10) == [cancelling]
        assert await jobs_being_cancelled(session, [running, cancelling]) == {cancelling}


# --- Races and lock order (separate connections) ------------------------------------------------


async def test_a_claim_racing_a_cancellation_never_escapes_it(database: Database) -> None:
    """Either the claim commits first (its URL is in flight: the job waits for it), or the
    cancellation does (the claim is refused). The job ends cancelled either way."""
    for _ in range(15):
        job_id, [seed] = await new_job(database)
        await dispatch(database, job_id)

        result, lease = await asyncio.gather(cancel(database, job_id), claim(database, seed))

        if lease is None:
            assert result.status is JobStatus.CANCELLED
            assert (await url_row(database, seed)).state == "cancelled"
        else:
            assert result.status is JobStatus.CANCELLING
            assert (await url_row(database, seed)).state == "in_progress"
            assert await cancel_claimed(database, lease)
            assert (await settle(database, job_id)).finalized
        assert (await job_row(database, job_id)).status == "cancelled"


async def test_a_cancellation_waits_for_a_dispatch_in_flight(database: Database) -> None:
    """The dispatcher publishes while it holds the row locks (ADR-0016). The cancellation
    waits for its commit, then cancels the URLs it just queued: no deadlock, nothing escapes."""
    job_id, url_ids = await new_job(database, urls=2)
    publishing = asyncio.Event()
    release = asyncio.Event()

    async def slow_publish(tasks: Any) -> list[str]:
        publishing.set()
        await release.wait()
        return [f"1-{index}" for index in range(len(tasks))]

    dispatching = asyncio.create_task(dispatch(database, job_id, publish=slow_publish))
    await publishing.wait()
    cancelling = asyncio.create_task(cancel(database, job_id))
    await until_a_session_waits_for_a_lock(database)
    assert not cancelling.done()  # waiting for the dispatched rows
    release.set()

    assert sorted(await asyncio.wait_for(dispatching, timeout=5)) == sorted(url_ids)
    result = await asyncio.wait_for(cancelling, timeout=5)
    assert (result.status, result.cancelled_urls) == (JobStatus.CANCELLED, 3)
    assert await states(database, job_id) == {"cancelled": 3}


async def test_a_cancellation_never_waits_for_a_result_being_recorded(database: Database) -> None:
    """Lock order: a result transaction holds its URL row and may then ask for the job row (to
    admit a redirect). T7 holds the job row first, so it must not wait for that URL row."""
    job_id, [seed, *_], lease = await in_flight_job(database)
    async with database.engine.connect() as other:
        transaction = await other.begin()
        await other.execute(  # the worker's fenced update, not committed yet
            text(
                "UPDATE crawl_urls SET state = 'fetched', outcome = 'redirect', http_status = 301,"
                " result_epoch = :token, lease_owner = NULL, lease_expires_at = NULL,"
                " finished_at = clock_timestamp()"
                " WHERE id = :id AND state = 'in_progress' AND lease_epoch = :token"
            ),
            {"id": seed, "token": lease.token},
        )
        result = await asyncio.wait_for(cancel(database, job_id), timeout=5)
        assert result.status is JobStatus.CANCELLING  # the URL is still in progress (committed)
        # The result transaction can now take the job row lock, as a redirect admission would.
        await asyncio.wait_for(
            other.execute(
                text("SELECT status FROM crawl_jobs WHERE id = :id FOR UPDATE"), {"id": job_id}
            ),
            timeout=5,
        )
        await transaction.commit()
    assert (await settle(database, job_id)).finalized


async def test_a_cancellation_and_a_quarantine_of_a_queued_url_never_deadlock(
    database: Database,
) -> None:
    """Regression (Phase 6 self-review, lock order). A quarantine (poison message) locks its
    queued URL row first; its event insert then checks the foreign key to the job row, which
    takes FOR KEY SHARE on it. T7 locks the job row first and then waits for that URL row. With
    FOR UPDATE on the job row the two deadlocked; FOR NO KEY UPDATE serializes T7 with every
    other job-row writer without blocking foreign-key checks."""
    job_id, [seed, _] = await new_job(database, urls=1)
    await dispatch(database, job_id)  # both URLs queued
    async with database.engine.connect() as quarantine:
        transaction = await quarantine.begin()
        # The quarantine's first statement: the URL row lock.
        await quarantine.execute(
            text("SELECT 1 FROM crawl_urls WHERE id = :id FOR NO KEY UPDATE"), {"id": seed}
        )
        cancelling = asyncio.create_task(cancel(database, job_id))
        await until_a_session_waits_for_a_lock(database)
        assert not cancelling.done()  # T7 holds the job row and waits for the URL row
        # The quarantine's event insert: the foreign-key check on the job row must not wait.
        await asyncio.wait_for(
            quarantine.execute(
                text(
                    "INSERT INTO crawl_events (job_id, crawl_url_id, event_type, detail)"
                    " VALUES (:job, :url, 'url.dead_lettered', '{}')"
                ),
                {"job": job_id, "url": seed},
            ),
            timeout=5,
        )
        await transaction.commit()
    result = await asyncio.wait_for(cancelling, timeout=5)
    assert (result.status, result.cancelled_urls) == (JobStatus.CANCELLED, 2)
    assert await states(database, job_id) == {"cancelled": 2}


# --- The API ---------------------------------------------------------------------------------


async def test_the_cancel_endpoint(crawl_client: TestClient, database: Database) -> None:
    job_id, [_seed, *_], lease = await in_flight_job(database)

    first = crawl_client.post(f"/api/v1/crawls/{job_id}/cancel")
    assert first.status_code == 202
    assert first.json()["status"] == "cancelling"
    assert first.headers["content-type"] == "application/json"
    assert crawl_client.post(f"/api/v1/crawls/{job_id}/cancel").status_code == 202  # idempotent
    assert crawl_client.get(f"/api/v1/crawls/{job_id}").json()["status"] == "cancelling"

    assert await cancel_claimed(database, lease)
    assert (await settle(database, job_id)).finalized

    done = crawl_client.post(f"/api/v1/crawls/{job_id}/cancel")
    assert done.status_code == 200
    assert done.json()["status"] == "cancelled"
    assert done.json()["finished_at"] is not None


async def test_the_cancel_endpoint_cancels_a_queued_job_at_once(
    crawl_client: TestClient, database: Database
) -> None:
    created = crawl_client.post(
        "/api/v1/crawls",
        json={"seed_url": "https://docs.example.com/", "max_depth": 1, "max_pages": 10},
    ).json()

    cancelled = crawl_client.post(f"/api/v1/crawls/{created['id']}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    progress = crawl_client.get(f"/api/v1/crawls/{created['id']}").json()["progress"]
    assert progress["urls_by_state"]["cancelled"] == 1


async def test_the_cancel_endpoint_reports_unknown_and_invalid_jobs(
    crawl_client: TestClient,
) -> None:
    unknown = crawl_client.post(f"/api/v1/crawls/{uuid.uuid4()}/cancel")
    assert unknown.status_code == 404
    assert unknown.headers["content-type"] == "application/problem+json"
    assert unknown.json()["code"] == "not_found"
    invalid = crawl_client.post("/api/v1/crawls/not-a-uuid/cancel")
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "validation_failed"
