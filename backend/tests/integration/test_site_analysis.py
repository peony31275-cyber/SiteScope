"""The website-analysis service (app/services/site_analysis.py) against PostgreSQL.

Crawl results are built directly in the exact states a crawl leaves (``tests/analysis_support``),
so every link class, redirect chain and limit can be checked without network access. The
service never sends a request; these tests have no HTTP server at all.
"""

import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.core.errors import NotFoundError
from app.db.models import UrlState
from app.db.session import Database
from app.services import site_analysis
from app.services.search_index import IndexingProgress, read_job_indexing
from app.services.site_analysis import (
    AnalysisTimeoutError,
    BrokenLinkPosition,
    DepthCount,
    LinkClass,
    LinkView,
    PageIndexStatus,
    PagePosition,
    TopLinkedPage,
    crawl_stats,
    link_graph,
    list_broken_links,
    list_links,
    list_pages,
)
from tests.analysis_support import Link, add_links, add_url, set_job, url_of
from tests.crawl_support import new_job
from tests.index_support import add_page, execute

pytestmark = pytest.mark.anyio

FETCHED, SKIPPED, FAILED = UrlState.FETCHED, UrlState.SKIPPED, UrlState.FAILED
PENDING, QUEUED = UrlState.PENDING, UrlState.QUEUED


def path(url: str) -> str:
    """A URL on the test host as its path (without the leading slash); other URLs unchanged."""
    return url.removeprefix(url_of(""))


async def links_of(database: Database, job_id: uuid.UUID, **filters: Any) -> list[LinkView]:
    async with database.session() as session:
        return (await list_links(session, job_id, limit=500, **filters)).links


async def classes(database: Database, job_id: uuid.UUID) -> dict[str, LinkClass]:
    """Every link of the job: target -> class (for tests where each target is linked once)."""
    return {path(link.target_url): link.classification for link in await links_of(database, job_id)}


async def mark_indexed(database: Database, page_id: uuid.UUID) -> None:
    await execute(
        database,
        "UPDATE search_documents SET status = 'indexed', indexed_content_version = 1,"
        " indexed_at = clock_timestamp(), search_vector = to_tsvector('english', 'indexed')"
        " WHERE page_id = :id",
        id=page_id,
    )


# --- Link classification -------------------------------------------------------------------------


async def test_every_link_class_follows_from_stored_crawl_results(database: Database) -> None:
    job_id, _ = await new_job(database, max_depth=3, max_pages=100)
    source = await add_page(database, job_id, path="src", depth=1)
    await add_page(database, job_id, path="ok-page", depth=2)
    results = {
        "nonhtml": (FETCHED, "non_html", 200, None),
        "parsefail": (FAILED, "parse_error", 200, None),
        "gone404": (FETCHED, "http_error", 404, None),
        "gone410": (FETCHED, "http_error", 410, None),
        "forbidden": (FETCHED, "http_error", 403, None),
        "badreq": (FETCHED, "http_error", 400, None),
        "unavailable": (FETCHED, "http_error", 503, None),
        "toomany": (FETCHED, "http_error", 429, None),
        "late": (FETCHED, "http_error", 408, None),
        "redirect-ok": (FETCHED, "redirect", 301, "ok-page"),
        "redirect-broken": (FETCHED, "redirect", 302, "redirect-mid"),
        "redirect-mid": (FETCHED, "redirect", 301, "gone404"),
        "loop-a": (FETCHED, "redirect", 302, "loop-b"),
        "loop-b": (FETCHED, "redirect", 302, "loop-a"),
        "invalid": (FETCHED, "invalid_redirect", 302, None),
        "redirect-out": (FETCHED, "redirect", 301, "https://other.test/x"),
        "redirect-unadmitted": (FETCHED, "redirect", 301, "never-admitted"),
        "robots": (SKIPPED, "robots_disallowed", None, None),
        "robots-down": (SKIPPED, "robots_unreachable", None, None),
        "blocked": (SKIPPED, "blocked_destination", None, None),
        "notallowed": (SKIPPED, "target_not_allowed", None, None),
        "dns": (FAILED, "dns_error", None, None),
        "slow": (FAILED, "read_timeout", None, None),
        "crashed": (FAILED, "max_attempts_exceeded", None, None),
        "waiting": (PENDING, None, None, None),
        "queued": (QUEUED, None, None, None),
        "cancelled": (UrlState.CANCELLED, "job_cancelled", None, None),
    }
    for name, (state, outcome, status, redirect) in results.items():
        await add_url(
            database,
            job_id,
            name,
            state=state,
            outcome=outcome,
            http_status=status,
            redirect_to=redirect,
            depth=2,
        )
    await add_links(
        database,
        job_id,
        source,
        [
            *(Link(name) for name in [*results, "ok-page", "unknown-followed"]),
            Link("unknown-nofollow", nofollow=True),
            Link("https://other.org/", internal=False),
        ],
    )

    assert await classes(database, job_id) == {
        "nonhtml": LinkClass.OK,
        "parsefail": LinkClass.OK,  # the server answered 200; only the parse failed
        "ok-page": LinkClass.OK,
        "redirect-ok": LinkClass.OK,
        "gone404": LinkClass.BROKEN,
        "gone410": LinkClass.BROKEN,
        "redirect-broken": LinkClass.BROKEN,  # two hops, then a 404
        "redirect-mid": LinkClass.BROKEN,
        "forbidden": LinkClass.HTTP_ERROR,
        "badreq": LinkClass.HTTP_ERROR,
        "unavailable": LinkClass.SERVER_ERROR,
        "toomany": LinkClass.SERVER_ERROR,
        "late": LinkClass.SERVER_ERROR,
        "loop-a": LinkClass.REDIRECT_ERROR,
        "loop-b": LinkClass.REDIRECT_ERROR,
        "invalid": LinkClass.REDIRECT_ERROR,
        "redirect-out": LinkClass.OUT_OF_SCOPE,
        "redirect-unadmitted": LinkClass.NOT_CRAWLED,
        "robots": LinkClass.ROBOTS_BLOCKED,
        "robots-down": LinkClass.ROBOTS_BLOCKED,
        "blocked": LinkClass.OUT_OF_SCOPE,
        "notallowed": LinkClass.OUT_OF_SCOPE,
        "dns": LinkClass.UNREACHABLE,
        "slow": LinkClass.UNREACHABLE,
        "crashed": LinkClass.UNREACHABLE,
        "waiting": LinkClass.NOT_CRAWLED,
        "queued": LinkClass.NOT_CRAWLED,
        "cancelled": LinkClass.NOT_CRAWLED,
        "unknown-followed": LinkClass.NOT_CRAWLED,  # the next level is not admitted yet
        "unknown-nofollow": LinkClass.NOT_FOLLOWED,
        "https://other.org/": LinkClass.OUT_OF_SCOPE,
    }


