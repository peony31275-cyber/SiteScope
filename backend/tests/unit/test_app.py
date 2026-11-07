"""Application factory and lifespan."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.asyncio import Redis

from app import __version__
from app.core.config import Settings
from app.db.session import Database
from app.main import create_app
from tests.support import LogReader, checked_out_connections


def test_create_app_uses_the_given_settings(settings: Settings) -> None:
    app = create_app(settings)

    assert app.state.settings is settings


def test_lifespan_logs_startup_and_shutdown(settings: Settings, read_logs: LogReader) -> None:
    app = create_app(settings)

    with TestClient(app):
        startup = [entry for entry in read_logs() if entry.get("event") == "app.startup"]
    shutdown = [entry for entry in read_logs() if entry.get("event") == "app.shutdown"]

    assert len(startup) == 1
    assert startup[0]["environment"] == "test"
    assert startup[0]["version"] == __version__
    assert len(shutdown) == 1
    assert shutdown[0]["uptime_seconds"] >= 0


def test_module_exposes_asgi_app_for_uvicorn() -> None:
    from app.main import app

    assert isinstance(app, FastAPI)


def test_lifespan_creates_and_closes_dependency_clients(settings: Settings) -> None:
    """Clients are created at startup without connecting, so no service is needed here."""
    app = create_app(settings)

    with TestClient(app):
        database = app.state.database
        assert isinstance(database, Database)
        assert isinstance(app.state.redis, Redis)
        assert checked_out_connections(database) == 0  # nothing connected yet

    assert checked_out_connections(database) == 0


def test_missing_credentials_are_reported_at_startup(
    settings: Settings, read_logs: LogReader
) -> None:
    with TestClient(create_app(settings)):
        warnings = [e for e in read_logs() if e.get("event") == "config.credentials_missing"]

    assert warnings[0]["settings"] == ["SITESCOPE_POSTGRES_PASSWORD", "SITESCOPE_REDIS_PASSWORD"]


def test_startup_log_contains_no_credentials(read_logs: LogReader) -> None:
    secret_settings = Settings(
        _env_file=None,
        postgres_password=SecretStr("pg-secret-value"),
        redis_password=SecretStr("redis-secret-value"),
    )

    with TestClient(create_app(secret_settings)):
        output = str(read_logs())

    assert "pg-secret-value" not in output
    assert "redis-secret-value" not in output
