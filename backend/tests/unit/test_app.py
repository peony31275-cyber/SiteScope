"""Application factory and lifespan."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import __version__
from app.core.config import Settings
from app.main import create_app
from tests.support import LogReader


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