async def test_a_redirect_is_judged_by_the_end_of_its_chain(database: Database) -> None:
    job_id, _ = await new_job(database)
    source = await add_page(database, job_id, path="src", depth=1)
    new_page = await add_page(database, job_id, path="new", depth=2)
    await add_url(
        database,
        job_id,
        "old",
        state=FETCHED,
        outcome="redirect",
        http_status=301,
        redirect_to="mid",
        depth=2,
    )
    await add_url(
        database,
        job_id,
        "mid",
        state=FETCHED,
        outcome="redirect",
        http_status=308,
        redirect_to="new",
        depth=2,
    )
    await add_url(
        database,
        job_id,
        "away",
        state=FETCHED,
        outcome="redirect",
        http_status=302,
        redirect_to="https://other.test/x",
        depth=2,
    )
    await add_url(
        database,
        job_id,
        "self",
        state=FETCHED,
        outcome="redirect",
        http_status=307,
        redirect_to="self",
        depth=2,
    )
    await add_links(
        database, job_id, source, [Link(name) for name in ("old", "away", "self", "new")]
    )

    links = {path(link.target_url): link for link in await links_of(database, job_id)}

    old = links["old"]
    assert (old.target_state, old.target_http_status, old.target_page_id) == (FETCHED, 301, None)
    assert (old.final_url, old.final_http_status, old.redirect_hops) == (url_of("new"), 200, 2)
    assert old.classification is LinkClass.OK
    away = links["away"]  # the end of the chain was never requested
    assert (away.final_url, away.final_http_status, away.redirect_hops) == (
        "https://other.test/x",
        None,
        1,
    )
    assert away.classification is LinkClass.OUT_OF_SCOPE
    loop = links["self"]
    assert (loop.final_url, loop.final_http_status, loop.redirect_hops) == (None, None, 0)
    assert loop.classification is LinkClass.REDIRECT_ERROR
    new = links["new"]
    assert (new.target_page_id, new.final_url, new.redirect_hops) == (new_page, None, 0)
    assert new.target_crawled
    assert loop.target_crawled


async def test_a_redirect_chain_is_followed_for_at_most_twenty_redirects(
    database: Database,
) -> None:
    """URLs admitted through links can form a redirect chain of any length: the crawler's hop
    limit only bounds the redirect targets it admits itself. A chain is followed for at most
    MAX_REDIRECT_HOPS (20) redirects, and a longer one is a redirect_error. Before the review,
    a chain of n URLs made the query follow about n * n / 2 links; this one has 300."""
    job_id, _ = await new_job(database, max_pages=1000)
    source = await add_page(database, job_id, path="src")
    await add_page(database, job_id, path="end", depth=2)
    count = 300
    for index in range(count):
        await add_url(
            database,
            job_id,
            f"r{index}",
            state=FETCHED,
            outcome="redirect",
            http_status=301,
            redirect_to=f"r{index + 1}" if index + 1 < count else "end",
            depth=2,
        )
    starts = [0, count - 21, count - 20, count - 1]  # 300, 21, 20 and 1 redirects to /end
    await add_links(database, job_id, source, [Link(f"r{index}") for index in starts])

    links = {
        path(link.target_url): link
        for link in await links_of(database, job_id, timeout_seconds=5.0)
    }
    async with database.session() as session:
        stats = await crawl_stats(session, job_id, timeout_seconds=5.0)

    assert site_analysis.MAX_REDIRECT_HOPS == 20
    assert {name: (link.classification, link.redirect_hops) for name, link in links.items()} == {
        "r0": (LinkClass.REDIRECT_ERROR, 20),
        f"r{count - 21}": (LinkClass.REDIRECT_ERROR, 20),
        f"r{count - 20}": (LinkClass.OK, 20),
        f"r{count - 1}": (LinkClass.OK, 1),
    }
    assert (links["r0"].final_url, links["r0"].final_http_status) == (url_of("r20"), 301)
    assert links[f"r{count - 20}"].final_url == url_of("end")
    assert stats.links_by_class[LinkClass.REDIRECT_ERROR] == 2


