"""The asynchronous index pipeline with real processes (Phase 7; marker ``fault``).

Every process is a separate operating-system process (``tests/process_support.py``): the
scheduler, crawler workers, the index dispatcher (``python -m app.scheduler.index_dispatcher``)
and indexers (``python -m app.workers.indexer``), against the disposable test database and Redis
database 15 under the test's own key prefix. Sites are served on 127.0.0.1 by
``LocalHttpServer``; nothing contacts the internet. The kill of an indexer and of a dispatcher
are placed with database locks, checked with ``pg_stat_activity``, not with sleeps; the
cancellation uses a page that answers too slowly for its BFS level to finish first.

Scenarios: the whole pipeline (crawl, persist, publish, index, search); an indexer killed in the
middle of an attempt; an index dispatcher killed between ``XADD`` and its commit; Redis and
PostgreSQL outages; a job cancelled while its pages are being indexed. Redis losing all its data
is in tests/fault/test_redis_data_loss.py.
"""

import asyncio
import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import Settings
from app.db.models import JobStatus
from app.db.session import Database
from app.queue.streams import IndexStream
from app.scheduler.status import collect
from app.services.crawl_cancellation import cancel_crawl_job
from app.services.crawl_jobs import get_crawl_job
from app.services.search import SearchResults, search_job
from app.services.search_index import job_indexing
from tests.crawl_support import (
    assert_job_invariants,
    create_local_job,
    events,
    job_row,
    new_job,
    stream_entries,
)
from tests.fetch_support import LocalHttpServer, Reply
from tests.index_support import add_page, document, finish_job, until_sessions_wait_for_locks
from tests.process_support import ALLOW_ALL_ROBOTS, Cluster, Site, TcpProxy, tree_site, wait_for
from tests.service_fixtures import ClusterFactory

pytestmark = pytest.mark.anyio

HOST = "127.0.0.1"


def html(title: str, body: str, links: tuple[str, ...] = (), *, noindex: bool = False) -> Reply:
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    document = (
        f"<!doctype html><html><head><title>{title}</title>{robots}"
        f'<meta name="description" content="{title} guide"></head>'
        f"<body><h1>{title}</h1><p>{body}</p>{anchors}</body></html>"
    )
    return Reply(headers=[("Content-Type", "text/html; charset=utf-8")], body=document.encode())


def docs_site() -> Site:
    """Six pages with distinct words, one of them noindex."""
    pages = {
        "/": (
            html(
                "SiteScope documentation",
                "Guides about queues, leases and search.",
                ("/queues", "/leases", "/search", "/private"),
            ),
            0,
        ),
        "/queues": (
            html(
                "Redis Streams queues",
                "Consumer groups acknowledge messages after the commit.",
                ("/queues/trimming", "/"),
            ),
            1,
        ),
        "/leases": (
            html(
                "Leases and fencing tokens",
                "A stale worker is fenced out; Redis is only the delivery plane.",
                ("/",),
            ),
            1,
        ),
        "/search": (html("Full-text search", "PostgreSQL ranks pages over weighted vectors."), 1),
        "/private": (html("Private notes", "fencing secrets", noindex=True), 1),
        "/queues/trimming": (
            html("Trimming streams", "Old entries are trimmed, never pending ones.", ("/queues",)),
            2,
        ),
    }
    routes = {"/robots.txt": ALLOW_ALL_ROBOTS} | {path: reply for path, (reply, _) in pages.items()}
    return Site(routes, {path: depth for path, (_, depth) in pages.items()})


async def statuses(database: Database, job_id: uuid.UUID) -> dict[str, int]:
    async with database.session() as session:
        progress = await job_indexing(session, job_id)
    counts = {
        "pending": progress.pending,
        "queued": progress.queued,
        "indexed": progress.indexed,
        "failed": progress.failed,
    }
    return {status: count for status, count in counts.items() if count}


async def indexing_completed(database: Database, job_id: uuid.UUID) -> bool:
    return (await job_row(database, job_id)).indexing_completed_at is not None


async def search(database: Database, job_id: uuid.UUID, query: str) -> SearchResults:
    async with database.session() as session:
        return await search_job(session, job_id, query)


def paths(results: SearchResults, port: int) -> list[str]:
    return [hit.url.removeprefix(f"http://{HOST}:{port}") for hit in results.hits]


