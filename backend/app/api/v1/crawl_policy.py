"""The crawl policy of this deployment: its approved crawl targets and job limits (Phase 9).

HTTP concerns only. The values are the server's configuration (``SITESCOPE_CRAWL_ALLOWED_*``,
``SITESCOPE_CRAWL_MAX_*_LIMIT``), which ``POST /api/v1/crawls`` enforces (ADR-0012). The
dashboard reads them to offer the approved targets and to check a request's limits before it
is sent; the server checks every request again. Nothing is resolved or fetched.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import SettingsDep

router = APIRouter(tags=["crawls"])


class CrawlPolicy(BaseModel):
    """What `POST /api/v1/crawls` accepts on this server."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "approved_hosts": ["docs.example.com", "*.example.org"],
                    "allowed_schemes": ["https"],
                    "allowed_ports": [443],
                    "max_depth_limit": 5,
                    "max_pages_limit": 1000,
                }
            ]
        }
    )

    approved_hosts: list[str] = Field(
        description="Approved host patterns, normalized. `docs.example.com` approves exactly "
        "that host; `*.example.org` approves its subdomains, not `example.org` itself. Empty: "
        "nothing is approved, and every crawl request is refused (`target_not_allowed`)."
    )
    allowed_schemes: list[Literal["http", "https"]] = Field(
        description="Schemes a seed URL may use, on every approved host."
    )
    allowed_ports: list[int] = Field(
        description="Ports a seed URL may use, on every approved host: its explicit port, or "
        "its scheme's default (80 for http, 443 for https)."
    )
    max_depth_limit: int = Field(description="The largest `max_depth` a job may request.")
    max_pages_limit: int = Field(description="The largest `max_pages` a job may request.")


@router.get("/crawl-policy", summary="Get the crawl policy")
async def get_crawl_policy(settings: SettingsDep) -> CrawlPolicy:
    """Return the crawl targets this server approves and its job limits.

    A seed URL is accepted when its host matches an approved pattern and its scheme and port
    are allowed (schemes and ports apply to every approved host), and `max_depth` and
    `max_pages` are within the limits. The server checks every request again: this is
    guidance for clients, not a promise that a URL can be crawled.
    """
    return CrawlPolicy(
        approved_hosts=list(settings.crawl_allowed_hosts),
        allowed_schemes=list(settings.crawl_allowed_schemes),
        allowed_ports=list(settings.crawl_allowed_ports),
        max_depth_limit=settings.crawl_max_depth_limit,
        max_pages_limit=settings.crawl_max_pages_limit,
    )
