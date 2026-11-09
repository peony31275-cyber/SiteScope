"""OpenAPI documentation of SiteScope's error responses (RFC 9457 problem details).

The responses themselves are built in ``app/core/errors.py``. These models only describe them
in the OpenAPI document, replacing FastAPI's default ``HTTPValidationError`` schema, which does
not match what the API returns.
"""

from collections.abc import Mapping
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
    401: "This deployment requires the administrator's bearer token for this operation, and the "
    "request carried none or a wrong one (`unauthorized`).",
    403: "This deployment is read-only: the operation is disabled (`writes_disabled`).",
    404: "The resource does not exist (`not_found`).",
    409: "The request conflicts with earlier ones: an `Idempotency-Key` reused for a different "
    "request (`idempotency_key_reused`).",
    422: "The request failed validation (`validation_failed`); `errors` lists each field.",
    429: "As many crawl jobs as this deployment allows are queued or running "
    "(`too_many_active_jobs`); retry after `Retry-After`.",
    503: "PostgreSQL is unavailable (`dependency_unavailable`); retry after `Retry-After`.",
}


def problem_responses(
    *statuses: int, descriptions: Mapping[int, str] | None = None
) -> dict[int | str, dict[str, Any]]:
    """The ``responses`` argument of a route that can answer with these problem statuses.

    ``descriptions`` replaces the default description of a status, for a route whose problems
    of that status have more causes (each with its ``code``).
    """
    custom = descriptions or {}
    unknown = set(custom) - set(statuses)
    if unknown:
        raise ValueError(f"descriptions for undocumented statuses: {sorted(unknown)}")
    return {
        status: {"model": ProblemDetails, "description": custom.get(status, _DESCRIPTIONS[status])}
        for status in statuses
    }
