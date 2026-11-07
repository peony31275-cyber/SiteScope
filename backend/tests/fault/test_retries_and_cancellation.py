"""Retries, cancellation and politeness recovery with real processes (Phase 6, ADR-0017).

Each test runs scheduler and worker processes (``tests/process_support.py``) against the
disposable test database and Redis database 15, crawling local sites on 127.0.0.1 whose pages
fail on purpose (``LocalHttpServer`` reply sequences: a flaky page answers 503 first, then 200).
The server's timeline of requests shows what the crawler did from the outside: retries,
Retry-After waits, strict BFS levels, and how soon a dead worker's politeness slot frees.

Timings come from ``FAST_TIMING``: lease 7 s, retries after up to 0.2 s, 0.4 s, ... (at most
1 s), cancellation checks every 0.2 s, politeness slot leases of 1 s.
"""

import asyncio
import time
import uuid
from collections import Counter
from email.utils import formatdate
from urllib.parse import urlsplit

import pytest
from redis.asyncio import Redis

from app.core.config import Settings
from app.db.models import CrawlUrl
from app.db.session import Database
from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream
from app.services.crawl_cancellation import cancel_crawl_job
from tests.crawl_support import (
    assert_job_invariants,
    create_local_job,
    events,
    job_row,
    url_rows,
)
from tests.fetch_support import LocalHttpServer, Reply, Served
from tests.process_support import (
    Cluster,
    ManagedProcess,
    assert_polite,
    assert_strict_bfs,
    page,
    tree_site,
    wait_for,
)
from tests.service_fixtures import ClusterFactory

pytestmark = pytest.mark.anyio

HOST = "127.0.0.1"


def unavailable(*, retry_after: str | None = None, status: int = 503) -> Reply:
    headers = [("Content-Type", "text/plain")]
    if retry_after is not None:
        headers.append(("Retry-After", retry_after))
    return Reply(status=status, reason="Unavailable", headers=headers, body=b"busy")


def slow(seconds: float) -> Reply:
    reply = page(title="Slow")
    reply.delay = seconds
    return reply


async def status_of(database: Database, job_id: uuid.UUID) -> str:
    return (await job_row(database, job_id)).status


async def finished(database: Database, job_id: uuid.UUID) -> bool:
    return await status_of(database, job_id) in ("completed", "failed", "cancelled")


async def rows_by_path(database: Database, job_id: uuid.UUID) -> dict[str, CrawlUrl]:
    return {urlsplit(row.normalized_url).path: row for row in await url_rows(database, job_id)}


def requests_for(server: LocalHttpServer, path: str) -> list[Served]:
    return sorted((item for item in server.served if item.target == path), key=lambda s: s.started)


def owner_process(cluster: Cluster, owner: str) -> ManagedProcess:
    [process] = [worker for worker in cluster.workers() if worker.identity == owner]
    return process


async def job_for(
    database: Database, crawl_settings: Settings, server: LocalHttpServer, max_depth: int
) -> uuid.UUID:
    return await create_local_job(
        database,
        crawl_settings,
        host=HOST,
        ports=[server.port],
        port=server.port,
        max_depth=max_depth,
    )


# --- Retries with strict BFS -------------------------------------------------------------------


