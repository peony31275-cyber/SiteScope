"""Helpers for integration tests: disposable databases, migrations, and sample rows."""

import contextlib
import hashlib
import re
import socket
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, func, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.redis import create_redis_client
from app.db.models import CrawlJob, CrawlUrl, JobStatus, Page
from app.db.session import Database

BACKEND_DIR = Path(__file__).resolve().parents[2]
APP_TABLES = (
    "crawl_jobs",
    "crawl_urls",
    "pages",
    "page_links",
    "search_documents",
    "crawl_events",
)
_DISPOSABLE_DB = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*_test(_[a-z0-9_]+)?$")


# --- Service probes -----------------------------------------------------------------------------


async def probe_postgres(settings: Settings) -> str | None:
    """None if PostgreSQL accepts a connection with these settings, else the error type."""
    engine = create_async_engine(
        maintenance_url(settings), poolclass=NullPool, connect_args={"timeout": 5}
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # reported to the caller, which skips or fails the tests
        return type(exc).__name__
    finally:
        await engine.dispose()
    return None


async def probe_redis(settings: Settings) -> str | None:
    """None if Redis answers PING with these settings, else the error type."""
    client = create_redis_client(settings, client_name="sitescope-tests-probe")
    try:
        await client.ping()
    except Exception as exc:  # reported to the caller, which skips or fails the tests
        return type(exc).__name__
    finally:
        await client.aclose()
    return None


# --- Failure simulation ---------------------------------------------------------------------------


def unused_port() -> int:
    """A local TCP port with nothing listening on it: connections are refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@contextlib.contextmanager
def blackhole_server() -> Iterator[int]:
    """A local port that accepts TCP connections but never answers: a hung dependency.

    The kernel completes the handshake for queued connections even though nothing calls
    accept(), so clients connect successfully and then wait forever for a reply.
    """
    server = socket.create_server(("127.0.0.1", 0), backlog=16)
    try:
        yield server.getsockname()[1]
    finally:
        server.close()


# --- Disposable databases -----------------------------------------------------------------------


def maintenance_url(settings: Settings) -> URL:
    """The server's built-in ``postgres`` database, used for CREATE/DROP DATABASE."""
    return settings.database_url.set(database="postgres")


def _require_disposable(name: str) -> None:
    if not _DISPOSABLE_DB.fullmatch(name):
        raise RuntimeError(f"refusing to recreate or drop non-test database {name!r}")


async def recreate_database(settings: Settings) -> None:
    """Drop (if present) and create ``settings.postgres_db``. Test databases only."""
    name = settings.postgres_db
    _require_disposable(name)
    engine = create_async_engine(
        maintenance_url(settings), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


async def drop_database(settings: Settings) -> None:
    name = settings.postgres_db
    _require_disposable(name)
    engine = create_async_engine(
        maintenance_url(settings), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await engine.dispose()


# --- Alembic --------------------------------------------------------------------------------------


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["configure_logging"] = False
    if connection is not None:
        config.attributes["connection"] = connection
    return config


async def run_alembic(settings: Settings, action: Callable[[Config], None]) -> None:
    """Run an Alembic command on the given database through a shared connection."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: action(alembic_config(sync)))
    finally:
        await engine.dispose()


async def upgrade(settings: Settings, revision: str = "head") -> None:
    await run_alembic(settings, lambda config: command.upgrade(config, revision))


async def downgrade(settings: Settings, revision: str) -> None:
    await run_alembic(settings, lambda config: command.downgrade(config, revision))


async def truncate_all(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(text(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY"))


# --- Sample rows ----------------------------------------------------------------------------------


def sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def new_job(
    *,
    seed_url: str = "https://example.test/",
    max_pages: int = 100,
    max_depth: int = 3,
    status: JobStatus = JobStatus.QUEUED,
) -> CrawlJob:
    started = status is not JobStatus.QUEUED
    return CrawlJob(
        seed_url=seed_url,
        allowed_hosts=["example.test"],
        max_depth=max_depth,
        max_pages=max_pages,
        admitted_count=1,
        status=status.value,
        # The database clock, never the client's: the Windows host and the WSL VM clocks
        # differ (Phase 2B observed ~160 ms), and ck_crawl_jobs_timestamps_ordered compares
        # this value with the database-generated created_at (architecture invariant 11).
        started_at=func.now() if started else None,
    )


def new_url(job_id: uuid.UUID, url: str = "https://example.test/", depth: int = 0) -> CrawlUrl:
    return CrawlUrl(job_id=job_id, normalized_url=url, depth=depth)


def new_page(job_id: uuid.UUID, crawl_url: CrawlUrl, *, text_value: str = "hello") -> Page:
    return Page(
        job_id=job_id,
        crawl_url_id=crawl_url.id,
        normalized_url=crawl_url.normalized_url,
        http_status=200,
        content_type="text/html",
        title="Example",
        meta_description="An example page",
        extracted_text=text_value,
        content_hash=sha256(text_value),
        byte_size=len(text_value),
        fetched_at=datetime.now(UTC),
    )


async def add_job_with_seed(
    session: AsyncSession, *, status: JobStatus = JobStatus.QUEUED, max_pages: int = 100
) -> tuple[CrawlJob, CrawlUrl]:
    """Insert a job and its seed URL (as the future job API will do, in one transaction)."""
    job = new_job(status=status, max_pages=max_pages)
    session.add(job)
    await session.flush()
    seed = new_url(job.id, job.seed_url)
    session.add(seed)
    await session.flush()
    return job, seed
