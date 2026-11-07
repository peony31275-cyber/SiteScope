"""The distributed crawler with real processes: scheduler and workers as separate OS processes.

Each test starts ``python -m app.scheduler`` and ``python -m app.workers.crawler`` processes
(``tests/process_support.py``) against the disposable test database and Redis database 15
under the test's own key prefix. They crawl sites served by ``LocalHttpServer`` on 127.0.0.1;
nothing contacts the internet. Worker participation is read from the workers' own JSON logs and
from the Redis consumer group, not inferred.

Scenarios (Phase 5): A, independent workers share one job; E, duplicate deliveries; F, competing
schedulers. Crash and outage scenarios are in tests/fault/test_recovery.py.
"""

import asyncio
import uuid
from collections import Counter
from typing import Any

import pytest
from redis.asyncio import Redis

from app.core.config import Settings
from app.db.session import Database
from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream
from tests.crawl_support import (
    assert_job_invariants,
    create_local_job,
    events,
    job_row,
    stream_entries,
    url_rows,
)
from tests.fetch_support import LocalHttpServer
from tests.process_support import (
    Cluster,
    Site,
    assert_polite,
    assert_strict_bfs,
    tree_site,
    wait_for,
)
from tests.service_fixtures import ClusterFactory

pytestmark = pytest.mark.anyio

HOST = "127.0.0.1"


async def finished(database: Database, job_id: uuid.UUID) -> bool:
    return (await job_row(database, job_id)).status in ("completed", "failed")


def completed_by_worker(cluster: Cluster) -> dict[str, list[str]]:
    """URL IDs each worker process recorded, from its own log (one line per message)."""
    return {
        worker.identity: [
            line["url_id"]
            for line in worker.events("task.handled")
            if line["reason"] == "completed"
        ]
        for worker in cluster.workers()
    }


async def assert_crawled_exactly(
    database: Database, job_id: uuid.UUID, site: Site, port: int, max_depth: int
) -> None:
    """The job completed with every page of the site up to ``max_depth`` stored."""
    job = await job_row(database, job_id)
    assert job.status == "completed", job.failure_reason
    prefix = f"http://{HOST}:{port}"
    rows = {
        row.normalized_url.removeprefix(prefix): (row.depth, row.state, row.outcome)
        for row in await url_rows(database, job_id)
    }
    assert rows == {
        path: (depth, "fetched", "html_stored")
        for path, depth in site.depths.items()
        if depth <= max_depth
    }
    await assert_job_invariants(database, job_id)


def page_requests(server: LocalHttpServer) -> Counter[str]:
    return Counter(request.target for request in server.requests if request.target != "/robots.txt")


# --- A. Independent workers crawl one job --------------------------------------------------------


async def test_independent_worker_processes_crawl_one_job_together(
    database: Database,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    site = tree_site(depth=3, branching=3, delay=0.02)  # 40 pages
    interval, per_host = 0.1, 2
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(
            hosts=[HOST],
            ports=[server.port],
            extra={
                "POLITENESS_INTERVAL_SECONDS": str(interval),
                "POLITENESS_MAX_CONCURRENT_PER_HOST": str(per_host),
            },
        )
        workers = [cluster.worker() for _ in range(3)]
        scheduler = cluster.scheduler()
        await cluster.started(*workers, scheduler)
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=3
        )

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=120, what="the job to finish", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert exit_codes == {process.name: 0 for process in cluster.processes}
    await assert_crawled_exactly(database, job_id, site, server.port, max_depth=3)
    assert page_requests(server) == Counter(dict.fromkeys(site.depths, 1))  # each page once
    assert_strict_bfs(server.served, site.depths)
    assert_polite(server.served, interval=interval, max_concurrent=per_host)

    # Participation, from each worker's own log: three processes, each one recorded results,
    # and every URL was recorded by exactly one of them.
    by_worker = completed_by_worker(cluster)
    assert len(by_worker) == 3
    assert len({worker.interpreter_pid for worker in workers}) == 3  # three OS processes
    assert all(by_worker.values()), {name: len(urls) for name, urls in by_worker.items()}
    recorded = [url_id for urls in by_worker.values() for url_id in urls]
    assert sorted(recorded) == sorted(str(row.id) for row in await url_rows(database, job_id))
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    described = await stream.describe()
    assert set(by_worker) <= {consumer["name"] for consumer in described["consumers"]}
    assert (await stream.pending()).count == 0
    assert described["group"]["lag"] == 0
    for worker in workers:
        assert worker.events("worker.stopped"), worker.tail()
    assert scheduler.events("scheduler.stopped"), scheduler.tail()


