"""End-to-end crawls with the single-process driver (app/workers/local_crawler.py).

Sites are served on 127.0.0.1 by ``LocalHttpServer`` (or, for the CLI test, by the standard
library's static file server); DNS answers for ``site.test`` come from ``ScriptedResolver``, a
test double. Jobs live in the disposable test database; politeness slots and robots.txt results
live in Redis database 15 under a key prefix unique to each test. Nothing contacts the internet.

Every scenario states its exact expected result: each URL with its depth, state and outcome.
After every crawl, ``assert_invariants`` checks the correctness invariants of
docs/architecture.md, section 11.
"""

import asyncio
import functools
import html
import http.server
import itertools
import json
import logging
import threading
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.redis import create_redis_client
from app.crawler.html_parser import ParseLimits
from app.crawler.politeness import PolitenessConfig, PolitenessGate, RedisSlotStore
from app.crawler.robots import RobotsPolicy
from app.db.models import (
    UNFINISHED_URL_STATES,
    WORKER_OUTCOME_STATES,
    CrawlEvent,
    CrawlJob,
    CrawlUrl,
    Page,
    PageLink,
    SearchDocument,
    UrlState,
)
from app.db.session import Database
from app.services.crawl_cancellation import cancel_crawl_job
from app.services.crawl_jobs import CrawlJobProgress, create_crawl_job, target_policy
from app.services.crawl_state import (
    RecordStatus,
    UrlResult,
    activate_job,
    claim_url,
    dispatch_urls,
    record_result,
)
from app.workers.crawl_task import CrawlContext
from app.workers.local_crawler import DriverConfig, LocalCrawler, main
from app.workers.retry import RetryPolicy
from tests.fetch_support import (
    LocalHttpServer,
    ReceivedRequest,
    RecordingBackend,
    Reply,
    ScriptedResolver,
    make_fetcher,
)
from tests.integration import helpers

pytestmark = pytest.mark.anyio

HOST = "site.test"
LOOPBACK = ["127.0.0.1"]
# Retries of transient failures (Phase 6), quick enough for tests.
FAST_RETRIES = RetryPolicy(max_attempts=3, base_delay=0.05, max_delay=0.2)
DEMO_SITE = helpers.BACKEND_DIR.parent / "demo-site"

Row = tuple[int, str, str | None]  # depth, state, outcome


@dataclass(frozen=True)
class P:
    """A fixture page: its links, and optional robots directives."""

    links: tuple[str, ...] = ()
    nofollow_links: tuple[str, ...] = ()  # rendered with rel="nofollow"
    meta_robots: str | None = None
    headers: tuple[tuple[str, str], ...] = ()


def render(page: P) -> Reply:
    meta = f'<meta name="robots" content="{page.meta_robots}">' if page.meta_robots else ""
    anchors = "".join(f'<a href="{html.escape(href)}">link</a>' for href in page.links)
    anchors += "".join(
        f'<a href="{html.escape(href)}" rel="nofollow">nf</a>' for href in page.nofollow_links
    )
    body = f"<!doctype html><title>Fixture</title>{meta}<p>Fixture page.</p>{anchors}"
    return Reply(
        headers=[("Content-Type", "text/html; charset=utf-8"), *page.headers],
        body=body.encode(),
    )


def moved(location: str, status: int = 301) -> Reply:
    return Reply(status=status, reason="Redirect", headers=[("Location", location)])


def routes(pages: dict[str, P | Reply], robots: bytes | Reply | None = None) -> dict[str, Reply]:
    built = {path: render(page) if isinstance(page, P) else page for path, page in pages.items()}
    if robots is not None:
        built["/robots.txt"] = (
            robots
            if isinstance(robots, Reply)
            else Reply(headers=[("Content-Type", "text/plain")], body=robots)
        )
    return built


@dataclass
class Run:
    job_id: uuid.UUID
    progress: CrawlJobProgress
    rows: dict[str, Row]  # path and query -> (depth, state, outcome)
    requests: list[ReceivedRequest]
    connections: list[tuple[str, int]]
    port: int

    def requested(self, path: str) -> int:
        return sum(1 for request in self.requests if request.target == path)


