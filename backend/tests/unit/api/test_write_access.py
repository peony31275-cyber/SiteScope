"""Write access to the API (Phase 11, ADR-0024): job creation and cancellation follow the
deployment's write access; reads stay public. No database is needed: a refused write never
reaches the service, and an accepted one is shown by reaching request validation (an
unapproved seed URL is a 422 that only the service raises)."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import PROBLEM_MEDIA_TYPE
from app.main import create_app
from tests.support import LogReader

TOKEN = "an-admin-token-of-at-least-32-characters"
UNAPPROVED = {"seed_url": "https://not-approved.example/", "max_depth": 1, "max_pages": 10}


def client_for(**values: Any) -> TestClient:
    settings = Settings(
        _env_file=None,
        crawl_allowed_hosts=("docs.example.com",),
        **values,
    )
    return TestClient(create_app(settings))


@pytest.fixture
def production() -> Iterator[TestClient]:
    with client_for(
        environment="production",
        postgres_password=SecretStr("a-production-postgres-password"),
        redis_password=SecretStr("a-production-redis-password"),
    ) as client:
        yield client


@pytest.fixture
def with_token() -> Iterator[TestClient]:
    with client_for(environment="test", admin_token=SecretStr(TOKEN)) as client:
        yield client


def writes(client: TestClient, headers: dict[str, str] | None = None) -> list[Any]:
    return [
        client.post("/api/v1/crawls", json=UNAPPROVED, headers=headers),
        client.post(f"/api/v1/crawls/{uuid.uuid4()}/cancel", headers=headers),
    ]


def test_production_without_a_token_is_read_only(
    production: TestClient, read_logs: LogReader
) -> None:
    for response in writes(production):
        assert response.status_code == 403
        assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
        assert response.json()["code"] == "writes_disabled"
    # Even a token is refused: there is none to compare it with.
    assert writes(production, {"Authorization": f"Bearer {TOKEN}"})[0].status_code == 403
    assert production.get("/api/v1/crawl-policy").json()["write_access"] == "disabled"
    refused = [line for line in read_logs() if line.get("event") == "api.write_refused"]
    assert {line["reason"] for line in refused} == {"writes_disabled"}
    assert {line["path"] for line in refused} >= {"/api/v1/crawls"}


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Bearer wrong-token-of-the-same-kind-but-not-it",
        f"Basic {TOKEN}",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        "Bearer ",
    ],
)
def test_a_write_without_the_right_token_is_unauthorized(
    with_token: TestClient, authorization: str | None
) -> None:
    headers = {"Authorization": authorization} if authorization is not None else None
    for response in writes(with_token, headers):
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Bearer realm="sitescope"'
        assert response.json()["code"] == "unauthorized"


def test_the_right_token_passes_and_is_never_logged(
    with_token: TestClient, read_logs: LogReader
) -> None:
    with_token.post("/api/v1/crawls", json=UNAPPROVED)  # refused first: logged without a token
    accepted = with_token.post(
        "/api/v1/crawls", json=UNAPPROVED, headers={"Authorization": f"Bearer {TOKEN}"}
    )

    # Past the access check: the service validated the request and refused the target.
    assert accepted.status_code == 422
    assert accepted.json()["errors"][0]["type"] == "target_not_allowed"
    assert with_token.get("/api/v1/crawl-policy").json()["write_access"] == "token"
    assert TOKEN not in str(read_logs())


def test_writes_are_open_outside_production_without_a_token() -> None:
    with client_for(environment="development") as client:
        response = client.post("/api/v1/crawls", json=UNAPPROVED)
        policy = client.get("/api/v1/crawl-policy").json()

    assert response.status_code == 422  # validated, not refused
    assert policy["write_access"] == "open"


def test_the_openapi_document_declares_the_bearer_scheme_on_writes_only() -> None:
    with client_for(environment="test") as client:
        document = client.get("/openapi.json").json()

    assert document["components"]["securitySchemes"]["AdminToken"]["scheme"] == "bearer"
    secured = {
        (path, method)
        for path, operations in document["paths"].items()
        for method, operation in operations.items()
        if operation.get("security")
    }
    assert secured == {
        ("/api/v1/crawls", "post"),
        ("/api/v1/crawls/{job_id}/cancel", "post"),
    }
