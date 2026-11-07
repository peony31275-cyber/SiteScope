"""Retry scheduling and poison quarantine against real PostgreSQL (Phase 6, ADR-0017).

``record_result(..., retry_delay=...)`` is the retry branch of T5: the same fenced update as a
result, back to ``pending`` with ``next_attempt_at`` by the database clock. These tests check the
transition itself, the fencing around it, BFS level blocking, rollback, and races with
takeovers, dispatchers and duplicate deliveries on separate connections.
"""

import asyncio
import math
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event, text

from app.db.models import CrawlUrl, JobStatus, UrlState
from app.db.session import Database
from app.services.crawl_state import (
    EXHAUSTED_OUTCOME,
    MAX_DELIVERIES_OUTCOME,
    AdvanceAction,
    Lease,
    RecordStatus,
    UrlResult,
    advance_job,
    claim_url,
    dispatch_urls,
    quarantine_url,
    record_result,
    recover_exhausted_url,
)
from app.workers.retry import RetryPolicy, RetryVerdict
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

HOPS = 5


def timeout() -> UrlResult:
    return UrlResult(UrlState.FAILED, "read_timeout", error="no data within 10 s")


def unavailable(retry_after: float | None = None) -> UrlResult:
    return UrlResult(
        UrlState.FAILED,
        "http_error",
        http_status=503,
        error="HTTP 503 from example.test",
        retry_after=retry_after,
    )


async def dispatch(database: Database, job_id: uuid.UUID) -> list[uuid.UUID]:
    async with database.session() as session:
        return await dispatch_urls(session, job_id, limit=100)


async def claim(
    database: Database, url_id: uuid.UUID, owner: str = "worker-a", *, max_attempts: int = 3
) -> Lease | None:
    async with database.session() as session:
        return await claim_url(
            session, url_id, owner=owner, lease_seconds=60.0, max_attempts=max_attempts
        )


async def leased(database: Database, job_id: uuid.UUID, url_id: uuid.UUID, owner: str) -> Lease:
    await dispatch(database, job_id)
    lease = await claim(database, url_id, owner)
    assert lease is not None
    return lease


async def record(
    database: Database, lease: Lease, result: UrlResult, *, retry_delay: float | None = None
) -> Any:
    async with database.session() as session:
        return await record_result(
            session, lease, result, max_redirect_hops=HOPS, retry_delay=retry_delay
        )


async def make_due(database: Database, url_id: uuid.UUID) -> None:
    """Move a retry's next_attempt_at into the past (as if its backoff had elapsed)."""
    await execute(
        database,
        "UPDATE crawl_urls SET next_attempt_at = clock_timestamp() - interval '1 second'"
        " WHERE id = :id AND state = 'pending'",
        id=url_id,
    )


async def seconds_until_due(database: Database, url_id: uuid.UUID) -> float:
    async with database.session() as session:
        value = await session.scalar(
            text(
                "SELECT extract(epoch FROM next_attempt_at - clock_timestamp())"
                " FROM crawl_urls WHERE id = :id"
            ),
            {"id": url_id},
        )
    return float(value)


