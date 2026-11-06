"""Shared fixtures. Every test gets an isolated environment and its own application instance."""

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.support import LogReader


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's SITESCOPE_* variables so that tests never depend on local config."""
    for name in list(os.environ):
        if name.upper().startswith("SITESCOPE_"):
            monkeypatch.delenv(name)


@pytest.fixture
def settings() -> Settings:
    """Test settings: defaults only. The local backend/.env file is never read."""
    return Settings(_env_file=None, environment="test")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A test client that also runs the application lifespan (startup and shutdown)."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def read_logs(capsys: pytest.CaptureFixture[str]) -> LogReader:
    """Return a function that parses the JSON log lines written to stdout since its last call."""

    def read() -> list[dict[str, Any]]:
        lines = capsys.readouterr().out.splitlines()
        return [json.loads(line) for line in lines if line.startswith("{")]

    return read
