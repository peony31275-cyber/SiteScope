"""Unversioned operational endpoints: the service index and the infrastructure liveness probe."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import __version__
from app.api.v1.router import API_V1_PREFIX

router = APIRouter(tags=["service"])


class ServiceInfo(BaseModel):
    """Entry point describing the service and where to find its endpoints."""

    name: str
    version: str
    api_base_url: str
    health_url: str
    docs_url: str | None
    openapi_url: str | None


class HealthResponse(BaseModel):
    """Minimal liveness response for load balancers and container health checks."""

    status: Literal["ok"]


@router.get("/", summary="Service information")
async def get_service_info(request: Request) -> ServiceInfo:
    """Describe the service and link to the API, health, and documentation endpoints."""
    return ServiceInfo(
        name="SiteScope API",
        version=__version__,
        api_base_url=API_V1_PREFIX,
        health_url="/health",
        docs_url=request.app.docs_url,
        openapi_url=request.app.openapi_url,
    )


@router.get("/health", summary="Liveness probe")
async def get_health() -> HealthResponse:
    """Return 200 while the process is able to serve requests.

    This is a liveness check only. It does not check PostgreSQL, Redis, or any other dependency.
    """
    return HealthResponse(status="ok")