async def test_a_target_without_a_final_result_is_not_judged_by_its_last_status(
    database: Database,
) -> None:
    """A URL waiting for a retry keeps its last attempt's status (crawl_state._RETRY), here 503;
    a cancelled one may keep it too. Neither status is a final response."""
    job_id, _ = await new_job(database)
    source = await add_page(database, job_id, path="src")
    await add_url(database, job_id, "retrying", state=PENDING, http_status=503)
    await add_url(
        database,
        job_id,
        "abandoned",
        state=UrlState.CANCELLED,
        outcome="job_cancelled",
        http_status=503,
    )
    await add_url(
        database,
        job_id,
        "via",
        state=FETCHED,
        outcome="redirect",
        http_status=301,
        redirect_to="retrying",
    )
    await add_links(database, job_id, source, [Link("retrying"), Link("abandoned"), Link("via")])

    links = {path(link.target_url): link for link in await links_of(database, job_id)}

    assert {
        name: (link.classification, link.target_http_status, link.final_http_status)
        for name, link in links.items()
    } == {
        "retrying": (LinkClass.NOT_CRAWLED, None, None),
        "abandoned": (LinkClass.NOT_CRAWLED, None, None),
        "via": (LinkClass.NOT_CRAWLED, 301, None),
    }
    assert links["via"].final_url == url_of("retrying")


async def test_links_cut_off_by_the_depth_limit_or_the_page_budget_are_beyond_limits(
    database: Database,
) -> None:
    job_id, _ = await new_job(database, max_depth=1, max_pages=10)
    top = await add_page(database, job_id, path="top", depth=0)
    deep = await add_page(database, job_id, path="deep", depth=1)
    await add_url(
        database,
        job_id,
        "moved",
        state=FETCHED,
        outcome="redirect",
        http_status=301,
        redirect_to="moved-to",
        depth=1,
    )
    await add_links(database, job_id, top, [Link("next-level"), Link("moved")])
    await add_links(database, job_id, deep, [Link("too-deep")])

    assert await classes(database, job_id) == {
        "next-level": LinkClass.NOT_CRAWLED,  # budget left, level 1 allowed: not admitted yet
        "moved": LinkClass.NOT_CRAWLED,
        "too-deep": LinkClass.BEYOND_LIMITS,  # its page is at max_depth
    }

    await set_job(database, job_id, admitted_count=10)  # the page budget is used up

    assert await classes(database, job_id) == {
        "next-level": LinkClass.BEYOND_LIMITS,
        "moved": LinkClass.BEYOND_LIMITS,
        "too-deep": LinkClass.BEYOND_LIMITS,
    }


async def test_links_that_may_not_be_followed_are_not_followed(database: Database) -> None:
    job_id, _ = await new_job(database, max_depth=1)
    page = await add_page(database, job_id, path="page", depth=0)
    closed = await add_page(database, job_id, path="closed", depth=0, nofollow=True)
    deep = await add_page(database, job_id, path="deep", depth=1)
    await add_page(database, job_id, path="known", depth=1)
    await add_links(database, job_id, page, [Link("marked", nofollow=True), Link("known")])
    await add_links(database, job_id, closed, [Link("from-closed"), Link("known")])
    await add_links(database, job_id, deep, [Link("deep-marked", nofollow=True)])

    found = {
        (path(link.source_url), path(link.target_url)): link.classification
        for link in await links_of(database, job_id)
    }

    assert found == {
        ("page", "marked"): LinkClass.NOT_FOLLOWED,
        ("page", "known"): LinkClass.OK,
        ("closed", "from-closed"): LinkClass.NOT_FOLLOWED,
        ("closed", "known"): LinkClass.OK,  # admitted through another page: its result decides
        ("deep", "deep-marked"): LinkClass.NOT_FOLLOWED,  # nofollow comes before the depth limit
    }


# --- Listings ------------------------------------------------------------------------------------


