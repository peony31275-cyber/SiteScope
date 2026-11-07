"""PostgreSQL sessions of the long-running processes end themselves when idle in a transaction.

A process frozen inside a transaction (a VM pause, a suspended process) would otherwise keep its
row locks: a frozen worker blocked every takeover of its URL, a frozen scheduler every claim of
the rows it was dispatching. These tests use the processes' own factories, so they also check
that the processes really set the timeouts.
"""

import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.db.session import Database
from app.scheduler import scheduler as scheduler_module
from app.services.crawl_state import claim_url, dispatch_urls
from app.workers import runtime
from tests.crawl_support import new_job

pytestmark = pytest.mark.anyio

_SETTING = text(
    "SELECT setting FROM pg_settings WHERE name = 'idle_in_transaction_session_timeout'"
)


async def session_timeout_ms(database: Database) -> int:
    try:
        async with database.session() as session:
            return int(await session.scalar(_SETTING) or 0)
    finally:
        await database.dispose()


async def test_the_processes_set_their_idle_transaction_timeouts(test_database: Settings) -> None:
    """Regression (Phase 5 review): only the scheduler had a timeout, and no test checked that
    the process actually used it."""
    worker = runtime.worker_database(test_database, application_name="sitescope-tests-worker")
    scheduler = scheduler_module.scheduler_database(test_database)

    assert await session_timeout_ms(worker) == runtime.idle_transaction_timeout_ms(test_database)
    assert await session_timeout_ms(scheduler) == scheduler_module.idle_transaction_timeout_ms(
        test_database
    )


async def test_a_worker_frozen_inside_its_claim_does_not_block_a_takeover(
    test_database: Settings, database: Database
) -> None:
    """Regression (Phase 5 review): a worker frozen between its claim's row lock and its commit
    kept the lock, and every other claim of the URL waited (then failed) for as long as it was
    frozen. Now PostgreSQL ends the frozen session after the lease margin (1 s here)."""
    settings = test_database.model_copy(update={"crawl_lease_margin_seconds": 1.0})
    job_id, [seed] = await new_job(database)
    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=1)
    frozen = runtime.worker_database(settings, application_name="sitescope-tests-frozen")
    try:
        async with frozen.session() as session:
            await session.begin()
            # The claim's first statement; then the process "freezes" (idle in the transaction).
            await session.execute(
                text("SELECT 1 FROM crawl_urls WHERE id = :id FOR NO KEY UPDATE"), {"id": seed}
            )
            started = time.monotonic()
            async with database.session() as other:
                lease = await claim_url(
                    other, seed, owner="crawler-other", lease_seconds=60, max_attempts=3
                )
            waited = time.monotonic() - started
            with pytest.raises(DBAPIError):
                await session.execute(text("SELECT 1"))  # its session was ended
    finally:
        await frozen.dispose()

    assert lease is not None
    assert lease.owner == "crawler-other"
    assert 0.5 < waited < 10  # it waited for the frozen session, not for the freeze to end
