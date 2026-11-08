"""Aggregates every version 1 router under a single prefix."""

from fastapi import APIRouter

from app.api.v1 import analysis, crawls, health, search

API_V1_PREFIX = "/api/v1"

router = APIRouter(prefix=API_V1_PREFIX)
router.include_router(health.router)
router.include_router(crawls.router)
router.include_router(analysis.router)
router.include_router(search.router)