async def test_links_are_listed_by_page_and_position_and_paged_without_gaps(
    database: Database,
) -> None:
    job_id, _ = await new_job(database)
    a = await add_page(database, job_id, path="a")
    b = await add_page(database, job_id, path="b")
    await add_url(database, job_id, "gone", state=FETCHED, outcome="http_error", http_status=404)
    elsewhere = Link("https://elsewhere.test/", internal=False, anchor="Elsewhere")
    await add_links(database, job_id, b, [Link("gone"), Link("a"), elsewhere])
    await add_links(database, job_id, a, [Link("b", anchor="B"), Link("gone"), Link("c")])

    everything = await links_of(database, job_id)

    assert [
        (path(link.source_url), link.position, path(link.target_url)) for link in everything
    ] == [
        ("a", 0, "b"),
        ("a", 1, "gone"),
        ("a", 2, "c"),
        ("b", 0, "gone"),
        ("b", 1, "a"),
        ("b", 2, "https://elsewhere.test/"),
    ]
    assert (everything[0].anchor_text, everything[0].target_page_id) == ("B", b)
    paged: list[LinkView] = []
    after = None
    async with database.session() as session:
        while True:
            listing = await list_links(session, job_id, limit=4, after=after)
            paged += listing.links
            if (after := listing.next_position) is None:
                break
    assert paged == everything
    async with database.session() as session:
        on_b = await list_links(session, job_id, limit=10, source_page_id=b)
        external = await list_links(session, job_id, limit=10, internal=False)
        internal = await list_links(session, job_id, limit=10, internal=True)
        broken = await list_links(session, job_id, limit=10, classification=LinkClass.BROKEN)
    assert [path(link.target_url) for link in on_b.links] == [
        "gone",
        "a",
        "https://elsewhere.test/",
    ]
    assert [link.anchor_text for link in external.links] == ["Elsewhere"]
    assert len(internal.links) == 5
    assert [(path(link.source_url), path(link.target_url)) for link in broken.links] == [
        ("a", "gone"),
        ("b", "gone"),
    ]


async def test_links_can_be_filtered_by_verification_and_by_redirects(
    database: Database,
) -> None:
    """The roadmap's reports: unchecked links (verified=false), redirecting links
    (redirected=true), each combined with the other filters."""
    job_id, _ = await new_job(database)
    source = await add_page(database, job_id, path="src")
    await add_page(database, job_id, path="ok")
    await add_url(database, job_id, "gone", state=FETCHED, outcome="http_error", http_status=404)
    await add_url(
        database,
        job_id,
        "moved",
        state=FETCHED,
        outcome="redirect",
        http_status=301,
        redirect_to="ok",
    )
    await add_url(
        database, job_id, "nowhere", state=FETCHED, outcome="invalid_redirect", http_status=302
    )
    await add_url(database, job_id, "secret", state=SKIPPED, outcome="robots_disallowed")
    await add_url(database, job_id, "slow", state=FAILED, outcome="read_timeout")
    await add_links(
        database,
        job_id,
        source,
        [
            *(Link(name) for name in ("ok", "gone", "moved", "nowhere", "secret", "slow", "later")),
            Link("https://other.test/", internal=False),
        ],
    )

    async def targets(**filters: Any) -> list[str]:
        return [path(link.target_url) for link in await links_of(database, job_id, **filters)]

    assert await targets(verified=True) == ["ok", "gone", "moved", "nowhere"]
    assert await targets(verified=False) == ["secret", "slow", "later", "https://other.test/"]
    assert await targets(redirected=True) == ["moved", "nowhere"]
    assert await targets(redirected=False) == [
        "ok",
        "gone",
        "secret",
        "slow",
        "later",
        "https://other.test/",
    ]
    assert await targets(redirected=True, classification=LinkClass.OK) == ["moved"]
    assert await targets(verified=False, internal=False) == ["https://other.test/"]
    paged: list[LinkView] = []
    after = None
    async with database.session() as session:
        while True:
            listing = await list_links(session, job_id, limit=1, after=after, verified=False)
            paged += listing.links
            if (after := listing.next_position) is None:
                break
    assert [path(link.target_url) for link in paged] == await targets(verified=False)


