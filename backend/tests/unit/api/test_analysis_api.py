"""Website-analysis endpoints: parameter and cursor validation, and the response mapping. No
database is needed: every request here is rejected before the service opens a transaction.
The queries are tested in tests/integration/test_site_analysis.py and test_analysis_api.py."""

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1.analysis_schemas import (
    MAX_CURSOR_CHARS,
    BrokenLinkItem,
    BrokenLinkList,
    CrawlStatsResponse,
    GraphNodeItem,
    LinkList,
    NodeKind,
    PageList,
    decode_broken_link_cursor,
    decode_link_cursor,
    decode_page_cursor,
)
from app.core.errors import PROBLEM_MEDIA_TYPE, InvalidInputError
from app.db.models import UrlState
from app.services.search_index import IndexingProgress
from app.services.site_analysis import (
    BrokenLinkListing,
    BrokenLinkPosition,
    CrawlStats,
    DepthCount,
    GraphNode,
    LinkClass,
    LinkListing,
    LinkPosition,
    LinkView,
    PageListing,
    PagePosition,
    TopLinkedPage,
)
from tests.support import LogReader

JOB = uuid.UUID("0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b")
PAGE = uuid.UUID("0199a1b2-8d4e-7f50-9b0c-1d2e3f4a5b6c")
NOW = datetime(2026, 11, 7, 10, 15, tzinfo=UTC)
BASE = f"/api/v1/crawls/{JOB}"


def cursor_of(value: Any) -> str:
    raw = json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def errors_of(response: Any) -> list[dict[str, Any]]:
    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == "validation_failed"
    errors: list[dict[str, Any]] = body["errors"]
    return errors


def link(**changes: Any) -> LinkView:
    values: dict[str, Any] = {
        "source_page_id": PAGE,
        "source_url": "https://docs.example.com/a",
        "target_url": "https://docs.example.com/gone",
        "anchor_text": "Gone",
        "position": 3,
        "is_internal": True,
        "nofollow": False,
        "target_state": UrlState.FETCHED,
        "target_http_status": 404,
        "target_page_id": None,
        "final_url": None,
        "final_http_status": None,
        "redirect_hops": 0,
        "checked_at": NOW,
        "classification": LinkClass.BROKEN,
    }
    return LinkView(**(values | changes))


# --- Cursors -------------------------------------------------------------------------------------


def test_cursors_round_trip_through_their_listings() -> None:
    page_position = PagePosition(2, "https://docs.example.com/a?b=c")
    link_position = LinkPosition("https://docs.example.com/a", 7, "https://docs.example.com/z")
    broken_position = BrokenLinkPosition("https://docs.example.com/z", "https://docs.example.com/a")

    pages = PageList.from_listing(PageListing([], page_position)).next_cursor
    links = LinkList.from_listing(LinkListing([], link_position)).next_cursor
    broken = BrokenLinkList.from_listing(BrokenLinkListing(0, [], broken_position)).next_cursor

    assert pages is not None
    assert links is not None
    assert broken is not None
    assert decode_page_cursor(pages) == page_position
    assert decode_link_cursor(links) == link_position
    assert decode_broken_link_cursor(broken) == broken_position
    assert all("=" not in cursor and "+" not in cursor for cursor in (pages, links, broken))
    assert PageList.from_listing(PageListing([], None)).next_cursor is None


def test_a_cursor_of_the_longest_urls_fits_the_cursor_limit() -> None:
    longest = "https://docs.example.com/" + "a" * (2048 - 25)
    broken = BrokenLinkList.from_listing(
        BrokenLinkListing(0, [], BrokenLinkPosition(longest, longest))
    ).next_cursor
    links = LinkList.from_listing(
        LinkListing([], LinkPosition(longest, 2**31 - 1, longest))
    ).next_cursor

    assert broken is not None
    assert links is not None
    assert max(len(broken), len(links)) <= MAX_CURSOR_CHARS
    assert decode_broken_link_cursor(broken).target_url == longest


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "!!!",
        "é",
        "bm90IGpzb24",  # "not json"
        cursor_of({"kind": "pages"}),
        cursor_of(["links", 0, "https://docs.example.com/"]),  # another listing's kind
        cursor_of(["pages", 0]),
        cursor_of(["pages", 0, "https://docs.example.com/", "extra"]),
        cursor_of(["pages", "0", "https://docs.example.com/"]),
        cursor_of(["pages", True, "https://docs.example.com/"]),  # bool is not an int here
        cursor_of(["pages", 1.0, "https://docs.example.com/"]),
        cursor_of(["pages", -1, "https://docs.example.com/"]),
        cursor_of(["pages", 2**31, "https://docs.example.com/"]),  # beyond a SQL integer
        cursor_of(["pages", 0, None]),
        cursor_of(["pages", 0, "https://docs.example.com/\u0000"]),  # NUL breaks PostgreSQL
        cursor_of(["pages", 0, "https://docs.example.com/é"]),
        cursor_of(["pages", 0, "https://docs.example.com/" + "a" * 2048]),
        cursor_of(["pages", 0, ["https://docs.example.com/"]]),
        base64.urlsafe_b64encode(b"[" * 6000).decode(),  # deep nesting: RecursionError
    ],
)
def test_a_cursor_that_is_not_one_of_this_listing_is_rejected(cursor: str) -> None:
    with pytest.raises(InvalidInputError) as raised:
        decode_page_cursor(cursor)

    [violation] = raised.value.violations
    assert (violation.loc, violation.type) == (("query", "cursor"), "invalid_cursor")