def indexed_by(cluster: Cluster) -> dict[str, list[str]]:
    """Page IDs each indexer process indexed, from its own log."""
    return {
        indexer.identity: [
            line["page_id"]
            for line in indexer.events("task.handled")
            if line["reason"] == "indexed"
        ]
        for indexer in cluster.indexers()
    }


async def attempts(database: Database, job_id: uuid.UUID) -> dict[str, int]:
    async with database.session() as session:
        rows = await session.execute(
            text("SELECT page_id, attempt_count FROM search_documents WHERE job_id = :id"),
            {"id": job_id},
        )
        return {str(row.page_id): row.attempt_count for row in rows}


# --- The pipeline --------------------------------------------------------------------------------


async def test_crawl_persist_publish_index_search_with_real_processes(
    database: Database,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
    test_database: Settings,
) -> None:
    site = docs_site()
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        processes = [
            cluster.scheduler(),
            cluster.worker(),
            cluster.worker(),
            cluster.index_dispatcher(),
            cluster.indexer(),
            cluster.indexer("--concurrency", "2"),
        ]
        await cluster.started(*processes)
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=2
        )

        async def fully_indexed() -> bool:
            return await indexing_completed(database, job_id)

        await wait_for(fully_indexed, within=120, what="the indexing to complete", cluster=cluster)
        report = await collect(test_database.model_copy(update={"redis_key_prefix": redis_prefix}))
        exit_codes = await cluster.stop_all()

    assert set(exit_codes.values()) == {0}
    job = await job_row(database, job_id)
    assert job.status == "completed", job.failure_reason
    assert job.finished_at is not None
    assert job.indexing_completed_at is not None
    assert job.indexing_completed_at >= job.finished_at
    await assert_job_invariants(database, job_id)
    assert await statuses(database, job_id) == {"indexed": 5}  # six pages, one noindex
    assert set((await attempts(database, job_id)).values()) == {1}
    # Each page was indexed by exactly one indexer process (read from their own logs).
    indexed = [page for pages in indexed_by(cluster).values() for page in pages]
    assert sorted(indexed) == sorted(await attempts(database, job_id))
    assert len(await events(database, job_id, "job.indexing_completed")) == 1
    stream = IndexStream(redis_client, key_prefix=redis_prefix)
    assert (await stream.pending()).count == 0

    # Search: title matches first, the noindex page never, nothing from another job.
    fencing = await search(database, job_id, "fencing")
    assert paths(fencing, server.port) == ["/leases"]
    redis = await search(database, job_id, "redis")
    assert paths(redis, server.port) == ["/queues", "/leases"]
    assert paths(await search(database, job_id, "trimmed"), server.port) == ["/queues/trimming"]
    assert redis.indexing.state.value == "indexed"
    async with database.session() as session:
        detail = await get_crawl_job(session, job_id)
    assert (detail.indexing.indexed, detail.indexing.state.value) == (5, "indexed")
    indexing = report["indexing"]
    assert indexing["documents"]["indexed"] == 5
    assert report["index_stream"]["exists"]


# --- Crashes -------------------------------------------------------------------------------------


