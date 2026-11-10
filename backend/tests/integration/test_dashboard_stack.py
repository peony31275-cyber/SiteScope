"""The local stack's storage against the real PostgreSQL and Redis: a start removes what an
interrupted run left (its database, its Redis keys), keeps every other key, and the end of the
run releases everything. A database and a key prefix of this test's own: never the running
demo's ``*_test_ui`` database, never its ``ui-stack`` keys."""

import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from tests import dashboard_stack as stack
from tests.integration import helpers

pytestmark = pytest.mark.anyio


async def tables(settings: Settings) -> set[str]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {name for (name,) in rows}
    finally:
        await engine.dispose()


async def test_a_start_removes_an_interrupted_runs_leftovers_and_nothing_else(
    service_settings: Settings,
    redis_client: Redis,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = service_settings.model_copy(
        update={"postgres_db": f"{service_settings.postgres_db}_launcher"}
    )
    prefix = f"ui-stack-test-{uuid.uuid4().hex[:8]}"
    unrelated = f"unrelated-{uuid.uuid4().hex[:8]}:keep"
    # An interrupted run: its database (with something in it) and its keys are still there.
    await helpers.recreate_database(settings)
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE leftover_marker (x int)"))
    finally:
        await engine.dispose()
    await redis_client.set(f"{prefix}:crawl", "1")
    await redis_client.set(f"{prefix}:index", "2")
    await redis_client.set(unrelated, "3")
    try:
        await stack.prepare_storage(settings, prefix)

        out = capsys.readouterr().out
        assert f"database {settings.postgres_db}, 2 Redis keys" in out
        assert [key async for key in redis_client.scan_iter(match=f"{prefix}:*")] == []
        assert await redis_client.get(unrelated) == "3"
        assert await tables(settings) == {*helpers.APP_TABLES, "alembic_version"}

        resources = stack.Resources(
            settings=settings, prefix=prefix, storage_touched=True, database_created=True
        )
        assert await stack.release_storage(resources, keep=False) is True
        assert not await stack.database_exists(settings)
        assert await redis_client.get(unrelated) == "3"
    finally:
        await redis_client.delete(unrelated)
        await helpers.drop_database(settings)
