"""Helpers shared by SiteScope's long-running processes (scheduler, crawler workers)."""

import argparse
import os
import secrets
import socket
from collections.abc import Callable

from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError


def process_name(role: str) -> str:
    """A name unique per process start: ``<role>-<host>-<pid>-<random>``.

    Crawler workers use it as their lease owner and Redis consumer name (ADR-0011, ADR-0016): a
    restarted process never reuses a name, so it cannot be mistaken for the process that held a
    lease or a pending message before it.
    """
    return f"{role}-{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"


def is_transient(error: BaseException) -> bool:
    """An infrastructure failure worth retrying: PostgreSQL or Redis unreachable or timing out."""
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return True
    return isinstance(
        error,
        RedisError | OSError | TimeoutError | OperationalError | InterfaceError | PoolTimeoutError,
    )


def error_type(error: BaseException) -> str:
    """The error's class (and the driver's, for database errors), safe to log.

    Messages are not logged: database errors can quote SQL, and other errors can quote URLs.
    """
    name = type(error).__qualname__
    original = getattr(error, "orig", None)
    if original is not None:
        name = f"{name}({type(original).__qualname__})"
    return name


def int_in_range(low: int, high: int) -> Callable[[str], int]:
    """An ``argparse`` type: an integer from ``low`` to ``high``."""

    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be from {low} to {high}")
        return number

    return parse
