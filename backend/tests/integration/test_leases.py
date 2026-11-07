"""Leases and fencing tokens on crawl_urls (ADR-0003).

The SQL statements below are the claim and commit patterns the Phase 5 worker will use. Here
they verify that the schema supports them, and that PostgreSQL itself rejects stale results.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.db.models import CrawlUrl, JobStatus, Page
from app.db.session import Database
from tests.integration.helpers import add_job_with_seed, new_page

pytestmark = pytest.mark.anyio

# Claim: queued -> in_progress, or take over an in_progress row whose lease has expired by the
# database clock. Increments the fencing token and clears the previous outcome marker.
CLAIM = text(
    """
    UPDATE crawl_urls AS u
    SET state = 'in_progress',
        lease_epoch = u.lease_epoch + 1,
        attempt_count = u.attempt_count + 1,
        lease_owner = :owner,
        lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds),
        result_epoch = NULL
    FROM crawl_jobs AS j
    WHERE u.id = :url_id
      AND j.id = u.job_id AND j.status = 'running'
      AND (u.state = 'queued'
           OR (u.state = 'in_progress' AND u.lease_expires_at < clock_timestamp()))
    RETURNING u.lease_epoch
    """
)

# Fenced commit: only the current lease holder can record the outcome.
COMPLETE = text(
    """
    UPDATE crawl_urls
    SET state = 'fetched', http_status = 200, outcome = 'html_stored', result_epoch = :epoch,
        lease_owner = NULL, lease_expires_at = NULL, finished_at = clock_timestamp()
    WHERE id = :url_id AND state = 'in_progress' AND lease_epoch = :epoch
    RETURNING id
    """
)

# The same update written by a buggy worker that forgot the fencing condition.
COMPLETE_WITHOUT_FENCING = text(
    """
    UPDATE crawl_urls
    SET state = 'fetched', http_status = 200, outcome = 'html_stored', result_epoch = :epoch,
        lease_owner = NULL, lease_expires_at = NULL, finished_at = clock_timestamp()
    WHERE id = :url_id
    """
)

# A buggy claim that grants the lease without issuing a new fencing token.
CLAIM_WITHOUT_NEW_TOKEN = text(
    """
    UPDATE crawl_urls AS u
    SET state = 'in_progress',
        attempt_count = u.attempt_count + 1,
        lease_owner = :owner,
        lease_expires_at = clock_timestamp() + make_interval(secs => 90),
        result_epoch = NULL
    WHERE u.id = :url_id
      AND (u.state = 'queued'
           OR (u.state = 'in_progress' AND u.lease_expires_at < clock_timestamp()))
    RETURNING u.lease_epoch
    """
)

# A stale worker that records its outcome under a newer epoch it issues to itself. Every CHECK
# constraint holds (result_epoch = lease_epoch), so only a transition rule can reject it.
COMPLETE_WITH_SELF_ISSUED_EPOCH = text(
    """
    UPDATE crawl_urls
    SET state = 'fetched', http_status = 200, outcome = 'html_stored',
        lease_epoch = lease_epoch + 1, result_epoch = lease_epoch + 1,
        lease_owner = NULL, lease_expires_at = NULL, finished_at = clock_timestamp()
    WHERE id = :url_id
    """
)


async def queued_url(database: Database) -> CrawlUrl:
    """A running job whose seed URL has been published to the crawl stream."""
    async with database.transaction() as session:
        _, seed = await add_job_with_seed(session, status=JobStatus.RUNNING)
        await session.execute(
            update(CrawlUrl)
            .where(CrawlUrl.id == seed.id)
            .values(state="queued", dispatched_at=func.now(), stream_entry_id="1730880000000-0")
        )
    return seed


async def claim(database: Database, url_id: uuid.UUID, owner: str) -> int | None:
    async with database.transaction() as session:
        epoch: int | None = (
            await session.execute(CLAIM, {"url_id": url_id, "owner": owner, "lease_seconds": 90})
        ).scalar_one_or_none()
    return epoch


async def expire_lease(database: Database, url_id: uuid.UUID) -> None:
    async with database.transaction() as session:
        await session.execute(
            text(
                "UPDATE crawl_urls SET lease_expires_at = clock_timestamp() - interval '1 second'"
                " WHERE id = :url_id"
            ),
            {"url_id": url_id},
        )


async def load(database: Database, url_id: uuid.UUID) -> CrawlUrl:
    async with database.session() as session:
        url = await session.get(CrawlUrl, url_id)
    assert url is not None
    return url


async def update_url(database: Database, url_id: uuid.UUID, **values: Any) -> None:
    async with database.transaction() as session:
        await session.execute(update(CrawlUrl).where(CrawlUrl.id == url_id).values(**values))


async def test_claim_increments_the_fencing_token_and_sets_a_lease(database: Database) -> None:
    url = await queued_url(database)

    epoch = await claim(database, url.id, "worker-a")

    stored = await load(database, url.id)
    assert epoch == 1
    assert stored.state == "in_progress"
    assert (stored.lease_epoch, stored.attempt_count, stored.lease_owner) == (1, 1, "worker-a")
    assert stored.lease_expires_at is not None


async def test_a_valid_lease_cannot_be_claimed_again(database: Database) -> None:
    url = await queued_url(database)
    await claim(database, url.id, "worker-a")

    assert await claim(database, url.id, "worker-b") is None


async def test_an_expired_lease_is_taken_over_with_a_new_epoch(database: Database) -> None:
    url = await queued_url(database)
    await claim(database, url.id, "worker-a")
    await expire_lease(database, url.id)

    epoch = await claim(database, url.id, "worker-b")

    stored = await load(database, url.id)
    assert epoch == 2
    assert (stored.lease_owner, stored.attempt_count) == ("worker-b", 2)


async def test_the_current_lease_holder_commits_its_result(database: Database) -> None:
    url = await queued_url(database)
    epoch = await claim(database, url.id, "worker-a")

    async with database.transaction() as session:
        completed = (
            await session.execute(COMPLETE, {"url_id": url.id, "epoch": epoch})
        ).scalar_one_or_none()

    stored = await load(database, url.id)
    assert completed == url.id
    assert (stored.state, stored.result_epoch, stored.lease_owner) == ("fetched", 1, None)
    assert stored.finished_at is not None


async def test_a_stale_worker_commit_matches_no_row(database: Database) -> None:
    url = await queued_url(database)
    stale_epoch = await claim(database, url.id, "worker-a")
    await expire_lease(database, url.id)
    await claim(database, url.id, "worker-b")

    async with database.transaction() as session:
        completed = (
            await session.execute(COMPLETE, {"url_id": url.id, "epoch": stale_epoch})
        ).scalar_one_or_none()

    assert completed is None  # fenced out: the worker must discard its result
    assert (await load(database, url.id)).state == "in_progress"


async def test_database_rejects_a_stale_result_and_rolls_back_its_writes(
    database: Database,
) -> None:
    """Even if a worker forgets the fencing condition, PostgreSQL rejects the stale result."""
    url = await queued_url(database)
    stale_epoch = await claim(database, url.id, "worker-a")
    await expire_lease(database, url.id)
    await claim(database, url.id, "worker-b")  # takeover: lease_epoch is now 2

    async def stale_worker_commit() -> None:
        # The stale worker writes its page and its outcome in one transaction.
        async with database.transaction() as session:
            session.add(new_page(url.job_id, url))
            await session.flush()
            parameters = {"url_id": url.id, "epoch": stale_epoch}
            await session.execute(COMPLETE_WITHOUT_FENCING, parameters)

    with pytest.raises(IntegrityError, match="ck_crawl_urls_result_epoch_is_current"):
        await stale_worker_commit()

    async with database.session() as session:
        pages = (await session.execute(select(func.count()).select_from(Page))).scalar_one()
    assert pages == 0  # the page write was rolled back together with the rejected outcome
    assert (await load(database, url.id)).lease_owner == "worker-b"


async def test_lease_epoch_can_never_decrease(database: Database) -> None:
    url = await queued_url(database)
    await claim(database, url.id, "worker-a")

    with pytest.raises(IntegrityError, match="cannot decrease"):
        await update_url(database, url.id, lease_epoch=0)


async def test_in_progress_requires_a_lease(database: Database) -> None:
    url = await queued_url(database)

    with pytest.raises(IntegrityError, match="ck_crawl_urls_lease_iff_in_progress"):
        await update_url(database, url.id, state="in_progress")


async def test_a_worker_outcome_must_name_its_lease_epoch(database: Database) -> None:
    url = await queued_url(database)
    await claim(database, url.id, "worker-a")

    with pytest.raises(IntegrityError, match="ck_crawl_urls_worker_outcome_has_result_epoch"):
        await update_url(
            database,
            url.id,
            state="failed",
            lease_owner=None,
            lease_expires_at=None,
            finished_at=func.now(),
        )


async def test_a_claim_must_clear_the_previous_outcome(database: Database) -> None:
    url = await queued_url(database)
    await claim(database, url.id, "worker-a")
    # Retryable failure: back to pending, recording which epoch decided it.
    await update_url(
        database,
        url.id,
        state="queued",
        result_epoch=1,
        lease_owner=None,
        lease_expires_at=None,
        dispatched_at=func.now(),
    )

    # A claim that forgets to clear result_epoch leaves a stale outcome marker: rejected.
    with pytest.raises(IntegrityError, match="ck_crawl_urls_result_epoch"):
        await update_url(
            database,
            url.id,
            state="in_progress",
            lease_epoch=2,
            lease_owner="worker-b",
            lease_expires_at=func.now(),
        )
    # The real claim statement clears it and succeeds.
    assert await claim(database, url.id, "worker-b") == 2


# --- Fencing tokens are issued only by claims (migration 0002, ADR-0011) --------------------------
#
# Regression tests for gaps found in the Phase 2C review. Under migration 0001 alone, each
# statement below was accepted, and fencing silently stopped working:
#
# * a claim without a new token leaves the previous holder's token valid, so a stale worker's
#   correctly fenced commit (WHERE lease_epoch = :token) still matches the row;
# * a stale worker can issue itself a newer epoch and record its outcome under it, which
#   satisfies every CHECK constraint (result_epoch = lease_epoch).


async def execute(database: Database, statement: Any, **parameters: Any) -> None:
    async with database.transaction() as session:
        await session.execute(statement, parameters)


async def test_a_claim_must_issue_a_new_fencing_token(database: Database) -> None:
    url = await queued_url(database)

    with pytest.raises(IntegrityError, match="requires a new lease_epoch"):
        await execute(database, CLAIM_WITHOUT_NEW_TOKEN, url_id=url.id, owner="worker-a")

    assert (await load(database, url.id)).state == "queued"


async def test_a_takeover_must_issue_a_new_fencing_token(database: Database) -> None:
    url = await queued_url(database)
    stale_epoch = await claim(database, url.id, "worker-a")
    await expire_lease(database, url.id)

    # Accepted under 0001: worker-b would then share epoch 1 with the stalled worker-a, whose
    # fenced commit would still match the row and overwrite worker-b's lease.
    with pytest.raises(IntegrityError, match="requires a new lease_epoch"):
        await execute(database, CLAIM_WITHOUT_NEW_TOKEN, url_id=url.id, owner="worker-b")

    stored = await load(database, url.id)
    assert (stored.lease_owner, stored.lease_epoch) == ("worker-a", stale_epoch)


async def test_a_stale_worker_cannot_issue_itself_a_newer_epoch(database: Database) -> None:
    url = await queued_url(database)
    await claim(database, url.id, "worker-a")
    await expire_lease(database, url.id)
    current_epoch = await claim(database, url.id, "worker-b")

    # Accepted under 0001: the stale worker's result replaced worker-b's lease.
    with pytest.raises(IntegrityError, match="may only advance by one, in a claim"):
        await execute(database, COMPLETE_WITH_SELF_ISSUED_EPOCH, url_id=url.id)

    # The current lease holder still commits normally.
    async with database.transaction() as session:
        completed = (
            await session.execute(COMPLETE, {"url_id": url.id, "epoch": current_epoch})
        ).scalar_one_or_none()
    assert completed == url.id
    assert (await load(database, url.id)).result_epoch == current_epoch


async def test_a_claim_advances_the_fencing_token_by_exactly_one(database: Database) -> None:
    url = await queued_url(database)

    with pytest.raises(IntegrityError, match="may only advance by one, in a claim"):
        await update_url(
            database,
            url.id,
            state="in_progress",
            lease_epoch=2,
            lease_owner="worker-a",
            lease_expires_at=func.now(),
        )


async def test_leaving_in_progress_keeps_the_fencing_token(database: Database) -> None:
    """Releases for retry, results and cancellation never change lease_epoch; they still work."""
    url = await queued_url(database)
    epoch = await claim(database, url.id, "worker-a")

    # Retryable failure: the lease holder releases the URL for a later attempt.
    await update_url(
        database,
        url.id,
        state="pending",
        result_epoch=epoch,
        lease_owner=None,
        lease_expires_at=None,
        next_attempt_at=func.now(),
    )
    # The reconciler cancels a URL whose job is being cancelled.
    await update_url(database, url.id, state="cancelled", finished_at=func.now())

    stored = await load(database, url.id)
    assert (stored.state, stored.lease_epoch) == ("cancelled", epoch)
