"""The read-only diagnostics command (app/scheduler/status.py) against real services."""

import pytest
from redis.asyncio import Redis

from app.core.config import Settings
from app.db.models import JobStatus
from app.db.session import Database
from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream
from app.scheduler.status import collect
from app.services.crawl_state import claim_url
from tests.crawl_support import execute, new_job, url_row
from tests.integration.helpers import unused_port

pytestmark = pytest.mark.anyio


async def test_the_report_shows_jobs_leases_and_the_stream(
    test_database: Settings, database: Database, redis_client: Redis, redis_prefix: str
) -> None:
    job_id, [seed, other] = await new_job(database, urls=1)
    await new_job(database, status=JobStatus.QUEUED)
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    await stream.ensure_group()
    await stream.publish([CrawlTask(job_id, seed), CrawlTask(job_id, other)])
    await stream.read("crawler-a", count=1, block_ms=100)
    await execute(
        database,
        "UPDATE crawl_urls SET state = 'queued', dispatched_at = clock_timestamp()"
        " WHERE job_id = :id",
        id=job_id,
    )
    async with database.session() as session:
        lease = await claim_url(session, seed, owner="crawler-a", lease_seconds=60, max_attempts=3)
    assert lease is not None
    before = await url_row(database, seed)

    report = await collect(test_database.model_copy(update={"redis_key_prefix": redis_prefix}))

    database_part, stream_part = report["database"], report["stream"]
    assert database_part["jobs_by_status"] == {"running": 1, "queued": 1}
    [active] = database_part["active_jobs"]
    assert active["job_id"] == str(job_id)
    assert active["urls_by_state"] == {"in_progress": 1, "queued": 1}
    assert database_part["live_leases_by_owner"] == {"crawler-a": 1}
    assert database_part["expired_leases"] == 0
    assert database_part["oldest_queued_seconds"] is not None
    assert stream_part["length"] == 2
    assert stream_part["pending"]["per_consumer"] == {"crawler-a": 1}
    assert stream_part["group"]["lag"] == 1
    after = await url_row(database, seed)  # read-only: nothing changed
    assert (after.state, after.lease_epoch, after.updated_at) == (
        before.state,
        before.lease_epoch,
        before.updated_at,
    )


async def test_the_report_names_an_unreachable_service_without_failing(
    test_database: Settings, redis_prefix: str
) -> None:
    report = await collect(
        test_database.model_copy(
            update={
                "redis_key_prefix": redis_prefix,
                "redis_port": unused_port(),
                "redis_socket_timeout_seconds": 1.0,
            }
        )
    )

    assert "error" in report["stream"]
    assert "jobs_by_status" in report["database"]
