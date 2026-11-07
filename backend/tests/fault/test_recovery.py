"""Crash and outage recovery with real processes (failure injection; marker ``fault``).

Each test runs scheduler and worker processes (``tests/process_support.py``) against the
disposable test database and Redis database 15, crawling local sites on 127.0.0.1, and breaks
something on purpose:

* B: a worker is killed while it holds a lease, and a message is abandoned before its claim;
* C: a worker is frozen mid-fetch (an unresponsive worker): its lease is taken over, and its
  late result is fenced out;
* D: the scheduler is killed mid-crawl and a new one takes over;
* G: Redis, then PostgreSQL, become unreachable for a while (through ``TcpProxy``).

The timings come from ``FAST_TIMING``: lease 7 s, reclaim after 7 s idle, reconcile after 10 s.
"""

import asyncio
import uuid
from collections import Counter
from urllib.parse import urlsplit

import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import Settings
from app.db.models import CrawlUrl
from app.db.session import Database
from app.queue.streams import CrawlStream
from tests.crawl_support import (
    assert_job_invariants,
    create_local_job,
    events,
    job_row,
    stream_entries,
    url_row,
    url_rows,
)
from tests.fetch_support import LocalHttpServer
from tests.process_support import (
    Cluster,
    ManagedProcess,
    Site,
    TcpProxy,
    assert_strict_bfs,
    tree_site,
    wait_for,
)
from tests.service_fixtures import ClusterFactory

pytestmark = pytest.mark.anyio

HOST = "127.0.0.1"


async def finished(database: Database, job_id: uuid.UUID) -> bool:
    return (await job_row(database, job_id)).status in ("completed", "failed")


async def row_for(database: Database, job_id: uuid.UUID, path: str) -> CrawlUrl | None:
    for row in await url_rows(database, job_id):
        if urlsplit(row.normalized_url).path == path:
            return row
    return None


async def assert_completed(
    database: Database, job_id: uuid.UUID, site: Site, port: int, max_depth: int
) -> list[CrawlUrl]:
    job = await job_row(database, job_id)
    assert job.status == "completed", job.failure_reason
    rows = await url_rows(database, job_id)
    prefix = f"http://{HOST}:{port}"
    assert {
        row.normalized_url.removeprefix(prefix): (row.depth, row.state, row.outcome) for row in rows
    } == {
        path: (depth, "fetched", "html_stored")
        for path, depth in site.depths.items()
        if depth <= max_depth
    }
    await assert_job_invariants(database, job_id)
    return rows


def owner_process(cluster: Cluster, owner: str) -> ManagedProcess:
    [process] = [worker for worker in cluster.workers() if worker.identity == owner]
    return process


def page_requests(server: LocalHttpServer) -> Counter[str]:
    return Counter(request.target for request in server.requests if request.target != "/robots.txt")


# --- B. Worker crashes ---------------------------------------------------------------------------


