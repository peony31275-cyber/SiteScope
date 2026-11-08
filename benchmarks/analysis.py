r"""Latency and query plans of the website-analysis service on large synthetic jobs (Phase 8).

What it measures:

1. **Latency.** Every analysis use case of ``app/services/site_analysis.py`` (the page, link and
   broken-link listings with and without filters, a page deep in a listing, the link graph at
   its default and maximum limits, the statistics) runs ``--repeat`` times, one call at a time,
   on one database session, as the API calls them: each call is a read-only REPEATABLE READ
   transaction with its statement timeout. Every call's wall time is kept. Repeating a call on
   the same connection also exercises PostgreSQL's plan cache: asyncpg prepares each statement
   once per connection, and after five executions PostgreSQL may switch a prepared statement to
   a generic plan, so a plan that is only fast with literal parameters shows up as a slower
   tail.
2. **Query plans.** ``EXPLAIN (ANALYZE, BUFFERS)`` of each analysis statement, saved as text.

The dataset is one job per size in ``--pages``, generated from a seeded random generator (the same
for every run): ``P`` stored HTML pages in BFS levels of branching 10, plus non-page URLs (404s
for 5% of P, redirects to pages for 2%, 503s, read timeouts and robots.txt refusals for 1% each),
and ``--links`` distinct links per page: 80% to pages, 8% to the non-page URLs, 7% to URLs the job
never admitted, 5% external.

Isolation: a disposable database ``<SITESCOPE_POSTGRES_DB>_test_bench`` (created, migrated and
dropped by this script). No Redis, no network. The numbers describe this machine only.

Run from backend\ (PostgreSQL up, as for the tests):

    .\.venv\Scripts\python.exe ..\benchmarks\analysis.py --pages 1000 10000 `
        --output ..\benchmarks\results\phase8-analysis.json `
        --plans ..\benchmarks\results\phase8-analysis-plans.txt
"""

import argparse
import asyncio
import json
import platform
import random
import statistics
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402 - after the path setup
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.services import site_analysis as analysis  # noqa: E402
from app.services.site_analysis import LinkClass, LinkPosition, PagePosition  # noqa: E402
from tests.crawl_support import new_job  # noqa: E402
from tests.integration import helpers  # noqa: E402

HOST = "bench.test"
SEED = 11
BRANCHING = 10
BATCH = 50_000

INSERT_URLS = text(
    """
    INSERT INTO crawl_urls (job_id, normalized_url, depth, state, outcome, http_status,
                            redirect_target, result_epoch, finished_at)
    SELECT :job_id, url, depth, state, outcome, status, target, 0, clock_timestamp()
    FROM unnest(CAST(:urls AS text[]), CAST(:depths AS integer[]), CAST(:states AS text[]),
                CAST(:outcomes AS text[]), CAST(:statuses AS integer[]),
                CAST(:targets AS text[]))
         AS t(url, depth, state, outcome, status, target)
    """
)
INSERT_PAGES = text(
    """
    INSERT INTO pages (job_id, crawl_url_id, normalized_url, http_status, content_type, title,
                       meta_description, headings, extracted_text, content_hash, byte_size,
                       fetched_at)
    SELECT u.job_id, u.id, u.normalized_url, 200, 'text/html', 'Page ' || u.normalized_url,
           'A generated page', '', 'generated body', sha256(convert_to(u.normalized_url, 'UTF8')),
           100, clock_timestamp()
    FROM crawl_urls AS u
    WHERE u.job_id = :job_id AND u.outcome = 'html_stored'
    """
)
INSERT_DOCUMENTS = text(
    "INSERT INTO search_documents (page_id, job_id) SELECT id, job_id FROM pages"
    " WHERE job_id = :job_id"
)
INSERT_LINKS = text(
    """
    INSERT INTO page_links (source_page_id, target_normalized_url, job_id, anchor_text,
                            position, is_internal, nofollow)
    SELECT p.id, t.target, :job_id, 'link ' || t.position, t.position, t.internal, false
    FROM unnest(CAST(:sources AS text[]), CAST(:targets AS text[]),
                CAST(:positions AS integer[]), CAST(:internal AS boolean[]))
         AS t(source, target, position, internal)
    JOIN pages AS p ON p.job_id = :job_id AND p.normalized_url = t.source
    """
)


def url(path: str) -> str:
    return f"https://{HOST}/{path}"