async def test_one_four_and_eight_workers_crawl_exactly_the_same_pages(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """The same budget-limited crawl with 1, 4 and 8 worker processes gives identical results.
    The budget (25 pages) ends in the middle of level 3, so it is the deterministic BFS prefix
    (source URL, then link position) that must not depend on how the work was spread."""
    site = tree_site(depth=3, branching=3, delay=0.01)  # 40 pages
    results: dict[int, dict[str, tuple[int, str, str | None]]] = {}
    async with LocalHttpServer(site.routes) as server:
        for workers in (1, 4, 8):
            cluster = clusters(hosts=[HOST], ports=[server.port])
            processes = [cluster.scheduler()] + [cluster.worker() for _ in range(workers)]
            await cluster.started(*processes)
            job_id = await create_local_job(
                database,
                crawl_settings,
                host=HOST,
                ports=[server.port],
                port=server.port,
                max_depth=3,
                max_pages=25,
            )

            async def done(job_id: uuid.UUID = job_id) -> bool:
                return await finished(database, job_id)

            await wait_for(
                done, within=120, what=f"the job with {workers} workers", cluster=cluster
            )
            exit_codes = await cluster.stop_all()
            assert set(exit_codes.values()) == {0}
            job = await job_row(database, job_id)
            assert (job.status, job.admitted_count) == ("completed", 25)
            await assert_job_invariants(database, job_id)
            prefix = f"http://{HOST}:{server.port}"
            results[workers] = {
                row.normalized_url.removeprefix(prefix): (row.depth, row.state, row.outcome)
                for row in await url_rows(database, job_id)
            }
            recorded = completed_by_worker(cluster)
            assert sum(len(urls) for urls in recorded.values()) == 25

    assert results[1] == results[4] == results[8]
    assert Counter(depth for depth, _, _ in results[1].values()) == {0: 1, 1: 3, 2: 9, 3: 12}


# --- E. Duplicate deliveries ---------------------------------------------------------------------


async def test_duplicate_messages_never_cause_a_second_crawl(
    database: Database,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Every task is published twice more while the job runs: once as soon as its URL is
    queued, and once after it is finished. Each page is still fetched and stored once; the
    duplicates are acknowledged once their claims are refused."""
    site = tree_site(depth=2, branching=4, delay=0.05)  # 21 pages
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        workers = [cluster.worker() for _ in range(2)]
        await cluster.started(*workers, cluster.scheduler())
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=2
        )
        duplicated: dict[uuid.UUID, int] = Counter()

        async def inject() -> None:
            while True:
                for row in await url_rows(database, job_id):
                    early = row.state in ("queued", "in_progress") and duplicated[row.id] == 0
                    late = row.state == "fetched" and duplicated[row.id] < 2
                    if early or late:
                        await stream.publish([CrawlTask(job_id, row.id)])
                        duplicated[row.id] += 1
                await asyncio.sleep(0.05)

        injector = asyncio.create_task(inject())
        try:

            async def done() -> bool:
                return await finished(database, job_id)

            await wait_for(done, within=120, what="the job to finish", cluster=cluster)
            await asyncio.sleep(0.5)  # let the injector publish the last late duplicates
        finally:
            injector.cancel()

        async def drained() -> bool:
            # Duplicates that met a live lease stay pending until reclaimed (7 s idle).
            summary = await stream.pending()
            described = await stream.describe()
            return summary.count == 0 and described["group"]["lag"] == 0

        await wait_for(drained, within=60, what="every message to be acknowledged", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert exit_codes == {process.name: 0 for process in cluster.processes}
    await assert_crawled_exactly(database, job_id, site, server.port, max_depth=2)
    assert page_requests(server) == Counter(dict.fromkeys(site.depths, 1))
    assert sum(duplicated.values()) >= len(site.depths) + 1  # duplicates were really injected
    handled = [line for line in cluster.all_lines() if line.get("event") == "task.handled"]
    completed = Counter(line["url_id"] for line in handled if line["reason"] == "completed")
    assert set(completed.values()) == {1}  # every URL recorded once, whatever the duplicates
    reasons = Counter(line["reason"] for line in handled)
    assert reasons["already_finished"] >= len(site.depths)  # the late duplicates, at least
    assert set(reasons) <= {"completed", "already_finished", "leased", "claim_contended"}
    assert await redis_client.xlen(stream.dead_letter_key) == 0


# --- F. Competing schedulers ---------------------------------------------------------------------


async def test_competing_schedulers_respect_the_cap_and_publish_each_url_once(
    database: Database,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Three scheduler processes run at once without a leader: three jobs, at most two running
    at a time, every URL published exactly once, each job crawled in strict BFS order."""
    sites = [tree_site(depth=2, branching=3, delay=0.02) for _ in range(3)]  # 13 pages each
    async with (
        LocalHttpServer(sites[0].routes) as first,
        LocalHttpServer(sites[1].routes) as second,
        LocalHttpServer(sites[2].routes) as third,
    ):
        servers = [first, second, third]
        ports = [server.port for server in servers]
        cluster = clusters(hosts=[HOST], ports=ports, extra={"SCHEDULER_MAX_RUNNING_JOBS": "2"})
        processes = [cluster.scheduler() for _ in range(3)] + [cluster.worker() for _ in range(3)]
        await cluster.started(*processes)
        jobs = [
            await create_local_job(
                database, crawl_settings, host=HOST, ports=ports, port=port, max_depth=2
            )
            for port in ports
        ]

        async def all_done() -> bool:
            return all([await finished(database, job_id) for job_id in jobs])

        await wait_for(all_done, within=180, what="all jobs to finish", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert exit_codes == {process.name: 0 for process in cluster.processes}
    for job_id, site, server in zip(jobs, sites, servers, strict=True):
        await assert_crawled_exactly(database, job_id, site, server.port, max_depth=2)
        assert page_requests(server) == Counter(dict.fromkeys(site.depths, 1))
        assert_strict_bfs(server.served, site.depths)
        assert len(await events(database, job_id, "job.started")) == 1

    # Never more than two jobs running at once (the activation cap, across schedulers). The
    # lifecycle events are replayed in the order of their IDs (a sequence), not of their
    # timestamps, which can step back with the database host's clock: an activation that saw
    # a job finished started after that job's completion event got its ID.
    lifecycle = sorted(
        [event for job_id in jobs for event in await events(database, job_id, "job.started")]
        + [event for job_id in jobs for event in await events(database, job_id, "job.completed")],
        key=lambda event: event.id,
    )
    running = peak = 0
    for event in lifecycle:
        running += 1 if event.event_type == "job.started" else -1
        peak = max(peak, running)
    assert (peak, running) == (2, 0)

    # Every URL of every job was published exactly once.
    published: Counter[Any] = Counter(
        CrawlTask.decode(fields).url_id
        for _, fields in await stream_entries(redis_client, f"{redis_prefix}:crawl")
    )
    all_urls = [row.id for job_id in jobs for row in await url_rows(database, job_id)]
    assert sorted(published) == sorted(all_urls)
    assert set(published.values()) == {1}
    # The schedulers really competed: more than one of them did work. Each of the 18 steps
    # (3 activations, 6 level advances or completions, 9 dispatches) goes to whichever
    # scheduler's pass comes first; all of them going to one of three schedulers has a
    # probability of about 3 * 3**-18 if passes are evenly spread.
    busy_schedulers = {
        line["scheduler_id"]
        for process in cluster.processes
        if process.name.startswith("scheduler")
        for line in process.events("scheduler.pass")
        if line["activated"] or line["advanced"] or line["finished"] or line["dispatched"]
    }
    assert len(busy_schedulers) >= 2