@dataclass
class Crawler:
    """Runs crawls against local sites with the real driver, fetcher, gate and robots policy."""

    database: Database
    redis: Redis
    prefix: str
    settings: Settings
    runs: list[Run] = field(default_factory=list)

    async def crawl(
        self,
        site: dict[str, Reply],
        *,
        max_depth: int = 3,
        max_pages: int = 100,
        concurrency: int = 4,
        interval: float = 0.0,
        lease_seconds: float = 30.0,
        before_run: Callable[[uuid.UUID, int], Awaitable[None]] | None = None,
    ) -> Run:
        async with LocalHttpServer(site) as server:
            settings = self.settings.model_copy(
                update={
                    "crawl_allowed_hosts": (HOST,),
                    "crawl_allowed_schemes": ("http",),
                    "crawl_allowed_ports": (server.port,),
                }
            )
            async with self.database.session() as session:
                job = await create_crawl_job(
                    session,
                    settings,
                    seed_url=f"http://{HOST}:{server.port}/",
                    max_depth=max_depth,
                    max_pages=max_pages,
                )
            backend = RecordingBackend()
            gate = PolitenessGate(
                RedisSlotStore(self.redis, key_prefix=self.prefix),
                PolitenessConfig(interval=interval, max_wait=20.0, slot_ttl=30.0),
            )
            fetcher = make_fetcher(
                resolver=ScriptedResolver({HOST: [LOOPBACK]}),
                port=server.port,
                hosts=(HOST,),
                internal=(HOST,),
                schemes=("http",),
                backend=backend,
                gate=gate,
            )
            context = CrawlContext(
                fetcher=fetcher,
                robots=RobotsPolicy(
                    fetcher=fetcher,
                    redis=self.redis,
                    crawler_name="SiteScopeBot",
                    key_prefix=self.prefix,
                    delay_sink=gate,
                ),
                targets=target_policy(settings),
                parse_limits=ParseLimits(),
                crawler_name="SiteScopeBot",
            )
            driver = LocalCrawler(
                self.database,
                context,
                DriverConfig(
                    concurrency=concurrency,
                    lease_seconds=lease_seconds,
                    task_deadline=lease_seconds / 2,
                    idle_poll_seconds=0.05,
                    retry=FAST_RETRIES,
                ),
            )
            if before_run is not None:
                await before_run(job.id, server.port)
            progress = await driver.run(job.id)
        run = Run(
            job_id=job.id,
            progress=progress,
            rows=await snapshot(self.database, job.id, server.port),
            requests=list(server.requests),
            connections=list(backend.targets),
            port=server.port,
        )
        await assert_invariants(self.database, job.id)
        assert set(run.connections) == {("127.0.0.1", server.port)}  # nowhere else, ever
        self.runs.append(run)
        return run


@pytest.fixture
def crawler(
    database: Database, redis_client: Redis, redis_prefix: str, crawl_settings: Settings
) -> Crawler:
    return Crawler(database, redis_client, redis_prefix, crawl_settings)


async def snapshot(database: Database, job_id: uuid.UUID, port: int) -> dict[str, Row]:
    prefix = f"http://{HOST}:{port}"
    async with database.session() as session:
        rows = await session.execute(
            select(CrawlUrl.normalized_url, CrawlUrl.depth, CrawlUrl.state, CrawlUrl.outcome).where(
                CrawlUrl.job_id == job_id
            )
        )
        return {
            url.removeprefix(prefix): (depth, state, outcome) for url, depth, state, outcome in rows
        }


