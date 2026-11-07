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
from app.core.redis import create_redis_client
from app.db.session import Database

logger = logging.getLogger(__name__)

OPENAPI_TAGS = [
    {"name": "service", "description": "Service index and infrastructure liveness probe."},
    {"name": "health", "description": "Versioned health endpoints."},
    {"name": "crawls", "description": "Create, inspect and list crawl jobs."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the process-wide PostgreSQL and Redis clients, and close them on shutdown.

    Both clients connect lazily, so the process starts even while a dependency is down:
    liveness stays green and the readiness probe reports the outage.
    """
    settings: Settings = app.state.settings
    database = Database(settings, application_name=SERVICE_NAME)
    redis = create_redis_client(settings, client_name=SERVICE_NAME)
    app.state.database = database
    app.state.redis = redis
    app.state.started_at = datetime.now(UTC)
    app.state.started_monotonic = time.monotonic()

    if missing := settings.missing_credentials():
        logger.warning(
            "Credentials are not configured; dependency connections will fail",
            extra={"event": "config.credentials_missing", "settings": missing},
        )
    logger.info(
        "Application started",
        extra={
            "event": "app.startup",
            "environment": settings.environment,
            "version": __version__,
            # Host, port and database only; credentials are never logged.
            "postgres": f"{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}",
            "redis": f"{settings.redis_host}:{settings.redis_port}/{settings.redis_db}",
        },
    )
    try:
        yield
    finally:
        try:
            await redis.aclose()
        finally:
            await database.dispose()
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
