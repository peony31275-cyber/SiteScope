"""Request context middleware.

For every HTTP request this middleware:

* assigns a request ID and returns it in the ``X-Request-ID`` response header;
* writes one structured access-log line;
* converts any unhandled exception into a problem+json 500 response.
"""

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import internal_error_response
from app.core.logging import request_id_var

REQUEST_ID_HEADER = "X-Request-ID"

# A client-supplied request ID is reused only if it is short and contains only safe characters.
# Anything else, such as control characters, would allow log injection, so a new ID is generated.
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

logger = logging.getLogger(__name__)


class RequestContextMiddleware:
    """Pure ASGI middleware. It must be the outermost middleware added by the application.

    Unhandled exceptions are handled here rather than by an ``Exception`` handler for two
    reasons: the traceback is logged exactly once and carries the request ID, and the 500 response
    carries the ``X-Request-ID`` header. Such exceptions are not re-raised, so middleware outside
    this one (e.g. FastAPI's telemetry hook) does not see them; SiteScope's logs are the record.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _client_request_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code: int | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "Unhandled exception while handling request",
                extra={"event": "http.unhandled_exception"},
            )
            if status_code is not None:
                raise  # The response has already started, so no error response can be sent.
            await internal_error_response(scope["path"])(scope, receive, send_with_request_id)
        finally:
            # The path is logged without its query string, which may contain sensitive values.
            logger.info(
                "%s %s %s",
                scope["method"],
                scope["path"],
                status_code,
                extra={
                    "event": "http.request",
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(token)


def _client_request_id(scope: Scope) -> str | None:
    """Return the client's ``X-Request-ID`` header value if it is safe to reuse."""
    for name, value in scope["headers"]:
        if name == b"x-request-id":
            candidate: str = value.decode("latin-1")
            return candidate if _SAFE_REQUEST_ID.fullmatch(candidate) else None
    return None
