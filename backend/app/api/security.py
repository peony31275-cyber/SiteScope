"""Write access to the API (Phase 11, ADR-0024).

Reads are public. Writes (creating and cancelling crawl jobs) follow the deployment's
``Settings.write_access``:

* ``open``: anyone may write; the default outside production (local development, tests);
* ``token``: a write must carry ``Authorization: Bearer <SITESCOPE_ADMIN_TOKEN>``; the token is
  compared in constant time, and a missing or wrong one is a 401 ``unauthorized``;
* ``disabled``: every write is a 403 ``writes_disabled``; production without an admin token, such
  as the public read-only demo.

The dashboard reads the mode from ``GET /api/v1/crawl-policy`` to hide what it cannot do; that
is a convenience: this check is the enforcement.
"""

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.dependencies import SettingsDep
from app.core.errors import UnauthorizedError, WritesDisabledError

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AdminToken",
    description="The administrator's token (SITESCOPE_ADMIN_TOKEN), on deployments that set one.",
)


async def require_write_access(
    request: Request,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Allow a write only as the deployment's write access says (module docstring)."""
    access = settings.write_access
    if access == "open":
        return
    if access == "disabled":
        _refused(request, "writes_disabled")
        raise WritesDisabledError(
            "This deployment is read-only: crawl jobs cannot be created or cancelled through "
            "the API."
        )
    token = settings.admin_token
    if (
        credentials is None
        or token is None
        or not hmac.compare_digest(
            credentials.credentials.encode("utf-8"), token.get_secret_value().encode("utf-8")
        )
    ):
        _refused(request, "unauthorized")
        raise UnauthorizedError("This operation needs the administrator's bearer token.")


def _refused(request: Request, reason: str) -> None:
    # The client's address comes from the proxy's X-Forwarded-For when uvicorn trusts it; the
    # token itself is never logged.
    logger.warning(
        "API write refused",
        extra={
            "event": "api.write_refused",
            "reason": reason,
            "method": request.method,
            "path": request.url.path,
            "client": request.client.host if request.client else None,
        },
    )
