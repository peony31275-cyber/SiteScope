"""Classification of exceptions as "backing service unavailable" (no services needed).

The exception types match what this machine actually raised (Phase 2B probe): a refused
PostgreSQL connection surfaces as the builtin ConnectionRefusedError, a wrong PostgreSQL password
as a DBAPIError carrying SQLSTATE 28P01, and Redis failures as redis.exceptions types.
"""

import pytest
from redis import exceptions as redis_errors
from sqlalchemy import exc as sqlalchemy_errors

from app.core.errors import is_dependency_unavailable


class FakeDriverError(Exception):
    """Stand-in for a DBAPI driver exception that carries a SQLSTATE code."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"SQLSTATE {sqlstate}")
        self.sqlstate = sqlstate


def dbapi_error(sqlstate: str, *, invalidated: bool = False) -> sqlalchemy_errors.DBAPIError:
    return sqlalchemy_errors.DBAPIError(
        "SELECT 1", None, FakeDriverError(sqlstate), connection_invalidated=invalidated
    )


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("refused"),
        TimeoutError("connect timed out"),
        sqlalchemy_errors.TimeoutError("QueuePool limit reached"),
        dbapi_error("28P01"),  # invalid password
        dbapi_error("08006"),  # connection failure
        dbapi_error("57P01"),  # admin shutdown
        dbapi_error("53300"),  # too many connections
        dbapi_error("22012", invalidated=True),  # driver reported the connection as dead
        redis_errors.ConnectionError("refused"),
        redis_errors.AuthenticationError("WRONGPASS"),
        redis_errors.TimeoutError("timed out"),
    ],
    ids=lambda exc: type(exc).__name__ + getattr(getattr(exc, "orig", None), "sqlstate", ""),
)
def test_recognises_outages(exc: BaseException) -> None:
    assert is_dependency_unavailable(exc)


@pytest.mark.parametrize(
    "exc",
    [
        dbapi_error("22012"),  # division by zero: a bug, not an outage
        dbapi_error("23505"),  # unique violation
        dbapi_error("40P01"),  # deadlock: retryable, but the database is up
        ValueError("bad input"),
        RuntimeError("boom"),
        redis_errors.ResponseError("WRONGTYPE"),
    ],
    ids=lambda exc: type(exc).__name__ + getattr(getattr(exc, "orig", None), "sqlstate", ""),
)
def test_does_not_misreport_ordinary_errors_as_outages(exc: BaseException) -> None:
    assert not is_dependency_unavailable(exc)
