"""Redis loses all its data in the middle of a crawl (Phase 6) or of the indexing (Phase 7).

Real processes, marker ``fault``.

``FLUSHALL`` is never sent to the development Redis, not even to its test database 15 (a
database number does not isolate ``FLUSHALL``). This test starts its own disposable Redis
container (``redis:8.10.3-alpine3.23``, no persistence, a random password, bound to 127.0.0.1
on a random port; Docker in WSL on Windows, the local Docker Engine elsewhere). It runs the
scheduler and the workers against it, and flushes it only after checking that it is not the
development server: another port, another ``run_id``, and a sentinel key in the development
Redis that must survive. The container is removed at the end.

PostgreSQL holds the truth (ADR-0001): the lost stream, consumer group, politeness slots and
robots.txt cache are rebuilt, the reconciler publishes the lost tasks again, and the job
completes with every invariant intact.
"""

import asyncio
import secrets
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import Settings
from app.core.redis import create_redis_client
from app.db.session import Database
from tests.crawl_support import (
    assert_job_invariants,
    create_local_job,
    job_row,
    new_job,
    url_rows,
)
from tests.fetch_support import LocalHttpServer
from tests.index_support import add_page, finish_job
from tests.process_support import tree_site, wait_for
from tests.service_fixtures import ClusterFactory

pytestmark = pytest.mark.anyio

HOST = "127.0.0.1"
IMAGE = "redis:8.10.3-alpine3.23"


@dataclass(frozen=True)
class DisposableRedis:
    name: str  # the container's name
    settings: Settings  # the test settings, pointed at this server


# Docker Engine runs inside WSL on the Windows development machine (docs/development.md, "Docker
# (WSL 2)"), and natively on Linux (CI).
DOCKER = ("wsl", "-e", "docker") if sys.platform == "win32" else ("docker",)


def docker(*arguments: str, timeout: float = 120) -> str:
    """Run ``docker`` (no shell: every argument is passed as is)."""
    completed = subprocess.run(  # noqa: S603 - a fixed program and arguments built here
        [*DOCKER, *arguments],  # wsl.exe or docker from PATH
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker {arguments[0]} failed: {completed.stderr.strip()[:300]}")
    return completed.stdout.strip()


@pytest.fixture
def disposable_redis(
    pytestconfig: pytest.Config, test_database: Settings
) -> Iterator[DisposableRedis]:
    try:
        docker("version", "--format", "{{.Server.Version}}", timeout=30)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        reason = f"Docker is unavailable for a disposable Redis ({exc})"
        if pytestconfig.getoption("--require-services"):
            pytest.fail(reason, pytrace=False)
        pytest.skip(reason)
    name = f"sitescope-test-redis-{secrets.token_hex(4)}"
    password = secrets.token_hex(16)
    docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--publish",
        "127.0.0.1::6379",
        IMAGE,
        "redis-server",
        "--save",
        "",
        "--appendonly",
        "no",
        "--requirepass",
        password,
    )
    try:
        address = docker("port", name, "6379/tcp").splitlines()[0]  # "127.0.0.1:49153"
        host, _, port = address.rpartition(":")
        settings = test_database.model_copy(
            update={
                "redis_host": host,
                "redis_port": int(port),
                "redis_password": SecretStr(password),
            }
        )
        yield DisposableRedis(name, settings)
    finally:
        docker("rm", "--force", name)


@pytest.fixture
async def disposable_client(disposable_redis: DisposableRedis) -> AsyncIterator[Redis]:
    client = create_redis_client(disposable_redis.settings, client_name="sitescope-tests-flush")
    try:
        for _ in range(100):  # the server may still be starting
            try:
                await client.ping()
                break
            except Exception:
                await asyncio.sleep(0.1)
        else:
            raise AssertionError("the disposable Redis did not start")
        yield client
    finally:
        await client.aclose()


async def flush_disposable(
    disposable: Redis, development: Redis, settings: Settings, development_settings: Settings
) -> None:
    """``FLUSHALL``, after proving that ``disposable`` is not the development server."""
    assert settings.redis_port != development_settings.redis_port
    disposable_id = (await disposable.info("server"))["run_id"]
    development_id = (await development.info("server"))["run_id"]
    assert disposable_id != development_id
    await disposable.flushall()