async def test_a_killed_workers_lease_and_an_abandoned_message_are_recovered(
    database: Database,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Crash recovery scenarios A and B. The seed's message is read by a consumer that dies
    before claiming (simulated by the test reading it under a consumer name of its own), and a
    worker process is killed (TerminateProcess) while it fetches /2. Both are recovered by the
    surviving worker: the message after 7 s idle (XAUTOCLAIM), the lease after it expired (a
    takeover with a new fencing token)."""
    site = tree_site(depth=1, branching=4, delays={"/2": 3.0})
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        scheduler = cluster.scheduler()
        await cluster.started(scheduler)
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=1
        )

        async def seed_published() -> bool:
            return (await stream.pending()).count == 0 and await redis_client.xlen(stream.key) > 0

        await wait_for(seed_published, within=30, what="the seed's message", cluster=cluster)
        # Crash scenario A: a consumer takes the message and dies before claiming the URL.
        [abandoned] = await stream.read("crawler-dead-before-claim", count=1, block_ms=1000)
        workers = [cluster.worker(), cluster.worker()]
        await cluster.started(*workers)

        async def slow_page_requested() -> bool:
            # The server holds the reply for 3 s: the worker is now in the middle of the fetch.
            row = await row_for(database, job_id, "/2")
            requested = any(request.target == "/2" for request in server.requests)
            return requested and row is not None and row.state == "in_progress"

        await wait_for(slow_page_requested, within=60, what="/2 to be fetched", cluster=cluster)
        slow = await row_for(database, job_id, "/2")
        assert slow is not None
        assert slow.lease_owner is not None
        victim = owner_process(cluster, slow.lease_owner)
        victim.kill()  # crash scenario B: the process dies during the fetch, holding the lease
        assert await victim.exited(within=10) != 0
        survivor = next(worker for worker in workers if worker is not victim)

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=90, what="the job to finish", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert exit_codes[scheduler.name] == exit_codes[survivor.name] == 0
    rows = await assert_completed(database, job_id, site, server.port, max_depth=1)
    seed = next(row for row in rows if row.depth == 0)
    assert (seed.attempt_count, seed.result_epoch) == (1, 1)  # never claimed before the crash
    slow = await row_for(database, job_id, "/2")
    assert slow is not None
    assert (slow.attempt_count, slow.lease_epoch, slow.result_epoch) == (2, 2, 2)
    recovered = list(await events(database, job_id, "url.recovered"))
    assert any(
        event.crawl_url_id == slow.id
        and event.detail["previous_owner"] == victim.identity
        and event.worker_id == survivor.identity
        for event in recovered
    )
    # Either worker may have reclaimed the abandoned message (before the crash).
    assert any(
        line.get("event") == "task.handled"
        and line["message_id"] == abandoned.entry_id
        and line["reclaimed"]
        and line["reason"] == "completed"
        for line in cluster.all_lines()
    )
    assert any(
        line["url_id"] == str(slow.id) and line["reclaimed"] and line["reason"] == "completed"
        for line in survivor.events("task.handled")
    )
    assert page_requests(server)["/2"] == 2  # the dead worker's request, then the takeover's
    assert (await stream.pending()).count == 0


# --- Graceful shutdown of a worker in the middle of a task ----------------------------------------


async def test_a_worker_stopped_mid_task_finishes_it_within_the_grace_period(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    site = tree_site(depth=1, branching=2, delays={"/1": 2.0})
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(
            hosts=[HOST], ports=[server.port], extra={"SHUTDOWN_GRACE_SECONDS": "10"}
        )
        workers = [cluster.worker(), cluster.worker()]
        await cluster.started(*workers, cluster.scheduler())
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=1
        )

        async def slow_page_leased() -> bool:
            row = await row_for(database, job_id, "/1")
            return row is not None and row.state == "in_progress"

        await wait_for(slow_page_leased, within=60, what="/1 to be leased", cluster=cluster)
        slow = await row_for(database, job_id, "/1")
        assert slow is not None
        assert slow.lease_owner is not None
        stopping = owner_process(cluster, slow.lease_owner)
        assert await stopping.stop(within=20) == 0  # CTRL_BREAK_EVENT: a graceful stop

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=60, what="the job to finish", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert set(exit_codes.values()) == {0}
    await assert_completed(database, job_id, site, server.port, max_depth=1)
    row = await row_for(database, job_id, "/1")
    assert row is not None
    assert (row.attempt_count, row.result_epoch) == (1, 1)
    assert any(
        line["url_id"] == str(slow.id) and line["reason"] == "completed" and line["settled"]
        for line in stopping.events("task.handled")
    )
    assert stopping.events("worker.stopping")
    assert stopping.events("worker.stopped")
    assert not stopping.events("worker.tasks_cancelled")
    assert page_requests(server)["/1"] == 1


async def test_a_worker_stopped_mid_task_leaves_it_recoverable_after_the_grace_period(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """The grace period (1 s) ends before the slow fetch (4 s): the task is cancelled, records
    nothing, and keeps its lease and its pending message; another worker takes it over."""
    site = tree_site(depth=1, branching=2, delays={"/1": 4.0})
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port], extra={"SHUTDOWN_GRACE_SECONDS": "1"})
        workers = [cluster.worker(), cluster.worker()]
        await cluster.started(*workers, cluster.scheduler())
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=1
        )

        async def slow_page_leased() -> bool:
            row = await row_for(database, job_id, "/1")
            return row is not None and row.state == "in_progress"

        await wait_for(slow_page_leased, within=60, what="/1 to be leased", cluster=cluster)
        slow = await row_for(database, job_id, "/1")
        assert slow is not None
        assert slow.lease_owner is not None
        stopping = owner_process(cluster, slow.lease_owner)
        assert await stopping.stop(within=20) == 0

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=60, what="the job to finish", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert set(exit_codes.values()) == {0}
    await assert_completed(database, job_id, site, server.port, max_depth=1)
    row = await row_for(database, job_id, "/1")
    assert row is not None
    assert (row.attempt_count, row.result_epoch) == (2, 2)
    assert stopping.events("worker.tasks_cancelled")
    assert any(line["url_id"] == str(slow.id) for line in stopping.events("task.cancelled"))
    assert not any(
        line["url_id"] == str(slow.id) for line in stopping.events("task.handled")
    )  # nothing recorded or acknowledged by the stopped worker


# --- C. An unresponsive worker is fenced out -----------------------------------------------------


async def test_a_frozen_workers_late_result_is_fenced_out(
    database: Database,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Crash recovery scenario D, and stale-worker fencing. A worker process is frozen
    (NtSuspendProcess) while it fetches /1. Its lease expires, another worker takes /1 over
    and records it. When the frozen worker resumes, its result is rejected by the fenced
    update (the token it holds is stale), nothing of it is written, and it does not
    acknowledge the message."""
    site = tree_site(depth=1, branching=2, delays={"/1": 2.0})
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        workers = [cluster.worker(), cluster.worker()]
        await cluster.started(*workers, cluster.scheduler())
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=1
        )

        async def slow_page_leased() -> bool:
            row = await row_for(database, job_id, "/1")
            return row is not None and row.state == "in_progress"

        await wait_for(slow_page_leased, within=60, what="/1 to be leased", cluster=cluster)
        slow = await row_for(database, job_id, "/1")
        assert slow is not None
        assert slow.lease_owner is not None
        frozen = owner_process(cluster, slow.lease_owner)
        frozen.suspend()
        frozen_identity = slow.lease_owner
        try:

            async def taken_over_and_recorded() -> bool:
                row = await row_for(database, job_id, "/1")
                return row is not None and row.state == "fetched"

            await wait_for(
                taken_over_and_recorded, within=60, what="the takeover of /1", cluster=cluster
            )
        finally:
            frozen.resume()

        async def fenced() -> bool:
            return any(
                line["reason"] == "fenced" and line["url_id"] == str(slow.id)
                for line in frozen.events("task.handled")
            )

        await wait_for(fenced, within=30, what="the stale result to be fenced", cluster=cluster)

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=60, what="the job to finish", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert set(exit_codes.values()) == {0}  # the frozen worker recovered and stopped cleanly
    await assert_completed(database, job_id, site, server.port, max_depth=1)
    row = await row_for(database, job_id, "/1")
    assert row is not None
    assert (row.lease_epoch, row.result_epoch) == (2, 2)  # the takeover's result stands
    [stale] = [
        line
        for line in frozen.events("task.handled")
        if line["url_id"] == str(slow.id) and line["reason"] == "fenced"
    ]
    assert (stale["disposition"], stale["settled"], stale["lease_epoch"]) == ("keep", False, 1)
    assert any(line["lease_epoch"] == 1 for line in frozen.events("task.fenced"))
    [recovered] = [
        event
        for event in await events(database, job_id, "url.recovered")
        if event.crawl_url_id == slow.id
    ]
    assert recovered.detail["previous_owner"] == frozen_identity
    assert recovered.worker_id != frozen_identity
    assert (await stream.pending()).count == 0


