"""Search endpoint: parameter validation, page arithmetic and the response mapping. No database
is needed: every request here is rejected before the search service opens a transaction. The
search itself is tested in tests/integration/test_search.py and test_analysis_api.py."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1.search_schemas import SearchResponse, search_offset
from app.core.errors import PROBLEM_MEDIA_TYPE, InvalidInputError
from app.indexing.queries import Snippet
from app.services.search import SearchHit, SearchResults
from app.services.search_index import IndexingProgress
from tests.support import LogReader

JOB = "0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b"
NOW = datetime(2026, 11, 7, 10, 15, tzinfo=UTC)


def errors_of(response: Any) -> list[dict[str, Any]]:
    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == "validation_failed"
    errors: list[dict[str, Any]] = body["errors"]
    return errors


@pytest.mark.parametrize(
    ("query", "loc", "error_type"),
    [
        ("q=redis", ["query", "job_id"], "missing"),
        (f"job_id={JOB}", ["query", "q"], "missing"),
        ("job_id=12&q=redis", ["query", "job_id"], "uuid_parsing"),
        (f"job_id={JOB}&q={'a' * 1025}", ["query", "q"], "string_too_long"),
        (f"job_id={JOB}&q=redis&page=0", ["query", "page"], "greater_than_equal"),
        (f"job_id={JOB}&q=redis&page=1002", ["query", "page"], "page_out_of_range"),
        (f"job_id={JOB}&q=redis&page={10**20}", ["query", "page"], "page_out_of_range"),
        (f"job_id={JOB}&q=redis&page_size=0", ["query", "page_size"], "greater_than_equal"),
        (f"job_id={JOB}&q=redis&page_size=51", ["query", "page_size"], "less_than_equal"),
        (f"job_id={JOB}&q=redis&page=x", ["query", "page"], "int_parsing"),
        (f"job_id={JOB}&q=redis&page=102", ["query", "page"], "page_out_of_range"),
        (f"job_id={JOB}&q=redis&page=22&page_size=50", ["query", "page"], "page_out_of_range"),
    ],
)
def test_invalid_searches_are_rejected_before_any_query(
    client: TestClient, query: str, loc: list[str], error_type: str
) -> None:
    [error] = errors_of(client.get(f"/api/v1/search?{query}"))

    assert (error["loc"], error["type"]) == (loc, error_type)


def test_the_deepest_page_is_the_last_one_within_the_offset_limit() -> None:
    assert search_offset("redis", 1, 10) == 0
    assert search_offset("redis", 101, 10) == 1000
    assert search_offset("redis", 21, 50) == 1000
    assert search_offset("redis", 1001, 1) == 1000
    with pytest.raises(InvalidInputError) as raised:
        search_offset("redis", 102, 10)
    [violation] = raised.value.violations
    assert (violation.loc, violation.type) == (("query", "page"), "page_out_of_range")
    assert "at most 101 for page_size 10" in violation.message


def test_a_query_error_and_a_page_error_are_reported_together(client: TestClient) -> None:
    """Before the review, the page was checked first and the query error went unreported."""
    errors = errors_of(client.get(f"/api/v1/search?job_id={JOB}&q=%20%09&page=102"))

    assert [(error["loc"], error["type"]) for error in errors] == [
        (["query", "q"], "empty_query"),
        (["query", "page"], "page_out_of_range"),
    ]


def test_search_queries_are_not_logged(client: TestClient, read_logs: LogReader) -> None:
    read_logs()

    client.get("/api/v1/search?job_id=12&q=confidential+merger+plans")

    logs = read_logs()
    assert any(entry.get("path") == "/api/v1/search" for entry in logs)
    assert "confidential" not in str(logs)


def test_search_results_are_mapped_with_plain_text_snippets_and_ranges() -> None:
    page_id = uuid.uuid4()
    results = SearchResults(
        job_id=uuid.UUID(JOB),
        query="consumer groups",
        searchable=True,
        total=11,
        limit=10,
        offset=10,
        hits=[
            SearchHit(
                page_id=page_id,
                url="https://docs.example.com/streams",
                title="Streams",
                rank=0.25,
                snippet=Snippet("Consumer groups <b>share</b>", ((0, 8), (9, 15))),
                fetched_at=NOW,
                indexed_at=NOW,
                content_version=2,
            )
        ],
        indexing=IndexingProgress(indexed=10, queued=1),
    )

    body = SearchResponse.from_results(results, page=2, page_size=10).model_dump(mode="json")

    assert {key: body[key] for key in ("query", "searchable", "total", "page", "page_size")} == {
        "query": "consumer groups",
        "searchable": True,
        "total": 11,
        "page": 2,
        "page_size": 10,
    }
    assert body["results"] == [
        {
            "page_id": str(page_id),
            "url": "https://docs.example.com/streams",
            "title": "Streams",
            "snippet": "Consumer groups <b>share</b>",  # text, never markup to render
            "highlights": [{"start": 0, "end": 8}, {"start": 9, "end": 15}],
            "score": 0.25,
            "crawled_at": "2026-11-07T10:15:00Z",
            "indexed_at": "2026-11-07T10:15:00Z",
        }
    ]
    assert body["indexing"]["status"] == "running"
    assert body["indexing"]["documents_by_status"] == {
        "pending": 0,
        "queued": 1,
        "indexed": 10,
        "failed": 0,
    }
