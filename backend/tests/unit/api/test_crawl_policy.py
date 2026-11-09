"""The crawl policy endpoint (Phase 9): the approved targets and job limits that job creation
enforces, read from the settings. No database is needed."""

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app


def test_nothing_is_approved_by_default(client: TestClient) -> None:
    response = client.get("/api/v1/crawl-policy")

    assert response.status_code == 200
    assert response.json() == {
        "approved_hosts": [],
        "allowed_schemes": ["http", "https"],
        "allowed_ports": [80, 443],
        "max_depth_limit": 5,
        "max_pages_limit": 1000,
        "max_active_jobs": 50,
        "write_access": "open",  # outside production, without an admin token
    }


def test_the_policy_is_the_one_job_creation_enforces() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        crawl_allowed_hosts=("Docs.Example.com", "*.example.org", "127.0.0.1"),
        crawl_allowed_schemes=("https",),
        crawl_allowed_ports=(443, 8443),
        crawl_max_depth_limit=3,
        crawl_max_pages_limit=100,
    )
    with TestClient(create_app(settings)) as client:
        policy = client.get("/api/v1/crawl-policy").json()
        # Every value the policy names is accepted, and the next one up is refused.
        accepted = client.post(
            "/api/v1/crawls",
            json={"seed_url": "https://docs.example.com:8443/", "max_depth": 4, "max_pages": 101},
        )

    assert policy == {
        "approved_hosts": ["docs.example.com", "*.example.org", "127.0.0.1"],  # normalized
        "allowed_schemes": ["https"],
        "allowed_ports": [443, 8443],
        "max_depth_limit": 3,
        "max_pages_limit": 100,
        "max_active_jobs": 50,
        "write_access": "open",
    }
    # Only the limits are refused: the host, scheme and port are approved by the policy.
    assert accepted.status_code == 422
    assert {(tuple(e["loc"]), e["type"]) for e in accepted.json()["errors"]} == {
        (("body", "max_depth"), "limit_exceeded"),
        (("body", "max_pages"), "limit_exceeded"),
    }


def test_the_policy_names_no_internal_origin_or_secret() -> None:
    """Only the crawl policy is exposed: not the fetcher's internal origins, nor anything else
    of the configuration."""
    settings = Settings(
        _env_file=None,
        environment="test",
        crawl_allowed_hosts=("127.0.0.1",),
        fetch_internal_origins=("http://127.0.0.1:8001",),
        postgres_password=SecretStr("not-in-the-response"),
        admin_token=SecretStr("admin-token-not-in-the-response"),
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/crawl-policy")

    assert set(response.json()) == {
        "approved_hosts",
        "allowed_schemes",
        "allowed_ports",
        "max_depth_limit",
        "max_pages_limit",
        "max_active_jobs",
        "write_access",
    }
    assert response.json()["write_access"] == "token"  # the mode, never the token
    assert "8001" not in response.text
    assert "not-in-the-response" not in response.text
