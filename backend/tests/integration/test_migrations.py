"""Alembic migrations against a separate disposable database (never the development database)."""

import asyncio
import os
import sys
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

REVISIONS = ["0001", "0002", "0003", "0004", "0005"]  # oldest first
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
        "sitescope_content_version_forward",
        "sitescope_index_version_forward",
        "sitescope_lease_epoch_monotonic",
        "sitescope_lease_epoch_per_claim",
        "sitescope_reject_update",
        "sitescope_set_updated_at",
    ]
    assert len(await user_triggers(empty_database)) == 9


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


async def test_0003_adds_the_robots_columns_with_defaults_for_existing_rows(
    empty_database: Settings,
) -> None:
    """Rows written before 0003 get robots_noindex/robots_nofollow/nofollow = false."""
    await helpers.upgrade(empty_database, "0002")
    engine = create_async_engine(empty_database.database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "WITH j AS (INSERT INTO crawl_jobs"
                    " (seed_url, allowed_hosts, max_depth, max_pages, admitted_count)"
                    " VALUES ('https://example.test/', '{example.test}', 1, 10, 1)"
                    " RETURNING id), u AS (INSERT INTO crawl_urls (job_id, normalized_url, depth,"
                    " state, result_epoch, finished_at) SELECT id, 'https://example.test/', 0,"
                    " 'skipped', 0, now() FROM j RETURNING id, job_id, normalized_url)"
                    " INSERT INTO pages (job_id, crawl_url_id, normalized_url, http_status,"
                    " content_hash, byte_size, fetched_at) SELECT job_id, id, normalized_url, 200,"
                    " decode(repeat('00', 32), 'hex'), 0, now() FROM u"
                )
            )
    finally:
        await engine.dispose()

    await helpers.upgrade(empty_database, "head")

    flags = await query(empty_database, "SELECT robots_noindex OR robots_nofollow FROM pages")
    assert flags == [False]
    assert await schema_differences(empty_database) == []


async def analysis_indexes(settings: Settings) -> dict[str, str]:
    rows = await query(
        settings,
        "SELECT indexname || ': ' || indexdef FROM pg_indexes"
        r" WHERE tablename IN ('pages', 'page_links') AND indexname LIKE 'ix\_%' ORDER BY 1",
    )
    return dict(row.split(": ", 1) for row in rows)


async def test_0005_replaces_two_indexes_by_wider_ones_and_back(
    empty_database: Settings,
) -> None:
    """Migration 0005 (Phase 8) widens two indexes for the analysis queries; the number of
    indexes does not change, and stepping down restores the 0004 definitions."""
    await helpers.upgrade(empty_database, "0004")
    before = await analysis_indexes(empty_database)

    await helpers.upgrade(empty_database, "0005")
    after = await analysis_indexes(empty_database)

    assert set(before) == {"ix_pages_job_id", "ix_page_links_job_id_target"}
    assert set(after) == {"ix_pages_job_id_normalized_url", "ix_page_links_job_id_target"}
    assert after["ix_pages_job_id_normalized_url"].endswith("(job_id, normalized_url)")
    assert after["ix_page_links_job_id_target"].endswith(
        "(job_id, target_normalized_url) INCLUDE (is_internal)"
    )
    await helpers.downgrade(empty_database, "0004")
    assert await analysis_indexes(empty_database) == before
    await helpers.upgrade(empty_database, "head")
    assert await schema_differences(empty_database) == []


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


# alembic/env.py, MIGRATION_LOCK: a bigint key below 2**32 is (classid 0, objid key) in pg_locks.
MIGRATION_LOCK = 0x5173_0003


def database_environment(settings: Settings) -> dict[str, str]:
    """The environment of an ``alembic`` process on ``settings``'s database."""
    password = settings.postgres_password
    return {
        **os.environ,
        "SITESCOPE_POSTGRES_HOST": settings.postgres_host,
        "SITESCOPE_POSTGRES_PORT": str(settings.postgres_port),
        "SITESCOPE_POSTGRES_USER": settings.postgres_user,
        "SITESCOPE_POSTGRES_PASSWORD": password.get_secret_value() if password else "",
        "SITESCOPE_POSTGRES_DB": settings.postgres_db,
    }


async def test_concurrent_upgrades_serialize_on_the_migration_lock(
    empty_database: Settings,
) -> None:
    """Two migration steps at once, as two deployments' one-shot ``migrate`` containers would
    run them: each waits for the migration lock before it reads the version table, so one builds
    the schema and the other finds it at head. The test holds the lock until both wait, so they
    overlap for certain."""
    engine = create_async_engine(empty_database.database_url, poolclass=NullPool)
    environment = database_environment(empty_database)
    try:
        async with engine.connect() as holder:
            await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK})
            processes = [
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "alembic",
                    "upgrade",
                    "head",
                    cwd=helpers.BACKEND_DIR,
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                for _ in range(2)
            ]
            waiting = 0
            for _ in range(600):  # up to 60 s for both processes to start and reach the lock
                waiting = int(
                    await holder.scalar(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                            " AND classid = 0 AND objid = :key AND NOT granted"
                        ),
                        {"key": MIGRATION_LOCK},
                    )
                    or 0
                )
                await holder.commit()  # a fresh snapshot of pg_locks for the next read
                if waiting == 2:
                    break
                await asyncio.sleep(0.1)
            await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK})
            await holder.commit()
        outputs = [await asyncio.wait_for(process.communicate(), 120) for process in processes]
    finally:
        await engine.dispose()

    assert waiting == 2, "both upgrades should have waited for the migration lock"
    codes = [process.returncode for process in processes]
    assert codes == [0, 0], [error.decode(errors="replace")[-500:] for _, error in outputs]
    assert await query(empty_database, "SELECT version_num FROM alembic_version") == [HEAD]
    assert await schema_differences(empty_database) == []