# --- D. The scheduler crashes and another one takes over -----------------------------------------


async def test_a_new_scheduler_finishes_the_job_of_a_killed_one(
    database: Database,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """The scheduler is killed mid-crawl (possibly inside a dispatch, after XADD and before
    COMMIT). Nothing is lost: a new scheduler process continues from PostgreSQL. Each page is
    still fetched exactly once, and in strict BFS order."""
    site = tree_site(depth=3, branching=3, delay=0.05)  # 40 pages
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        first = cluster.scheduler()
        workers = [cluster.worker(), cluster.worker()]
        await cluster.started(first, *workers)
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=3
        )

        async def at_level_two() -> bool:
            return (await job_row(database, job_id)).current_level >= 2

        await wait_for(at_level_two, within=60, what="level 2", cluster=cluster)
        first.kill()
        assert await first.exited(within=10) != 0
        level = (await job_row(database, job_id)).current_level
        await asyncio.sleep(2)  # without a scheduler, no level is admitted
        assert (await job_row(database, job_id)).current_level == level
        assert (await job_row(database, job_id)).status == "running"
        second = cluster.scheduler()
        await cluster.started(second)

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=120, what="the job to finish", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert [exit_codes[process.name] for process in (second, *workers)] == [0, 0, 0]
    await assert_completed(database, job_id, site, server.port, max_depth=3)
    assert page_requests(server) == Counter(dict.fromkeys(site.depths, 1))
    assert_strict_bfs(server.served, site.depths)
    assert any(line["dispatched"] for line in second.events("scheduler.pass"))
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    assert (await stream.pending()).count == 0