async def test_the_crawl_survives_a_redis_that_lost_all_its_data(
    database: Database,
    crawl_settings: Settings,
    clusters: ClusterFactory,
    disposable_redis: DisposableRedis,
    disposable_client: Redis,
    redis_client: Redis,
    service_settings: Settings,
) -> None:
    sentinel = f"sitescope-test-sentinel-{uuid.uuid4().hex}"
    await redis_client.set(sentinel, "must survive", ex=600)  # development Redis, DB 15
    site = tree_site(depth=2, branching=4, delay=0.5)  # 21 pages, about 5 s with 2 per host
    try:
        async with LocalHttpServer(site.routes) as server:
            # A dispatch window wider than the workers' task slots (as before ADR-0022), so that
            # dispatched tasks still wait in the stream when Redis loses its data and the
            # reconciler must publish them again. With the default window (two tasks per
            # politeness slot) the workers claim every dispatched task at once, and nothing
            # waits to be lost (docs/performance.md, "Recovery from failures").
            cluster = clusters(
                hosts=[HOST],
                ports=[server.port],
                settings=disposable_redis.settings,
                extra={"SCHEDULER_TASKS_PER_HOST_SLOT": "50"},
            )
            processes = [cluster.scheduler(), cluster.worker(), cluster.worker()]
            await cluster.started(*processes)
            job_id = await create_local_job(
                database,
                crawl_settings,
                host=HOST,
                ports=[server.port],
                port=server.port,
                max_depth=2,
            )

            async def level_two_queued() -> bool:
                rows = await url_rows(database, job_id)
                return any(row.depth == 2 and row.state == "queued" for row in rows)

            await wait_for(level_two_queued, within=60, what="level 2 queued", cluster=cluster)
            prefix = cluster.environment["SITESCOPE_REDIS_KEY_PREFIX"]
            assert await disposable_client.exists(f"{prefix}:crawl")  # the stream lives there
            marker = f"{prefix}:flush-marker"  # written by this test only
            await disposable_client.set(marker, "before the flush")
            await flush_disposable(
                disposable_client, redis_client, disposable_redis.settings, service_settings
            )
            flushed_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            # The running processes write keys again at once (politeness slots, robots.txt, the
            # stream), so the database need not be empty by the time this runs: "DBSIZE == 0"
            # failed in 2 of 4 verification runs. The marker shows that the flush happened.
            assert not await disposable_client.exists(marker)
            assert await redis_client.get(sentinel) == "must survive"

            async def done() -> bool:
                return (await job_row(database, job_id)).status in ("completed", "failed")

            await wait_for(done, within=120, what="the job to finish", cluster=cluster)
            assert all(process.alive for process in processes)
            exit_codes = await cluster.stop_all()
    finally:
        await redis_client.delete(sentinel)

    assert set(exit_codes.values()) == {0}
    job = await job_row(database, job_id)
    assert job.status == "completed", job.failure_reason
    rows = await url_rows(database, job_id)
    assert {urlsplit(row.normalized_url).path for row in rows} == set(site.depths)
    assert {(row.state, row.outcome) for row in rows} == {("fetched", "html_stored")}
    await assert_job_invariants(database, job_id)
    lines = cluster.all_lines()
    # The lost tasks were found by the reconciler and published again; the group was rebuilt.
    republished = sum(
        int(line.get("republished", 0)) for line in lines if line.get("event") == "scheduler.pass"
    )
    assert republished >= 1, "nothing was published again"
    # Some process created the lost consumer group again (the scheduler's reconciliation or a
    # worker's read, whichever came first), and it exists at the end.
    recreated = [
        line
        for line in lines
        if line.get("event") in ("stream.group_created", "stream.group_recreated")
        and line["timestamp"] >= flushed_at
    ]
    assert recreated, "the lost consumer group was not created again"
    groups = await disposable_client.xinfo_groups(f"{prefix}:crawl")
    assert [group["name"] for group in groups] == ["crawlers"]


async def test_indexing_survives_a_redis_that_lost_all_its_data(
    database: Database,
    clusters: ClusterFactory,
    disposable_redis: DisposableRedis,
    disposable_client: Redis,
    redis_client: Redis,
    service_settings: Settings,
) -> None:
    """Phase 7. The index dispatcher has published every document (they are ``queued``) and no
    indexer has read them yet when Redis loses everything. The documents stay ``queued`` in
    PostgreSQL; the dispatcher's reconciliation finds their entries gone and publishes them
    again; the indexers started afterwards index every page."""
    sentinel = f"sitescope-test-sentinel-{uuid.uuid4().hex}"
    await redis_client.set(sentinel, "must survive", ex=600)  # development Redis, DB 15
    job_id, _ = await new_job(database)
    for index in range(12):
        await add_page(database, job_id, path=f"p{index}", body=f"lost index task {index}")
    await finish_job(database, job_id)
    try:
        cluster = clusters(hosts=[HOST], ports=[80], settings=disposable_redis.settings)
        dispatcher = cluster.index_dispatcher()
        await cluster.started(dispatcher)
        prefix = cluster.environment["SITESCOPE_REDIS_KEY_PREFIX"]

        async def all_queued() -> bool:
            return await queued_documents(database, job_id) == 12

        await wait_for(all_queued, within=60, what="every document queued", cluster=cluster)
        assert await disposable_client.xlen(f"{prefix}:index") == 12
        marker = f"{prefix}:flush-marker"
        await disposable_client.set(marker, "before the flush")
        await flush_disposable(
            disposable_client, redis_client, disposable_redis.settings, service_settings
        )
        assert not await disposable_client.exists(marker)
        assert await redis_client.get(sentinel) == "must survive"
        indexers = [cluster.indexer(), cluster.indexer()]
        await cluster.started(*indexers)

        async def complete() -> bool:
            return (await job_row(database, job_id)).indexing_completed_at is not None

        await wait_for(complete, within=120, what="the indexing to complete", cluster=cluster)
        exit_codes = await cluster.stop_all()
    finally:
        await redis_client.delete(sentinel)

    assert set(exit_codes.values()) == {0}
    assert await indexed_documents(database, job_id) == 12
    republished = sum(
        int(line.get("republished", 0))
        for line in dispatcher.lines()
        if line.get("event") == "index_dispatcher.pass"
    )
    assert republished >= 12, "the lost tasks were not published again"
    groups = await disposable_client.xinfo_groups(f"{prefix}:index")
    assert [group["name"] for group in groups] == ["indexers"]


async def queued_documents(database: Database, job_id: uuid.UUID) -> int:
    async with database.session() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM search_documents WHERE job_id = :id AND status = 'queued'"
                ),
                {"id": job_id},
            )
            or 0
        )


async def indexed_documents(database: Database, job_id: uuid.UUID) -> int:
    async with database.session() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM search_documents"
                    " WHERE job_id = :id AND status = 'indexed'"
                ),
                {"id": job_id},
            )
            or 0
        )