def depth_of(index: int) -> int:
    """BFS levels of branching 10: page 0 at depth 0, pages 1-10 at 1, 11-110 at 2, ..."""
    depth, first, width = 0, 0, 1
    while index >= first + width:
        first += width
        width *= BRANCHING
        depth += 1
    return depth


def dataset(pages: int, links_per_page: int) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
    """Crawl URL columns and link columns of one generated job."""
    rng = random.Random(SEED)  # noqa: S311 - a reproducible dataset, not a secret
    urls: dict[str, list[Any]] = {
        key: [] for key in ("urls", "depths", "states", "outcomes", "statuses", "targets")
    }

    def add(
        path: str, depth: int, state: str, outcome: str, status: int | None, target: str | None
    ) -> None:
        urls["urls"].append(url(path))
        urls["depths"].append(depth)
        urls["states"].append(state)
        urls["outcomes"].append(outcome)
        urls["statuses"].append(status)
        urls["targets"].append(target)

    page_paths = [f"p{index}" for index in range(1, pages)]  # the seed is the job's own URL
    for index, path in enumerate(page_paths, start=1):
        add(path, depth_of(index), "fetched", "html_stored", 200, None)
    max_depth = depth_of(pages - 1)
    extras: list[str] = []
    for kind, share, state, outcome, status in (
        ("missing", 0.05, "fetched", "http_error", 404),
        ("old", 0.02, "fetched", "redirect", 301),
        ("down", 0.01, "fetched", "http_error", 503),
        ("slow", 0.01, "failed", "read_timeout", None),
        ("private", 0.01, "skipped", "robots_disallowed", None),
    ):
        for number in range(max(1, int(pages * share))):
            path = f"{kind}-{number}"
            target = url(rng.choice(page_paths)) if kind == "old" else None
            add(path, rng.randint(1, max_depth), state, outcome, status, target)
            extras.append(path)
    never = [f"never-{number}" for number in range(max(1, pages // 10))]

    links: dict[str, list[Any]] = {
        key: [] for key in ("sources", "targets", "positions", "internal")
    }
    sources = ["", *page_paths]
    for source in sources:
        targets: set[str] = set()
        while len(targets) < links_per_page:
            draw = rng.random()
            if draw < 0.80:
                targets.add(url(rng.choice(sources)))
            elif draw < 0.88:
                targets.add(url(rng.choice(extras)))
            elif draw < 0.95:
                targets.add(url(rng.choice(never)))
            else:
                targets.add(f"https://ext{rng.randrange(pages)}.example/")
        for position, target in enumerate(sorted(targets, key=lambda _: rng.random())):
            links["sources"].append(url(source))
            links["targets"].append(target)
            links["positions"].append(position)
            links["internal"].append(target.startswith(f"https://{HOST}/"))
    return urls, links


async def store(database: Database, pages: int, links_per_page: int) -> uuid.UUID:
    job_id, _ = await new_job(database, max_depth=10, max_pages=100_000, host=HOST)
    urls, links = dataset(pages, links_per_page)
    async with database.transaction() as session:
        await session.execute(
            text(
                "UPDATE crawl_urls SET state = 'fetched', outcome = 'html_stored',"
                " http_status = 200, result_epoch = 0, finished_at = clock_timestamp()"
                " WHERE job_id = :job_id"
            ),
            {"job_id": job_id},
        )
        await session.execute(INSERT_URLS, {"job_id": job_id, **urls})
        await session.execute(INSERT_PAGES, {"job_id": job_id})
        await session.execute(INSERT_DOCUMENTS, {"job_id": job_id})
        await session.execute(
            text(
                "UPDATE crawl_jobs SET status = 'completed', finished_at = clock_timestamp(),"
                " admitted_count = (SELECT count(*) FROM crawl_urls WHERE job_id = :job_id),"
                " current_level = :level WHERE id = :job_id"
            ),
            {"job_id": job_id, "level": max(urls["depths"])},
        )
    total = len(links["sources"])
    for start in range(0, total, BATCH):
        chunk = {key: values[start : start + BATCH] for key, values in links.items()}
        async with database.transaction() as session:
            await session.execute(INSERT_LINKS, {"job_id": job_id, **chunk})
    async with database.engine.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text("VACUUM (ANALYZE)"))
    return job_id


async def counts(database: Database, job_id: uuid.UUID) -> dict[str, int]:
    async with database.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM crawl_urls WHERE job_id = :id) AS urls,"
                    " (SELECT count(*) FROM pages WHERE job_id = :id) AS pages,"
                    " (SELECT count(*) FROM page_links WHERE job_id = :id) AS links"
                ),
                {"id": job_id},
            )
        ).one()
    return {"crawl_urls": row.urls, "pages": row.pages, "page_links": row.links}


