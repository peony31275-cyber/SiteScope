"""OpenAPI documentation of SiteScope's error responses (RFC 9457 problem details).

The responses themselves are built in ``app/core/errors.py``. These models only describe them
in the OpenAPI document, replacing FastAPI's default ``HTTPValidationError`` schema, which does
not match what the API returns.
"""

from typing import Any

from pydantic import BaseModel


class ValidationErrorItem(BaseModel):
    """One invalid request field."""

    loc: list[str | int]
    msg: str
    type: str


class ProblemDetails(BaseModel):
    """An RFC 9457 problem details body (media type ``application/problem+json``)."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str
    code: str
    request_id: str | None = None
    errors: list[ValidationErrorItem] | None = None


_DESCRIPTIONS = {
    404: "The resource does not exist (`not_found`).",
    409: "The request conflicts with earlier ones: an `Idempotency-Key` reused for a different "
    "request (`idempotency_key_reused`).",
    422: "The request failed validation (`validation_failed`); `errors` lists each field.",
    503: "PostgreSQL is unavailable (`dependency_unavailable`); retry after `Retry-After`.",
}


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """The ``responses`` argument of a route that can answer with these problem statuses."""
    return {
        status: {"model": ProblemDetails, "description": _DESCRIPTIONS[status]}
        for status in statuses
    }