async def assert_invariants(database: Database, job_id: uuid.UUID) -> None:
    """The correctness invariants of docs/architecture.md, section 11, for one job."""
    async with database.session() as session:
        job = await session.get(CrawlJob, job_id)
        assert job is not None
        urls = list(await session.scalars(select(CrawlUrl).where(CrawlUrl.job_id == job_id)))
        pages = list(await session.scalars(select(Page).where(Page.job_id == job_id)))
        documents = set(
            await session.scalars(
                select(SearchDocument.page_id).where(SearchDocument.job_id == job_id)
            )
        )
    by_id = {url.id: url for url in urls}
    # 1. One row per (job, normalized URL).
    assert len({url.normalized_url for url in urls}) == len(urls)
    # 2. The budget: admitted_count counts the rows, and never exceeds max_pages.
    assert job.admitted_count == len(urls) <= job.max_pages
    # 3. Depths: a URL descends from a stored page one level up, or from a redirect at its
    #    own level (the shortest-path property itself is checked by the expected results).
    for url in urls:
        assert url.depth <= job.max_depth
        if url.discovered_from_url_id is None:
            assert (url.normalized_url, url.depth, url.redirect_hops) == (job.seed_url, 0, 0)
            continue
        parent = by_id[url.discovered_from_url_id]
        if url.redirect_hops:
            assert (parent.outcome, parent.redirect_target) == ("redirect", url.normalized_url)
            assert (url.depth, url.redirect_hops) == (parent.depth, parent.redirect_hops + 1)
        else:
            assert parent.outcome == "html_stored"
            assert url.depth == parent.depth + 1
    # 4. One result per URL, under its lease; one page per stored HTML URL.
    for url in urls:
        if url.state in WORKER_OUTCOME_STATES:
            assert url.result_epoch == url.lease_epoch
            assert url.lease_epoch >= 1
    assert sorted(page.crawl_url_id for page in pages) == sorted(
        url.id for url in urls if url.outcome == "html_stored"
    )
    # 5. A finished job has no unfinished URL.
    if job.status in ("completed", "failed"):
        assert not [url for url in urls if url.state in UNFINISHED_URL_STATES]
    # 6. One search document per indexable page; none for noindex pages.
    assert documents == {page.id for page in pages if not page.robots_noindex}


def stored(depth: int) -> Row:
    return (depth, "fetched", "html_stored")


# --- A graph with cycles, a diamond, a shortcut, robots rules and a redirect ---------------------

GRAPH: dict[str, P | Reply] = {
    "/": P(
        links=(
            "/a",
            "/b",
            "/private/secret",
            "/private/open",
            "http://other.test/x",
            "/a",
            "mailto:x@example.org",
        )
    ),
    "/a": P(links=("/c", "/")),  # a cycle back to the seed
    "/b": P(links=("/c", "/d", "/old")),  # /c is shared with /a: a diamond
    "/c": P(links=("/e",)),
    "/d": P(links=("/e", "/b")),
    "/old": moved("/new"),  # a redirect: /new is admitted at the same depth
    "/new": P(links=("/f",)),
    "/e": P(links=("/g",)),  # /g would be depth 4 > max_depth
    "/f": P(links=("/",)),
    "/g": P(),
    "/private/secret": P(),
    "/private/open": P(links=("/e",)),  # a shortcut: /e at depth 2, not 3
}
GRAPH_ROBOTS = b"User-agent: *\nDisallow: /private/\nAllow: /private/open\n"
GRAPH_EXPECTED: dict[str, Row] = {
    "/": stored(0),
    "/a": stored(1),
    "/b": stored(1),
    "/private/secret": (1, "skipped", "robots_disallowed"),
    "/private/open": stored(1),
    "/c": stored(2),
    "/d": stored(2),
    "/old": (2, "fetched", "redirect"),
    "/new": stored(2),
    "/e": stored(2),  # through the shortcut
    "/f": stored(3),
    "/g": stored(3),
}


async def test_a_graph_is_crawled_breadth_first_with_exact_depths(crawler: Crawler) -> None:
    run = await crawler.crawl(routes(GRAPH, GRAPH_ROBOTS), max_depth=3)
    assert run.rows == GRAPH_EXPECTED
    assert run.progress.job.status == "completed"
    assert (run.progress.job.current_level, run.progress.job.admitted_count) == (3, 12)
    assert run.requested("/robots.txt") == 1  # cached for the rest of the crawl
    for path, (_, state, _) in GRAPH_EXPECTED.items():
        assert run.requested(path) == (0 if state == "skipped" else 1), path
    assert run.requested("/private/secret") == 0


