from typing import Any

from fastapi.testclient import TestClient

from app import __version__

ANALYSIS_AND_SEARCH = [
    ("GET", f"/api/v1/crawls/{{job_id}}/{name}")
    for name in ("pages", "links", "broken-links", "link-graph", "stats")
] + [("GET", "/api/v1/search")]


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
        ("POST", "/api/v1/crawls/{job_id}/cancel"): "cancel_crawl",
        ("GET", "/api/v1/crawls/{job_id}/pages"): "list_crawl_pages",
        ("GET", "/api/v1/crawls/{job_id}/links"): "list_crawl_links",
        ("GET", "/api/v1/crawls/{job_id}/broken-links"): "list_crawl_broken_links",
        ("GET", "/api/v1/crawls/{job_id}/link-graph"): "get_crawl_link_graph",
        ("GET", "/api/v1/crawls/{job_id}/stats"): "get_crawl_stats",
        ("GET", "/api/v1/search"): "search_pages",
    }


def test_crawl_errors_are_documented_as_problem_details(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    documented = {
        ("POST", "/api/v1/crawls"): {"200", "201", "409", "422", "503"},
        ("GET", "/api/v1/crawls"): {"200", "422", "503"},
        ("GET", "/api/v1/crawls/{job_id}"): {"200", "404", "422", "503"},
        ("POST", "/api/v1/crawls/{job_id}/cancel"): {"200", "202", "404", "422", "503"},
        **{key: {"200", "404", "422", "503"} for key in ANALYSIS_AND_SEARCH},
    }
    for (method, path), statuses in documented.items():
        responses = paths[path][method.lower()]["responses"]
        assert set(responses) == statuses
        for status in statuses - {"200", "201", "202"}:
            reference = responses[status]["content"]["application/json"]["schema"]["$ref"]
            assert reference == "#/components/schemas/ProblemDetails"
    # FastAPI's default validation schema does not match the problem+json responses.
    assert "HTTPValidationError" not in schema["components"]["schemas"]


def parameters(schema: dict[str, Any], path: str) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in schema["paths"][path]["get"]["parameters"]}


def response_schema(schema: dict[str, Any], path: str) -> str:
    content = schema["paths"][path]["get"]["responses"]["200"]["content"]
    reference: str = content["application/json"]["schema"]["$ref"]
    return reference.removeprefix("#/components/schemas/")


def test_the_analysis_and_search_endpoints_document_their_parameters(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    search = parameters(schema, "/api/v1/search")
    assert (search["job_id"]["required"], search["q"]["required"]) == (True, True)
    assert search["job_id"]["schema"]["format"] == "uuid"
    assert search["q"]["schema"]["maxLength"] == 1024
    assert search["page"]["schema"]["minimum"] == 1
    assert "maximum" not in search["page"]["schema"]  # any deeper page is page_out_of_range
    assert (search["page_size"]["schema"]["maximum"], search["page_size"]["schema"]["default"]) == (
        50,
        10,
    )
    pages = parameters(schema, "/api/v1/crawls/{job_id}/pages")
    assert set(pages) == {"job_id", "limit", "cursor", "depth", "index_status"}
    assert (pages["limit"]["schema"]["maximum"], pages["limit"]["schema"]["default"]) == (200, 50)
    assert pages["cursor"]["schema"]["anyOf"][0]["maxLength"] == 8192
    links = parameters(schema, "/api/v1/crawls/{job_id}/links")
    assert set(links) == {
        "job_id",
        "limit",
        "cursor",
        "source_page_id",
        "internal",
        "classification",
        "verified",
        "redirected",
    }
    graph = parameters(schema, "/api/v1/crawls/{job_id}/link-graph")
    nodes, edges = graph["max_nodes"]["schema"], graph["max_edges"]["schema"]
    assert (nodes["type"], nodes["minimum"], nodes["maximum"], nodes["default"]) == (
        "integer",
        1,
        2000,
        500,
    )
    assert (edges["minimum"], edges["maximum"], edges["default"]) == (0, 10_000, 2000)
    classes = schema["components"]["schemas"]["LinkClass"]["enum"]
    assert set(classes) == {
        "ok",
        "broken",
        "server_error",
        "http_error",
        "redirect_error",
        "out_of_scope",
        "not_followed",
        "beyond_limits",
        "not_crawled",
        "robots_blocked",
        "unreachable",
    }


def test_the_analysis_and_search_endpoints_document_their_responses(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    components = schema["components"]["schemas"]

    assert {path: response_schema(schema, path) for _, path in ANALYSIS_AND_SEARCH} == {
        "/api/v1/crawls/{job_id}/pages": "PageList",
        "/api/v1/crawls/{job_id}/links": "LinkList",
        "/api/v1/crawls/{job_id}/broken-links": "BrokenLinkList",
        "/api/v1/crawls/{job_id}/link-graph": "LinkGraphResponse",
        "/api/v1/crawls/{job_id}/stats": "CrawlStatsResponse",
        "/api/v1/search": "SearchResponse",
    }
    for name in ("PageList", "BrokenLinkList", "LinkGraphResponse", "CrawlStatsResponse"):
        assert components[name]["examples"], name
    assert components["SearchResponse"]["examples"][0]["results"][0]["highlights"]
    for _, path in ANALYSIS_AND_SEARCH:
        operation = schema["paths"][path]["get"]
        assert operation["summary"]
        assert operation["description"]
        assert "timeout" in operation["responses"]["503"]["description"]
    assert (
        "invalid_cursor"
        in schema["paths"]["/api/v1/crawls/{job_id}/links"]["get"]["responses"]["422"][
            "description"
        ]
    )
    assert (
        "page_out_of_range"
        in schema["paths"]["/api/v1/search"]["get"]["responses"]["422"]["description"]
    )
    progress = components["CrawlProgress"]["properties"]
    assert "pages_stored" in progress
    tags = {tag["name"]: tag["description"] for tag in schema["tags"]}
    for term in ("*page*", "*broken link*", "*unverified*", "*document*", "*discovered*"):
        assert term in tags["analysis"], term
    assert "search" in tags


def test_the_idempotency_key_header_is_documented(client: TestClient) -> None:
    operation = client.get("/openapi.json").json()["paths"]["/api/v1/crawls"]["post"]
    [header] = [item for item in operation["parameters"] if item["in"] == "header"]
    assert header["name"] == "Idempotency-Key"
    assert header["required"] is False
    assert header["schema"]["anyOf"][0]["maxLength"] == 255
    replay = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert replay == "#/components/schemas/CrawlJobSummary"


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