def test_link_and_broken_link_cursors_are_checked_against_their_own_shape() -> None:
    with pytest.raises(InvalidInputError):
        decode_link_cursor(cursor_of(["links", "https://a.test/", "1", "https://b.test/"]))
    with pytest.raises(InvalidInputError):
        decode_broken_link_cursor(cursor_of(["broken-links", "https://a.test/", 5]))
    with pytest.raises(InvalidInputError):
        decode_broken_link_cursor(cursor_of(["links", "https://a.test/", 1, "https://b.test/"]))


# --- Request validation through HTTP -------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "loc", "error_type"),
    [
        ("/api/v1/crawls/not-a-uuid/pages", ["path", "job_id"], "uuid_parsing"),
        ("/api/v1/crawls/not-a-uuid/stats", ["path", "job_id"], "uuid_parsing"),
        (f"{BASE}/pages?limit=0", ["query", "limit"], "greater_than_equal"),
        (f"{BASE}/pages?limit=201", ["query", "limit"], "less_than_equal"),
        (f"{BASE}/pages?depth=-1", ["query", "depth"], "greater_than_equal"),
        (f"{BASE}/pages?index_status=done", ["query", "index_status"], "enum"),
        (f"{BASE}/pages?cursor=garbage", ["query", "cursor"], "invalid_cursor"),
        (
            f"{BASE}/pages?cursor={'a' * (MAX_CURSOR_CHARS + 1)}",
            ["query", "cursor"],
            "string_too_long",
        ),
        (f"{BASE}/links?classification=dead", ["query", "classification"], "enum"),
        (f"{BASE}/links?internal=maybe", ["query", "internal"], "bool_parsing"),
        (f"{BASE}/links?source_page_id=12", ["query", "source_page_id"], "uuid_parsing"),
        (f"{BASE}/links?cursor=garbage", ["query", "cursor"], "invalid_cursor"),
        (f"{BASE}/broken-links?limit=500", ["query", "limit"], "less_than_equal"),
        (f"{BASE}/broken-links?cursor=garbage", ["query", "cursor"], "invalid_cursor"),
        (f"{BASE}/link-graph?max_nodes=0", ["query", "max_nodes"], "greater_than_equal"),
        (f"{BASE}/link-graph?max_nodes=2001", ["query", "max_nodes"], "less_than_equal"),
        (f"{BASE}/link-graph?max_edges=-1", ["query", "max_edges"], "greater_than_equal"),
        (f"{BASE}/link-graph?max_edges=10001", ["query", "max_edges"], "less_than_equal"),
    ],
)
def test_invalid_analysis_requests_are_rejected_before_any_query(
    client: TestClient, path: str, loc: list[str], error_type: str
) -> None:
    [error] = errors_of(client.get(path))

    assert (error["loc"], error["type"]) == (loc, error_type)


def test_a_cursor_from_another_listing_is_rejected(client: TestClient) -> None:
    pages_cursor = cursor_of(["pages", 0, "https://docs.example.com/"])

    [error] = errors_of(client.get(f"{BASE}/links?cursor={pages_cursor}"))

    assert error["type"] == "invalid_cursor"


def test_the_analysis_endpoints_are_read_only(client: TestClient) -> None:
    for name in ("pages", "links", "broken-links", "link-graph", "stats"):
        response = client.post(f"{BASE}/{name}")
        assert response.status_code == 405, name
        assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE


def test_cursors_are_not_logged(client: TestClient, read_logs: LogReader) -> None:
    secret = cursor_of(["pages", 0, "https://docs.example.com/private?token=s3cr3t"])
    read_logs()

    client.get(f"{BASE}/pages?cursor={secret}x&depth=-1")

    logs = read_logs()
    assert any(entry.get("path") == f"{BASE}/pages" for entry in logs)
    assert secret not in str(logs)
    assert "s3cr3t" not in str(logs)


