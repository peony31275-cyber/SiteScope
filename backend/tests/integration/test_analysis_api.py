"""The Phase 8 endpoints through HTTP, against PostgreSQL (and Redis, to index pages through the
Phase 7 services): search, pages, links, broken links, the link graph, statistics, and the job
detail's progress.

The site is built directly in the states a finished crawl leaves (``tests/analysis_support``):

    /          the seed: 301 to /home                           depth 0
    /home      page "Home"                                       depth 0 (a redirect target)
    /docs      page "Docs"                                       depth 1 (max_depth)
    /team      page, noindex                                     depth 1
    /gone      404                                               depth 1
    /old       301 to /docs                                      depth 1
    /secret    disallowed by robots.txt                          depth 1

    /home links to /docs, /gone, /old, /secret, /team and https://other.example/ (external);
    /docs links to /home, /gone and /deeper (beyond max_depth, never admitted).
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import Settings
from app.core.errors import PROBLEM_MEDIA_TYPE
from app.db.models import UrlState
from app.db.session import Database
from app.main import create_app
from app.queue.streams import IndexStream
from tests.analysis_support import Link, add_links, add_url, set_job, url_of
from tests.crawl_support import new_job
from tests.index_support import add_page, execute, finish_job, index_everything

pytestmark = pytest.mark.anyio

FETCHED = UrlState.FETCHED


@pytest.fixture
async def stream(redis_client: Redis, redis_prefix: str) -> IndexStream:
    stream = IndexStream(redis_client, key_prefix=redis_prefix)
    await stream.ensure_group()
    return stream


@pytest.fixture
def slow_client(test_database: Settings) -> Iterator[TestClient]:
    """The application with short search and analysis time limits."""
    settings = test_database.model_copy(
        update={"search_timeout_seconds": 0.3, "analysis_timeout_seconds": 0.3}
    )
    with TestClient(create_app(settings)) as client:
        yield client


def ok(client: TestClient, path: str, **params: Any) -> Any:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def path_of(url: str) -> str:
    return url.removeprefix(url_of(""))


def problem(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body: dict[str, Any] = response.json()
    assert body["code"] == code
    assert "Traceback" not in response.text
    return body


async def crawled_site(database: Database, stream: IndexStream) -> tuple[uuid.UUID, dict[str, Any]]:
    """The site of the module docstring, as a completed job with its pages indexed."""
    job_id, _ = await new_job(database, max_depth=1, max_pages=50)
    await execute(
        database,
        "UPDATE crawl_urls SET state = 'fetched', outcome = 'redirect', http_status = 301,"
        " redirect_target = :target, result_epoch = 0, finished_at = clock_timestamp()"
        " WHERE job_id = :job AND normalized_url = :seed",
        job=job_id,
        seed=url_of(""),
        target=url_of("home"),
    )
    home = await add_page(
        database, job_id, path="home", title="Home", body="Welcome to our consumer site"
    )
    docs = await add_page(
        database,
        job_id,
        path="docs",
        depth=1,
        title="Docs",
        body="Consumer groups let workers share a stream",
    )
    team = await add_page(database, job_id, path="team", depth=1, noindex=True)
    await add_url(database, job_id, "gone", state=FETCHED, outcome="http_error", http_status=404)
    await add_url(
        database,
        job_id,
        "old",
        state=FETCHED,
        outcome="redirect",
        http_status=301,
        redirect_to="docs",
    )
    await add_url(database, job_id, "secret", state=UrlState.SKIPPED, outcome="robots_disallowed")
    await add_links(
        database,
        job_id,
        home,
        [
            Link("docs", anchor="Documentation"),
            Link("gone", anchor="Old page"),
            Link("old"),
            Link("secret"),
            Link("team"),
            Link("https://other.example/", internal=False),
        ],
    )
    await add_links(database, job_id, docs, [Link("home"), Link("gone"), Link("deeper")])
    await set_job(database, job_id, admitted_count=7, current_level=1)
    await finish_job(database, job_id)
    assert await index_everything(database, stream) == 2  # /team is noindex
    return job_id, {"home": home, "docs": docs, "team": team}


# --- Pages ---------------------------------------------------------------------------------------


async def test_pages_are_listed_in_bfs_order_without_bodies(
    api_client: TestClient, database: Database, stream: IndexStream
) -> None:
    job_id, pages = await crawled_site(database, stream)
    base = f"/api/v1/crawls/{job_id}/pages"

    first = ok(api_client, base, limit=2)
    rest = ok(api_client, base, limit=2, cursor=first["next_cursor"])

    items = first["items"] + rest["items"]
    assert [(path_of(item["url"]), item["depth"], item["index_status"]) for item in items] == [
        ("home", 0, "indexed"),
        ("docs", 1, "indexed"),
        ("team", 1, "noindex"),
    ]
    assert rest["next_cursor"] is None
    assert set(items[0]) == {
        "page_id",
        "url",
        "title",
        "description",
        "http_status",
        "content_type",
        "depth",
        "crawled_at",
        "noindex",
        "nofollow",
        "index_status",
        "indexed_at",
    }  # no body, headings or extracted text
    assert (items[0]["page_id"], items[0]["title"], items[0]["http_status"]) == (
        str(pages["home"]),
        "Home",
        200,
    )
    assert items[0]["indexed_at"] is not None
    assert items[2]["indexed_at"] is None
    filtered = ok(api_client, base, depth=1, index_status="indexed")
    assert [path_of(item["url"]) for item in filtered["items"]] == ["docs"]


# --- Links and broken links ----------------------------------------------------------------------


async def test_links_are_listed_with_their_classification(
    api_client: TestClient, database: Database, stream: IndexStream
) -> None:
    job_id, pages = await crawled_site(database, stream)
    base = f"/api/v1/crawls/{job_id}/links"

    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = ok(api_client, base, limit=4, **({"cursor": cursor} if cursor else {}))
        items += page["items"]
        if (cursor := page["next_cursor"]) is None:
            break

    assert [
        (path_of(item["source_url"]), path_of(item["target_url"]), item["classification"])
        for item in items
    ] == [
        ("docs", "home", "ok"),
        ("docs", "gone", "broken"),
        ("docs", "deeper", "beyond_limits"),
        ("home", "docs", "ok"),
        ("home", "gone", "broken"),
        ("home", "old", "ok"),
        ("home", "secret", "robots_blocked"),
        ("home", "team", "ok"),
        ("home", "https://other.example/", "out_of_scope"),
    ]
    old = items[5]
    assert {
        key: old[key] for key in ("target_http_status", "final_http_status", "redirect_hops")
    } == {
        "target_http_status": 301,
        "final_http_status": 200,
        "redirect_hops": 1,
    }
    assert path_of(old["final_url"]) == "docs"
    assert (old["verified"], old["target_crawled"], old["internal"]) == (True, True, True)
    deeper = items[2]
    assert (deeper["target_state"], deeper["target_crawled"], deeper["verified"]) == (
        None,
        False,
        False,
    )
    assert items[3]["anchor_text"] == "Documentation"
    assert items[3]["target_page_id"] == str(pages["docs"])
    on_docs = ok(api_client, base, source_page_id=str(pages["docs"]))
    external = ok(api_client, base, internal="false")
    unverified = ok(api_client, base, classification="robots_blocked")
    unchecked = ok(api_client, base, verified="false")
    redirecting = ok(api_client, base, redirected="true")
    assert len(on_docs["items"]) == 3
    assert [path_of(item["target_url"]) for item in unchecked["items"]] == [
        "deeper",
        "secret",
        "https://other.example/",
    ]
    assert [path_of(item["target_url"]) for item in redirecting["items"]] == ["old"]
    assert [item["target_url"] for item in external["items"]] == ["https://other.example/"]
    assert [path_of(item["target_url"]) for item in unverified["items"]] == ["secret"]


async def test_broken_links_are_the_confirmed_404_and_410_links_only(
    api_client: TestClient, database: Database, stream: IndexStream
) -> None:
    job_id, pages = await crawled_site(database, stream)
    base = f"/api/v1/crawls/{job_id}/broken-links"

    first = ok(api_client, base, limit=1)
    second = ok(api_client, base, limit=1, cursor=first["next_cursor"])

    assert (first["total"], second["total"], second["next_cursor"]) == (2, 2, None)
    found = first["items"] + second["items"]
    assert [(path_of(item["target_url"]), path_of(item["source_url"])) for item in found] == [
        ("gone", "docs"),
        ("gone", "home"),
    ]
    from_home = found[1]
    assert (from_home["status"], from_home["anchor_text"], from_home["source_page_id"]) == (
        404,
        "Old page",
        str(pages["home"]),
    )
    assert (from_home["final_url"], from_home["redirect_hops"]) == (None, 0)
    assert from_home["checked_at"] is not None


# --- The link graph ------------------------------------------------------------------------------


async def test_the_link_graph_tells_pages_from_urls_never_crawled(
    api_client: TestClient, database: Database, stream: IndexStream
) -> None:
    job_id, _ = await crawled_site(database, stream)

    graph = ok(api_client, f"/api/v1/crawls/{job_id}/link-graph")
    stats = ok(api_client, f"/api/v1/crawls/{job_id}/stats")

    assert [
        (node["id"], path_of(node["url"]), node["kind"], node["in_degree"], node["out_degree"])
        for node in graph["nodes"]
    ] == [
        (0, "", "crawl_url", 0, None),  # the seed redirected: it has no page
        (1, "home", "page", 1, 5),
        (2, "docs", "page", 1, 3),
        (3, "gone", "crawl_url", 2, None),
        (4, "old", "crawl_url", 1, None),
        (5, "secret", "crawl_url", 1, None),
        (6, "team", "page", 1, 0),
        (7, "deeper", "link_target", 1, None),
    ]
    assert [(edge["source"], edge["target"]) for edge in graph["edges"]] == [
        (2, 1),
        (2, 3),
        (2, 7),
        (1, 2),
        (1, 3),
        (1, 4),
        (1, 5),
        (1, 6),
    ]
    assert (graph["total_nodes"], graph["total_edges"], graph["truncated"]) == (8, 8, False)
    assert graph["total_nodes"] == stats["crawl"]["urls_discovered"]
    assert graph["total_edges"] == stats["links"]["internal"]

    cut = ok(api_client, f"/api/v1/crawls/{job_id}/link-graph", max_nodes=3, max_edges=2)
    assert [path_of(node["url"]) for node in cut["nodes"]] == ["", "home", "docs"]
    assert [(edge["source"], edge["target"]) for edge in cut["edges"]] == [(2, 1), (1, 2)]
    assert (cut["truncated"], cut["max_nodes"], cut["max_edges"]) == (True, 3, 2)


# --- Statistics and the job detail ---------------------------------------------------------------


async def test_statistics_and_job_progress_count_the_stored_results(
    api_client: TestClient, database: Database, stream: IndexStream
) -> None:
    job_id, _ = await crawled_site(database, stream)

    stats = ok(api_client, f"/api/v1/crawls/{job_id}/stats")
    detail = ok(api_client, f"/api/v1/crawls/{job_id}")

    assert stats["status"] == "completed"
    assert stats["crawl"] == {
        "urls_discovered": 8,
        "urls_admitted": 7,
        "urls_by_state": {
            "pending": 0,
            "queued": 0,
            "in_progress": 0,
            "fetched": 6,
            "failed": 0,
            "skipped": 1,
            "cancelled": 0,
        },
        "pages_stored": 3,
        "current_depth": 1,
        "max_depth": 1,
        "max_pages": 50,
    }
    assert stats["http_status"] == {
        "informational_1xx": 0,
        "success_2xx": 3,
        "redirect_3xx": 2,
        "client_error_4xx": 1,
        "server_error_5xx": 0,
        "no_response": 0,
    }
    assert stats["depths"] == [
        {"depth": 0, "urls": 2, "fetched": 2, "pages": 1},
        {"depth": 1, "urls": 5, "fetched": 4, "pages": 2},
    ]
    assert stats["links"] == {
        "total": 9,
        "internal": 8,
        "external": 1,
        "internal_targets": 7,
        "broken": 2,
        "verified": 6,
        "unverified": 3,
        "by_class": {
            "ok": 4,
            "broken": 2,
            "server_error": 0,
            "http_error": 0,
            "redirect_error": 0,
            "out_of_scope": 1,
            "not_followed": 0,
            "beyond_limits": 1,
            "not_crawled": 0,
            "robots_blocked": 1,
            "unreachable": 0,
        },
    }
    assert [(path_of(top["url"]), top["in_links"]) for top in stats["most_linked"]] == [
        ("gone", 2),
        *((name, 1) for name in ("deeper", "docs", "home", "old", "secret", "team")),
    ]
    assert stats["indexing"]["documents_by_status"]["indexed"] == 2
    assert stats["indexing"] == detail["indexing"]
    assert detail["progress"]["pages_stored"] == 3
    assert (detail["progress"]["urls_total"], detail["progress"]["urls_finished"]) == (7, 7)
    assert detail["status"] == "completed"
    assert detail["finished_at"] is not None
    assert detail["indexing"]["completed_at"] is None  # the index dispatcher has not marked it


# --- Search --------------------------------------------------------------------------------------


async def test_search_returns_ranked_indexed_pages_of_one_job(
    api_client: TestClient, database: Database, stream: IndexStream
) -> None:
    job_id, pages = await crawled_site(database, stream)
    other, _ = await new_job(database)  # the same words in another job are never found
    await add_page(database, other, path="docs", body="Consumer groups let workers share a stream")
    await index_everything(database, stream)

    found = ok(api_client, "/api/v1/search", job_id=str(job_id), q="  stream ")
    both = ok(api_client, "/api/v1/search", job_id=str(job_id), q="consumer", page_size=1)
    second = ok(api_client, "/api/v1/search", job_id=str(job_id), q="consumer", page=2, page_size=1)
    beyond = ok(api_client, "/api/v1/search", job_id=str(job_id), q="consumer", page=3, page_size=1)

    assert (found["query"], found["searchable"], found["total"]) == ("stream", True, 1)
    [hit] = found["results"]
    assert (hit["page_id"], path_of(hit["url"]), hit["title"]) == (
        str(pages["docs"]),
        "docs",
        "Docs",
    )
    assert hit["score"] > 0
    assert hit["crawled_at"] is not None
    assert hit["indexed_at"] is not None
    words = [
        "".join(list(hit["snippet"])[item["start"] : item["end"]]).lower()
        for item in hit["highlights"]
    ]
    assert words == ["stream"]
    assert (both["total"], second["total"], beyond["total"]) == (2, 2, 2)
    assert {both["results"][0]["page_id"], second["results"][0]["page_id"]} == {
        str(pages["home"]),
        str(pages["docs"]),
    }
    assert (beyond["results"], beyond["page"], beyond["page_size"]) == ([], 3, 1)
    assert found["indexing"]["documents_by_status"]["indexed"] == 2


async def test_a_query_without_searchable_terms_finds_nothing(
    api_client: TestClient, database: Database, stream: IndexStream
) -> None:
    job_id, _ = await crawled_site(database, stream)

    stop_words = ok(api_client, "/api/v1/search", job_id=str(job_id), q="the and of")
    excluded = ok(api_client, "/api/v1/search", job_id=str(job_id), q="-consumer")

    for result in (stop_words, excluded):
        assert (result["searchable"], result["total"], result["results"]) == (False, 0, [])


async def test_search_rejects_invalid_queries_as_problems(
    api_client: TestClient, database: Database
) -> None:
    job_id, _ = await new_job(database)

    blank = api_client.get("/api/v1/search", params={"job_id": str(job_id), "q": " \t​ "})
    long = api_client.get("/api/v1/search", params={"job_id": str(job_id), "q": "w" * 257})
    special = api_client.get(
        "/api/v1/search", params={"job_id": str(job_id), "q": "a & b | !c <-> 'd':* (e"}
    )

    assert problem(blank, 422, "validation_failed")["errors"][0]["type"] == "empty_query"
    assert problem(long, 422, "validation_failed")["errors"][0]["type"] == "query_too_long"
    assert special.status_code == 200  # tsquery operators are plain text in web-search syntax
    assert special.json()["total"] == 0


async def test_a_search_of_a_job_without_indexed_pages_finds_nothing_yet(
    api_client: TestClient, database: Database
) -> None:
    job_id, _ = await new_job(database)
    await add_page(database, job_id, path="a", body="stream")  # not indexed yet

    result = ok(api_client, "/api/v1/search", job_id=str(job_id), q="stream")

    assert (result["total"], result["results"]) == (0, [])
    assert result["indexing"]["status"] == "pending"
    assert result["indexing"]["documents_by_status"]["pending"] == 1


# --- Errors --------------------------------------------------------------------------------------


async def test_an_unknown_job_is_a_404_problem_everywhere(
    api_client: TestClient, database: Database
) -> None:
    await new_job(database)  # another job exists
    unknown = uuid.uuid4()

    responses = [
        api_client.get(f"/api/v1/crawls/{unknown}/{name}")
        for name in ("pages", "links", "broken-links", "link-graph", "stats")
    ]
    responses.append(api_client.get("/api/v1/search", params={"job_id": str(unknown), "q": "x"}))

    for response in responses:
        body = problem(response, 404, "not_found")
        assert str(unknown) in body["detail"]


async def test_a_slow_analysis_or_search_is_a_503_problem(
    slow_client: TestClient, database: Database
) -> None:
    """Exclusive table locks (as a migration might take) make the queries wait past their time
    limits; the waits end, and the locks are released, without a 500."""
    job_id, _ = await new_job(database)
    async with database.transaction() as blocker:
        await blocker.execute(
            text("LOCK TABLE page_links, search_documents IN ACCESS EXCLUSIVE MODE")
        )
        analysis = slow_client.get(f"/api/v1/crawls/{job_id}/broken-links")
        searched = slow_client.get("/api/v1/search", params={"job_id": str(job_id), "q": "x"})

    assert "0.3 s" in problem(analysis, 503, "analysis_timeout")["detail"]
    assert "0.3 s" in problem(searched, 503, "search_timeout")["detail"]
    assert ok(slow_client, f"/api/v1/crawls/{job_id}/broken-links")["total"] == 0
