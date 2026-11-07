"""Crawl endpoints: request validation. No database is needed, because every request here is
rejected before the service opens a transaction. Persistence is tested in
tests/integration/test_crawl_jobs.py."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1.crawl_schemas import decode_cursor, encode_cursor
from app.core.config import Settings
from app.core.errors import PROBLEM_MEDIA_TYPE, InvalidInputError
from app.main import create_app
from app.services.crawl_jobs import JobPosition
from tests.support import LogReader

VALID = {"seed_url": "https://docs.example.com/", "max_depth": 2, "max_pages": 50}


@pytest.fixture
def crawl_client() -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        environment="test",
        crawl_allowed_hosts=("docs.example.com",),
        crawl_allowed_schemes=("https",),
        crawl_max_depth_limit=3,
        crawl_max_pages_limit=100,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def errors_of(response: Any) -> list[dict[str, Any]]:
    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["request_id"] == response.headers["X-Request-ID"]
    errors: list[dict[str, Any]] = body["errors"]
    return errors


@pytest.mark.parametrize(
    ("seed_url", "error_type", "message"),
    [
        ("ftp://docs.example.com/", "invalid_url", "scheme 'ftp' is not supported"),
        ("docs.example.com", "invalid_url", "must be absolute"),
        ("https://user:pw@docs.example.com/", "invalid_url", "credentials"),
        ("https://127.1/", "invalid_url", "dotted-decimal"),
        ("https://other.example/", "target_not_allowed", "not an approved crawl target"),
        ("https://www.docs.example.com/", "target_not_allowed", "not an approved crawl target"),
        ("https://docs.example.com.evil.test/", "target_not_allowed", "not an approved"),
        ("http://docs.example.com/", "target_not_allowed", "scheme 'http' is not allowed"),
        ("https://docs.example.com:8443/", "target_not_allowed", "port 8443 is not allowed"),
        ("https://169.254.169.254/latest/meta-data/", "target_not_allowed", "not an approved"),
    ],
)
def test_rejects_invalid_or_unapproved_seed_urls(
    crawl_client: TestClient, seed_url: str, error_type: str, message: str
) -> None:
    response = crawl_client.post("/api/v1/crawls", json={**VALID, "seed_url": seed_url})

    [error] = errors_of(response)
    assert error["loc"] == ["body", "seed_url"]
    assert error["type"] == error_type
    assert message in error["msg"]


def test_reports_every_rule_violation_at_once(crawl_client: TestClient) -> None:
    response = crawl_client.post(
        "/api/v1/crawls",
        json={"seed_url": "https://other.example/", "max_depth": 4, "max_pages": 101},
    )

    errors = errors_of(response)
    assert [(error["loc"][-1], error["type"]) for error in errors] == [
        ("seed_url", "target_not_allowed"),
        ("max_depth", "limit_exceeded"),
        ("max_pages", "limit_exceeded"),
    ]
    assert errors[1]["msg"] == "max_depth must be at most 3 on this server"
    assert errors[2]["msg"] == "max_pages must be at most 100 on this server"


@pytest.mark.parametrize(
    ("body", "loc", "error_type"),
    [
        ({"max_depth": 2, "max_pages": 50}, ["body", "seed_url"], "missing"),
        ({**VALID, "max_depth": "2"}, ["body", "max_depth"], "int_type"),  # strict: no strings
        ({**VALID, "max_depth": 2.0}, ["body", "max_depth"], "int_type"),
        ({**VALID, "max_pages": True}, ["body", "max_pages"], "int_type"),
        ({**VALID, "max_depth": -1}, ["body", "max_depth"], "greater_than_equal"),
        ({**VALID, "max_pages": 0}, ["body", "max_pages"], "greater_than_equal"),
        ({**VALID, "seed_url": 42}, ["body", "seed_url"], "string_type"),
        ({**VALID, "seed_url": ""}, ["body", "seed_url"], "string_too_short"),
        ({**VALID, "seed_url": "https://a/" + "a" * 5000}, ["body", "seed_url"], "string_too_long"),
        ({**VALID, "allowed_hosts": ["x"]}, ["body", "allowed_hosts"], "extra_forbidden"),
    ],
)
def test_rejects_malformed_bodies(
    crawl_client: TestClient, body: dict[str, Any], loc: list[str], error_type: str
) -> None:
    errors = errors_of(crawl_client.post("/api/v1/crawls", json=body))

    assert [(error["loc"], error["type"]) for error in errors] == [(loc, error_type)]


def test_nothing_is_approved_by_default(client: TestClient) -> None:
    """The default settings approve no crawl target at all."""
    [error] = errors_of(client.post("/api/v1/crawls", json=VALID))

    assert error["type"] == "target_not_allowed"
    assert error["msg"] == "no crawl targets are approved on this server"


def test_rejections_are_logged_without_the_url(
    crawl_client: TestClient, read_logs: LogReader
) -> None:
    read_logs()

    crawl_client.post(
        "/api/v1/crawls", json={**VALID, "seed_url": "https://other.example/?token=s3cr3t"}
    )

    logs = read_logs()
    rejected = [entry for entry in logs if entry.get("event") == "job.rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reasons"] == ["target_not_allowed"]
    assert "s3cr3t" not in str(logs)


def test_get_rejects_a_malformed_job_id(crawl_client: TestClient) -> None:
    [error] = errors_of(crawl_client.get("/api/v1/crawls/not-a-uuid"))

    assert error["loc"] == ["path", "job_id"]


@pytest.mark.parametrize(
    ("query", "loc", "error_type"),
    [
        ({"limit": "0"}, ["query", "limit"], "greater_than_equal"),
        ({"limit": "101"}, ["query", "limit"], "less_than_equal"),
        ({"cursor": "not-a-cursor"}, ["query", "cursor"], "invalid_cursor"),
        ({"cursor": "x" * 257}, ["query", "cursor"], "string_too_long"),
    ],
)
def test_list_rejects_invalid_paging_parameters(
    crawl_client: TestClient, query: dict[str, str], loc: list[str], error_type: str
) -> None:
    [error] = errors_of(crawl_client.get("/api/v1/crawls", params=query))

    assert (error["loc"], error["type"]) == (loc, error_type)


def test_cursor_round_trip() -> None:
    position = JobPosition(
        created_at=datetime(2026, 11, 6, 21, 13, 7, 788123, tzinfo=UTC), id=uuid.uuid4()
    )

    cursor = encode_cursor(position)

    assert decode_cursor(cursor) == position
    assert cursor.isascii()
    assert "=" not in cursor  # unpadded


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "%%%",
        encode_cursor(JobPosition(datetime(2026, 1, 1, tzinfo=UTC), uuid.uuid4())) + "x",
        "MjAyNi0wMS0wMVQwMDowMDowMA",  # "2026-01-01T00:00:00": no time zone, no id
        "bm90IGEgY3Vyc29y",  # "not a cursor"
        "ü",
    ],
)
def test_tampered_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(InvalidInputError):
        decode_cursor(cursor)