async def test_pages_are_listed_in_bfs_order_with_their_index_status(database: Database) -> None:
    job_id, _ = await new_job(database)
    b = await add_page(database, job_id, path="b", depth=1, title="B", description="About B")
    a = await add_page(database, job_id, path="a", depth=1, title="A")
    await add_page(database, job_id, path="z", depth=0, title="Z")
    await add_page(database, job_id, path="hidden", depth=2, noindex=True, nofollow=True)
    await add_url(database, job_id, "file.pdf", state=FETCHED, outcome="non_html", http_status=200)
    await mark_indexed(database, a)

    async with database.session() as session:
        listing = await list_pages(session, job_id, limit=10)

    assert [(path(page.url), page.depth, page.index_status) for page in listing.pages] == [
        ("z", 0, PageIndexStatus.PENDING),
        ("a", 1, PageIndexStatus.INDEXED),
        ("b", 1, PageIndexStatus.PENDING),
        ("hidden", 2, PageIndexStatus.NOINDEX),
    ]  # the PDF was fetched, but it is not a page
    assert listing.next_position is None
    first, second, third, fourth = listing.pages
    assert (second.page_id, second.title, second.http_status, second.content_type) == (
        a,
        "A",
        200,
        "text/html",
    )
    assert second.indexed_at is not None
    assert first.indexed_at is None
    assert (third.page_id, third.description) == (b, "About B")
    assert (fourth.noindex, fourth.nofollow, third.noindex) == (True, True, False)

    async with database.session() as session:
        depth_one = await list_pages(session, job_id, limit=10, depth=1)
        noindex = await list_pages(session, job_id, limit=10, index_status=PageIndexStatus.NOINDEX)
        indexed = await list_pages(session, job_id, limit=10, index_status=PageIndexStatus.INDEXED)
        start = await list_pages(session, job_id, limit=2)
        rest = await list_pages(session, job_id, limit=2, after=start.next_position)
    assert [path(page.url) for page in depth_one.pages] == ["a", "b"]
    assert [path(page.url) for page in noindex.pages] == ["hidden"]
    assert [page.page_id for page in indexed.pages] == [a]
    assert start.next_position == PagePosition(1, url_of("a"))
    assert [path(page.url) for page in start.pages + rest.pages] == ["z", "a", "b", "hidden"]
    assert rest.next_position is None


async def test_broken_links_are_the_links_to_confirmed_404_and_410_targets(
    database: Database,
) -> None:
    job_id, _ = await new_job(database)
    a = await add_page(database, job_id, path="a")
    b = await add_page(database, job_id, path="b")
    for name, status in (("gone", 404), ("removed", 410), ("forbidden", 403), ("down", 503)):
        await add_url(
            database, job_id, name, state=FETCHED, outcome="http_error", http_status=status
        )
    await add_url(
        database,
        job_id,
        "moved",
        state=FETCHED,
        outcome="redirect",
        http_status=301,
        redirect_to="gone",
    )
    await add_url(database, job_id, "timeout", state=FAILED, outcome="read_timeout")
    await add_url(database, job_id, "secret", state=SKIPPED, outcome="robots_disallowed")
    names = ("gone", "removed", "forbidden", "down", "moved", "timeout", "secret", "unknown")
    await add_links(database, job_id, a, [Link(name) for name in names])
    await add_links(
        database, job_id, b, [Link("gone"), Link("https://other.test/gone", internal=False)]
    )

    async with database.session() as session:
        listing = await list_broken_links(session, job_id, limit=10)

    assert listing.total == 4
    assert [(path(link.target_url), path(link.source_url)) for link in listing.links] == [
        ("gone", "a"),
        ("gone", "b"),
        ("moved", "a"),
        ("removed", "a"),
    ]
    assert listing.next_position is None
    moved = listing.links[2]
    assert (moved.target_http_status, moved.final_url, moved.final_http_status) == (
        301,
        url_of("gone"),
        404,
    )
    pages: list[list[LinkView]] = []
    after = None
    async with database.session() as session:
        while True:
            chunk = await list_broken_links(session, job_id, limit=1, after=after)
            assert chunk.total == 4
            pages.append(chunk.links)
            if (after := chunk.next_position) is None:
                break
        beyond = await list_broken_links(
            session, job_id, limit=5, after=BrokenLinkPosition(url_of("zzz"), url_of("a"))
        )
    assert [link for chunk in pages for link in chunk] == listing.links
    assert len(pages) == 4
    assert (beyond.total, beyond.links, beyond.next_position) == (4, [], None)


async def test_a_job_without_links_has_no_broken_links(database: Database) -> None:
    job_id, _ = await new_job(database)
    async with database.session() as session:
        listing = await list_broken_links(session, job_id, limit=10)
    assert (listing.total, listing.links, listing.next_position) == (0, [], None)


# --- The link graph ------------------------------------------------------------------------------


async def graph_job(database: Database) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """The seed (pending), two pages, a 404 target and a target never admitted; returns
    (job, home page, docs page)."""
    job_id, _ = await new_job(database)
    home = await add_page(database, job_id, path="home", depth=0, title="Home")
    docs = await add_page(database, job_id, path="docs", depth=1, title="Docs")
    await add_url(
        database, job_id, "gone", state=FETCHED, outcome="http_error", http_status=404, depth=1
    )
    await add_links(
        database,
        job_id,
        home,
        [Link("docs"), Link("gone"), Link("home"), Link("https://other.test/", internal=False)],
    )
    await add_links(database, job_id, docs, [Link("home", nofollow=True), Link("later")])
    return job_id, home, docs


