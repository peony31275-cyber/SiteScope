"""Domain errors and RFC 9457 problem-details responses.

Every error response has the media type ``application/problem+json`` and the same members:

* RFC 9457 members: ``type``, ``title``, ``status``, ``detail`` and ``instance``;
* extensions: ``code``, a stable machine-readable error code, and ``request_id``, which matches
  the ``X-Request-ID`` response header and the server logs.

See docs/api-conventions.md.
"""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

PROBLEM_MEDIA_TYPE = "application/problem+json"
INTERNAL_ERROR_DETAIL = "An unexpected error occurred. Quote the request_id when reporting it."
VALIDATION_ERROR_DETAIL = "The request failed validation."


class DomainError(Exception):
    """An expected error that is reported to the API client.

    The service layer raises subclasses of this error, and the API layer converts them into
    problem responses. Services therefore choose an error type, never an HTTP response.
    """

    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    """The requested resource does not exist."""

    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    """The request conflicts with the current state of a resource."""

    status_code = 409
    code = "conflict"


def problem_response(
    *,
    status: int,
    code: str,
    instance: str,
    detail: str | None = None,
    extensions: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build an RFC 9457 problem-details response for the current request."""
    body: dict[str, Any] = {"type": "about:blank", "title": _title(status), "status": status}
    if detail:
        body["detail"] = detail
    body["instance"] = instance
    body["code"] = code
    request_id = request_id_var.get()
    if request_id is not None:
        body["request_id"] = request_id
    if extensions:
        body.update(extensions)
    return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_MEDIA_TYPE)


def internal_error_response(instance: str) -> JSONResponse:
    """Build the response for an unexpected server error. It never includes exception details."""
    return problem_response(
        status=500, code="internal_error", instance=instance, detail=INTERNAL_ERROR_DETAIL
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register the handlers that turn expected exceptions into problem responses.

    Unexpected exceptions are handled by ``RequestContextMiddleware``. It logs each one with the
    request ID and responds with ``internal_error_response``.
    """
    app.add_exception_handler(DomainError, _handle_domain_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)


# Starlette types every handler as (Request, Exception). Each handler narrows the exception to
# the type it was registered for.


async def _handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):
        raise exc
    return problem_response(
        status=exc.status_code, code=exc.code, instance=request.url.path, detail=exc.detail
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    # FastAPI's HTTPException allows non-string details, so the type check is needed. Starlette's
    # default detail is the status phrase, which would only repeat ``title``.
    detail: str | None = exc.detail if isinstance(exc.detail, str) else None
    if detail == _title(exc.status_code):
        detail = None
    return problem_response(
        status=exc.status_code,
        code=_code(exc.status_code),
        instance=request.url.path,
        detail=detail,
        headers=exc.headers,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    errors = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return problem_response(
        status=422,
        code="validation_failed",
        instance=request.url.path,
        detail=VALIDATION_ERROR_DETAIL,
        extensions={"errors": jsonable_encoder(errors)},
    )


def _title(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def _code(status: int) -> str:
    """Derive a machine-readable code from the status, e.g. 405 -> "method_not_allowed"."""
    try:
        return HTTPStatus(status).name.lower()
    except ValueError:
        return f"http_{status}"