async def test_one_worker_and_many_workers_produce_the_same_crawl(crawler: Crawler) -> None:
    single = await crawler.crawl(routes(GRAPH, GRAPH_ROBOTS), concurrency=1)
    many = await crawler.crawl(routes(GRAPH, GRAPH_ROBOTS), concurrency=6)
    assert single.rows == many.rows == GRAPH_EXPECTED


async def test_links_are_recorded_with_their_scope(crawler: Crawler) -> None:
    run = await crawler.crawl(routes(GRAPH, GRAPH_ROBOTS), max_depth=1)
    async with crawler.database.session() as session:
        links = (
            await session.execute(
                select(PageLink.target_normalized_url, PageLink.position, PageLink.is_internal)
                .join(Page, Page.id == PageLink.source_page_id)
                .where(Page.normalized_url == f"http://{HOST}:{run.port}/")
                .order_by(PageLink.position)
            )
        ).all()
    base = f"http://{HOST}:{run.port}"
    assert [tuple(link) for link in links] == [
        (f"{base}/a", 0, True),
        (f"{base}/b", 1, True),
        (f"{base}/private/secret", 2, True),
        (f"{base}/private/open", 3, True),
        ("http://other.test/x", 4, False),  # recorded, never fetched
    ]
    # Pages at max_depth (1) are fetched; their links are recorded but not admitted.
    assert set(run.rows) == {"/", "/a", "/b", "/private/secret", "/private/open"}


# --- Robots directives and failures --------------------------------------------------------------

DIRECTIVES: dict[str, P | Reply] = {
    "/": P(
        links=(
            "/noindex",
            "/nofollow",
            "/header-noindex",
            "/header-other-bot",
            "/missing",
            "/error",
            "/text",
            "/reset",
        ),
        nofollow_links=("/rel-nofollow",),
    ),
    "/noindex": P(meta_robots="noindex", links=("/from-noindex",)),
    "/nofollow": P(meta_robots="nofollow", links=("/from-nofollow",)),
    "/header-noindex": P(headers=(("X-Robots-Tag", "noindex"),), links=("/from-header",)),
    "/header-other-bot": P(
        headers=(("X-Robots-Tag", "otherbot: noindex, nofollow"),), links=("/from-other-bot",)
    ),
    "/error": Reply(status=500, reason="Server Error", body=b"oops"),
    "/text": Reply(headers=[("Content-Type", "text/plain")], body=b"plain"),
    "/reset": Reply(raw=b""),  # the connection closes without a response
    "/from-noindex": P(),
    "/from-nofollow": P(),
    "/from-header": P(),
    "/from-other-bot": P(),
    "/rel-nofollow": P(),
}
DIRECTIVES_EXPECTED: dict[str, Row] = {
    "/": stored(0),
    "/noindex": stored(1),
    "/nofollow": stored(1),
    "/header-noindex": stored(1),
    "/header-other-bot": stored(1),
    "/missing": (1, "fetched", "http_error"),  # 404: a response was received
    "/error": (1, "fetched", "http_error"),
    "/text": (1, "fetched", "non_html"),
    "/reset": (1, "failed", "connection_error"),
    "/from-noindex": stored(2),  # noindex does not stop links from being followed
    "/from-header": stored(2),
    "/from-other-bot": stored(2),  # another bot's directives do not apply
}


