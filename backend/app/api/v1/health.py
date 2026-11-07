"""Versioned health endpoints.

Liveness and readiness are deliberately separate:

* ``/live`` reports only that this process is serving requests. It never touches a dependency,
  so an outage of PostgreSQL or Redis cannot make an orchestrator restart a healthy process.
* ``/ready`` reports whether the process can do useful work right now, which requires PostgreSQL
  and Redis. It answers 503 while either is unavailable.
"""

import time
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app import __version__
from app.api import SERVICE_NAME
from app.api.dependencies import DatabaseDep, RedisDep, SettingsDep
from app.services.health import check_readiness

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    """Liveness details of the API process."""

    status: Literal["ok"]
    service: str
    version: str
    uptime_seconds: float
    checked_at: datetime


class DependencyStatus(BaseModel):
    """Result of one dependency check. Deliberately contains no connection details."""

    status: Literal["ok", "unavailable", "timeout"]
    latency_ms: float


class ReadinessResponse(BaseModel):
    """Readiness of the API process and its required dependencies."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, DependencyStatus]
    checked_at: datetime


@router.get("/live", summary="Liveness probe")
async def get_liveness(request: Request) -> LivenessResponse:
    """Confirm that the API process is running and its event loop is serving requests.

    It reports only the process's own state and checks no external dependency.
    """
    started: float = request.app.state.started_monotonic
    return LivenessResponse(
        status="ok",
        service=SERVICE_NAME,
        version=__version__,
        uptime_seconds=round(time.monotonic() - started, 3),
        checked_at=datetime.now(UTC),
    )


@router.get(
    "/ready",
    summary="Readiness probe",
    responses={
        503: {
            "model": ReadinessResponse,
            "description": "PostgreSQL or Redis is unavailable; the body says which.",
        }
    },
)
async def get_readiness(
    response: Response, settings: SettingsDep, database: DatabaseDep, redis: RedisDep
) -> ReadinessResponse:
    """Check PostgreSQL (``SELECT 1``) and Redis (``PING``) under a short deadline.

    Returns 200 only when both respond. Otherwise returns 503 with the same body shape.
    """
    checks = await check_readiness(
        database, redis, timeout_seconds=settings.readiness_timeout_seconds
    )
    ready = all(check.status == "ok" for check in checks.values())
    response.status_code = 200 if ready else 503
    response.headers["Cache-Control"] = "no-store"
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        checks={
            name: DependencyStatus(status=check.status, latency_ms=check.latency_ms)
            for name, check in checks.items()
        },
        checked_at=datetime.now(UTC),
    )
