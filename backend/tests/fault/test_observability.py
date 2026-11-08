"""Heartbeats and GET /metrics with real processes (Phase 10, ADR-0021). Marker ``fault``.

A scheduler, two crawler workers, an index dispatcher and an indexer crawl and index a small
site. Each process must appear with its own heartbeat; the per-process counters rendered by
``/metrics`` must add up to the authoritative count in PostgreSQL (nothing missed, nothing
counted twice); a killed worker must disappear within three heartbeat intervals and a worker
stopped gracefully at once.
"""

import asyncio
import re
import time
from collections import Counter

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import Settings
from app.db.session import Database
from app.main import create_app
from app.observability.heartbeat import read_heartbeats
from tests.crawl_support import create_local_job, job_row
from tests.fetch_support import LocalHttpServer
from tests.process_support import tree_site, wait_for
from tests.service_fixtures import ClusterFactory

pytestmark = pytest.mark.anyio

HOST = "127.0.0.1"
SAMPLE = re.compile(r"^(?P<name>[a-z_]+)\{(?P<labels>[^}]*)\} (?P<value>[-+0-9.eInfa]+)$")


def samples(exposition: str, name: str) -> list[tuple[dict[str, str], float]]:
    found = []
    for line in exposition.splitlines():
        match = SAMPLE.match(line)
        if match is None or match["name"] != name:
            continue
        labels = dict(re.findall(r'(\w+)="([^"]*)"', match["labels"]))
        found.append((labels, float(match["value"])))
    return found


async def test_processes_publish_heartbeats_and_metrics_that_add_up_to_the_database(
    clusters: ClusterFactory,
    database: Database,
    test_database: Settings,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    site = tree_site(depth=2, branching=4)  # 21 pages
    async with LocalHttpServer(site.routes, host=HOST) as server:
        cluster = clusters(
            hosts=[HOST], ports=[server.port], extra={"HEARTBEAT_INTERVAL_SECONDS": "1"}
        )
        scheduler = cluster.scheduler()
        first, second = cluster.worker(), cluster.worker()
        dispatcher, indexer = cluster.index_dispatcher(), cluster.indexer()
        await cluster.started(scheduler, first, second, dispatcher, indexer)
        job_id = await create_local_job(
            database, test_database, host=HOST, ports=[server.port], port=server.port, max_depth=2
        )

        async def indexed() -> bool:
            job = await job_row(database, job_id)
            return job.status == "completed" and job.indexing_completed_at is not None

        await wait_for(indexed, within=60, what="the crawl and its indexing", cluster=cluster)

        beats = await read_heartbeats(redis_client, key_prefix=redis_prefix)
        assert Counter(beat.role for beat in beats) == {
            "scheduler": 1,
            "crawler": 2,
            "index_dispatcher": 1,
            "indexer": 1,
        }
        names = {beat.name for beat in beats}
        assert {first.identity, second.identity, scheduler.identity} <= names

        async with database.session() as session:
            recorded = int(
                await session.scalar(
                    text(
                        "SELECT count(*) FROM crawl_urls WHERE job_id = :job_id"
                        " AND result_epoch IS NOT NULL"
                    ),
                    {"job_id": job_id},
                )
                or 0
            )
        assert recorded == len(site.depths)
        settings = test_database.model_copy(update={"redis_key_prefix": redis_prefix})

        def completed_by_process(exposition: str) -> dict[str, float]:
            return {
                labels["process"]: value
                for labels, value in samples(exposition, "sitescope_tasks_handled_total")
                if labels["role"] == "crawler" and labels["reason"] == "completed"
            }

        # The last heartbeats carry the final counts within one interval.
        with TestClient(create_app(settings)) as client:
            deadline = time.monotonic() + 10
            while True:
                exposition = client.get("/metrics").text
                per_process = completed_by_process(exposition)
                if sum(per_process.values()) == recorded or time.monotonic() > deadline:
                    break
                await asyncio.sleep(0.2)
        # One series per worker process, and their sum is the database's count: no process
        # missed, none counted twice.
        assert set(per_process) == {first.identity, second.identity}
        assert sum(per_process.values()) == recorded
        assert ('sitescope_crawl_jobs{status="completed"} 1') in exposition.splitlines()

        # A killed worker leaves within three intervals; a stopped one at once.
        first.kill()
        killed = first.identity

        async def expired() -> bool:
            current = await read_heartbeats(redis_client, key_prefix=redis_prefix)
            return killed not in {beat.name for beat in current}

        began = time.monotonic()
        await wait_for(expired, within=10, what="the killed worker's heartbeat to expire")
        assert time.monotonic() - began <= 3 * 1 + 2  # three intervals, and a margin
        assert await second.stop() == 0
        remaining = await read_heartbeats(redis_client, key_prefix=redis_prefix)
        assert second.identity not in {beat.name for beat in remaining}