async def test_robots_directives_and_failures(crawler: Crawler) -> None:
    run = await crawler.crawl(routes(DIRECTIVES), max_depth=2)  # no robots.txt: 404
    assert run.rows == DIRECTIVES_EXPECTED
    async with crawler.database.session() as session:
        flags = dict(
            (
                await session.execute(
                    select(Page.normalized_url, Page.robots_noindex).where(
                        Page.job_id == run.job_id
                    )
                )
            ).all()
        )
        statuses = dict(
            (
                await session.execute(
                    select(CrawlUrl.normalized_url, CrawlUrl.http_status).where(
                        CrawlUrl.job_id == run.job_id
                    )
                )
            ).all()
        )
    base = f"http://{HOST}:{run.port}"
    assert {url.removeprefix(base) for url, noindex in flags.items() if noindex} == {
        "/noindex",
        "/header-noindex",
    }
    assert (statuses[f"{base}/missing"], statuses[f"{base}/error"]) == (404, 500)
    assert statuses[f"{base}/reset"] is None
    # Phase 6: transient failures were tried three times (the attempts), permanent ones once.
    requested = Counter(request.target for request in run.requests)
    assert (requested["/error"], requested["/reset"], requested["/missing"]) == (3, 3, 1)
    assert (requested["/text"], requested["/noindex"]) == (1, 1)


# --- The page budget -----------------------------------------------------------------------------

BUDGET: dict[str, P | Reply] = {
    "/": P(links=("/a", "/b")),
    "/a": P(links=("/a3", "/a1", "/a2")),
    "/b": P(links=("/b1", "/a1")),
    "/a1": P(),
    "/a2": P(),
    "/a3": P(),
    "/b1": P(),
}


@pytest.mark.parametrize("concurrency", [1, 4])
async def test_the_budget_admits_the_same_bfs_prefix_with_any_concurrency(
    crawler: Crawler, concurrency: int
) -> None:
    run = await crawler.crawl(routes(BUDGET), max_pages=5, concurrency=concurrency)
    # Level 2 has room for two of a3, a1, a2 (from /a) and b1 (from /b): BFS order decides.
    assert run.rows == {
        "/": stored(0),
        "/a": stored(1),
        "/b": stored(1),
        "/a3": stored(2),
        "/a1": stored(2),
    }
    assert run.progress.job.admitted_count == 5
    assert run.requested("/a2") == run.requested("/b1") == 0


# --- Redirects -----------------------------------------------------------------------------------

REDIRECTS: dict[str, P | Reply] = {
    "/": P(links=("/r0", "/loop-a", "/away")),
    **{f"/r{n}": moved(f"/r{n + 1}", 302) for n in range(6)},
    "/r6": P(),
    "/loop-a": moved("/loop-b"),
    "/loop-b": moved("/loop-a"),
    "/away": moved("http://other.test/"),  # outside the job: recorded, not followed
}


async def test_redirect_chains_loops_and_other_hosts(crawler: Crawler) -> None:
    run = await crawler.crawl(routes(REDIRECTS))
    redirect: Row = (1, "fetched", "redirect")
    assert run.rows == {
        "/": stored(0),
        # The hop limit (5) stops the chain: /r5's target /r6 is never admitted.
        **{f"/r{n}": redirect for n in range(6)},
        "/loop-a": redirect,
        "/loop-b": redirect,  # its target is already known: the loop ends
        "/away": redirect,
    }
    assert run.requested("/r6") == 0


# --- Jobs that fail ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("robots", "outcome"),
    [
        (Reply(status=503, reason="Unavailable"), "robots_unreachable"),
        (b"User-agent: *\nDisallow: /\n", "robots_disallowed"),
    ],
)
async def test_a_job_fails_when_robots_txt_keeps_it_out(
    crawler: Crawler, robots: bytes | Reply, outcome: str
) -> None:
    run = await crawler.crawl(routes({"/": P(links=("/a",))}, robots))
    assert run.rows == {"/": (0, "skipped", outcome)}
    job = run.progress.job
    assert job.status == "failed"
    assert job.failure_reason == f"the seed URL was not fetched (skipped: {outcome})"
    assert run.requested("/") == 0


# --- Crash recovery ------------------------------------------------------------------------------