async def test_a_scheduler_killed_between_xadd_and_commit_leaves_only_a_duplicate(
    database: Database,
    test_database: Settings,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Failure window F2, forced with real processes. The test holds ``LOCK TABLE crawl_urls IN
    SHARE MODE``: the dispatcher's ``SELECT ... FOR UPDATE`` and its ``XADD`` go through, but
    its ``UPDATE`` (marking the seed ``queued``) waits. The scheduler is killed right then: the
    message is in Redis, the dispatch never commits. The orphaned message is acknowledged
    without a fetch, a new scheduler publishes the seed again, and the job completes with every
    page fetched once."""
    site = tree_site(depth=1, branching=3)
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        workers = [cluster.worker(), cluster.worker()]
        await cluster.started(*workers)
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=1
        )
        [seed] = await url_rows(database, job_id)
        blocker = Database(test_database, application_name="sitescope-tests-blocker")
        try:
            async with blocker.session() as session:
                await session.begin()
                await session.execute(text("LOCK TABLE crawl_urls IN SHARE MODE"))
                first = cluster.scheduler()
                await cluster.started(first)

                async def published_not_committed() -> bool:
                    row = await row_for(database, job_id, "/")
                    published = await redis_client.xlen(stream.key) >= 1
                    return published and row is not None and row.state == "pending"

                await wait_for(
                    published_not_committed, within=30, what="XADD before COMMIT", cluster=cluster
                )
                first.kill()  # between XADD and COMMIT
                assert await first.exited(within=10) != 0
                await asyncio.sleep(0.5)
                await session.rollback()  # release the lock; the dead dispatch rolls back
        finally:
            await blocker.dispose()
        [(orphan, _)] = await stream_entries(redis_client, stream.key)
        assert (await url_row(database, seed.id)).state == "pending"  # nothing committed
        second = cluster.scheduler()
        await cluster.started(second)

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=60, what="the job to finish", cluster=cluster)

        async def drained() -> bool:
            return (await stream.pending()).count == 0

        await wait_for(drained, within=30, what="every message to be acknowledged", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert [exit_codes[process.name] for process in (second, *workers)] == [0, 0, 0]
    await assert_completed(database, job_id, site, server.port, max_depth=1)
    assert page_requests(server) == Counter(dict.fromkeys(site.depths, 1))
    entries = await stream_entries(redis_client, stream.key)
    seed_entries = [entry for entry, fields in entries if fields["url_id"] == str(seed.id)]
    assert seed_entries[0] == orphan  # the orphan, then the second scheduler's message
    assert len(seed_entries) == 2
    handled = [
        line
        for line in cluster.all_lines()
        if line.get("event") == "task.handled" and line["url_id"] == str(seed.id)
    ]
    orphan_handling = [line for line in handled if line["message_id"] == orphan]
    assert orphan_handling, "the orphaned message was never handled"
    assert all(line["disposition"] == "ack" for line in orphan_handling)
    # Usually "not_dispatched"; "completed" if the second dispatch committed while the worker
    # was still deciding (the claim then went through the orphan message).
    assert {line["reason"] for line in orphan_handling} <= {"not_dispatched", "completed"}
    assert sum(line["reason"] == "completed" for line in handled) == 1


# --- G. Redis and PostgreSQL outages -------------------------------------------------------------


async def test_processes_ride_out_redis_and_postgresql_outages(
    database: Database,
    test_database: Settings,
    crawl_settings: Settings,
    clusters: ClusterFactory,
) -> None:
    """Crash recovery scenarios E and F. The processes reach Redis and PostgreSQL through
    proxies, which cut every connection for 3 s, Redis first, then PostgreSQL. The processes
    log the failures, back off, recover on their own, and the job completes with every
    invariant intact. A page may be fetched twice (an attempt cut off mid-way is retried by a
    takeover), but it is stored once."""
    site = tree_site(depth=1, branching=12, delay=0.2)  # 13 pages, slow enough to overlap
    async with (
        LocalHttpServer(site.routes) as server,
        TcpProxy(test_database.redis_host, test_database.redis_port) as redis_proxy,
        TcpProxy(test_database.postgres_host, test_database.postgres_port) as postgres_proxy,
    ):
        proxied = test_database.model_copy(
            update={
                "redis_host": "127.0.0.1",
                "redis_port": redis_proxy.port,
                "postgres_host": "127.0.0.1",
                "postgres_port": postgres_proxy.port,
            }
        )
        cluster = clusters(
            hosts=[HOST],
            ports=[server.port],
            settings=proxied,
            extra={"CRAWL_MAX_ATTEMPTS": "5", "DB_CONNECT_TIMEOUT_SECONDS": "2"},
        )
        processes = [cluster.scheduler(), cluster.worker(), cluster.worker()]
        await cluster.started(*processes)
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=1
        )

        async def some_fetched() -> bool:
            return sum(row.state == "fetched" for row in await url_rows(database, job_id)) >= 3

        await wait_for(some_fetched, within=60, what="the first pages", cluster=cluster)
        redis_proxy.cut()
        await asyncio.sleep(3)
        redis_proxy.restore()
        postgres_proxy.cut()
        await asyncio.sleep(3)
        postgres_proxy.restore()

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=120, what="the job to finish", cluster=cluster)
        assert all(process.alive for process in processes)  # nobody crashed
        exit_codes = await cluster.stop_all()

    assert set(exit_codes.values()) == {0}
    job = await job_row(database, job_id)
    assert job.status == "completed", job.failure_reason
    await assert_job_invariants(database, job_id)
    rows = await url_rows(database, job_id)
    assert {urlsplit(row.normalized_url).path for row in rows} == set(site.depths)
    # Known Phase 5 limitation (docs/architecture.md, section 10.4): slots whose release failed
    # during the Redis outage stay held until their TTL (fetch deadline + 10 s), and a request
    # that waits for one longer than the politeness maximum wait fails as rate_limited, which
    # stays terminal until Phase 6 retries retryable failures. Nothing else may go wrong.
    outcomes = Counter((row.state, row.outcome) for row in rows)
    assert set(outcomes) <= {("fetched", "html_stored"), ("failed", "rate_limited")}, outcomes
    assert next(row for row in rows if row.depth == 0).state == "fetched"
    lines = cluster.all_lines()
    failures = [
        line for line in lines if line.get("event") in ("worker.error", "scheduler.pass_failed")
    ]
    recoveries = [
        line for line in lines if line.get("event") in ("worker.recovered", "scheduler.recovered")
    ]
    assert failures, "the outages were not noticed"
    assert recoveries, "no process reported its recovery"
    assert all(line["level"] == "WARNING" for line in failures)  # transient: no tracebacks
    # Every stored page was requested (a page cut off mid-way may have been requested twice);
    # a rate_limited URL was never requested at all: politeness refused it before sending.
    requests = page_requests(server)
    fetched = {urlsplit(row.normalized_url).path for row in rows if row.state == "fetched"}
    assert fetched <= set(requests) <= set(site.depths)