async def test_the_link_graph_holds_known_urls_and_their_internal_links(
    database: Database,
) -> None:
    job_id, home, docs = await graph_job(database)

    async with database.session() as session:
        graph = await link_graph(session, job_id, max_nodes=100, max_edges=100)
        stats = await crawl_stats(session, job_id)

    assert [
        (path(n.url), n.depth, n.crawl_state, n.http_status, n.in_degree, n.out_degree)
        for n in graph.nodes
    ] == [
        ("", 0, PENDING, None, 0, None),  # the seed, not crawled yet: no inbound link, no page
        ("home", 0, FETCHED, 200, 2, 3),  # a self link counts; the external link does not
        ("docs", 1, FETCHED, 200, 1, 2),
        ("gone", 1, FETCHED, 404, 1, None),
        ("later", None, None, None, 1, None),  # a link target the job never admitted
    ]
    assert [(n.page_id, n.title) for n in graph.nodes] == [
        (None, None),
        (home, "Home"),
        (docs, "Docs"),
        (None, None),
        (None, None),
    ]
    assert [(e.source, e.target, e.nofollow) for e in graph.edges] == [
        (2, 1, True),  # docs -> home
        (2, 4, False),  # docs -> later
        (1, 2, False),  # home -> docs
        (1, 3, False),  # home -> gone
        (1, 1, False),  # home -> home
    ]
    assert (graph.total_nodes, graph.total_edges, graph.truncated) == (5, 5, False)
    assert graph.total_nodes == stats.urls_discovered


async def test_the_link_graph_is_cut_at_its_limits_in_a_stable_order(database: Database) -> None:
    job_id, _, _ = await graph_job(database)

    async with database.session() as session:
        cut = await link_graph(session, job_id, max_nodes=3, max_edges=1)
        no_edges = await link_graph(session, job_id, max_nodes=2, max_edges=0)

    assert [path(n.url) for n in cut.nodes] == ["", "home", "docs"]
    assert [(e.source, e.target) for e in cut.edges] == [(2, 1)]  # docs -> home comes first
    assert (cut.total_nodes, cut.total_edges, cut.truncated) == (5, 5, True)
    assert cut.nodes[1].in_degree == 2  # degrees count every link, also those cut off
    assert ([path(n.url) for n in no_edges.nodes], no_edges.edges) == (["", "home"], [])
    assert (no_edges.total_nodes, no_edges.total_edges, no_edges.truncated) == (5, 5, True)


async def test_the_link_graph_limits_must_be_usable(database: Database) -> None:
    job_id, _ = await new_job(database)
    async with database.session() as session:
        with pytest.raises(ValueError, match="max_nodes"):
            await link_graph(session, job_id, max_nodes=0, max_edges=10)
        with pytest.raises(ValueError, match="max_edges"):
            await link_graph(session, job_id, max_nodes=10, max_edges=-1)


# --- Statistics ----------------------------------------------------------------------------------


async def test_statistics_count_stored_results_only(database: Database) -> None:
    job_id, _ = await new_job(database, max_depth=2, max_pages=50)  # the seed is pending
    home = await add_page(database, job_id, path="home", depth=0)
    docs = await add_page(database, job_id, path="docs", depth=1)
    await add_page(database, job_id, path="team", depth=1, noindex=True)
    await mark_indexed(database, docs)
    for name, state, outcome, status, redirect, depth in (
        ("file.pdf", FETCHED, "non_html", 200, None, 1),
        ("gone", FETCHED, "http_error", 404, None, 1),
        ("old", FETCHED, "redirect", 301, "docs", 1),
        ("down", FETCHED, "http_error", 503, None, 1),
        ("slow", FAILED, "read_timeout", None, None, 1),
        ("secret", SKIPPED, "robots_disallowed", None, None, 1),
        ("next", QUEUED, None, None, None, 2),
        ("retry", PENDING, None, 503, None, 2),  # waiting for a retry: not a final status
    ):
        await add_url(
            database,
            job_id,
            name,
            state=state,
            outcome=outcome,
            http_status=status,
            redirect_to=redirect,
            depth=depth,
        )
    home_links = ["docs", "gone", "old", "slow", "secret", "new-target"]
    await add_links(
        database,
        job_id,
        home,
        [*(Link(name) for name in home_links), Link("https://ext.test/", internal=False)],
    )
    await add_links(
        database, job_id, docs, [Link(name) for name in ("home", "gone", "down", "file.pdf")]
    )

    async with database.session() as session:
        stats = await crawl_stats(session, job_id)

    assert (stats.status, stats.current_level, stats.max_depth, stats.max_pages) == (
        "running",
        0,
        2,
        50,
    )
    assert stats.urls_admitted == 12
    assert stats.urls_discovered == 13  # plus new-target, linked but never admitted
    assert stats.urls_by_state == {
        PENDING: 2,
        QUEUED: 1,
        UrlState.IN_PROGRESS: 0,
        FETCHED: 7,
        FAILED: 1,
        SKIPPED: 1,
        UrlState.CANCELLED: 0,
    }
    assert stats.pages_stored == 3
    assert (
        stats.status_1xx,
        stats.status_2xx,
        stats.status_3xx,
        stats.status_4xx,
        stats.status_5xx,
        stats.no_response,
    ) == (0, 4, 1, 1, 1, 1)
    assert stats.depths == [DepthCount(0, 2, 1, 1), DepthCount(1, 8, 6, 2), DepthCount(2, 2, 0, 0)]
    assert (stats.indexing.pending, stats.indexing.indexed, stats.indexing.documents) == (1, 1, 2)
    assert (stats.links_total, stats.links_internal, stats.links_external) == (11, 10, 1)
    assert stats.internal_targets == 9
    assert stats.links_by_class == {
        LinkClass.OUT_OF_SCOPE: 1,
        LinkClass.NOT_FOLLOWED: 0,
        LinkClass.BEYOND_LIMITS: 0,
        LinkClass.NOT_CRAWLED: 1,
        LinkClass.REDIRECT_ERROR: 0,
        LinkClass.OK: 4,
        LinkClass.BROKEN: 2,
        LinkClass.SERVER_ERROR: 1,
        LinkClass.HTTP_ERROR: 0,
        LinkClass.ROBOTS_BLOCKED: 1,
        LinkClass.UNREACHABLE: 1,
    }
    assert (stats.links_broken, stats.links_verified, stats.links_unverified) == (2, 7, 4)
    assert stats.top_linked[0] == TopLinkedPage(url_of("gone"), None, 2)
    assert [(path(top.url), top.in_links) for top in stats.top_linked[1:]] == [
        (name, 1)
        for name in ("docs", "down", "file.pdf", "home", "new-target", "old", "secret", "slow")
    ]
    assert {path(top.url): top.page_id for top in stats.top_linked if top.page_id} == {
        "docs": docs,
        "home": home,
    }


