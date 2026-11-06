from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app import __version__


def test_liveness(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "sitescope-api"
    assert body["version"] == __version__
    assert body["uptime_seconds"] >= 0
    checked_at = datetime.fromisoformat(body["checked_at"])
    assert checked_at.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - checked_at) < timedelta(minutes=1)


def test_liveness_reports_no_dependency_status(client: TestClient) -> None:
    """Liveness must not claim anything about PostgreSQL or Redis; readiness is separate."""
    body = client.get("/api/v1/health/live").json()

    assert set(body) == {"status", "service", "version", "uptime_seconds", "checked_at"}


def test_uptime_increases(client: TestClient) -> None:
    first = client.get("/api/v1/health/live").json()["uptime_seconds"]
    second = client.get("/api/v1/health/live").json()["uptime_seconds"]

    assert second >= first
