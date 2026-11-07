"""Readiness probe and request-path handling of dependency failures, against real services.

Failures are real, not mocked: wrong passwords (rejected by the server), closed ports
(connection refused) and a "blackhole" port that accepts connections but never answers.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from app.api.dependencies import RedisDep, SessionDep
from app.core.config import Settings
from app.core.errors import PROBLEM_MEDIA_TYPE
from app.main import create_app
from tests.integration.helpers import blackhole_server, unused_port

WRONG_PASSWORD = "deliberately-wrong-password"

# Test-only routes that touch the dependencies the way future endpoints will.
probe_router = APIRouter(prefix="/_test")


@probe_router.get("/db")
async def query_database(session: SessionDep) -> dict[str, int]:
    value: int = (await session.execute(text("SELECT 1"))).scalar_one()
    return {"value": value}


@probe_router.get("/db/bad-sql")
async def run_bad_sql(session: SessionDep) -> None:
    await session.execute(text("SELECT 1 / 0"))


@probe_router.get("/redis")
async def ping_redis(redis: RedisDep) -> dict[str, bool]:
    return {"pong": bool(await redis.ping())}


@contextmanager
def client_for(settings: Settings, **overrides: Any) -> Iterator[TestClient]:
    app = create_app(settings.model_copy(update=overrides))
    app.include_router(probe_router)
    with TestClient(app) as client:
        yield client


def readiness(client: TestClient) -> tuple[int, dict[str, Any]]:
    response = client.get("/api/v1/health/ready")
    return response.status_code, response.json()


# --- Readiness ------------------------------------------------------------------------------------


def test_ready_when_postgres_and_redis_are_available(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/health/ready")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ready"
    assert {name: check["status"] for name, check in body["checks"].items()} == {
        "postgres": "ok",
        "redis": "ok",
    }
    assert all(check["latency_ms"] >= 0 for check in body["checks"].values())
    assert response.headers["cache-control"] == "no-store"


def test_not_ready_when_postgres_rejects_the_credentials(test_database: Settings) -> None:
    with client_for(test_database, postgres_password=SecretStr(WRONG_PASSWORD)) as client:
        status, body = readiness(client)

    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"]["status"] == "unavailable"
    assert body["checks"]["redis"]["status"] == "ok"


def test_not_ready_when_postgres_is_unreachable(test_database: Settings) -> None:
    with client_for(test_database, postgres_port=unused_port()) as client:
        status, body = readiness(client)

    assert status == 503
    # Windows may retry a refused local connection for ~2 s, so it can surface as a timeout.
    assert body["checks"]["postgres"]["status"] in {"unavailable", "timeout"}
    assert body["checks"]["redis"]["status"] == "ok"


def test_not_ready_when_redis_rejects_the_credentials(test_database: Settings) -> None:
    with client_for(test_database, redis_password=SecretStr(WRONG_PASSWORD)) as client:
        status, body = readiness(client)

    assert status == 503
    assert body["checks"]["redis"]["status"] == "unavailable"
    assert body["checks"]["postgres"]["status"] == "ok"


def test_not_ready_when_redis_is_unreachable(test_database: Settings) -> None:
    with client_for(test_database, redis_port=unused_port()) as client:
        status, body = readiness(client)

    assert status == 503
    assert body["checks"]["redis"]["status"] in {"unavailable", "timeout"}


def test_a_hanging_dependency_times_out_within_the_deadline(test_database: Settings) -> None:
    with (
        blackhole_server() as port,
        client_for(test_database, postgres_port=port, readiness_timeout_seconds=0.5) as client,
    ):
        started = time.perf_counter()
        status, body = readiness(client)
        elapsed = time.perf_counter() - started

    assert status == 503
    assert body["checks"]["postgres"]["status"] == "timeout"
    assert elapsed < 3  # bounded by the readiness deadline, not by connect timeouts


def test_readiness_never_exposes_connection_details(test_database: Settings) -> None:
    with client_for(
        test_database,
        postgres_password=SecretStr(WRONG_PASSWORD),
        redis_password=SecretStr(WRONG_PASSWORD),
    ) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    for secret_or_detail in (
        WRONG_PASSWORD,
        test_database.postgres_user,
        test_database.postgres_db,
        test_database.postgres_host,
        "postgresql",
        "asyncpg",
    ):
        assert secret_or_detail not in response.text


def test_liveness_stays_up_while_dependencies_are_down(test_database: Settings) -> None:
    with client_for(
        test_database,
        postgres_password=SecretStr(WRONG_PASSWORD),
        redis_password=SecretStr(WRONG_PASSWORD),
    ) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/health/ready").status_code == 503


# --- Dependency failures on the request path ------------------------------------------------


def assert_dependency_problem(response: Any) -> None:
    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["retry-after"] == "5"
    body = response.json()
    assert body["code"] == "dependency_unavailable"
    assert WRONG_PASSWORD not in response.text


def test_database_route_works_when_postgres_is_up(test_database: Settings) -> None:
    with client_for(test_database) as client:
        response = client.get("/_test/db")

    assert response.status_code == 200
    assert response.json() == {"value": 1}


def test_rejected_database_credentials_become_a_503_problem(test_database: Settings) -> None:
    with client_for(test_database, postgres_password=SecretStr(WRONG_PASSWORD)) as client:
        assert_dependency_problem(client.get("/_test/db"))


def test_unreachable_database_becomes_a_503_problem(test_database: Settings) -> None:
    with client_for(test_database, postgres_port=unused_port()) as client:
        assert_dependency_problem(client.get("/_test/db"))


def test_sql_errors_are_not_reported_as_outages(test_database: Settings) -> None:
    with client_for(test_database) as client:
        response = client.get("/_test/db/bad-sql")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"


def test_rejected_redis_credentials_become_a_503_problem(test_database: Settings) -> None:
    with client_for(test_database, redis_password=SecretStr(WRONG_PASSWORD)) as client:
        assert_dependency_problem(client.get("/_test/redis"))


def test_unreachable_redis_becomes_a_503_problem(test_database: Settings) -> None:
    with client_for(test_database, redis_port=unused_port()) as client:
        assert_dependency_problem(client.get("/_test/redis"))