Call = Callable[[AsyncSession], Awaitable[Any]]


async def cases(database: Database, job_id: uuid.UUID) -> dict[str, Call]:
    async with database.session() as session:
        # The first page in BFS order (as list_pages orders them), read directly: the cases must
        # not depend on the plans they measure (Phase 10 measures them with stale statistics).
        source = (
            await session.execute(
                text(
                    "SELECT p.id FROM pages AS p JOIN crawl_urls AS u ON u.id = p.crawl_url_id"
                    " WHERE p.job_id = :id ORDER BY u.depth, p.normalized_url LIMIT 1"
                ),
                {"id": job_id},
            )
        ).scalar_one()
        deep_url = (
            await session.execute(
                text(
                    "SELECT p.normalized_url FROM pages AS p"
                    " JOIN crawl_urls AS u ON u.id = p.crawl_url_id"
                    " WHERE p.job_id = :id ORDER BY u.depth DESC, p.normalized_url DESC LIMIT 1"
                    " OFFSET 100"
                ),
                {"id": job_id},
            )
        ).scalar_one()
        deep_depth = (
            await session.execute(
                text("SELECT depth FROM crawl_urls WHERE job_id = :id AND normalized_url = :url"),
                {"id": job_id, "url": deep_url},
            )
        ).scalar_one()
        # About nine tenths through the links listing (ordered by the linking page's URL).
        deep_source = (
            await session.execute(
                text(
                    "SELECT normalized_url FROM pages WHERE job_id = :id"
                    " ORDER BY normalized_url DESC LIMIT 1"
                    " OFFSET (SELECT count(*) / 10 FROM pages WHERE job_id = :id)"
                ),
                {"id": job_id},
            )
        ).scalar_one()
    deep = PagePosition(deep_depth, deep_url)
    deep_link = LinkPosition(deep_source, 0, "")
    return {
        "pages: first 50": lambda s: analysis.list_pages(s, job_id, limit=50),
        "pages: 50 after a deep cursor": lambda s: analysis.list_pages(
            s, job_id, limit=50, after=deep
        ),
        "links: first 50": lambda s: analysis.list_links(s, job_id, limit=50),
        "links: 50 after a deep cursor": lambda s: analysis.list_links(
            s, job_id, limit=50, after=deep_link
        ),
        "links: one source page": lambda s: analysis.list_links(
            s, job_id, limit=200, source_page_id=source
        ),
        "links: class broken, first 50": lambda s: analysis.list_links(
            s, job_id, limit=50, classification=LinkClass.BROKEN
        ),
        "broken links: first 50": lambda s: analysis.list_broken_links(s, job_id, limit=50),
        "link graph: 500 nodes, 2000 edges": lambda s: analysis.link_graph(
            s, job_id, max_nodes=500, max_edges=2000
        ),
        "link graph: 2000 nodes, 10000 edges": lambda s: analysis.link_graph(
            s, job_id, max_nodes=2000, max_edges=10_000
        ),
        "statistics": lambda s: analysis.crawl_stats(s, job_id),
    }


async def measure(database: Database, job_id: uuid.UUID, repeat: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, call in (await cases(database, job_id)).items():
        timings: list[float] = []
        async with database.session() as session:  # one connection: the plan cache is reused
            for _ in range(repeat):
                began = time.perf_counter()
                await call(session)
                timings.append((time.perf_counter() - began) * 1000)
        ordered = sorted(timings)
        results[name] = {
            "ms": [round(value, 2) for value in timings],
            "min": round(ordered[0], 2),
            "p50": round(statistics.median(ordered), 2),
            "p95": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 2),
            "max": round(ordered[-1], 2),
        }
        print(  # noqa: T201 - the benchmark's output
            json.dumps({"case": name, **{k: v for k, v in results[name].items() if k != "ms"}}),
            flush=True,
        )
    return results


