"""Error handling, exercised through test-only routes mounted on a real application instance."""

from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import (
    INTERNAL_ERROR_DETAIL,
    PROBLEM_MEDIA_TYPE,
    ConflictError,
    NotFoundError,
)
from app.main import create_app
from tests.support import LogReader

# Test-only routes. (Not named test_*: pytest would try to collect a callable router.)
error_router = APIRouter(prefix="/_test")


@error_router.get("/not-found")
async def raise_not_found() -> None:
    raise NotFoundError("Crawl job 42 does not exist.")


@error_router.get("/conflict")
async def raise_conflict() -> None:
    raise ConflictError("Idempotency key was reused with a different request body.")


@error_router.get("/forbidden")
async def raise_http_exception() -> None:
    raise HTTPException(status_code=403, detail="Not allowed in this test.")


@error_router.get("/validated")
async def require_integer(limit: int) -> dict[str, int]:
    return {"limit": limit}


@error_router.get("/crash")
async def crash() -> None:
    raise RuntimeError("secret internal detail")


@pytest.fixture
def error_client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    app.include_router(error_router)
    with TestClient(app) as client:
        yield client


def assert_problem(response: httpx2.Response, status: int, code: str) -> dict[str, Any]:
    """Check the members that every problem response must have, and return the body."""
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body: dict[str, Any] = response.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    assert body["instance"] == response.request.url.path
    assert body["request_id"] == response.headers["X-Request-ID"]
    return body


def test_unknown_route_returns_404_problem(client: TestClient) -> None:
    body = assert_problem(client.get("/no-such-route"), 404, "not_found")

    assert body["title"] == "Not Found"
    assert "detail" not in body, "a detail that only repeats the title is omitted"


def test_wrong_method_returns_405_problem_with_allow_header(client: TestClient) -> None:
    response = client.post("/health")

    assert_problem(response, 405, "method_not_allowed")
    assert "GET" in response.headers["allow"]


def test_domain_not_found_error(error_client: TestClient) -> None:
    body = assert_problem(error_client.get("/_test/not-found"), 404, "not_found")

    assert body["detail"] == "Crawl job 42 does not exist."


def test_domain_conflict_error(error_client: TestClient) -> None:
    body = assert_problem(error_client.get("/_test/conflict"), 409, "conflict")

    assert body["title"] == "Conflict"


def test_http_exception_keeps_its_detail(error_client: TestClient) -> None:
    body = assert_problem(error_client.get("/_test/forbidden"), 403, "forbidden")

    assert body["detail"] == "Not allowed in this test."


@pytest.mark.parametrize(
    ("query", "error_type"),
    [({}, "missing"), ({"limit": "many"}, "int_parsing")],
)
def test_validation_error_lists_each_problem(
    error_client: TestClient, query: dict[str, str], error_type: str
) -> None:
    response = error_client.get("/_test/validated", params=query)

    body = assert_problem(response, 422, "validation_failed")
    assert body["errors"] == [
        {"loc": ["query", "limit"], "msg": body["errors"][0]["msg"], "type": error_type}
    ]


def test_unhandled_exception_returns_generic_500(
    error_client: TestClient, read_logs: LogReader
) -> None:
    read_logs()

    response = error_client.get("/_test/crash")

    body = assert_problem(response, 500, "internal_error")
    assert body["detail"] == INTERNAL_ERROR_DETAIL
    assert "secret internal detail" not in response.text

    logs = read_logs()
    failures = [entry for entry in logs if entry.get("event") == "http.unhandled_exception"]
    assert len(failures) == 1
    assert failures[0]["request_id"] == body["request_id"]
    assert "RuntimeError: secret internal detail" in failures[0]["exception"]
    access = [entry for entry in logs if entry.get("event") == "http.request"]
    assert access[-1]["status_code"] == 500