async def test_every_finished_request_is_counted_in_exactly_one_status_bucket(
    database: Database,
) -> None:
    """A 101 can be a final response (the fetcher records it as an HTTP error); before the
    review it fell in no bucket, and the buckets did not add up."""
    job_id, _ = await new_job(database)
    for name, state, outcome, status in (
        ("switch", FETCHED, "http_error", 101),
        ("ok", FETCHED, "non_html", 200),
        ("unparsed", FAILED, "parse_error", 200),
        ("moved", FETCHED, "redirect", 302),
        ("gone", FETCHED, "http_error", 404),
        ("down", FETCHED, "http_error", 503),
        ("slow", FAILED, "read_timeout", None),
        ("secret", SKIPPED, "robots_disallowed", None),  # never requested: not counted
        ("retrying", PENDING, None, 503),  # not finished: not counted
    ):
        redirect = "ok" if outcome == "redirect" else None
        await add_url(
            database,
            job_id,
            name,
            state=state,
            outcome=outcome,
            http_status=status,
            redirect_to=redirect,
        )

    async with database.session() as session:
        stats = await crawl_stats(session, job_id)

    buckets = (
        stats.status_1xx,
        stats.status_2xx,
        stats.status_3xx,
        stats.status_4xx,
        stats.status_5xx,
        stats.no_response,
    )
    assert buckets == (1, 2, 1, 1, 1, 1)
    assert sum(buckets) == stats.urls_by_state[FETCHED] + stats.urls_by_state[FAILED]


async def test_statistics_of_a_job_that_has_not_started_crawling(database: Database) -> None:
    job_id, _ = await new_job(database)

    async with database.session() as session:
        stats = await crawl_stats(session, job_id)

    assert (stats.urls_discovered, stats.urls_admitted, stats.urls_by_state[PENDING]) == (1, 1, 1)
    assert (stats.pages_stored, stats.no_response, stats.status_2xx) == (0, 0, 0)
    assert stats.depths == [DepthCount(0, 1, 0, 0)]
    assert (stats.indexing.documents, stats.links_total, stats.internal_targets) == (0, 0, 0)
    assert set(stats.links_by_class.values()) == {0}
    assert (stats.links_unverified, stats.top_linked) == (0, [])