async def plans(database: Database, job_id: uuid.UUID) -> str:
    """EXPLAIN (ANALYZE, BUFFERS) of every analysis statement, with the API's default limits."""
    async with database.session() as session:
        source = (
            await session.execute(
                text("SELECT id FROM pages WHERE job_id = :id ORDER BY normalized_url LIMIT 1"),
                {"id": job_id},
            )
        ).scalar_one()
        nodes = (
            await session.execute(analysis._GRAPH_NODES, {"job_id": job_id, "max_nodes": 500})
        ).all()
    edges = {
        "urls": [row.url for row in nodes],
        "page_ids": [row.page_id for row in nodes if row.page_id is not None],
        "max_edges": 2000,
    }
    link_filters = {"source_page_id": None, "internal": None}
    no_cursor = {"after_url": None, "after_position": None, "after_target": None}
    statements: list[tuple[str, Any, dict[str, Any]]] = [
        (
            "pages: first 50",
            analysis._PAGES,
            {
                "depth": None,
                "index_status": None,
                "after_depth": None,
                "after_url": None,
                "limit": 51,
            },
        ),
        ("links: first 50", analysis._LINKS_PAGE, {**link_filters, **no_cursor, "limit": 51}),
        (
            "links: one source page",
            analysis._LINKS_PAGE,
            {**link_filters, "source_page_id": source, **no_cursor, "limit": 201},
        ),
        (
            "links: class broken, first 50",
            analysis._LINKS_FILTERED,
            {
                **link_filters,
                **no_cursor,
                "classification": "broken",
                "verified": None,
                "redirected": None,
                "limit": 51,
            },
        ),
        (
            "broken links: first 50",
            analysis._BROKEN_LINKS,
            {"after_target": None, "after_source": None, "limit": 51},
        ),
        ("statistics: link classes", analysis._LINK_CLASS_COUNTS, {}),
        ("statistics: link totals", analysis._LINK_TOTALS, {}),
        ("statistics: most linked", analysis._TOP_LINKED, {"limit": 10}),
        ("statistics: depths", analysis._DEPTHS, {}),
        ("link graph: nodes (500)", analysis._GRAPH_NODES, {"max_nodes": 500}),
        ("link graph: edges (2000)", analysis._GRAPH_EDGES, edges),
        ("link graph: edge total", analysis._GRAPH_EDGE_TOTAL, {}),
    ]
    sections: list[str] = []
    for name, statement, params in statements:
        async with database.session() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            rows = await session.execute(
                text("EXPLAIN (ANALYZE, BUFFERS) " + statement.text), {"job_id": job_id, **params}
            )
            sections.append(f"=== {name}\n" + "\n".join(row[0] for row in rows))
    return "\n\n".join(sections) + "\n"


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pages", type=int, nargs="+", default=[1000, 10_000])
    parser.add_argument("--links", type=int, default=50, help="distinct links per page")
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plans", type=Path)
    args = parser.parse_args()

    base = Settings()
    settings = base.model_copy(update={"postgres_db": f"{base.postgres_db}_test_bench"})
    helpers.require_disposable(settings.postgres_db)
    await helpers.recreate_database(settings)
    await helpers.upgrade(settings, "head")
    database = Database(settings, application_name="sitescope-benchmark-analysis")
    report: dict[str, Any] = {
        "command": " ".join(sys.argv),
        "environment": environment(),
        "links_per_page": args.links,
        "repeat": args.repeat,
        "runs": [],
    }
    plan_text: list[str] = []
    try:
        async with database.engine.connect() as connection:
            version = await connection.execute(text("SHOW server_version"))
            report["environment"]["postgresql"] = version.scalar_one()
        for pages in args.pages:
            began = time.perf_counter()
            job_id = await store(database, pages, args.links)
            stored = await counts(database, job_id)
            print(  # noqa: T201 - the benchmark's output
                json.dumps(
                    {
                        "pages": pages,
                        **stored,
                        "load_seconds": round(time.perf_counter() - began, 1),
                    }
                ),
                flush=True,
            )
            report["runs"].append(
                {
                    "pages": pages,
                    "rows": stored,
                    "cases": await measure(database, job_id, args.repeat),
                }
            )
            plan_text.append(f"##### {pages} pages: {stored}\n\n" + await plans(database, job_id))
    finally:
        await database.dispose()
        await helpers.drop_database(settings)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    if args.plans:
        args.plans.write_text("\n".join(plan_text), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    asyncio.run(main())
