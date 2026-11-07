"""Alembic migrations against a separate disposable database (never the development database)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db.base import Base
from tests.integration import helpers

pytestmark = pytest.mark.anyio

REVISIONS = ["0001", "0002"]  # oldest first
HEAD = REVISIONS[-1]


@pytest.fixture
async def empty_database(
    service_settings: Settings, services_available: None
) -> AsyncIterator[Settings]:
    settings = service_settings.model_copy(
        update={"postgres_db": f"{service_settings.postgres_db}_migrations"}
    )
    await helpers.recreate_database(settings)
    try:
        yield settings
    finally:
        await helpers.drop_database(settings)


async def query(settings: Settings, sql: str) -> list[Any]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return list((await connection.execute(text(sql))).scalars().all())
    finally:
        await engine.dispose()


async def public_tables(settings: Settings) -> set[str]:
    rows = await query(
        settings, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
    )
    return set(rows)


async def sitescope_functions(settings: Settings) -> list[str]:
    return await query(
        settings, "SELECT proname FROM pg_proc WHERE proname LIKE 'sitescope\\_%' ORDER BY 1"
    )


async def user_triggers(settings: Settings) -> list[str]:
    return await query(
        settings,
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
        " WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace ORDER BY 1",
    )


async def schema_differences(settings: Settings) -> list[Any]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync: compare_metadata(
                    MigrationContext.configure(sync, opts={"compare_type": True}), Base.metadata
                )
            )
    finally:
        await engine.dispose()


async def test_history_is_a_single_linear_chain() -> None:
    script = ScriptDirectory.from_config(helpers.alembic_config())

    assert script.get_heads() == [HEAD]
    assert script.get_bases() == [REVISIONS[0]]
    walked = [revision.revision for revision in script.walk_revisions()]  # newest first
    assert walked == list(reversed(REVISIONS))


async def test_upgrade_builds_the_schema_from_an_empty_database(empty_database: Settings) -> None:
    assert await public_tables(empty_database) == set()

    await helpers.upgrade(empty_database, "head")

    assert await public_tables(empty_database) == {*helpers.APP_TABLES, "alembic_version"}
    assert await query(empty_database, "SELECT version_num FROM alembic_version") == [HEAD]
    assert await sitescope_functions(empty_database) == [
        "sitescope_lease_epoch_monotonic",
        "sitescope_lease_epoch_per_claim",
        "sitescope_reject_update",
        "sitescope_set_updated_at",
    ]
    assert len(await user_triggers(empty_database)) == 7


async def test_migrated_schema_matches_the_models(empty_database: Settings) -> None:
    await helpers.upgrade(empty_database, "head")

    assert await schema_differences(empty_database) == []


async def test_upgrade_at_head_is_a_no_op(empty_database: Settings) -> None:
    await helpers.upgrade(empty_database, "head")
    await helpers.upgrade(empty_database, "head")

    assert await query(empty_database, "SELECT version_num FROM alembic_version") == [HEAD]


async def test_downgrade_removes_everything_and_upgrade_restores_it(
    empty_database: Settings,
) -> None:
    await helpers.upgrade(empty_database, "head")

    await helpers.downgrade(empty_database, "base")

    assert await public_tables(empty_database) == {"alembic_version"}
    assert await sitescope_functions(empty_database) == []
    assert await query(empty_database, "SELECT version_num FROM alembic_version") == []

    await helpers.upgrade(empty_database, "head")

    assert await public_tables(empty_database) == {*helpers.APP_TABLES, "alembic_version"}
    assert await schema_differences(empty_database) == []


async def test_0002_downgrades_to_0001_and_back(empty_database: Settings) -> None:
    """Migration 0002 only adds the claim trigger; stepping down removes exactly that."""
    await helpers.upgrade(empty_database, "head")

    await helpers.downgrade(empty_database, "0001")

    assert "crawl_urls_lease_epoch_per_claim" not in await user_triggers(empty_database)
    assert "sitescope_lease_epoch_per_claim" not in await sitescope_functions(empty_database)
    assert len(await user_triggers(empty_database)) == 6
    await helpers.upgrade(empty_database, "head")
    assert "crawl_urls_lease_epoch_per_claim" in await user_triggers(empty_database)
    assert await query(empty_database, "SELECT version_num FROM alembic_version") == [HEAD]


async def test_an_interrupted_migration_leaves_no_partial_schema(empty_database: Settings) -> None:
    """Transactional DDL: if the migration transaction does not commit, nothing remains."""

    class Interrupted(Exception):
        pass

    def upgrade_then_fail(config: Any) -> None:
        command.upgrade(config, "head")
        raise Interrupted  # e.g. the process dies before the transaction commits

    with pytest.raises(Interrupted):
        await helpers.run_alembic(empty_database, upgrade_then_fail)

    assert await public_tables(empty_database) == set()