async def test_statistics_come_from_one_snapshot(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another transaction stores a page with links between two of the statistics' statements:
    none of the counts sees it (REPEATABLE READ). At READ COMMITTED the link totals, read after
    the indexing counts, would include the late page while the URL counts would not."""
    job_id, _ = await new_job(database)
    page = await add_page(database, job_id, path="a")
    await add_links(database, job_id, page, [Link("x")])
    original = read_job_indexing  # the function the statistics call

    async def read_then_store_another_page(session: Any, job: uuid.UUID) -> IndexingProgress | None:
        progress = await original(session, job)
        late = await add_page(database, job_id, path="late")
        await add_links(database, job_id, late, [Link("y"), Link("z")])
        return progress

    monkeypatch.setattr(site_analysis, "read_job_indexing", read_then_store_another_page)
    async with database.session() as session:
        stats = await crawl_stats(session, job_id)
    monkeypatch.undo()

    assert (stats.urls_admitted, stats.pages_stored, stats.links_total) == (2, 1, 1)
    assert stats.indexing.pending == 1
    async with database.session() as session:
        after = await crawl_stats(session, job_id)
    assert (after.urls_admitted, after.pages_stored, after.links_total) == (3, 2, 3)


# --- Jobs, errors and limits ---------------------------------------------------------------------


async def test_one_job_never_sees_another_jobs_rows(database: Database) -> None:
    """Both jobs know the same URLs; job 1's links are judged by job 1's results only."""
    first, _ = await new_job(database)
    second, _ = await new_job(database)
    mine = await add_page(database, first, path="a")
    theirs = await add_page(database, second, path="a")
    await add_page(database, second, path="b")
    await add_url(database, second, "gone", state=FETCHED, outcome="http_error", http_status=404)
    await add_links(database, first, mine, [Link("gone"), Link("b")])
    await add_links(database, second, theirs, [Link("gone"), Link("b")])

    async with database.session() as session:
        links = await list_links(session, first, limit=10)
        foreign_page = await list_links(session, first, limit=10, source_page_id=theirs)
        broken = await list_broken_links(session, first, limit=10)
        pages = await list_pages(session, first, limit=10)
        graph = await link_graph(session, first, max_nodes=10, max_edges=10)
        stats = await crawl_stats(session, first)
        theirs_broken = await list_broken_links(session, second, limit=10)

    assert [
        (path(link.target_url), link.classification, link.target_page_id) for link in links.links
    ] == [("gone", LinkClass.NOT_CRAWLED, None), ("b", LinkClass.NOT_CRAWLED, None)]
    assert foreign_page.links == []
    assert (broken.total, broken.links) == (0, [])
    assert [page.page_id for page in pages.pages] == [mine]
    assert [(path(n.url), n.page_id) for n in graph.nodes] == [
        ("", None),
        ("a", mine),
        ("b", None),
        ("gone", None),
    ]
    assert (stats.urls_admitted, stats.pages_stored, stats.links_total) == (2, 1, 2)
    assert [link.source_page_id for link in theirs_broken.links] == [theirs]


async def test_an_unknown_job_is_not_found(database: Database) -> None:
    unknown = uuid.uuid4()
    async with database.session() as session:
        with pytest.raises(NotFoundError):
            await list_pages(session, unknown, limit=10)
        with pytest.raises(NotFoundError):
            await list_links(session, unknown, limit=10)
        with pytest.raises(NotFoundError):
            await list_broken_links(session, unknown, limit=10)
        with pytest.raises(NotFoundError):
            await link_graph(session, unknown, max_nodes=10, max_edges=10)
        with pytest.raises(NotFoundError):
            await crawl_stats(session, unknown)


async def test_an_analysis_transaction_is_read_only_bounded_and_planned_per_call(
    database: Database,
) -> None:
    """Every use case runs in _read_only: one read-only REPEATABLE READ snapshot, with the
    statement timeout and custom plans (a generic plan was measured up to 8 times slower;
    benchmarks/results/phase8-analysis.md). None of the settings outlives the transaction."""
    job_id, _ = await new_job(database)

    async def setting(session: Any, name: str) -> str:
        value: str = await session.scalar(text(f"SELECT current_setting('{name}')"))
        return value

    async with database.session() as session:
        async with site_analysis._read_only(session, job_id, 2.5) as job:
            assert job.id == job_id
            assert await setting(session, "transaction_isolation") == "repeatable read"
            assert await setting(session, "transaction_read_only") == "on"
            assert await setting(session, "statement_timeout") == "2500ms"
            assert await setting(session, "plan_cache_mode") == "force_custom_plan"
        assert await setting(session, "plan_cache_mode") == "auto"
        assert await setting(session, "statement_timeout") != "2500ms"


async def test_an_analysis_that_waits_too_long_is_stopped_by_postgresql(
    database: Database,
) -> None:
    """A lock held elsewhere (an exclusive table lock, as a migration may take) makes the query
    wait; its statement timeout stops it."""
    job_id, _ = await new_job(database)
    async with database.transaction() as blocker:
        await blocker.execute(text("LOCK TABLE page_links IN ACCESS EXCLUSIVE MODE"))
        began = time.monotonic()
        with pytest.raises(AnalysisTimeoutError):
            async with database.session() as session:
                await list_links(session, job_id, limit=10, timeout_seconds=0.3)
        assert time.monotonic() - began < 2.0
    async with database.session() as session:
        assert (await list_links(session, job_id, limit=10)).links == []


async def test_an_analysis_the_server_does_not_stop_is_stopped_by_the_service(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the server-side limit (the statement is replaced), the service's own deadline
    stops the waiting query, and the connection is usable afterwards."""
    job_id, _ = await new_job(database)
    monkeypatch.setattr(site_analysis, "_STATEMENT_TIMEOUT", text("SELECT CAST(:timeout AS text)"))
    async with database.transaction() as blocker:
        await blocker.execute(text("LOCK TABLE page_links IN ACCESS EXCLUSIVE MODE"))
        began = time.monotonic()
        with pytest.raises(AnalysisTimeoutError):
            async with database.session() as session:
                await crawl_stats(session, job_id, timeout_seconds=0.3)
        assert time.monotonic() - began < 2.0
    async with database.session() as session:
        assert (await crawl_stats(session, job_id)).links_total == 0