async def test_flaky_pages_are_retried_and_every_level_waits_for_its_retries(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """A deterministic tree (13 pages, 3 levels) whose pages fail transiently: 503 twice, 503
    with Retry-After, 500, 429. Two schedulers at once and three worker processes. Every page
    ends stored, each flaky page after exactly its failures plus one attempt (no retry is
    scheduled or dispatched twice), and no page of a level is requested before the previous
    level is complete, retries included."""
    site = tree_site(depth=2, branching=3)
    flaky = {
        "/0": [unavailable(), unavailable()],
        "/1": [unavailable(retry_after="1")],
        "/0/1": [unavailable(status=500)],
        "/2/2": [unavailable(retry_after="1", status=429)],
    }
    async with LocalHttpServer(site.routes, sequences=flaky) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        processes = [
            cluster.scheduler(),
            cluster.scheduler(),
            cluster.worker(),
            cluster.worker(),
            cluster.worker(),
        ]
        await cluster.started(*processes)
        job_id = await job_for(database, crawl_settings, server, max_depth=2)

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=90, what="the job to finish", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert set(exit_codes.values()) == {0}
    assert await status_of(database, job_id) == "completed"
    rows = await rows_by_path(database, job_id)
    stored = ("fetched", "html_stored")
    assert {path: (row.state, row.outcome) for path, row in rows.items()} == dict.fromkeys(
        site.depths, stored
    )
    attempts = {path: rows[path].attempt_count for path in flaky}
    assert attempts == {"/0": 3, "/1": 2, "/0/1": 2, "/2/2": 2}
    assert all(rows[path].attempt_count == 1 for path in site.depths if path not in flaky)
    retries = await events(database, job_id, "url.retry_scheduled")
    assert len(retries) == 5
    expected_requests = {path: attempts.get(path, 1) for path in site.depths}
    requested = Counter(item.target for item in server.served if item.target in site.depths)
    assert dict(requested) == expected_requests  # one request per attempt, none duplicated
    # Retry-After: the second request for /1 started at least 1 s after the first ended.
    first, second = requests_for(server, "/1")
    assert second.started - first.finished >= 0.9
    assert_strict_bfs(server.served, site.depths)
    await assert_job_invariants(database, job_id)


async def test_exhausted_retries_let_the_level_complete(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """/1 always fails with 500. After its last attempt (3) its failure is final, the level is
    complete, and the crawl moves on: /1's children are never admitted (it stored no page)."""
    site = tree_site(depth=2, branching=2)
    routes = {**site.routes, "/1": unavailable(status=500)}
    async with LocalHttpServer(routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        processes = [cluster.scheduler(), cluster.worker(), cluster.worker()]
        await cluster.started(*processes)
        job_id = await job_for(database, crawl_settings, server, max_depth=2)

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=90, what="the job to finish", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert set(exit_codes.values()) == {0}
    assert await status_of(database, job_id) == "completed"
    rows = await rows_by_path(database, job_id)
    assert set(rows) == {"/", "/0", "/1", "/0/0", "/0/1"}
    failed = rows["/1"]
    assert (failed.state, failed.outcome, failed.http_status, failed.attempt_count) == (
        "fetched",
        "http_error",
        500,
        3,
    )
    assert len(requests_for(server, "/1")) == 3
    # Level 2 started only after /1's last attempt: pending retries blocked the level.
    last_attempt = requests_for(server, "/1")[-1]
    assert min(item.started for item in server.served if item.target.startswith("/0/")) >= (
        last_attempt.finished
    )
    assert_strict_bfs(server.served, site.depths)
    await assert_job_invariants(database, job_id)


async def test_retry_after_in_both_forms_is_honoured_by_the_real_fetcher(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """delay-seconds on a 503, and an HTTP-date on a 429 (counted from the response's Date)."""
    now = time.time()
    dated = Reply(
        status=429,
        reason="Too Many Requests",
        headers=[
            ("Content-Type", "text/plain"),
            ("Date", formatdate(now, usegmt=True)),
            ("Retry-After", formatdate(now + 2, usegmt=True)),
        ],
        body=b"slow down",
    )
    routes = {
        "/robots.txt": tree_site(0, 0).routes["/robots.txt"],
        "/": page("/a", "/b"),
        "/a": page(title="A"),
        "/b": page(title="B"),
    }
    sequences = {"/a": [unavailable(retry_after="2")], "/b": [dated]}
    async with LocalHttpServer(routes, sequences=sequences) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        processes = [cluster.scheduler(), cluster.worker(), cluster.worker()]
        await cluster.started(*processes)
        job_id = await job_for(database, crawl_settings, server, max_depth=1)

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=60, what="the job to finish", cluster=cluster)
        await cluster.stop_all()

    assert await status_of(database, job_id) == "completed"
    for path in ("/a", "/b"):
        first, second = requests_for(server, path)
        assert second.started - first.finished >= 1.9, path
    details = sorted(
        (event.detail["http_status"], event.detail["retry_after"])
        for event in await events(database, job_id, "url.retry_scheduled")
    )
    assert details == [(429, 2.0), (503, 2.0)]


async def test_simultaneous_failures_across_workers_retry_at_spread_out_times(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """Ten pages fail at once on three workers. Their retries are jittered independently over
    [0, 1.5 s]: no retry storm at one moment."""
    site = tree_site(depth=1, branching=10)
    sequences = {f"/{index}": [unavailable()] for index in range(10)}
    async with LocalHttpServer(site.routes, sequences=sequences) as server:
        cluster = clusters(
            hosts=[HOST],
            ports=[server.port],
            extra={"RETRY_BASE_DELAY_SECONDS": "1.5", "RETRY_MAX_DELAY_SECONDS": "1.5"},
        )
        processes = [cluster.scheduler(), cluster.worker(), cluster.worker(), cluster.worker()]
        await cluster.started(*processes)
        job_id = await job_for(database, crawl_settings, server, max_depth=1)

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=90, what="the job to finish", cluster=cluster)
        await cluster.stop_all()

    assert await status_of(database, job_id) == "completed"
    retries = await events(database, job_id, "url.retry_scheduled")
    assert len(retries) == 10
    assert len({event.worker_id for event in retries}) >= 2  # several processes failed
    delays = sorted(event.detail["delay_ms"] for event in retries)
    assert all(0 <= delay <= 1500 for delay in delays)
    assert delays[-1] - delays[0] > 300  # p < 1e-5 for ten independent uniform delays
    rows = await rows_by_path(database, job_id)
    assert {row.attempt_count for path, row in rows.items() if path != "/"} == {2}
    await assert_job_invariants(database, job_id)


# --- Recovery around retries -----------------------------------------------------------------


async def test_a_scheduler_restart_during_backoff_loses_no_retry(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """The retry lives in PostgreSQL (pending + next_attempt_at), not in a timer: a new
    scheduler, started while the old one is dead, dispatches it when it is due."""
    routes = {
        "/robots.txt": tree_site(0, 0).routes["/robots.txt"],
        "/": page("/a"),
        "/a": page(title="A"),
    }
    async with LocalHttpServer(routes, sequences={"/a": [unavailable(retry_after="3")]}) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        first_scheduler = cluster.scheduler()
        processes = [first_scheduler, cluster.worker()]
        await cluster.started(*processes)
        job_id = await job_for(database, crawl_settings, server, max_depth=1)

        async def retry_scheduled() -> bool:
            return bool(await events(database, job_id, "url.retry_scheduled"))

        await wait_for(retry_scheduled, within=60, what="the retry", cluster=cluster)
        first_scheduler.kill()
        await first_scheduler.exited()
        assert (await rows_by_path(database, job_id))["/a"].state == "pending"
        await asyncio.sleep(1.0)
        second_scheduler = cluster.scheduler()
        await cluster.started(second_scheduler)

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=60, what="the job to finish", cluster=cluster)
        await cluster.stop_all()

    assert await status_of(database, job_id) == "completed"
    retried = (await rows_by_path(database, job_id))["/a"]
    assert (retried.state, retried.outcome, retried.attempt_count) == ("fetched", "html_stored", 2)
    first, second = requests_for(server, "/a")
    assert second.started - first.finished >= 2.9
    assert len(await events(database, job_id, "url.retry_scheduled")) == 1


async def test_a_worker_killed_during_a_retry_attempt_is_taken_over(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """/a fails (503), its retry hangs, and the worker running the retry is killed. The lease
    expires, another worker takes the URL over with a new token, and the third attempt
    succeeds; the crashed attempt counted."""
    routes = {
        "/robots.txt": tree_site(0, 0).routes["/robots.txt"],
        "/": page("/a"),
        "/a": page(title="A"),
    }
    sequences = {"/a": [unavailable(), slow(30.0)]}
    async with LocalHttpServer(routes, sequences=sequences) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        processes = [cluster.scheduler(), cluster.worker(), cluster.worker()]
        await cluster.started(*processes)
        job_id = await job_for(database, crawl_settings, server, max_depth=1)

        async def retry_in_flight() -> bool:
            row = (await rows_by_path(database, job_id)).get("/a")
            return (
                row is not None
                and row.state == "in_progress"
                and row.attempt_count == 2
                and len(requests_for(server, "/a")) >= 1
                and any(request.target == "/a" for request in server.requests[-3:])
            )

        await wait_for(retry_in_flight, within=60, what="the retry attempt", cluster=cluster)
        await asyncio.sleep(0.3)  # the retry's request is at the server
        holder = (await rows_by_path(database, job_id))["/a"].lease_owner
        assert holder is not None
        owner_process(cluster, holder).kill()

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=90, what="the job to finish", cluster=cluster)
        await cluster.stop_all()

    assert await status_of(database, job_id) == "completed"
    row = (await rows_by_path(database, job_id))["/a"]
    assert (row.state, row.attempt_count, row.lease_epoch, row.result_epoch) == (
        "fetched",
        3,
        3,
        3,
    )
    [recovered] = [
        event
        for event in await events(database, job_id, "url.recovered")
        if event.crawl_url_id == row.id
    ]
    assert recovered.detail["previous_owner"] == holder
    await assert_job_invariants(database, job_id)


async def test_duplicate_messages_during_backoff_change_nothing(
    database: Database,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    routes = {
        "/robots.txt": tree_site(0, 0).routes["/robots.txt"],
        "/": page("/a"),
        "/a": page(title="A"),
    }
    stream = CrawlStream(redis_client, key_prefix=redis_prefix)
    async with LocalHttpServer(routes, sequences={"/a": [unavailable(retry_after="3")]}) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        processes = [cluster.scheduler(), cluster.worker(), cluster.worker()]
        await cluster.started(*processes)
        job_id = await job_for(database, crawl_settings, server, max_depth=1)

        async def retry_scheduled() -> bool:
            return bool(await events(database, job_id, "url.retry_scheduled"))

        await wait_for(retry_scheduled, within=60, what="the retry", cluster=cluster)
        waiting = (await rows_by_path(database, job_id))["/a"]
        await stream.publish([CrawlTask(job_id, waiting.id)] * 3)  # duplicates of its task

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=60, what="the job to finish", cluster=cluster)
        await cluster.stop_all()

    row = (await rows_by_path(database, job_id))["/a"]
    assert (row.state, row.attempt_count) == ("fetched", 2)
    assert len(requests_for(server, "/a")) == 2
    reasons = Counter(
        line["reason"] for line in cluster.all_lines() if line.get("event") == ("task.handled")
    )
    assert reasons["not_dispatched"] >= 3  # the duplicates found the URL waiting: acknowledged
    assert (await stream.pending()).count == 0


# --- Politeness slots of crashed workers ---------------------------------------------------------


async def test_a_killed_workers_politeness_slot_frees_within_one_lease(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """One request per host at a time. A worker is killed while its request holds the host's
    slot. The slot is a lease of 1 s, so another worker's request starts about a second
    later, instead of after the old fixed expiry (fetch deadline + 10 s = 16 s here).

    The workers run one task each, so the killed process held nothing but the slot. A second
    task of it, waiting for the slot, would also stay at the head of the host's queue until
    its waiter entry expires (waiter_ttl, 6 s here: a Phase 4 rule, ADR-0015); a pre-check run
    with two tasks per worker measured 6.95 s that way."""
    site = tree_site(depth=1, branching=3)
    sequences = {"/0": [slow(30.0)]}
    async with LocalHttpServer(site.routes, sequences=sequences) as server:
        cluster = clusters(
            hosts=[HOST], ports=[server.port], extra={"POLITENESS_MAX_CONCURRENT_PER_HOST": "1"}
        )
        processes = [
            cluster.scheduler(),
            cluster.worker("--concurrency", "1"),
            cluster.worker("--concurrency", "1"),
        ]
        await cluster.started(*processes)
        job_id = await job_for(database, crawl_settings, server, max_depth=1)

        async def holding() -> bool:
            row = (await rows_by_path(database, job_id)).get("/0")
            return (
                row is not None
                and row.state == "in_progress"
                and any(request.target == "/0" for request in server.requests)
            )

        await wait_for(holding, within=60, what="/0 to hold the slot", cluster=cluster)
        holder = (await rows_by_path(database, job_id))["/0"].lease_owner
        assert holder is not None
        killed_at = time.monotonic()
        owner_process(cluster, holder).kill()

        async def done() -> bool:
            return await finished(database, job_id)

        await wait_for(done, within=90, what="the job to finish", cluster=cluster)
        await cluster.stop_all()

    assert await status_of(database, job_id) == "completed"
    later = [item for item in server.served if item.started > killed_at]
    assert later, "nothing was requested after the kill"
    first_after = min(item.started for item in later)
    assert first_after - killed_at < 5.0, first_after - killed_at  # about 1 s; 16 s before
    # The rules held for the live requests (the killed request is still "served" by this test
    # server until its 30 s delay ends; the crawler had abandoned it with its process).
    killed_request = requests_for(server, "/0")[0]
    live = [item for item in server.served if item is not killed_request]
    assert_polite(live, interval=0.05, max_concurrent=1)
    await assert_job_invariants(database, job_id)


# --- Cancellation with live processes ------------------------------------------------------------


async def cancel(database: Database, job_id: uuid.UUID) -> str:
    async with database.session() as session:
        return (await cancel_crawl_job(session, job_id)).status.value


async def test_cancelling_a_running_crawl_stops_the_workers_promptly(
    database: Database,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """Level-1 pages take 10 s, longer than the fetch deadline (6 s here). Without the workers'
    cancellation check, a fetch in flight would end only at its deadline; with it, the job is
    cancelled within a few 0.2 s checks and one scheduler pass."""
    level_one = [f"/{index}" for index in range(8)]
    site = tree_site(depth=1, branching=8, delays=dict.fromkeys(level_one, 10.0))
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        processes = [cluster.scheduler(), cluster.worker(), cluster.worker()]
        await cluster.started(*processes)
        job_id = await job_for(database, crawl_settings, server, max_depth=1)

        async def crawling_level_one() -> bool:
            rows = await url_rows(database, job_id)
            fetching = any(request.target in level_one for request in server.requests)
            return fetching and any(row.state == "in_progress" and row.depth == 1 for row in rows)

        await wait_for(crawling_level_one, within=60, what="level 1 in flight", cluster=cluster)
        requested_at = time.monotonic()
        assert await cancel(database, job_id) == "cancelling"

        async def cancelled() -> bool:
            return await status_of(database, job_id) == "cancelled"

        await wait_for(cancelled, within=15, what="the cancellation", cluster=cluster)
        latency = time.monotonic() - requested_at
        settled_requests = len(server.requests)
        await asyncio.sleep(1.5)
        assert len(server.requests) == settled_requests  # nothing new was requested
        assert all(process.alive for process in processes)
        stream = CrawlStream(redis_client, key_prefix=redis_prefix)

        async def acknowledged() -> bool:
            return (await stream.pending()).count == 0

        await wait_for(acknowledged, within=15, what="every message settled", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert set(exit_codes.values()) == {0}
    assert latency < 4.0, latency  # about 0.5 s; at its fetch deadline (6 s) without the check
    rows = await url_rows(database, job_id)
    assert {row.state for row in rows} <= {"fetched", "cancelled"}
    stopped = [row for row in rows if row.state == "cancelled" and row.result_epoch is not None]
    assert stopped, "no in-flight task was stopped by its worker"
    assert all(row.outcome == "job_cancelled" for row in rows if row.state == "cancelled")
    job = await job_row(database, job_id)
    assert job.cancel_requested_at is not None
    assert job.finished_at is not None
    await assert_job_invariants(database, job_id)


async def test_a_cancellation_completes_after_a_scheduler_restart(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """The scheduler is killed, then the job is cancelled: the workers stop their tasks, but
    the job stays cancelling until a scheduler runs its finalizer. A new one does."""
    site = tree_site(depth=1, branching=6, delay=3.0)
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        first_scheduler = cluster.scheduler()
        processes = [first_scheduler, cluster.worker(), cluster.worker()]
        await cluster.started(*processes)
        job_id = await job_for(database, crawl_settings, server, max_depth=1)

        async def crawling_level_one() -> bool:
            rows = await url_rows(database, job_id)
            return sum(row.state == "in_progress" and row.depth == 1 for row in rows) >= 1

        await wait_for(crawling_level_one, within=60, what="level 1 in flight", cluster=cluster)
        first_scheduler.kill()
        await first_scheduler.exited()
        assert await cancel(database, job_id) == "cancelling"

        async def all_final() -> bool:
            rows = await url_rows(database, job_id)
            return all(row.state in ("fetched", "cancelled") for row in rows)

        await wait_for(all_final, within=15, what="the workers to stop", cluster=cluster)
        await asyncio.sleep(0.5)
        assert await status_of(database, job_id) == "cancelling"  # no finalizer without one
        restarted_at = time.monotonic()
        second_scheduler = cluster.scheduler()
        await cluster.started(second_scheduler)

        async def cancelled() -> bool:
            return await status_of(database, job_id) == "cancelled"

        await wait_for(cancelled, within=30, what="the finalizer", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert time.monotonic() - restarted_at < 30
    del exit_codes[first_scheduler.name]  # killed on purpose
    assert set(exit_codes.values()) == {0}
    assert len(await events(database, job_id, "job.cancelled")) == 1
    await assert_job_invariants(database, job_id)