async def test_an_indexer_killed_mid_attempt_loses_no_page(
    database: Database,
    test_database: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """The test holds one page's row lock, so the first indexer's attempt on it waits inside
    its index transaction (seen in pg_stat_activity). That indexer runs one task at a time and
    the held page is published first, so this is the only message it reads. It is killed. Its
    attempt is counted, its transaction never commits, and its message stays pending: a second
    indexer takes the expired lease over and indexes the page. Every other page is indexed once,
    by the second indexer."""
    job_id, _ = await new_job(database)
    pages = [await add_page(database, job_id, path=f"p{i}", body=f"page {i}") for i in range(6)]
    held = pages[0]
    await finish_job(database, job_id)
    cluster = clusters(hosts=[HOST], ports=[80])
    blocker = Database(test_database, application_name="sitescope-tests-blocker")
    try:
        async with blocker.session() as session:
            await session.execute(
                text("SELECT 1 FROM pages WHERE id = :id FOR UPDATE"), {"id": held}
            )
            dispatcher, first = cluster.index_dispatcher(), cluster.indexer("--concurrency", "1")
            await cluster.started(dispatcher, first)

            async def attempt_waits() -> bool:
                row = await document(database, held)
                return row.lease_owner is not None

            await wait_for(
                attempt_waits, within=60, what="the held page's attempt", cluster=cluster
            )
            await until_sessions_wait_for_locks(database, within=30)
            dead_owner = (await document(database, held)).lease_owner
            first.kill()
            assert await first.exited(within=10) != 0
            await session.rollback()
    finally:
        await blocker.dispose()
    second = cluster.indexer()
    await cluster.started(second)

    async def done() -> bool:
        return await indexing_completed(database, job_id)

    await wait_for(done, within=90, what="the indexing to complete", cluster=cluster)
    exit_codes = await cluster.stop_all()

    assert (exit_codes[dispatcher.name], exit_codes[second.name]) == (0, 0)
    assert await statuses(database, job_id) == {"indexed": 6}
    counts = await attempts(database, job_id)
    assert counts.pop(str(held)) == 2  # the killed attempt counts
    assert set(counts.values()) == {1}
    takeovers = second.events("index.lease_taken_over")
    assert any(line["previous_owner"] == dead_owner for line in takeovers), second.tail()
    assert (await IndexStream(redis_client, key_prefix=redis_prefix).pending()).count == 0


async def test_an_index_dispatcher_killed_between_xadd_and_commit_leaves_only_duplicates(
    database: Database,
    test_database: Settings,
    clusters: ClusterFactory,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    """``LOCK TABLE search_documents IN SHARE MODE`` lets the dispatcher lock its rows
    (``FOR UPDATE``) and publish them, but blocks its ``UPDATE``. It is killed right then: the
    messages are in Redis, the dispatch never commits. A new dispatcher publishes the documents
    again; each page is indexed once, and every message is acknowledged."""
    job_id, _ = await new_job(database)
    pages = [await add_page(database, job_id, path=f"p{i}", body=f"doc {i}") for i in range(3)]
    await finish_job(database, job_id)
    stream = IndexStream(redis_client, key_prefix=redis_prefix)
    cluster = clusters(hosts=[HOST], ports=[80])
    blocker = Database(test_database, application_name="sitescope-tests-blocker")
    try:
        async with blocker.session() as session:
            await session.execute(text("LOCK TABLE search_documents IN SHARE MODE"))
            first = cluster.index_dispatcher()
            await cluster.started(first)

            async def published_not_committed() -> bool:
                return await redis_client.xlen(stream.key) >= len(pages)

            await wait_for(
                published_not_committed, within=30, what="XADD before COMMIT", cluster=cluster
            )
            await until_sessions_wait_for_locks(database, within=30)
            first.kill()  # between XADD and COMMIT
            assert await first.exited(within=10) != 0
            await session.rollback()
    finally:
        await blocker.dispose()
    orphans = {entry for entry, _ in await stream_entries(redis_client, stream.key)}
    assert await statuses(database, job_id) == {"pending": 3}  # nothing committed
    processes = [cluster.index_dispatcher(), cluster.indexer(), cluster.indexer()]
    await cluster.started(*processes)

    async def done() -> bool:
        return await indexing_completed(database, job_id)

    await wait_for(done, within=60, what="the indexing to complete", cluster=cluster)

    async def drained() -> bool:
        return (await stream.pending()).count == 0

    await wait_for(drained, within=30, what="every message to be acknowledged", cluster=cluster)
    exit_codes = await cluster.stop_all()

    assert [exit_codes[process.name] for process in processes] == [0, 0, 0]
    assert await statuses(database, job_id) == {"indexed": 3}
    assert set((await attempts(database, job_id)).values()) == {1}
    assert await stream.length() == 6  # every document published twice
    handled = [
        line
        for indexer in cluster.indexers()
        for line in indexer.events("task.handled")
        if line["message_id"] in orphans
    ]
    assert len(handled) == 3
    assert all(line["disposition"] == "ack" for line in handled)
    assert {line["reason"] for line in handled} <= {
        "not_dispatched",
        "indexed",
        "already_indexed",
    }


# --- Outages -------------------------------------------------------------------------------------


async def test_index_processes_ride_out_redis_and_postgresql_outages(
    database: Database, test_database: Settings, clusters: ClusterFactory
) -> None:
    """The index dispatcher and two indexers reach Redis and PostgreSQL through proxies, which
    cut every connection for 3 s, Redis first, then PostgreSQL. The processes back off and
    recover on their own; every page is indexed and no document fails."""
    job_id, _ = await new_job(database)
    for index in range(40):
        await add_page(database, job_id, path=f"p{index}", body=f"outage page {index}")
    await finish_job(database, job_id)
    async with (
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
            ports=[80],
            settings=proxied,
            extra={"INDEX_DISPATCH_BATCH_SIZE": "2", "INDEX_DISPATCHER_INTERVAL_SECONDS": "0.3"},
        )
        processes = [cluster.index_dispatcher(), cluster.indexer(), cluster.indexer()]
        await cluster.started(*processes)

        async def some_indexed() -> bool:
            return (await statuses(database, job_id)).get("indexed", 0) >= 4

        await wait_for(some_indexed, within=60, what="indexing to start", cluster=cluster)
        # The outages themselves last a fixed time; nothing waits on their effects this way.
        redis_proxy.cut()
        await asyncio.sleep(3)
        redis_proxy.restore()
        postgres_proxy.cut()
        await asyncio.sleep(3)
        postgres_proxy.restore()

        async def done() -> bool:
            return await indexing_completed(database, job_id)

        await wait_for(done, within=120, what="the indexing to complete", cluster=cluster)
        assert all(process.alive for process in processes)
        exit_codes = await cluster.stop_all()

    assert set(exit_codes.values()) == {0}
    assert await statuses(database, job_id) == {"indexed": 40}
    logged = {line.get("event") for line in cluster.all_lines()}
    assert logged & {"worker.error", "index_dispatcher.pass_failed"}  # they met the outages
    assert logged & {"worker.recovered", "index_dispatcher.recovered"}  # and recovered


# --- Cancellation --------------------------------------------------------------------------------


async def test_a_job_cancelled_while_indexing_keeps_its_stored_pages_indexed(
    database: Database, crawl_settings: Settings, clusters: ClusterFactory
) -> None:
    """Policy (ADR-0018): cancelling stops the crawl, not the indexing. Pages stored before the
    cancellation took effect are indexed and searchable; the job stays ``cancelled``, and its
    indexing completes. Page "/0" answers after 30 s (its fetch deadline is 6 s), so BFS level 1
    cannot finish, and level 2 cannot start, before the cancellation."""
    site = tree_site(depth=2, branching=4, delay=0.3, delays={"/0": 30.0})  # 21 pages
    async with LocalHttpServer(site.routes) as server:
        cluster = clusters(hosts=[HOST], ports=[server.port])
        processes = [
            cluster.scheduler(),
            cluster.worker(),
            cluster.index_dispatcher(),
            cluster.indexer(),
        ]
        await cluster.started(*processes)
        job_id = await create_local_job(
            database, crawl_settings, host=HOST, ports=[server.port], port=server.port, max_depth=2
        )

        async def some_indexed() -> bool:
            return (await statuses(database, job_id)).get("indexed", 0) >= 2

        await wait_for(some_indexed, within=60, what="the first pages indexed", cluster=cluster)
        async with database.session() as session:
            await cancel_crawl_job(session, job_id)

        async def done() -> bool:
            job = await job_row(database, job_id)
            return job.status == JobStatus.CANCELLED.value and job.indexing_completed_at is not None

        await wait_for(done, within=90, what="cancellation and indexing", cluster=cluster)
        exit_codes = await cluster.stop_all()

    assert set(exit_codes.values()) == {0}
    job = await job_row(database, job_id)
    assert job.status == "cancelled"  # never reactivated by indexing
    progress = await statuses(database, job_id)
    async with database.session() as session:
        stored = await session.scalar(
            text("SELECT count(*) FROM pages WHERE job_id = :id"), {"id": job_id}
        )
    assert progress == {"indexed": stored}
    assert 2 <= stored <= 4  # the root and at most the three fast pages of level 1
    results = await search(database, job_id, "text")  # every tree page says "Some text."
    assert results.total == stored
    [cancelled] = await events(database, job_id, "job.cancelled")
    [completed] = await events(database, job_id, "job.indexing_completed")
    assert completed.id > cancelled.id
