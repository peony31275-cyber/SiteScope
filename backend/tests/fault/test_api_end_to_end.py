"""Phase 8 end to end with real processes: the demo site crawled, indexed, searched and
analyzed through the HTTP API (marker ``fault``, like every process test).

The job is created with ``POST /api/v1/crawls``. The scheduler, two crawler workers, the index
dispatcher and two indexers run as separate processes (``tests/process_support.py``) against the
disposable test database and Redis database 15 under this test's key prefix; the demo site
(``demo-site/``) is served on 127.0.0.1 by ``LocalHttpServer``. Once its indexing is complete,
every Phase 8 endpoint is called and checked against the demo site's known structure
(demo-site/README.md):

* 14 URLs admitted, 12 pages stored (``/team.html`` is noindex: 11 indexed), one 404
  (``/about.html`` links to ``/missing.html``), one URL disallowed by robots.txt
  (``/private/notes.html``), one link never followed (``/docs/faq.html`` is nofollow, and is
  the only page linking to ``/docs/legacy.html``), two external links;
* 26 stored links, 24 of them internal, to 15 distinct internal targets.
"""

import uuid
from collections import Counter
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.session import Database
from app.main import create_app
from tests.crawl_support import assert_job_invariants, job_row
from tests.fetch_support import LocalHttpServer
from tests.integration.test_local_crawler import DEMO_EXPECTED, demo_routes
from tests.process_support import wait_for
from tests.service_fixtures import ClusterFactory

pytestmark = pytest.mark.anyio

HOST = "127.0.0.1"
PAGES = sorted(
    (depth, path)
    for path, (depth, state, outcome) in DEMO_EXPECTED.items()
    if outcome == "html_stored"
)


def ok(client: TestClient, path: str, **params: Any) -> Any:
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def every_item(client: TestClient, path: str, limit: int, **params: Any) -> list[dict[str, Any]]:
    """Every item of a cursor-paged listing."""
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = ok(client, path, limit=limit, **params, **({"cursor": cursor} if cursor else {}))
        items += page["items"]
        if (cursor := page["next_cursor"]) is None:
            return items


