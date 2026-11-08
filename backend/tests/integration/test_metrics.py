"""GET /metrics against the real PostgreSQL and Redis (Phase 10, ADR-0021): state counted at
the source, the streams, and the processes' heartbeats, rendered once each."""

import uuid
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from app.core.config import Settings
from app.db.models import UrlState
from app.db.session import Database
from app.main import create_app
from app.observability.exposition import CONTENT_TYPE
from app.observability.heartbeat import HeartbeatPublisher
from app.observability.metrics import Registry
from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream
from app.services.crawl_state import Lease, UrlResult, claim_url, dispatch_urls, record_result
from tests.crawl_support import age_dispatch, expire_lease, new_job, states
from tests.index_support import add_page
from tests.integration.helpers import unused_port

pytestmark = pytest.mark.anyio


async def claim(database: Database, url_id: uuid.UUID) -> Lease:
    async with database.session() as session:
        lease = await claim_url(
            session, url_id, owner="crawler-test", lease_seconds=60, max_attempts=3
        )
    assert lease is not None
    return lease


def sample(lines: Sequence[str], name: str) -> float:
    return next(float(line.split()[-1]) for line in lines if line.split()[0] == name)


async def test_metrics_count_state_at_the_source_and_render_streams_and_processes(
    test_database: Settings, database: Database, redis_client: Redis, redis_prefix: str
) -> None:
    job_id, [seed, retried, oldest, _queued] = await new_job(database, urls=3)
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    await stream.ensure_group()

    async def publish(tasks: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> list[str]:
        return await stream.publish([CrawlTask(job, url) for job, url in tasks])

    async with database.session() as session:
        await dispatch_urls(session, job_id, limit=10, publish=publish)
    await claim(database, seed)
    await expire_lease(database, seed, seconds_ago=5)  # waiting for a takeover
    lease = await claim(database, retried)
    async with database.session() as session:  # a failed attempt, retried in ten minutes
        await record_result(
            session,
            lease,
            UrlResult(UrlState.FAILED, "read_timeout"),
            max_redirect_hops=5,
            retry_delay=600,
        )
    await age_dispatch(database, oldest, seconds=30)
    await add_page(database, job_id, path="stored")  # a fetched URL, its pending document
    registry = Registry()
    registry.counter("sitescope_tasks_handled_total", "Handled.", ("reason",)).inc(
        2, reason="completed"
    )
    await HeartbeatPublisher(
        redis_client,
        key_prefix=redis_prefix,
        name="crawler-h-1-aa",
        role="crawler",
        interval=5,
        state=lambda: {"in_flight": 1, "last_progress_seconds_ago": 2.0},
        registry=registry,
    ).beat()
    settings = test_database.model_copy(update={"redis_key_prefix": redis_prefix})

    with TestClient(create_app(settings)) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    assert response.headers["cache-control"] == "no-store"
    lines = response.text.splitlines()
    assert 'sitescope_source_up{source="postgres"} 1' in lines
    assert 'sitescope_source_up{source="redis"} 1' in lines
    counted = await states(database, job_id)
    assert counted == {"in_progress": 1, "pending": 1, "queued": 2, "fetched": 1}
    for state in UrlState:
        assert (
            f'sitescope_crawl_urls{{state="{state.value}"}} {counted.get(state.value, 0)}' in lines
        )
    assert 'sitescope_crawl_jobs{status="running"} 1' in lines
    assert sample(lines, "sitescope_crawl_urls_waiting_for_retry") == 1
    assert sample(lines, "sitescope_crawl_leases_expired") == 1
    assert 29 <= sample(lines, "sitescope_crawl_oldest_queued_seconds") < 300
    assert 'sitescope_search_documents{status="pending"} 1' in lines
    # Four tasks published, none read by a consumer yet.
    assert 'sitescope_stream_entries{stream="crawl"} 4' in lines
    assert 'sitescope_stream_undelivered{stream="crawl"} 4' in lines
    assert 'sitescope_stream_pending{stream="crawl"} 0' in lines
    labels = 'role="crawler",process="crawler-h-1-aa"'
    assert f"sitescope_process_up{{{labels}}} 1" in lines
    assert f"sitescope_process_in_flight_tasks{{{labels}}} 1" in lines
    assert f'sitescope_tasks_handled_total{{{labels},reason="completed"}} 2' in lines
    # Every family is announced once.
    types = [line for line in lines if line.startswith("# TYPE ")]
    assert len(types) == len(set(types))


async def test_metrics_report_a_redis_outage_and_still_count_postgresql(
    test_database: Settings,
) -> None:
    settings = test_database.model_copy(update={"redis_port": unused_port()})

    with TestClient(create_app(settings)) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    lines = response.text.splitlines()
    assert 'sitescope_source_up{source="redis"} 0' in lines
    assert 'sitescope_source_up{source="postgres"} 1' in lines
    assert any(line.startswith("sitescope_crawl_urls{") for line in lines)
    assert not any(line.startswith("sitescope_process_up") for line in lines)
