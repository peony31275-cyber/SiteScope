from fastapi.testclient import TestClient

from app import __version__


def test_openapi_document_lists_every_endpoint(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "SiteScope API"
    assert schema["info"]["version"] == __version__
    operations = {
        (method.upper(), path): operation["operationId"]
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    }
    assert operations == {
        ("GET", "/"): "get_service_info",
        ("GET", "/health"): "get_health",
        ("GET", "/api/v1/health/live"): "get_liveness",
        ("GET", "/api/v1/health/ready"): "get_readiness",
        ("POST", "/api/v1/crawls"): "create_crawl",
        ("GET", "/api/v1/crawls"): "list_crawls",
        ("GET", "/api/v1/crawls/{job_id}"): "get_crawl",
    }


def test_crawl_errors_are_documented_as_problem_details(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    documented = {
        ("POST", "/api/v1/crawls"): {"201", "422", "503"},
        ("GET", "/api/v1/crawls"): {"200", "422", "503"},
        ("GET", "/api/v1/crawls/{job_id}"): {"200", "404", "422", "503"},
    }
    for (method, path), statuses in documented.items():
        responses = paths[path][method.lower()]["responses"]
        assert set(responses) == statuses
        for status in statuses - {"200", "201"}:
            reference = responses[status]["content"]["application/json"]["schema"]["$ref"]
            assert reference == "#/components/schemas/ProblemDetails"
    # FastAPI's default validation schema does not match the problem+json responses.
    assert "HTTPValidationError" not in schema["components"]["schemas"]


def test_readiness_documents_its_503_response(client: TestClient) -> None:
    responses = client.get("/openapi.json").json()["paths"]["/api/v1/health/ready"]["get"][
        "responses"
    ]

    assert set(responses) >= {"200", "503"}


def test_interactive_docs_are_served(client: TestClient) -> None:
    swagger = client.get("/docs")
    redoc = client.get("/redoc")

    assert swagger.status_code == 200
    assert "swagger-ui" in swagger.text
    assert redoc.status_code == 200
    assert "redoc" in redoc.text.lower()
