r"""Recovery from failures, measured with real processes (Phase 10).

Every scenario runs ``--repeat`` times with the short timings of the process tests
(``tests/process_support.FAST_TIMING``: 7 s crawl leases, messages reclaimed after 7 s idle,
checked every 0.5 s; index tasks reclaimed after 5 s; reconciliation after 10 s). Recovery times
scale with those settings: with the defaults (90 s leases, 100 s reclaim idle time) a crashed
worker's tasks are taken over after about 100 s.

Scenarios (the clock is the benchmark's; it marks the fault, the service's return, the first
progress after it and the job's end):

* ``control``: the crawl workload without a fault (the reference completion time).
* ``worker_crash``: 2 crawler workers; one is killed (TerminateProcess) while it holds leases.
  Measured: the time until every URL it held was claimed again or finished, and until the job
  completed; duplicate fetches (the server's request counts), takeovers, attempts.
* ``indexer_crash``: 2 indexers index 3,000 pages; one is killed mid-way; then 200 pages change
  content (version 2, back to pending). Measured: the time until the killed indexer's documents
  were indexed, until all were; every document must end indexed at its page's current version.
* ``redis_restart``: a disposable Redis container with append-only persistence, stopped
  (``docker stop``) mid-crawl and started 5 s later.
* ``redis_data_loss``: the same without persistence: Redis comes back empty, so the stream, the
  consumer group and the politeness state are gone; the reconciler publishes the tasks again.
* ``postgres_restart``: a disposable PostgreSQL container, stopped mid-crawl and started 5 s
  later. No URL may fail because of the outage (delivery counts must not exhaust attempts).
* ``cancellation``: three jobs on three hosts crawled by 3 workers; one job is cancelled through
  the service the API uses. Measured: the time until it is ``cancelled``, requests to its host
  that started after the cancellation committed, and whether the other jobs complete; then the
  scheduler is restarted and the cancelled job must stay cancelled with no new request.

Every crawl ends with the correctness invariants of ``tests/crawl_support.assert_job_invariants``.
The development database and Redis are never stopped: the outages use disposable containers in
WSL Docker (random passwords, bound to 127.0.0.1, removed at the end).

Run from backend\ with PostgreSQL and Redis up (and Docker in WSL)::

    .\.venv\Scripts\python.exe ..\benchmarks\recovery.py `
        --output ..\benchmarks\results\phase10-recovery.json
"""

import argparse
import asyncio
import contextlib
import secrets
import socket
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchlib import (
    VariantCluster,
    bench_settings,
    emit,
    environment,
    fresh_database,
    process_environment,
    trials,
    write_json,
)
from indexing import HOST as INDEX_HOST
from indexing import dataset, store
from pydantic import SecretStr
from sqlalchemy import text

from app.core.config import Settings
from app.core.redis import create_redis_client
from app.db.session import Database
from app.services.crawl_cancellation import cancel_crawl_job
from tests.crawl_support import assert_job_invariants, create_local_job, new_job
from tests.fetch_support import LocalHttpServer
from tests.index_support import change_content
from tests.integration import helpers
from tests.process_support import tree_site

SCENARIOS = (
    "control",
    "worker_crash",
    "indexer_crash",
    "redis_restart",
    "redis_data_loss",
    "postgres_restart",
    "cancellation",
)
HOSTS = ["127.0.0.2", "127.0.0.3", "127.0.0.4"]
REDIS_IMAGE = "redis:8.10.3-alpine3.23"
POSTGRES_IMAGE = "postgres:18.6-trixie"
OUTAGE_SECONDS = 5.0
CRAWL_SETTINGS = {
    "LOG_LEVEL": "INFO",
    "CRAWL_CONCURRENCY": "4",
    "POLITENESS_INTERVAL_SECONDS": "0",
    "POLITENESS_MAX_CONCURRENT_PER_HOST": "4",
}
_TERMINAL = text(
    "SELECT count(*) FROM crawl_urls"
    " WHERE job_id = :job_id AND state NOT IN ('pending', 'queued', 'in_progress')"
)
_JOB_STATUS = text("SELECT status FROM crawl_jobs WHERE id = :job_id")
_HELD = text("SELECT id FROM crawl_urls WHERE state = 'in_progress' AND lease_owner = :owner")
_STILL_HELD = text(
    "SELECT count(*) FROM crawl_urls"
    " WHERE id = ANY(CAST(:ids AS uuid[])) AND state = 'in_progress' AND lease_owner = :owner"
)
_URL_SUMMARY = text(
    """
    SELECT count(*) FILTER (WHERE state = 'failed') AS failed,
           count(*) FILTER (WHERE outcome = 'max_deliveries_exceeded') AS quarantined,
           count(*) FILTER (WHERE attempt_count > 1) AS retried,
           max(attempt_count) AS max_attempts,
           count(*) FILTER (WHERE outcome = 'infrastructure_error') AS infrastructure_errors,
           count(*) AS urls
    FROM crawl_urls WHERE job_id = :job_id
    """
)
_EVENTS = text(
    "SELECT event_type, count(*) AS n FROM crawl_events WHERE job_id = :job_id GROUP BY 1"
)