async def test_a_crashed_workers_lease_is_taken_over_after_it_expires(crawler: Crawler) -> None:
    stale = []

    async def crash_while_holding_the_seed(job_id: uuid.UUID, port: int) -> None:
        async with crawler.database.session() as session:
            await activate_job(session, job_id)
        async with crawler.database.session() as session:
            [seed_id] = await dispatch_urls(session, job_id, limit=10)
        async with crawler.database.session() as session:
            lease = await claim_url(
                session, seed_id, owner="crashed-worker", lease_seconds=0.5, max_attempts=3
            )
        assert lease is not None
        stale.append(lease)  # the worker "crashes": it never records a result

    run = await crawler.crawl(routes(GRAPH, GRAPH_ROBOTS), before_run=crash_while_holding_the_seed)
    assert run.rows == GRAPH_EXPECTED
    async with crawler.database.session() as session:
        recovered = list(
            await session.scalars(
                select(CrawlEvent).where(
                    CrawlEvent.job_id == run.job_id, CrawlEvent.event_type == "url.recovered"
                )
            )
        )
        seed = await session.get(CrawlUrl, stale[0].url_id)
    assert [event.detail["previous_owner"] for event in recovered] == ["crashed-worker"]
    assert seed is not None
    assert (seed.attempt_count, seed.lease_epoch) == (2, stale[0].token + 1)

    # The crashed worker comes back with its old token: its result is rejected.
    async with crawler.database.session() as session:
        late = await record_result(
            session,
            stale[0],
            UrlResult(UrlState.FAILED, "read_timeout", error="the worker woke up late"),
            max_redirect_hops=5,
        )
    assert late.status is RecordStatus.FENCED
    await assert_invariants(crawler.database, run.job_id)


async def test_a_job_cancelled_during_the_run_ends_cancelled(crawler: Crawler) -> None:
    """Phase 6: the seed is in flight when the job is cancelled. Its result is kept, nothing
    new is admitted or claimed, and the run settles the cancellation before it returns."""
    site = routes({"/": P(links=("/a", "/b")), "/a": P(), "/b": P()})
    site["/"].delay = 1.0
    cancellations: list[asyncio.Task[None]] = []

    async def cancel_while_fetching(job_id: uuid.UUID, port: int) -> None:
        async def cancel() -> None:
            await asyncio.sleep(0.3)
            async with crawler.database.session() as session:
                await cancel_crawl_job(session, job_id)

        cancellations.append(asyncio.create_task(cancel()))

    run = await crawler.crawl(site, max_depth=1, before_run=cancel_while_fetching)
    await cancellations[0]

    assert run.progress.job.status == "cancelled"
    assert run.rows == {"/": stored(0)}
    assert [request.target for request in run.requests if request.target != "/robots.txt"] == ["/"]


# --- Politeness ----------------------------------------------------------------------------------


async def test_requests_to_the_host_are_spaced_by_the_interval(crawler: Crawler) -> None:
    site = routes({"/": P(links=("/a", "/b", "/c", "/d")), **{f"/{p}": P() for p in "abcd"}})
    run = await crawler.crawl(site, concurrency=4, interval=0.2)
    assert len(run.requests) == 6  # robots.txt and five pages
    times = sorted(request.received_at for request in run.requests)
    gaps = [later - earlier for earlier, later in itertools.pairwise(times)]
    # Starts are 0.2 s apart by the Redis schedule; the margin absorbs timer resolution.
    assert min(gaps) >= 0.17, gaps


# --- The demo site -------------------------------------------------------------------------------

DEMO_EXPECTED: dict[str, Row] = {
    "/": stored(0),
    "/about.html": stored(1),
    "/docs/": stored(1),
    "/blog/": stored(1),
    "/private/press-kit.html": stored(1),
    "/private/notes.html": (1, "skipped", "robots_disallowed"),
    "/team.html": stored(2),
    "/missing.html": (2, "fetched", "http_error"),
    "/docs/getting-started.html": stored(2),
    "/docs/architecture.html": stored(2),
    "/docs/faq.html": stored(2),
    "/blog/crawling-politely.html": stored(2),
    "/blog/search-basics.html": stored(2),
    "/docs/glossary.html": stored(3),
}


