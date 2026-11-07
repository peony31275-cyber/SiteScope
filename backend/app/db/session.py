"""Engine, connection pool and session management for PostgreSQL.

Usage rules:

* Create one ``Database`` per process (the API creates it in its lifespan) and share it freely:
  the engine and its pool are safe for concurrent use.
* Never share an ``AsyncSession`` between concurrent tasks. Use one session per request
  (the ``get_session`` API dependency) or per unit of work (``Database.transaction()``).
* Transactions are explicit: ``async with database.transaction() as session:`` commits when the
  block succeeds and rolls back on any exception. PostgreSQL runs at its default READ COMMITTED
  isolation; correctness comes from constraints, row locks and conditional updates
  (docs/architecture.md, section 9).
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings

# Recycle pooled connections after 30 minutes, so long-lived idle connections (for example
# through WSL's port forwarding) are replaced instead of failing on first use.
_POOL_RECYCLE_SECONDS = 1800


class Database:
    """Process-wide PostgreSQL access: one engine (connection pool) and a session factory.

    ``server_settings`` adds PostgreSQL session parameters (for example the scheduler's
    ``idle_in_transaction_session_timeout``).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        application_name: str,
        server_settings: Mapping[str, str] | None = None,
    ) -> None:
        self.engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_recycle=_POOL_RECYCLE_SECONDS,
            # Test each pooled connection before use; replaces connections the server closed.
            pool_pre_ping=True,
            # Keep statement parameters (URLs, page text) out of exception messages and logs.
            hide_parameters=True,
            connect_args={
                "timeout": settings.db_connect_timeout_seconds,
                "command_timeout": settings.db_command_timeout_seconds,
                "server_settings": {
                    **(server_settings or {}),
                    "application_name": application_name,
                    "TimeZone": "UTC",
                },
            },
        )
        # expire_on_commit=False: objects stay readable after commit without lazy reloads,
        # which async sessions cannot perform implicitly.
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session with no transaction opened yet; the caller decides the boundaries."""
        async with self.sessionmaker() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """One unit of work: commits on success, rolls back on any exception."""
        async with self.sessionmaker() as session, session.begin():
            yield session

    async def ping(self) -> None:
        """Run ``SELECT 1`` on a pooled connection; raises if PostgreSQL is unreachable."""
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        """Close every pooled connection. Call once at process shutdown."""
        await self.engine.dispose()
