"""Helpers shared by tests. Fixtures live in conftest.py."""

from collections.abc import Callable
from typing import Any

# Type of the ``read_logs`` fixture: returns the JSON log lines written since its previous call.
type LogReader = Callable[[], list[dict[str, Any]]]
