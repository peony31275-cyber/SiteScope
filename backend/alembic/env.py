"""Alembic environment for SiteScope.

Where the database URL comes from, in order:

1. ``config.attributes["connection"]``: an open connection supplied by a caller (tests share
   their connection this way);
2. ``config.attributes["database_url"]``: a SQLAlchemy URL supplied by a caller;
3. the application settings (``SITESCOPE_POSTGRES_*`` from the environment or backend/.env).

Each migration runs in a single transaction. PostgreSQL has transactional DDL, so a migration
that fails part-way leaves no partial schema behind.

Concurrent upgrades serialize (Phase 11). The transaction first takes an advisory lock
(``MIGRATION_LOCK``), before Alembic reads the version table. A second upgrade started meanwhile
waits for the first to commit, then finds nothing left to do. A deployment runs one migration
step before the other services start (``deploy/compose.yaml``); the lock makes a second one
harmless.
"""

import asyncio
from logging.config import fileConfig
from typing import Final

from alembic import context
from sqlalchemy import URL, pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db import models  # noqa: F401 - registers every table on Base.metadata
from app.db.base import Base

config = context.config

# The CLI configures logging from alembic.ini. Programmatic callers (tests) pass
# configure_logging=False so the application's logging setup stays intact.
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Next to the application's advisory locks (crawl_state.ACTIVATION_LOCK, crawl_jobs.CREATION_LOCK).
MIGRATION_LOCK: Final = 0x5173_0003


def _database_url() -> URL:
    url = config.attributes.get("database_url")
    if isinstance(url, URL):
        return url
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without connecting to a database."""
    context.configure(
        url=_database_url().render_as_string(hide_password=True),
        target_metadata=target_metadata,
        compare_type=True,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if isinstance(connection, Connection):
        _run_migrations(connection)
    else:
        asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