# --- Responses -----------------------------------------------------------------------------------


def test_a_broken_link_reports_the_status_that_confirmed_it() -> None:
    direct = BrokenLinkItem.from_view(link())
    redirected = BrokenLinkItem.from_view(
        link(
            target_url="https://docs.example.com/old",
            target_http_status=301,
            final_url="https://docs.example.com/gone",
            final_http_status=410,
            redirect_hops=1,
        )
    )

    assert (direct.status, direct.final_url, direct.target_http_status) == (404, None, 404)
    assert (redirected.status, redirected.final_url, redirected.target_http_status) == (
        410,
        "https://docs.example.com/gone",
        301,
    )
    assert redirected.redirect_hops == 1


def test_links_report_whether_a_response_verified_them() -> None:
    listing = LinkList.from_listing(
        LinkListing(
            [
                link(),
                link(
                    target_state=None,
                    target_http_status=None,
                    checked_at=None,
                    classification=LinkClass.BEYOND_LIMITS,
                ),
                link(
                    target_state=UrlState.FAILED,
                    target_http_status=None,
                    classification=LinkClass.UNREACHABLE,
                ),
            ],
            None,
        )
    )

    assert [(item.verified, item.target_crawled) for item in listing.items] == [
        (True, True),
        (False, False),
        (False, True),
    ]


def test_graph_nodes_tell_pages_crawl_urls_and_link_targets_apart() -> None:
    page = GraphNode("https://a.test/", PAGE, "A", 0, UrlState.FETCHED, 200, 0, 2)
    crawl_url = GraphNode("https://a.test/gone", None, None, 1, UrlState.FETCHED, 404, 1, None)
    target = GraphNode("https://a.test/later", None, None, None, None, None, 1, None)

    items = [
        GraphNodeItem.from_node(index, node) for index, node in enumerate((page, crawl_url, target))
    ]

    assert [(item.id, item.kind) for item in items] == [
        (0, NodeKind.PAGE),
        (1, NodeKind.CRAWL_URL),
        (2, NodeKind.LINK_TARGET),
    ]


def test_statistics_are_mapped_field_by_field() -> None:
    stats = CrawlStats(
        job_id=JOB,
        status="completed",
        current_level=2,
        max_depth=3,
        max_pages=100,
        urls_discovered=20,
        urls_admitted=18,
        urls_by_state=dict.fromkeys(UrlState, 0) | {UrlState.FETCHED: 16, UrlState.SKIPPED: 2},
        pages_stored=14,
        status_1xx=0,
        status_2xx=14,
        status_3xx=1,
        status_4xx=1,
        status_5xx=0,
        no_response=0,
        depths=[DepthCount(0, 1, 1, 1), DepthCount(1, 17, 15, 13)],
        indexing=IndexingProgress(indexed=13, completed_at=NOW),
        links_total=50,
        links_internal=45,
        links_external=5,
        internal_targets=19,
        links_by_class=dict.fromkeys(LinkClass, 0)
        | {
            LinkClass.OK: 40,
            LinkClass.BROKEN: 2,
            LinkClass.OUT_OF_SCOPE: 5,
            LinkClass.NOT_FOLLOWED: 3,
        },
        top_linked=[TopLinkedPage("https://a.test/", PAGE, 12)],
    )

    body = CrawlStatsResponse.from_stats(stats).model_dump(mode="json")

    assert body["crawl"] == {
        "urls_discovered": 20,
        "urls_admitted": 18,
        "urls_by_state": {
            "pending": 0,
            "queued": 0,
            "in_progress": 0,
            "fetched": 16,
            "failed": 0,
            "skipped": 2,
            "cancelled": 0,
        },
        "pages_stored": 14,
        "current_depth": 2,
        "max_depth": 3,
        "max_pages": 100,
    }
    assert body["http_status"] == {
        "informational_1xx": 0,
        "success_2xx": 14,
        "redirect_3xx": 1,
        "client_error_4xx": 1,
        "server_error_5xx": 0,
        "no_response": 0,
    }
    assert body["depths"][1] == {"depth": 1, "urls": 17, "fetched": 15, "pages": 13}
    assert body["indexing"]["status"] == "indexed"
    assert {key: body["links"][key] for key in ("total", "broken", "verified", "unverified")} == {
        "total": 50,
        "broken": 2,
        "verified": 42,
        "unverified": 8,
    }
    assert body["links"]["by_class"]["not_followed"] == 3
    assert body["most_linked"] == [{"url": "https://a.test/", "page_id": str(PAGE), "in_links": 12}]
