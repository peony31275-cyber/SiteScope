"""FastAPI application factory and the ASGI entrypoint (``uvicorn app.main:app``)."""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app import __version__
from app.api import SERVICE_NAME
from app.api.root import router as root_router
from app.api.v1.router import router as api_v1_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware

logger = logging.getLogger(__name__)

OPENAPI_TAGS = [
    {"name": "service", "description": "Service index and infrastructure liveness probe."},
    {"name": "health", "description": "Versioned health endpoints."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run startup and shutdown work for the process.

    Today this records the start time and logs both transitions. Clients that need explicit
    setup and teardown (database engine, Redis) are added here as they arrive.
    """
    settings: Settings = app.state.settings
    app.state.started_at = datetime.now(UTC)
    app.state.started_monotonic = time.monotonic()
    logger.info(
        "Application started",
        extra={
            "event": "app.startup",
            "environment": settings.environment,
            "version": __version__,
        },
    )
    try:
        yield
    finally:
        logger.info(
            "Application stopped",
            extra={
                "event": "app.shutdown",
                "uptime_seconds": round(time.monotonic() - app.state.started_monotonic, 3),
            },
        )


def _operation_id(route: APIRoute) -> str:
    """Use the handler's function name as the OpenAPI operationId, giving clean client names."""
    return route.name


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a configured application. Tests pass explicit settings; servers use the environment."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, service=SERVICE_NAME)

    app = FastAPI(
        title="SiteScope API",
        summary="Distributed web crawling and search infrastructure.",
        version=__version__,
        openapi_tags=OPENAPI_TAGS,
        generate_unique_id_function=_operation_id,
        lifespan=lifespan,
    )
    app.state.settings = settings

    register_exception_handlers(app)
    # Must remain the last (outermost) middleware added; see RequestContextMiddleware.
    app.add_middleware(RequestContextMiddleware)

    app.include_router(root_router)
    app.include_router(api_v1_router)
    return app


app = create_app()
