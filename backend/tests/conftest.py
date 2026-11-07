"""Shared fixtures. Every test gets an isolated environment and its own application instance."""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.support import LogReader

INTEGRATION_DIR = Path(__file__).parent / "integration"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-services",
        action="store_true",
        default=False,
        help="Fail integration tests, instead of skipping them, when PostgreSQL or Redis "
        "is unavailable (use in CI).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every test under tests/integration so `-m integration` selects them."""
    for item in items:
        if INTEGRATION_DIR in item.path.parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def anyio_backend() -> str:
    """Async tests run on asyncio (AnyIO's pytest plugin, bundled with AnyIO)."""
    return "asyncio"


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
