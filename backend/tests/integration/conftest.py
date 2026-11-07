"""Fixtures for integration tests against the real PostgreSQL and Redis services.

Isolation rules:

* The developer's connection settings (environment / backend/.env) are used only to reach the
  servers. Tests never touch the development database: they use a disposable database named
  ``<SITESCOPE_POSTGRES_DB>_test``, created and migrated with Alembic at session start, and
  dropped at session end. Every test truncates the tables afterwards.
* Redis tests use logical database 15 and namespaced keys.
* If a service is unavailable the tests are skipped with the reason, or fail when pytest runs
  with ``--require-services``.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.redis import create_redis_client
from app.db.session import Database
from app.main import create_app
from tests.integration import helpers

TEST_REDIS_DB = 15
START_HINT = "start the services with `wsl docker compose up -d --wait` in backend/"


def _unavailable(config: pytest.Config, reason: str) -> NoReturn:
    if config.getoption("--require-services"):
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def service_settings(pytestconfig: pytest.Config) -> Settings:
    """The developer's servers, but a disposable database and Redis DB 15."""
    try:
        base = Settings()
    except ValidationError as exc:
        _unavailable(pytestconfig, f"settings are invalid ({exc.error_count()} errors)")
    if missing := base.missing_credentials():
        _unavailable(pytestconfig, f"set {', '.join(missing)} in backend/.env to run these tests")
    return base.model_copy(
        update={
            "environment": "test",
            "postgres_db": f"{base.postgres_db}_test",
            "redis_db": TEST_REDIS_DB,
        }
    )


@pytest.fixture(scope="session")
def services_available(pytestconfig: pytest.Config, service_settings: Settings) -> None:
    settings = service_settings
    if error := asyncio.run(helpers.probe_postgres(settings)):
        where = f"{settings.postgres_host}:{settings.postgres_port}"
        _unavailable(pytestconfig, f"PostgreSQL unavailable at {where} ({error}); {START_HINT}")
    if error := asyncio.run(helpers.probe_redis(settings)):
        where = f"{settings.redis_host}:{settings.redis_port}"
        _unavailable(pytestconfig, f"Redis unavailable at {where} ({error}); {START_HINT}")


@pytest.fixture(scope="session")
def test_database(service_settings: Settings, services_available: None) -> Iterator[Settings]:
    """Settings for a freshly created, fully migrated, disposable test database."""
    asyncio.run(helpers.recreate_database(service_settings))
    asyncio.run(helpers.upgrade(service_settings, "head"))
    yield service_settings
    asyncio.run(helpers.drop_database(service_settings))


@pytest.fixture
async def database(test_database: Settings) -> AsyncIterator[Database]:
    database = Database(test_database, application_name="sitescope-tests")
    try:
        yield database
    finally:
        await helpers.truncate_all(database)
        await database.dispose()


@pytest.fixture
async def redis_client(
    service_settings: Settings, services_available: None
) -> AsyncIterator[Redis]:
    client = create_redis_client(service_settings, client_name="sitescope-tests")
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def api_client(test_database: Settings) -> Iterator[TestClient]:
    """The real application, wired to the test database and Redis DB 15."""
    with TestClient(create_app(test_database)) as client:
        yield client