def docker(*arguments: str, timeout: float = 120) -> str:
    """``docker`` inside WSL, arguments passed as they are (no shell)."""
    completed = subprocess.run(  # noqa: S603 - a fixed program and arguments built here
        ["wsl", "-e", "docker", *arguments],  # noqa: S607 - wsl.exe from PATH
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker {arguments[0]} failed: {completed.stderr.strip()[:300]}")
    return completed.stdout.strip()


def published_port(name: str, port: str) -> int:
    return int(docker("port", name, f"{port}/tcp").splitlines()[0].rpartition(":")[2])


def free_port() -> int:
    """A loopback port that is free on this machine now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run_container(name: str, container_port: int, *arguments: str) -> int:
    """``docker run`` a container that publishes ``container_port`` on a loopback port chosen
    here; returns that port.

    Not an ephemeral mapping (``127.0.0.1::6379``): Docker may give such a container another port
    when it is stopped and started, while the processes keep the port they were given.
    """
    for _ in range(5):
        port = free_port()
        try:
            docker(
                "run",
                "--detach",
                "--name",
                name,
                "--publish",
                f"127.0.0.1:{port}:{container_port}",
                *arguments,
            )
        except RuntimeError as exc:
            with contextlib.suppress(RuntimeError):
                docker("rm", "--force", "--volumes", name)
            if "already" in str(exc) or "in use" in str(exc):
                continue  # taken in WSL meanwhile: another port
            raise
        return port
    raise RuntimeError(f"no free loopback port for {name}")


def start_again(name: str, container_port: int, port: int) -> None:
    """``docker start`` a stopped container; it must come back on the same port."""
    docker("start", name)
    now = published_port(name, str(container_port))
    if now != port:
        raise RuntimeError(f"{name} came back on port {now}, not {port}")


@dataclass
class Clock:
    """Moments of one trial, in seconds since its start (the benchmark's monotonic clock)."""

    started: float = field(default_factory=time.monotonic)
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str) -> float:
        self.marks[name] = round(time.monotonic() - self.started, 3)
        return self.marks[name]

    def between(self, first: str, second: str) -> float | None:
        if first in self.marks and second in self.marks:
            return round(self.marks[second] - self.marks[first], 3)
        return None


async def until(
    condition: Callable[[], Awaitable[bool]], *, within: float, what: str, interval: float = 0.1
) -> None:
    """Poll ``condition`` (errors count as "not yet": the database may be down)."""
    deadline = time.monotonic() + within
    while True:
        with contextlib.suppress(Exception):
            if await condition():
                return
        if time.monotonic() > deadline:
            raise RuntimeError(f"timed out waiting for {what}")
        await asyncio.sleep(interval)


async def scalar(database: Database, statement: Any, **params: Any) -> Any:
    async with database.session() as session:
        return await session.scalar(statement, params)


async def crawl_report(
    database: Database, job_id: uuid.UUID, servers: list[LocalHttpServer]
) -> dict[str, Any]:
    async with database.session() as session:
        summary = (await session.execute(_URL_SUMMARY, {"job_id": job_id})).one()
        events = {
            row.event_type: row.n for row in await session.execute(_EVENTS, {"job_id": job_id})
        }
        status = await session.scalar(_JOB_STATUS, {"job_id": job_id})
    requests = Counter(
        item.target for server in servers for item in server.served if item.target != "/robots.txt"
    )
    invariant_error = None
    try:
        await assert_job_invariants(database, job_id)
    except AssertionError as exc:
        invariant_error = repr(exc)[:300]
    return {
        "job_status": status,
        "urls": summary.urls,
        "failed_urls": summary.failed,
        "quarantined_urls": summary.quarantined,
        "infrastructure_errors": summary.infrastructure_errors,
        "urls_retried": summary.retried,
        "max_attempts": summary.max_attempts,
        "events": events,
        "page_requests": sum(requests.values()),
        "duplicate_fetches": sum(count - 1 for count in requests.values() if count > 1),
        "invariants_ok": invariant_error is None,
        "invariant_error": invariant_error,
    }


@contextlib.asynccontextmanager
async def crawl(
    settings: Settings,
    database: Database,
    *,
    hosts: list[str],
    workers: int,
    delay: float,
    logs: Path,
    tag: str,
) -> AsyncIterator[tuple[VariantCluster, list[LocalHttpServer], list[uuid.UUID]]]:
    """Servers, a scheduler and ``workers`` workers, and one job per host (depth 3, branching 5:
    156 pages a host, each answered after ``delay`` s)."""
    site = tree_site(depth=3, branching=5, delay=delay)
    servers = [LocalHttpServer(site.routes, host=host) for host in hosts]
    for server in servers:
        await server.__aenter__()
    ports = [server.port for server in servers]
    prefix = f"bench-{uuid.uuid4().hex[:10]}"
    env = process_environment(
        settings, prefix=prefix, hosts=hosts, ports=ports, extra=CRAWL_SETTINGS
    )
    run_dir = logs / f"{tag}-{prefix}"
    run_dir.mkdir(parents=True)
    cluster = VariantCluster(env, run_dir)
    try:
        processes = [cluster.scheduler()] + [cluster.worker() for _ in range(workers)]
        await cluster.started(*processes)
        jobs = [
            await create_local_job(
                database, settings, host=host, ports=ports, port=server.port, max_depth=3
            )
            for host, server in zip(hosts, servers, strict=True)
        ]
        yield cluster, servers, jobs
    finally:
        cluster.kill_all()
        for server in servers:
            await server.__aexit__(None, None, None)
        redis = create_redis_client(settings, client_name="sitescope-benchmark")
        try:
            keys = [key async for key in redis.scan_iter(match=f"{prefix}:*", count=1000)]
            if keys:
                await redis.delete(*keys)
        except Exception:  # noqa: S110 - a disposable Redis may be gone already
            pass
        finally:
            await redis.aclose()


async def finished(database: Database, job_id: uuid.UUID) -> bool:
    return await scalar(database, _JOB_STATUS, job_id=job_id) in (
        "completed",
        "failed",
        "cancelled",
    )


async def progress_after(database: Database, job_id: uuid.UUID, clock: Clock, name: str) -> None:
    """Mark ``name`` when the job's next URL finishes."""
    counted: list[int] = []

    async def read() -> bool:  # retried: a pooled connection may have died with the server
        counted.append(int(await scalar(database, _TERMINAL, job_id=job_id)))
        return True

    await until(read, within=120, what="the URL count")
    before = counted[-1]

    async def moved() -> bool:
        return int(await scalar(database, _TERMINAL, job_id=job_id)) > int(before)

    await until(moved, within=300, what="progress")
    clock.mark(name)


async def wait_half_done(database: Database, job_id: uuid.UUID, total: int, share: float) -> None:
    async def far_enough() -> bool:
        return int(await scalar(database, _TERMINAL, job_id=job_id)) >= total * share

    await until(far_enough, within=300, what="mid-crawl")


# --- Scenarios ------------------------------------------------------------------------------------


async def control(settings: Settings, database: Database, logs: Path) -> dict[str, Any]:
    await helpers.truncate_all(database)
    async with crawl(
        settings, database, hosts=HOSTS[:1], workers=2, delay=0.3, logs=logs, tag="control"
    ) as (_, servers, [job_id]):
        clock = Clock()  # from the job's creation: the processes are running already
        await until(lambda: finished(database, job_id), within=600, what="the job")
        clock.mark("completed")
        report = await crawl_report(database, job_id, servers)
    return {"seconds": clock.marks, **report}


async def worker_crash(settings: Settings, database: Database, logs: Path) -> dict[str, Any]:
    await helpers.truncate_all(database)
    async with crawl(
        settings, database, hosts=HOSTS[:1], workers=2, delay=0.3, logs=logs, tag="worker-crash"
    ) as (cluster, servers, [job_id]):
        clock = Clock()
        await wait_half_done(database, job_id, 156, 0.3)
        victim = cluster.workers()[0]
        name = victim.identity
        victim.kill()
        clock.mark("killed")
        async with database.session() as session:
            held = [row.id for row in await session.execute(_HELD, {"owner": name})]

        async def released() -> bool:
            count = await scalar(database, _STILL_HELD, ids=held, owner=name)
            return int(count) == 0

        # Nothing held at the kill means nothing to recover: no recovery time is reported.
        if held:
            await until(released, within=300, what="the takeover")
            clock.mark("recovered")
        await until(lambda: finished(database, job_id), within=600, what="the job")
        clock.mark("completed")
        report = await crawl_report(database, job_id, servers)
    return {
        "seconds": clock.marks,
        "held_by_killed_worker": len(held),
        "recovery_seconds": clock.between("killed", "recovered"),
        "completion_after_kill_seconds": clock.between("killed", "completed"),
        **report,
    }


async def indexer_crash(settings: Settings, database: Database, logs: Path) -> dict[str, Any]:
    await helpers.truncate_all(database)
    _, columns = dataset(3000, 1000)
    job_id, _ = await new_job(database, host=INDEX_HOST)
    prefix = f"bench-{uuid.uuid4().hex[:10]}"
    env = process_environment(
        settings,
        prefix=prefix,
        hosts=["127.0.0.1"],
        ports=[80],
        extra={
            "LOG_LEVEL": "INFO",
            "INDEX_DISPATCH_BATCH_SIZE": "1000",
            "INDEX_MAX_OUTSTANDING": "3000",
            "INDEX_DISPATCHER_INTERVAL_SECONDS": "0.1",
        },
    )
    run_dir = logs / f"indexer-crash-{prefix}"
    run_dir.mkdir(parents=True)
    cluster = VariantCluster(env, run_dir)
    try:
        processes = [cluster.index_dispatcher()] + [cluster.indexer() for _ in range(2)]
        await cluster.started(*processes)
        clock = Clock()
        await store(database, job_id, columns)
        clock.mark("stored")

        async def indexed(at_least: int) -> bool:
            count = await scalar(
                database,
                text("SELECT count(*) FROM search_documents WHERE status = 'indexed'"),
            )
            return int(count) >= at_least

        await until(lambda: indexed(600), within=300, what="20 % indexed")
        victim = cluster.indexers()[0]
        name = victim.identity
        victim.kill()
        clock.mark("killed")
        async with database.session() as session:
            held = [
                row.page_id
                for row in await session.execute(
                    text(
                        "SELECT page_id FROM search_documents"
                        " WHERE status = 'queued' AND lease_owner = :owner"
                    ),
                    {"owner": name},
                )
            ]
            changed = [
                row.id
                for row in await session.execute(
                    text("SELECT id FROM pages WHERE job_id = :job_id ORDER BY id LIMIT 200"),
                    {"job_id": job_id},
                )
            ]
        for page_id in changed:  # new content while indexing runs: version 2, pending again
            await change_content(database, page_id, title="changed title")

        async def recovered() -> bool:
            count = await scalar(
                database,
                text(
                    "SELECT count(*) FROM search_documents"
                    " WHERE page_id = ANY(CAST(:ids AS uuid[])) AND status <> 'indexed'"
                ),
                ids=held,
            )
            return int(count) == 0

        if held:  # nothing held at the kill: no recovery time to report
            await until(recovered, within=300, what="the killed indexer's documents")
            clock.mark("recovered")
        await until(lambda: indexed(3000), within=600, what="every document")
        clock.mark("all_indexed")
        await cluster.stop_all()
    finally:
        cluster.kill_all()
        redis = create_redis_client(settings, client_name="sitescope-benchmark")
        try:
            keys = [key async for key in redis.scan_iter(match=f"{prefix}:*", count=1000)]
            if keys:
                await redis.delete(*keys)
        finally:
            await redis.aclose()
    async with database.session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FILTER (WHERE d.indexed_content_version = p.content_version)
                               AS current,
                           count(*) FILTER (WHERE p.content_version = 2
                                            AND d.indexed_content_version = 2) AS changed_current,
                           count(*) FILTER (WHERE d.status = 'failed') AS failed,
                           count(*) FILTER (WHERE d.attempt_count > 1) AS retried,
                           max(d.attempt_count) AS max_attempts,
                           count(*) AS documents
                    FROM search_documents AS d JOIN pages AS p ON p.id = d.page_id
                    """
                )
            )
        ).one()
    takeovers = sum(len(process.events("index.lease_taken_over")) for process in cluster.indexers())
    return {
        "seconds": clock.marks,
        "held_by_killed_indexer": len(held),
        "recovery_seconds": clock.between("killed", "recovered"),
        "completion_after_kill_seconds": clock.between("killed", "all_indexed"),
        "documents": row.documents,
        "indexed_at_current_version": row.current,
        "changed_pages_indexed_at_version_2": row.changed_current,
        "changed_pages": len(changed),
        "failed_documents": row.failed,
        "documents_retried": row.retried,
        "max_attempts": row.max_attempts,
        "lease_takeovers": takeovers,
        "no_stale_version": row.current == row.documents,
    }


@contextlib.asynccontextmanager
async def disposable_redis(
    settings: Settings, *, persistent: bool
) -> AsyncIterator[tuple[Settings, str]]:
    name = f"sitescope-bench-redis-{secrets.token_hex(4)}"
    password = secrets.token_hex(16)
    persistence = (
        ["--appendonly", "yes", "--appendfsync", "everysec"]
        if persistent
        else ["--save", "", "--appendonly", "no"]
    )
    port = run_container(
        name, 6379, REDIS_IMAGE, "redis-server", *persistence, "--requirepass", password
    )
    try:
        yield (
            settings.model_copy(update={"redis_port": port, "redis_password": SecretStr(password)}),
            name,
        )
    finally:
        docker("rm", "--force", "--volumes", name)


async def redis_ping(settings: Settings) -> bool:
    redis = create_redis_client(settings, client_name="sitescope-benchmark")
    try:
        return bool(await redis.ping())
    finally:
        await redis.aclose()


async def redis_outage(
    settings: Settings, database: Database, logs: Path, *, persistent: bool
) -> dict[str, Any]:
    await helpers.truncate_all(database)
    tag = "redis-restart" if persistent else "redis-data-loss"
    async with disposable_redis(settings, persistent=persistent) as (redis_settings, container):
        await until(lambda: redis_ping(redis_settings), within=60, what="the disposable Redis")
        async with crawl(
            redis_settings, database, hosts=HOSTS[:1], workers=2, delay=0.3, logs=logs, tag=tag
        ) as (cluster, servers, [job_id]):
            clock = Clock()
            await wait_half_done(database, job_id, 156, 0.3)
            docker("stop", "--time", "5", container)
            clock.mark("stopped")
            await asyncio.sleep(OUTAGE_SECONDS)
            start_again(container, 6379, redis_settings.redis_port)
            await until(lambda: redis_ping(redis_settings), within=60, what="Redis back")
            clock.mark("back")
            await progress_after(database, job_id, clock, "first_progress")
            await until(lambda: finished(database, job_id), within=600, what="the job")
            clock.mark("completed")
            report = await crawl_report(database, job_id, servers)
            lines = cluster.all_lines()
    return {
        "seconds": clock.marks,
        "outage_seconds": clock.between("stopped", "back"),
        "resume_after_return_seconds": clock.between("back", "first_progress"),
        "completion_after_return_seconds": clock.between("back", "completed"),
        "worker_errors": sum(1 for line in lines if line.get("event") == "worker.error"),
        "pass_failures": sum(1 for line in lines if line.get("event") == "scheduler.pass_failed"),
        "republished": sum(
            int(line.get("count", 0)) for line in lines if line.get("event") == "task.republished"
        ),
        **report,
    }


async def postgres_restart(settings: Settings, logs: Path) -> dict[str, Any]:
    name = f"sitescope-bench-postgres-{secrets.token_hex(4)}"
    password = secrets.token_hex(16)
    database_name = f"{settings.postgres_db}_recovery"
    port = run_container(
        name,
        5432,
        "--env",
        f"POSTGRES_USER={settings.postgres_user}",
        "--env",
        f"POSTGRES_PASSWORD={password}",
        "--env",
        f"POSTGRES_DB={database_name}",
        POSTGRES_IMAGE,
    )
    database: Database | None = None
    try:
        pg_settings = settings.model_copy(
            update={
                "postgres_port": port,
                "postgres_password": SecretStr(password),
                "postgres_db": database_name,
            }
        )
        helpers.require_disposable(pg_settings.postgres_db)

        async def ready() -> bool:
            return await helpers.probe_postgres(pg_settings) is None

        await until(ready, within=120, what="the disposable PostgreSQL", interval=0.5)
        await asyncio.sleep(2)  # the image restarts the server once after initialization
        await until(ready, within=120, what="the disposable PostgreSQL", interval=0.5)
        await helpers.upgrade(pg_settings, "head")
        database = Database(pg_settings, application_name="sitescope-benchmark")
        async with crawl(
            pg_settings, database, hosts=HOSTS[:1], workers=2, delay=0.3, logs=logs, tag="pg"
        ) as (cluster, servers, [job_id]):
            clock = Clock()
            await wait_half_done(database, job_id, 156, 0.3)
            docker("stop", "--time", "10", name)
            clock.mark("stopped")
            await asyncio.sleep(OUTAGE_SECONDS)
            start_again(name, 5432, port)
            await until(ready, within=120, what="PostgreSQL back", interval=0.2)
            clock.mark("back")
            await progress_after(database, job_id, clock, "first_progress")
            await until(lambda: finished(database, job_id), within=600, what="the job")
            clock.mark("completed")
            report = await crawl_report(database, job_id, servers)
            lines = cluster.all_lines()
        return {
            "seconds": clock.marks,
            "outage_seconds": clock.between("stopped", "back"),
            "resume_after_return_seconds": clock.between("back", "first_progress"),
            "completion_after_return_seconds": clock.between("back", "completed"),
            "worker_errors": sum(1 for line in lines if line.get("event") == "worker.error"),
            "pass_failures": sum(
                1 for line in lines if line.get("event") == "scheduler.pass_failed"
            ),
            "dead_lettered": sum(
                1
                for line in lines
                if line.get("event") == "task.handled" and line.get("disposition") == "dead_letter"
            ),
            **report,
        }
    finally:
        if database is not None:
            await database.dispose()
        docker("rm", "--force", "--volumes", name)


async def cancellation(settings: Settings, database: Database, logs: Path) -> dict[str, Any]:
    await helpers.truncate_all(database)
    async with crawl(
        settings, database, hosts=HOSTS, workers=3, delay=0.2, logs=logs, tag="cancellation"
    ) as (cluster, servers, jobs):
        clock = Clock()
        target, others = jobs[0], jobs[1:]
        await wait_half_done(database, target, 156, 0.3)
        async with database.session() as session:
            await cancel_crawl_job(session, target)
        cancelled_at = time.monotonic()
        clock.mark("cancel_committed")
        await until(
            lambda: _status_is(database, target, "cancelled"), within=300, what="the cancellation"
        )
        clock.mark("cancelled")
        # Restart the scheduler: the cancelled job must stay cancelled.
        scheduler = next(
            process for process in cluster.processes if process.name.startswith("scheduler")
        )
        restarted_at = time.monotonic()
        scheduler.kill()
        restarted = cluster.scheduler()
        await cluster.started(restarted)
        for job_id in others:
            await until(lambda job_id=job_id: finished(database, job_id), within=600, what="a job")
        clock.mark("others_completed")
        await asyncio.sleep(2)  # a request in flight finishes (pages answer after 0.2 s)
        status = await scalar(database, _JOB_STATUS, job_id=target)
        reports = [await crawl_report(database, job_id, servers) for job_id in jobs]
        # Read at the end: the server records a request when it finishes.
        late = [
            item
            for item in servers[0].served
            if item.started > cancelled_at and item.target != "/robots.txt"
        ]
    return {
        "seconds": clock.marks,
        "cancel_to_cancelled_seconds": clock.between("cancel_committed", "cancelled"),
        "requests_started_after_cancel": len(late),
        "latest_start_after_cancel_seconds": (
            round(max(item.started for item in late) - cancelled_at, 3) if late else None
        ),
        "requests_after_scheduler_restart": sum(1 for item in late if item.started > restarted_at),
        "status_after_restart": status,
        "other_jobs": [report["job_status"] for report in reports[1:]],
        "invariants_ok": all(report["invariants_ok"] for report in reports),
        "failed_urls": sum(report["failed_urls"] for report in reports),
    }


async def _status_is(database: Database, job_id: uuid.UUID, status: str) -> bool:
    return await scalar(database, _JOB_STATUS, job_id=job_id) == status


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    keys = (
        "recovery_seconds",
        "completion_after_kill_seconds",
        "outage_seconds",
        "resume_after_return_seconds",
        "completion_after_return_seconds",
        "cancel_to_cancelled_seconds",
    )
    summary: dict[str, Any] = {}
    for scenario in SCENARIOS:
        tried = [run for run in runs if run["scenario"] == scenario]
        if not tried:
            continue
        members = [run for run in tried if "error" not in run]
        entry: dict[str, Any] = {"trials": len(members), "failed_runs": len(tried) - len(members)}
        for key in keys:
            values = [run[key] for run in members if run.get(key) is not None]
            if values:
                entry[key] = trials(values)
        completed = [run["seconds"].get("completed") for run in members]
        if completed and all(value is not None for value in completed):
            entry["completed_seconds"] = trials([float(value) for value in completed])
        entry["all_invariants_ok"] = all(run.get("invariants_ok", True) for run in members)
        summary[scenario] = entry
    return summary


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--logs", type=Path, default=Path(tempfile.gettempdir()) / "sitescope-bench-recovery"
    )
    args = parser.parse_args()
    chosen = args.scenarios.split(",")
    if unknown := set(chosen) - set(SCENARIOS):
        raise SystemExit(f"unknown scenarios: {sorted(unknown)}")
    settings = bench_settings()
    await fresh_database(settings)
    database = Database(settings, application_name="sitescope-benchmark")
    runs: list[dict[str, Any]] = []
    env_info: dict[str, Any] = {}

    def save(*, complete: bool) -> None:
        """Written after every run, so a failure keeps the runs before it."""
        write_json(
            args.output,
            {
                "benchmark": "recovery",
                "complete": complete,
                "environment": env_info,
                "configuration": {
                    "timings": "tests/process_support.FAST_TIMING (7 s leases and reclaim idle, "
                    "0.5 s reclaim interval, 5 s index reclaim idle, 10 s reconciliation)",
                    "crawl_site": "a tree of depth 3 and branching 5 (156 pages) per host",
                    "crawl_settings": CRAWL_SETTINGS,
                    "outage_seconds": OUTAGE_SECONDS,
                    "repeat": args.repeat,
                    "median_of": "trials",
                    "clock": "starts when the processes are running (the job's creation)",
                },
                "summary": summarize(runs),
                "runs": runs,
            },
        )

    try:
        env_info = await environment(settings, database)
        for trial in range(1, args.repeat + 1):
            for scenario in chosen:
                try:
                    result = await run_scenario(scenario, settings, database, args.logs)
                except Exception as exc:  # recorded in the results; the other runs still go on
                    result = {"error": repr(exc)[:500]}
                record = {"scenario": scenario, "trial": trial, **result}
                runs.append(record)
                emit(record)
                save(complete=False)
    finally:
        await database.dispose()
        await helpers.drop_database(settings)
    save(complete=True)
    if failed := sum(1 for run in runs if "error" in run):
        raise SystemExit(f"{failed} run(s) failed; their errors are in {args.output}")


async def run_scenario(
    scenario: str, settings: Settings, database: Database, logs: Path
) -> dict[str, Any]:
    if scenario == "control":
        return await control(settings, database, logs)
    if scenario == "worker_crash":
        return await worker_crash(settings, database, logs)
    if scenario == "indexer_crash":
        return await indexer_crash(settings, database, logs)
    if scenario == "redis_restart":
        return await redis_outage(settings, database, logs, persistent=True)
    if scenario == "redis_data_loss":
        return await redis_outage(settings, database, logs, persistent=False)
    if scenario == "postgres_restart":
        return await postgres_restart(settings, logs)
    return await cancellation(settings, database, logs)


if __name__ == "__main__":
    asyncio.run(main())