def demo_routes() -> dict[str, Reply]:
    """The demo-site directory as routes (a directory's index.html serves its path)."""
    served: dict[str, Reply] = {}
    for file in sorted(DEMO_SITE.rglob("*")):
        if file.suffix not in (".html", ".txt"):
            continue
        path = "/" + file.relative_to(DEMO_SITE).as_posix()
        if file.name == "index.html":
            path = path.removesuffix("index.html")
        content_type = "text/html; charset=utf-8" if file.suffix == ".html" else "text/plain"
        served[path] = Reply(headers=[("Content-Type", content_type)], body=file.read_bytes())
    return served


async def test_the_demo_site(crawler: Crawler) -> None:
    run = await crawler.crawl(demo_routes(), max_depth=3)
    assert run.rows == DEMO_EXPECTED
    async with crawler.database.session() as session:
        pages = await session.scalar(
            select(func.count()).select_from(Page).where(Page.job_id == run.job_id)
        )
        documents = await session.scalar(
            select(func.count())
            .select_from(SearchDocument)
            .where(SearchDocument.job_id == run.job_id)
        )
    assert (pages, documents) == (12, 11)  # /team.html is noindex
    assert run.requested("/docs/legacy.html") == 0  # linked only from a nofollow page


# --- The command-line interface ------------------------------------------------------------------


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.mark.usefixtures("restore_logging")
def test_the_cli_crawls_the_demo_site_from_a_static_file_server(
    test_database: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DEMO_SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    prefix = f"test-{uuid.uuid4().hex[:12]}"
    settings = test_database.model_copy(
        update={
            "crawl_allowed_hosts": ("127.0.0.1",),
            "crawl_allowed_schemes": ("http",),
            "crawl_allowed_ports": (port,),
            "fetch_internal_origins": (f"http://127.0.0.1:{port}",),
            "politeness_interval_seconds": 0.0,
            "redis_key_prefix": prefix,
        }
    )
    try:
        job_id = asyncio.run(_create_job(settings, f"http://127.0.0.1:{port}/"))
        assert main([str(job_id)], settings=settings) == 0
        output = capsys.readouterr().out
        report = json.loads(output[output.index('{\n  "job_id"') :])
        assert report["status"] == "completed"
        assert report["urls_by_state"]["fetched"] == 13
        assert report["urls_by_state"]["skipped"] == 1
        rows = asyncio.run(_snapshot_ip(settings, job_id, port))
        assert rows == DEMO_EXPECTED
    finally:
        server.shutdown()
        server.server_close()
        asyncio.run(_cleanup(settings, prefix))


async def _create_job(settings: Settings, seed: str) -> uuid.UUID:
    database = Database(settings, application_name="sitescope-tests")
    try:
        async with database.session() as session:
            job = await create_crawl_job(
                session, settings, seed_url=seed, max_depth=3, max_pages=100
            )
        return job.id
    finally:
        await database.dispose()


async def _snapshot_ip(settings: Settings, job_id: uuid.UUID, port: int) -> dict[str, Row]:
    database = Database(settings, application_name="sitescope-tests")
    try:
        async with database.session() as session:
            rows = await session.execute(
                select(
                    CrawlUrl.normalized_url, CrawlUrl.depth, CrawlUrl.state, CrawlUrl.outcome
                ).where(CrawlUrl.job_id == job_id)
            )
            prefix = f"http://127.0.0.1:{port}"
            return {url.removeprefix(prefix): (d, s, o) for url, d, s, o in rows}
    finally:
        await database.dispose()


async def _cleanup(settings: Settings, prefix: str) -> None:
    database = Database(settings, application_name="sitescope-tests")
    redis = create_redis_client(settings, client_name="sitescope-tests")
    try:
        await helpers.truncate_all(database)
        keys = [key async for key in redis.scan_iter(match=f"{prefix}:*", count=500)]
        if keys:
            await redis.delete(*keys)
    finally:
        await redis.aclose()
        await database.dispose()


@pytest.mark.usefixtures("restore_logging")
def test_the_cli_reports_an_unknown_job(
    test_database: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = uuid.uuid4()
    assert main([str(missing)], settings=test_database) == 2
    assert f"Crawl job {missing} does not exist." in capsys.readouterr().err
