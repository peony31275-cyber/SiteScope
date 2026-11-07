"""Fixtures for integration tests that need the API application.

The service fixtures (``test_database``, ``database``, ``redis_client``, ``redis_prefix``,
``crawl_settings``, ...) and their isolation rules are in ``tests/service_fixtures.py``, shared
with ``tests/fault``.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.session import Database
from app.main import create_app


@pytest.fixture
def api_client(test_database: Settings) -> Iterator[TestClient]:
    """The real application, wired to the test database and Redis DB 15."""
    with TestClient(create_app(test_database)) as client:
        yield client


@pytest.fixture
def crawl_client(crawl_settings: Settings, database: Database) -> Iterator[TestClient]:
    """The real application with approved crawl targets. ``database`` empties the tables after
    the test (it is torn down after this client)."""
    with TestClient(create_app(crawl_settings)) as client:
        yield client
