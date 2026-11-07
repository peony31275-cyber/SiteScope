"""Helpers shared by tests. Fixtures live in conftest.py."""

from collections.abc import Callable
from typing import Any

from sqlalchemy.pool import QueuePool

from app.db.session import Database

# Type of the ``read_logs`` fixture: returns the JSON log lines written since its previous call.
type LogReader = Callable[[], list[dict[str, Any]]]


def checked_out_connections(database: Database) -> int:
    """Connections currently borrowed from the engine's pool (the async pool is a QueuePool)."""
    pool = database.engine.pool
    assert isinstance(pool, QueuePool)
    return pool.checkedout()
