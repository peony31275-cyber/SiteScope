from fastapi.testclient import TestClient

from app import __version__


def test_service_info(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": "SiteScope API",
        "version": __version__,
        "api_base_url": "/api/v1",
        "health_url": "/health",
        "docs_url": "/docs",
        "openapi_url": "/openapi.json",
    }


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