@contextmanager
def failing_statement(database: Database, prefix: str) -> Iterator[None]:
    """Make every SQL statement that starts with ``prefix`` fail (a write error mid-transaction)."""

    def before(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().startswith(prefix):
            raise RuntimeError(f"injected failure: {prefix}")

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", before)


# --- The retry transition ------------------------------------------------------------------------


async def test_a_retry_returns_the_url_to_pending_until_its_database_time(
    database: Database,
) -> None:
    job_id, [seed] = await new_job(database)
    lease = await leased(database, job_id, seed, "worker-a")

    recorded = await record(database, lease, unavailable(retry_after=30.0), retry_delay=30.0)

    assert recorded.status is RecordStatus.RETRY_SCHEDULED
    assert recorded.next_attempt_at is not None
    row = await url_row(database, seed)
    assert row.state == "pending"
    # The lease is released, the token and the attempt count stay; no outcome yet.
    assert (row.lease_owner, row.lease_expires_at) == (None, None)
    assert (row.lease_epoch, row.attempt_count, row.result_epoch) == (1, 1, None)
    assert (row.outcome, row.finished_at) == (None, None)
    assert (row.http_status, row.last_error) == (503, "http_error: HTTP 503 from example.test")
    assert row.next_attempt_at == recorded.next_attempt_at
    assert 28.0 < await seconds_until_due(database, seed) <= 30.0  # the database clock
    [scheduled] = await events(database, job_id, "url.retry_scheduled")
    assert (scheduled.from_state, scheduled.to_state) == ("in_progress", "pending")
    assert (scheduled.worker_id, scheduled.lease_epoch) == ("worker-a", 1)
    assert scheduled.detail == {
        "attempt": 1,
        "outcome": "http_error",
        "http_status": 503,
        "delay_ms": 30000,
        "retry_after": 30.0,
    }


async def test_a_retry_is_dispatched_only_when_due(database: Database) -> None:
    job_id, [seed] = await new_job(database)
    lease = await leased(database, job_id, seed, "worker-a")
    await record(database, lease, timeout(), retry_delay=60.0)

    assert await dispatch(database, job_id) == []  # not due: no in-memory timer either
    await make_due(database, seed)
    assert await dispatch(database, job_id) == [seed]
    assert (await url_row(database, seed)).state == "queued"


async def test_the_next_attempt_is_a_new_claim_with_a_new_token(database: Database) -> None:
    job_id, [seed] = await new_job(database)
    first = await leased(database, job_id, seed, "worker-a")
    await record(database, first, timeout(), retry_delay=0.0)
    second = await leased(database, job_id, seed, "worker-b")

    assert (second.token, second.attempt) == (2, 2)
    # The first attempt's token is dead: neither a result nor another retry is accepted.
    assert (await record(database, first, html_result())).status is RecordStatus.FENCED
    assert (await record(database, first, timeout(), retry_delay=0.0)).status is (
        RecordStatus.FENCED
    )
    assert (await record(database, second, html_result())).status is RecordStatus.RECORDED
    row = await url_row(database, seed)
    assert (row.state, row.lease_epoch, row.result_epoch, row.attempt_count) == (
        "fetched",
        2,
        2,
        2,
    )


async def test_a_retry_is_scheduled_once_per_claim(database: Database) -> None:
    """A duplicate or replayed retry (the same lease) cannot schedule a second retry, and
    cannot create an attempt: only a claim counts one."""
    job_id, [seed] = await new_job(database)
    lease = await leased(database, job_id, seed, "worker-a")

    assert (await record(database, lease, timeout(), retry_delay=5.0)).status is (
        RecordStatus.RETRY_SCHEDULED
    )
    assert (await record(database, lease, timeout(), retry_delay=0.0)).status is (
        RecordStatus.FENCED
    )
    assert (await record(database, lease, html_result())).status is RecordStatus.FENCED
    row = await url_row(database, seed)
    assert (row.state, row.attempt_count, row.lease_epoch) == ("pending", 1, 1)
    assert 3.0 < await seconds_until_due(database, seed) <= 5.0  # the first delay stands
    assert len(await events(database, job_id, "url.retry_scheduled")) == 1


async def test_a_stale_worker_cannot_schedule_a_retry(database: Database) -> None:
    job_id, [seed] = await new_job(database)
    stale = await leased(database, job_id, seed, "worker-a")
    await expire_lease(database, seed)
    current = await claim(database, seed, "worker-b")
    assert current is not None

    assert (await record(database, stale, timeout(), retry_delay=0.0)).status is (
        RecordStatus.FENCED
    )
    row = await url_row(database, seed)
    assert (row.state, row.lease_owner, row.lease_epoch) == ("in_progress", "worker-b", 2)
    assert await events(database, job_id, "url.retry_scheduled") == []
    assert (await record(database, current, html_result())).status is RecordStatus.RECORDED


async def test_terminal_results_are_never_overwritten_by_a_retry(database: Database) -> None:
    job_id, [seed] = await new_job(database)
    lease = await leased(database, job_id, seed, "worker-a")
    assert (await record(database, lease, html_result())).status is RecordStatus.RECORDED

    assert (await record(database, lease, timeout(), retry_delay=0.0)).status is (
        RecordStatus.FENCED
    )
    row = await url_row(database, seed)
    assert (row.state, row.outcome, row.result_epoch) == ("fetched", "html_stored", 1)


@pytest.mark.parametrize("delay", [-1.0, math.nan, math.inf])
async def test_an_invalid_delay_writes_nothing(database: Database, delay: float) -> None:
    job_id, [seed] = await new_job(database)
    lease = await leased(database, job_id, seed, "worker-a")

    with pytest.raises(ValueError, match="invalid retry delay"):
        await record(database, lease, timeout(), retry_delay=delay)

    row = await url_row(database, seed)
    assert (row.state, row.lease_epoch) == ("in_progress", 1)


async def test_a_failed_retry_transaction_rolls_back_completely(database: Database) -> None:
    """The event insert fails after the fenced update: the whole retry rolls back, and the
    worker still holds its lease (it may record again, or the lease expires)."""
    job_id, [seed] = await new_job(database)
    lease = await leased(database, job_id, seed, "worker-a")

    with failing_statement(database, "INSERT INTO crawl_events"), pytest.raises(RuntimeError):
        await record(database, lease, timeout(), retry_delay=0.0)

    row = await url_row(database, seed)
    assert (row.state, row.lease_owner, row.lease_epoch, row.attempt_count) == (
        "in_progress",
        "worker-a",
        1,
        1,
    )
    assert row.next_attempt_at is not None
    assert await events(database, job_id, "url.retry_scheduled") == []
    assert (await record(database, lease, timeout(), retry_delay=0.0)).status is (
        RecordStatus.RETRY_SCHEDULED
    )


# --- Attempts and exhaustion ---------------------------------------------------------------------


async def test_the_last_attempt_fails_for_good_and_nothing_claims_it_again(
    database: Database,
) -> None:
    """Attempts are counted by claims only, never decrease, and the failure of the last
    allowed attempt is the URL's final result (the retry policy says EXHAUSTED)."""
    policy = RetryPolicy(max_attempts=2, base_delay=0.1, max_delay=0.1)
    job_id, [seed] = await new_job(database)
    attempts: list[int] = []
    for owner in ("worker-a", "worker-b"):
        await make_due(database, seed)
        await dispatch(database, job_id)
        lease = await claim(database, seed, owner, max_attempts=2)
        assert lease is not None
        attempts.append(lease.attempt)
        decision = policy.decide(timeout(), attempt=lease.attempt)
        recorded = await record(database, lease, timeout(), retry_delay=decision.delay)
        expected = RecordStatus.RETRY_SCHEDULED if lease.attempt < 2 else RecordStatus.RECORDED
        assert recorded.status is expected
    assert attempts == [1, 2]
    assert policy.decide(timeout(), attempt=2).verdict is RetryVerdict.EXHAUSTED
    row = await url_row(database, seed)
    assert (row.state, row.outcome, row.attempt_count, row.result_epoch) == (
        "failed",
        "read_timeout",
        2,
        2,
    )
    assert await claim(database, seed, "worker-c", max_attempts=2) is None


# --- Strict BFS ----------------------------------------------------------------------------------


async def test_a_pending_retry_blocks_the_next_level_until_it_is_final(database: Database) -> None:
    job_id, [seed, other] = await new_job(database, urls=1)
    seed_lease = await leased(database, job_id, seed, "worker-a")
    other_lease = await claim(database, other, "worker-b")
    assert other_lease is not None
    assert (await record(database, seed_lease, html_result())).status is RecordStatus.RECORDED
    await record(database, other_lease, timeout(), retry_delay=0.0)  # due at once

    async def advance() -> Any:
        async with database.session() as session:
            return await advance_job(session, job_id)

    # Due or not, a URL waiting for its retry is unfinished: the level is not done.
    assert (await advance()).action is AdvanceAction.LEVEL_IN_PROGRESS
    retry = await leased(database, job_id, other, "worker-c")
    assert (await advance()).action is AdvanceAction.LEVEL_IN_PROGRESS
    assert (await record(database, retry, html_result())).status is RecordStatus.RECORDED
    finished = await advance()
    assert finished.action is AdvanceAction.COMPLETED  # no links: the job ends after level 0
    assert (await job_row(database, job_id)).status == "completed"


# --- Races on separate connections ---------------------------------------------------------------


async def test_a_retry_racing_a_takeover_has_exactly_one_winner(database: Database) -> None:
    """The stale holder schedules a retry while another worker takes the expired lease over.
    Either the retry commits first (and the claim finds the URL pending: refused), or the claim
    does (and the retry is fenced out). Never both, never a lost attempt."""
    for _ in range(15):
        job_id, [seed] = await new_job(database)
        stale = await leased(database, job_id, seed, "worker-a")
        await expire_lease(database, seed)

        retried, takeover = await asyncio.gather(
            record(database, stale, timeout(), retry_delay=30.0),
            claim(database, seed, "worker-b"),
        )

        row = await url_row(database, seed)
        if retried.status is RecordStatus.RETRY_SCHEDULED:
            assert takeover is None
            assert (row.state, row.lease_epoch, row.attempt_count) == ("pending", 1, 1)
        else:
            assert retried.status is RecordStatus.FENCED
            assert takeover is not None
            assert takeover.token == 2
            assert (row.state, row.lease_owner, row.attempt_count) == ("in_progress", "worker-b", 2)


async def test_concurrent_dispatchers_release_a_due_retry_once(database: Database) -> None:
    job_id, [seed] = await new_job(database)
    lease = await leased(database, job_id, seed, "worker-a")
    await record(database, lease, timeout(), retry_delay=0.0)

    released = await asyncio.gather(*(dispatch(database, job_id) for _ in range(6)))

    assert sorted(len(ids) for ids in released) == [0, 0, 0, 0, 0, 1]
    assert (await url_row(database, seed)).state == "queued"


async def test_duplicate_deliveries_during_backoff_change_nothing(database: Database) -> None:
    """Messages of the first attempt, delivered again while the URL waits for its retry, find
    it pending: their claims are refused, and no attempt is counted."""
    job_id, [seed] = await new_job(database)
    lease = await leased(database, job_id, seed, "worker-a")
    await record(database, lease, timeout(), retry_delay=30.0)

    refused = await asyncio.gather(*(claim(database, seed, f"dup-{n}") for n in range(5)))

    assert refused == [None] * 5
    row = await url_row(database, seed)
    assert (row.state, row.attempt_count, row.lease_epoch) == ("pending", 1, 1)


async def test_simultaneous_failures_retry_spread_out(database: Database) -> None:
    """Many URLs failing together are retried at independent, jittered times (no storm)."""
    policy = RetryPolicy(max_attempts=3, base_delay=20.0, max_delay=60.0)
    job_id, url_ids = await new_job(database, urls=29)
    await dispatch(database, job_id)
    leases = [await claim(database, url_id, f"worker-{n % 4}") for n, url_id in enumerate(url_ids)]

    async def fail(lease: Lease | None) -> Any:
        assert lease is not None
        decision = policy.decide(unavailable(), attempt=lease.attempt)
        return await record(database, lease, unavailable(), retry_delay=decision.delay)

    results = await asyncio.gather(*(fail(lease) for lease in leases))

    assert {result.status for result in results} == {RecordStatus.RETRY_SCHEDULED}
    due = sorted([await seconds_until_due(database, url_id) for url_id in url_ids])
    assert all(-1.0 < seconds <= 20.0 for seconds in due)
    # Full jitter over [0, 20 s]: the chance that 30 delays all fall within 5 s is below 1e-15;
    # a fixed delay would put every retry at the same moment.
    assert due[-1] - due[0] > 5.0
    assert await states(database, job_id) == {"pending": 30}


# --- Poison messages: quarantine ---------------------------------------------------------------

MAX_ATTEMPTS = 3  # the limit these tests pass to claims and quarantines


def snapshot(row: CrawlUrl) -> tuple[Any, ...]:
    return (
        row.state,
        row.outcome,
        row.lease_epoch,
        row.result_epoch,
        row.attempt_count,
        row.lease_owner,
    )


async def quarantine(
    database: Database, url_id: uuid.UUID, job_id: uuid.UUID, deliveries: int = 11
) -> bool:
    async with database.session() as session:
        return await quarantine_url(
            session,
            url_id,
            job_id=job_id,
            owner="worker-q",
            deliveries=deliveries,
            max_attempts=MAX_ATTEMPTS,
        )


async def use_up_attempts(database: Database, url_id: uuid.UUID) -> None:
    """As if SITESCOPE_CRAWL_MAX_ATTEMPTS had been lowered below the URL's attempts: no claim can
    take it any more (attempt_count is a plain column; no trigger guards it)."""
    await execute(
        database,
        "UPDATE crawl_urls SET attempt_count = :n WHERE id = :id",
        n=MAX_ATTEMPTS,
        id=url_id,
    )


async def test_a_queued_url_of_a_poison_message_fails_through_a_fenced_quarantine(
    database: Database,
) -> None:
    job_id, [seed] = await new_job(database)
    await dispatch(database, job_id)
    await use_up_attempts(database, seed)
    assert await claim(database, seed) is None  # stuck: its message could never be settled

    assert await quarantine(database, seed, job_id)

    row = await url_row(database, seed)
    # A new token (the quarantine claim), no attempt counted, the failure under that token.
    assert (row.state, row.outcome) == ("failed", MAX_DELIVERIES_OUTCOME)
    assert (row.lease_epoch, row.result_epoch, row.attempt_count) == (1, 1, MAX_ATTEMPTS)
    assert row.last_error == "its task message was delivered 11 times without a result"
    [dead] = await events(database, job_id, "url.dead_lettered")
    assert (dead.from_state, dead.to_state, dead.worker_id, dead.lease_epoch) == (
        "queued",
        "failed",
        "worker-q",
        1,
    )
    assert dead.detail == {"reason": MAX_DELIVERIES_OUTCOME, "deliveries": 11}
    assert not await quarantine(database, seed, job_id)  # once only


@pytest.mark.parametrize("attempts", [0, MAX_ATTEMPTS - 1])
async def test_a_claimable_url_is_never_quarantined(database: Database, attempts: int) -> None:
    """Regression (Phase 6 independent review): delivery counts also grow while PostgreSQL is
    down, so a URL with attempts left is left to its claims, which count attempts."""
    job_id, [seed] = await new_job(database)
    await dispatch(database, job_id)
    await execute(
        database, "UPDATE crawl_urls SET attempt_count = :n WHERE id = :id", n=attempts, id=seed
    )
    before = snapshot(await url_row(database, seed))

    assert not await quarantine(database, seed, job_id)

    assert snapshot(await url_row(database, seed)) == before
    assert await claim(database, seed) is not None


async def test_an_expired_last_attempt_is_recovered_not_quarantined(database: Database) -> None:
    """An expired lease after the last attempt ends as max_attempts_exceeded through the
    recovery claim, as in Phase 4; the quarantine leaves it alone."""
    job_id, [seed] = await new_job(database)
    await dispatch(database, job_id)
    lease = await claim(database, seed, max_attempts=1)
    assert lease is not None
    await expire_lease(database, seed)

    assert not await quarantine(database, seed, job_id)
    async with database.session() as session:
        assert await recover_exhausted_url(session, seed, owner="worker-r", max_attempts=1)
    row = await url_row(database, seed)
    assert (row.state, row.outcome, row.lease_epoch, row.result_epoch) == (
        "failed",
        EXHAUSTED_OUTCOME,
        2,
        2,
    )
    # The holder of the expired lease is fenced out.
    assert (await record(database, lease, html_result())).status is RecordStatus.FENCED


@pytest.mark.parametrize("situation", ["live_lease", "pending", "finished", "job_cancelling"])
async def test_quarantine_never_touches_live_waiting_or_finished_work(
    database: Database, situation: str
) -> None:
    """Even with its attempts used up, such a URL is not quarantined: only its state counts."""
    status = JobStatus.CANCELLING if situation == "job_cancelling" else JobStatus.RUNNING
    job_id, [seed] = await new_job(database, status=status)
    if situation != "pending":  # queued as by a dispatch (also for the cancelling job)
        await execute(
            database,
            "UPDATE crawl_urls SET state = 'queued', dispatched_at = clock_timestamp()"
            " WHERE id = :id",
            id=seed,
        )
    if situation in ("live_lease", "finished"):
        lease = await claim(database, seed)
        assert lease is not None
        if situation == "finished":
            await record(database, lease, html_result())
    await use_up_attempts(database, seed)
    before = snapshot(await url_row(database, seed))

    assert not await quarantine(database, seed, job_id)

    assert snapshot(await url_row(database, seed)) == before
    assert await events(database, job_id, "url.dead_lettered") == []


async def test_quarantine_never_crosses_jobs(database: Database) -> None:
    job_id, [seed] = await new_job(database)
    await dispatch(database, job_id)
    await use_up_attempts(database, seed)
    assert not await quarantine(database, seed, uuid.uuid4())
    assert (await url_row(database, seed)).state == "queued"


async def test_the_quarantine_and_the_event_commit_together(database: Database) -> None:
    job_id, [seed] = await new_job(database)
    await dispatch(database, job_id)
    await use_up_attempts(database, seed)

    with failing_statement(database, "INSERT INTO crawl_events"), pytest.raises(RuntimeError):
        await quarantine(database, seed, job_id)

    row = await url_row(database, seed)
    assert (row.state, row.lease_epoch) == ("queued", 0)
    assert await events(database, job_id, "url.dead_lettered") == []
    assert await quarantine(database, seed, job_id)  # and it works once the failure is gone
