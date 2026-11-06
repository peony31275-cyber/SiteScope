from fastapi.testclient import TestClient

from app import __version__


def test_openapi_document_lists_every_endpoint(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "SiteScope API"
    assert schema["info"]["version"] == __version__
    operations = {
        path: operation["get"]["operationId"] for path, operation in schema["paths"].items()
    }
    assert operations == {
        "/": "get_service_info",
        "/health": "get_health",
        "/api/v1/health/live": "get_liveness",
    }


def test_interactive_docs_are_served(client: TestClient) -> None:
    swagger = client.get("/docs")
    redoc = client.get("/redoc")

    assert swagger.status_code == 200
    assert "swagger-ui" in swagger.text
    assert redoc.status_code == 200
    assert "redoc" in redoc.text.lower()
