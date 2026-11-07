"""Dependency checks for the readiness probe.

Each check runs under its own deadline and never raises: a failure is reported as a status, and
the details (exception type only, never connection strings or passwords) go to the server logs.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis

from app.core.errors import is_dependency_unavailable
from app.db.session import Database

logger = logging.getLogger(__name__)

CheckStatus = Literal["ok", "unavailable", "timeout"]


@dataclass(frozen=True, slots=True)
class DependencyCheck:
    status: CheckStatus
    latency_ms: float


async def check_readiness(
    database: Database, redis: Redis, *, timeout_seconds: float
) -> dict[str, DependencyCheck]:
    """Check PostgreSQL and Redis concurrently. The whole check takes at most ~timeout_seconds."""
    postgres, redis_check = await asyncio.gather(
        _timed("postgres", database.ping, timeout_seconds),
        _timed("redis", lambda: _ping_redis(redis), timeout_seconds),
    )
    return {"postgres": postgres, "redis": redis_check}


async def _ping_redis(redis: Redis) -> None:
    await redis.ping()


async def _timed(
    name: str, probe: Callable[[], Awaitable[None]], timeout_seconds: float
) -> DependencyCheck:
    started = time.perf_counter()
    status: CheckStatus = "ok"
    try:
        async with asyncio.timeout(timeout_seconds):
            await probe()
    except TimeoutError:
        status = "timeout"
    except Exception as exc:  # a health check must report failures, never raise them
        status = "unavailable"
        logger.warning(
            "Dependency check failed",
            extra={
                "event": "health.dependency_unavailable",
                "dependency": name,
                "error_type": type(exc).__name__,
                "recognised_outage": is_dependency_unavailable(exc),
            },
        )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    if status == "timeout":
        logger.warning(
            "Dependency check timed out",
            extra={
                "event": "health.dependency_timeout",
                "dependency": name,
                "timeout_seconds": timeout_seconds,
            },
        )
    return DependencyCheck(status=status, latency_ms=latency_ms)