async def test_the_demo_site_through_the_api_with_real_processes(
    database: Database, test_database: Settings, clusters: ClusterFactory, redis_prefix: str
) -> None:
    async with LocalHttpServer(demo_routes()) as server:
        origin = f"http://{HOST}:{server.port}"
        api_settings = test_database.model_copy(
            update={
                "crawl_allowed_hosts": (HOST,),
                "crawl_allowed_schemes": ("http",),
                "crawl_allowed_ports": (server.port,),
                "redis_key_prefix": redis_prefix,
            }
        )
        with TestClient(create_app(api_settings)) as api:
            created = api.post(
                "/api/v1/crawls", json={"seed_url": f"{origin}/", "max_depth": 3, "max_pages": 100}
            )
            assert created.status_code == 201, created.text
            job_id = created.json()["id"]
            cluster = clusters(hosts=[HOST], ports=[server.port])
            processes = [
                cluster.scheduler(),
                cluster.worker(),
                cluster.worker(),
                cluster.index_dispatcher(),
                cluster.indexer(),
                cluster.indexer(),
            ]
            await cluster.started(*processes)

            async def indexing_completed() -> bool:
                job = await job_row(database, uuid.UUID(job_id))
                return job.indexing_completed_at is not None

            await wait_for(
                indexing_completed,
                within=180,
                what="the demo site to be crawled and indexed",
                cluster=cluster,
            )
            exit_codes = await cluster.stop_all()
            served = server.requests  # every request the processes sent

            def rel(url: str) -> str:
                return url.removeprefix(origin)

            base = f"/api/v1/crawls/{job_id}"
            detail = ok(api, base)
            stats = ok(api, f"{base}/stats")
            pages = every_item(api, f"{base}/pages", 5)
            links = every_item(api, f"{base}/links", 10)
            broken = ok(api, f"{base}/broken-links")
            graph = ok(api, f"{base}/link-graph")
            classified = {
                name: ok(api, f"{base}/links", classification=name)["items"]
                for name in ("broken", "robots_blocked", "not_followed")
            }
            external = ok(api, f"{base}/links", internal="false")["items"]
            search = {
                query: ok(api, "/api/v1/search", job_id=job_id, q=query)
                for query in ("fencing", "frontier", "noindex", "politely")
            }

    assert set(exit_codes.values()) == {0}
    await assert_job_invariants(database, uuid.UUID(job_id))
    requested = {request.target for request in served}
    assert "/private/notes.html" not in requested  # disallowed by robots.txt
    assert "/docs/legacy.html" not in requested  # linked only from a nofollow page
    assert "/missing.html" in requested

    # The job detail: the crawl and its indexing, completed separately.
    assert (detail["status"], detail["progress"]["urls_total"]) == ("completed", 14)
    assert detail["progress"]["pages_stored"] == 12
    assert detail["indexing"]["status"] == "indexed"
    assert detail["indexing"]["completed_at"] >= detail["finished_at"]

    # Statistics.
    assert stats["crawl"] == {
        "urls_discovered": 15,  # plus /docs/legacy.html, linked only from a nofollow page
        "urls_admitted": 14,
        "urls_by_state": {
            "pending": 0,
            "queued": 0,
            "in_progress": 0,
            "fetched": 13,
            "failed": 0,
            "skipped": 1,
            "cancelled": 0,
        },
        "pages_stored": 12,
        "current_depth": 3,
        "max_depth": 3,
        "max_pages": 100,
    }
    assert stats["http_status"] == {
        "informational_1xx": 0,
        "success_2xx": 12,
        "redirect_3xx": 0,
        "client_error_4xx": 1,
        "server_error_5xx": 0,
        "no_response": 0,
    }
    assert stats["depths"] == [
        {"depth": 0, "urls": 1, "fetched": 1, "pages": 1},
        {"depth": 1, "urls": 5, "fetched": 4, "pages": 4},
        {"depth": 2, "urls": 7, "fetched": 7, "pages": 6},
        {"depth": 3, "urls": 1, "fetched": 1, "pages": 1},
    ]
    assert stats["indexing"]["documents_by_status"] == {
        "pending": 0,
        "queued": 0,
        "indexed": 11,
        "failed": 0,
    }
    assert stats["links"] == {
        "total": 26,
        "internal": 24,
        "external": 2,
        "internal_targets": 15,
        "broken": 1,
        "verified": 22,
        "unverified": 4,
        "by_class": {
            "ok": 21,
            "broken": 1,
            "server_error": 0,
            "http_error": 0,
            "redirect_error": 0,
            "out_of_scope": 2,
            "not_followed": 1,
            "beyond_limits": 0,
            "not_crawled": 0,
            "robots_blocked": 1,
            "unreachable": 0,
        },
    }
    assert [(rel(top["url"]), top["in_links"]) for top in stats["most_linked"]] == [
        ("/", 3),
        ("/docs/architecture.html", 3),
        ("/about.html", 2),
        ("/blog/", 2),
        ("/blog/crawling-politely.html", 2),
        ("/blog/search-basics.html", 2),
        ("/docs/", 2),
        ("/docs/faq.html", 1),
        ("/docs/getting-started.html", 1),
        ("/docs/glossary.html", 1),
    ]

    # Pages, in BFS order; links, each classified once; the listings agree with the counts.
    assert [(page["depth"], rel(page["url"])) for page in pages] == PAGES
    assert {rel(page["url"]): page["index_status"] for page in pages if page["noindex"]} == {
        "/team.html": "noindex"
    }
    assert {page["index_status"] for page in pages if not page["noindex"]} == {"indexed"}
    assert len(links) == 26
    assert len({(link["source_page_id"], link["target_url"]) for link in links}) == 26
    assert Counter(link["classification"] for link in links) == {
        name: count for name, count in stats["links"]["by_class"].items() if count
    }
    assert [
        (rel(link["source_url"]), rel(link["target_url"]), link["anchor_text"])
        for link in classified["broken"]
    ] == [("/about.html", "/missing.html", "2024 roadmap")]
    assert [
        (rel(link["source_url"]), rel(link["target_url"])) for link in classified["robots_blocked"]
    ] == [("/", "/private/notes.html")]
    assert [
        (rel(link["source_url"]), rel(link["target_url"])) for link in classified["not_followed"]
    ] == [("/docs/faq.html", "/docs/legacy.html")]
    assert {(link["target_url"], link["nofollow"]) for link in external} == {
        ("https://example.org/", False),
        ("https://www.rfc-editor.org/rfc/rfc9309", True),
    }

    # Broken links: the one confirmed 404, never an unvisited URL.
    assert broken["total"] == 1
    [item] = broken["items"]
    assert (rel(item["source_url"]), rel(item["target_url"]), item["status"]) == (
        "/about.html",
        "/missing.html",
        404,
    )

    # The link graph: every discovered URL, the never-admitted one last.
    assert (graph["total_nodes"], graph["total_edges"], graph["truncated"]) == (15, 24, False)
    assert graph["total_nodes"] == stats["crawl"]["urls_discovered"]
    assert len(graph["edges"]) == stats["links"]["internal"]
    kinds = {rel(node["url"]): node["kind"] for node in graph["nodes"]}
    assert Counter(kinds.values()) == {"page": 12, "crawl_url": 2, "link_target": 1}
    assert (kinds["/missing.html"], kinds["/private/notes.html"]) == ("crawl_url", "crawl_url")
    assert rel(graph["nodes"][-1]["url"]) == "/docs/legacy.html"
    assert kinds["/docs/legacy.html"] == "link_target"
    home = graph["nodes"][0]
    assert (rel(home["url"]), home["depth"], home["in_degree"], home["out_degree"]) == (
        "/",
        0,
        3,
        5,
    )
    assert [node["depth"] for node in graph["nodes"][:-1]] == sorted(
        node["depth"] for node in graph["nodes"][:-1]
    )

    # Search: indexed pages of this job only; the noindex page never.
    def found(query: str) -> list[str]:
        return [rel(result["url"]) for result in search[query]["results"]]

    assert found("fencing") == ["/docs/architecture.html"]
    assert found("frontier") == ["/docs/glossary.html"]
    assert (found("noindex"), search["noindex"]["total"]) == ([], 0)
    # "politely", "polite" and "politeness" share the English stem "polit". Of the ten demo
    # files that use one of them, /private/notes.html (robots.txt) and /docs/legacy.html
    # (nofollow) were never crawled, so they are not indexed, and never found.
    assert search["politely"]["total"] == 8
    assert set(found("politely")) == {
        "/",
        "/about.html",
        "/blog/",
        "/blog/crawling-politely.html",
        "/docs/",
        "/docs/architecture.html",
        "/docs/faq.html",
        "/docs/glossary.html",
    }
    # The title weighs most: the two pages with the stem in their title come first
    # ("Crawling politely", "Architecture of a polite crawler"; ts_rank counts every weighted
    # occurrence, and the architecture page also has it in a heading and its body).
    assert set(found("politely")[:2]) == {"/blog/crawling-politely.html", "/docs/architecture.html"}
    assert search["fencing"]["indexing"]["status"] == "indexed"
