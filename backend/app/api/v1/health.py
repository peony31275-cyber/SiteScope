"""Versioned health endpoints."""

import time
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import __version__
from app.api import SERVICE_NAME

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    """Liveness details of the API process."""

    status: Literal["ok"]
    service: str
    version: str
    uptime_seconds: float
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
