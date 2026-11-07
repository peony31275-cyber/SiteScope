"""The crawl-state use cases against real PostgreSQL (app/services/crawl_state.py).

Each test builds the rows it needs in the disposable test database. The concurrency tests run
several transactions at once on separate connections and check that locks, conditional updates
and unique constraints keep the invariants of docs/architecture.md, section 11.
"""

import asyncio
import dataclasses
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.errors import NotFoundError
from app.db.models import (
    CrawlEvent,
    CrawlJob,
    CrawlUrl,
    JobStatus,
    Page,
    PageLink,
    SearchDocument,
    UrlState,
)
from app.db.session import Database
from app.services.crawl_state import (
    EXHAUSTED_OUTCOME,
    AdvanceAction,
    Lease,
    LinkRecord,
    PageRecord,
    RecordStatus,
    UrlResult,
    activate_job,
    advance_job,
    claim_url,
    dispatch_urls,
    find_actionable_urls,
    record_result,
    recover_exhausted_url,
)

pytestmark = pytest.mark.anyio

SEED = "https://example.test/"
HOPS = 5  # the redirect hop limit used by these tests


# --- Helpers -------------------------------------------------------------------------------------


async def new_job(
    database: Database,
    *,
    status: JobStatus = JobStatus.RUNNING,
    max_pages: int = 100,
    max_depth: int = 3,
    extra: tuple[tuple[str, int], ...] = (),
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """A job with its seed and ``extra`` (url, depth) rows, all pending. admitted_count matches."""
    async with database.transaction() as session:
        job = CrawlJob(
            seed_url=SEED,
            allowed_hosts=["example.test"],
            max_depth=max_depth,
            max_pages=max_pages,
            status=status.value,
            admitted_count=1 + len(extra),
            started_at=func.now() if status is not JobStatus.QUEUED else None,
        )
        session.add(job)
        await session.flush()
        urls = {SEED: CrawlUrl(job_id=job.id, normalized_url=SEED, depth=0)}
        for url, depth in extra:
            urls[url] = CrawlUrl(job_id=job.id, normalized_url=url, depth=depth)
        session.add_all(urls.values())
        await session.flush()
        return job.id, {url: row.id for url, row in urls.items()}


async def dispatch(database: Database, job_id: uuid.UUID, limit: int = 100) -> list[uuid.UUID]:
    async with database.session() as session:
        return await dispatch_urls(session, job_id, limit=limit)


async def claim(
    database: Database,
    url_id: uuid.UUID,
    owner: str = "worker-a",
    *,
    lease_seconds: float = 60.0,
    max_attempts: int = 3,
) -> Lease | None:
    async with database.session() as session:
        return await claim_url(
            session, url_id, owner=owner, lease_seconds=lease_seconds, max_attempts=max_attempts
        )


async def leased(database: Database, job_id: uuid.UUID, url_id: uuid.UUID, owner: str) -> Lease:
    """Dispatch the job's due URLs and claim ``url_id``."""
    await dispatch(database, job_id)
    lease = await claim(database, url_id, owner)
    assert lease is not None
    return lease


async def record(database: Database, lease: Lease, result: UrlResult) -> Any:
    async with database.session() as session:
        return await record_result(session, lease, result, max_redirect_hops=HOPS)


async def advance(database: Database, job_id: uuid.UUID) -> Any:
    async with database.session() as session:
        return await advance_job(session, job_id)


async def expire_lease(database: Database, url_id: uuid.UUID) -> None:
    """Move the lease expiry into the past by the database clock (a renewal, allowed)."""
    async with database.transaction() as session:
        await session.execute(
            text(
                "UPDATE crawl_urls SET lease_expires_at = clock_timestamp() - interval '1 second'"
                " WHERE id = :id AND state = 'in_progress'"
            ),
            {"id": url_id},
        )


async def url_row(database: Database, url_id: uuid.UUID) -> CrawlUrl:
    async with database.session() as session:
        row = await session.get(CrawlUrl, url_id)
        assert row is not None
        return row


async def job_row(database: Database, job_id: uuid.UUID) -> CrawlJob:
    async with database.session() as session:
        row = await session.get(CrawlJob, job_id)
        assert row is not None
        return row


async def count(database: Database, model: Any, **where: Any) -> int:
    async with database.session() as session:
        statement = select(func.count()).select_from(model)
        for column, value in where.items():
            statement = statement.where(getattr(model, column) == value)
        return int(await session.scalar(statement) or 0)


async def events(database: Database, job_id: uuid.UUID) -> list[CrawlEvent]:
    async with database.session() as session:
        rows = await session.scalars(
            select(CrawlEvent).where(CrawlEvent.job_id == job_id).order_by(CrawlEvent.id)
        )
        return list(rows)


async def urls_by_depth(database: Database, job_id: uuid.UUID) -> dict[str, int]:
    async with database.session() as session:
        rows = await session.execute(
            select(CrawlUrl.normalized_url, CrawlUrl.depth).where(CrawlUrl.job_id == job_id)
        )
        return {row.normalized_url: row.depth for row in rows}


def html_page(*links: LinkRecord, noindex: bool = False, nofollow: bool = False) -> PageRecord:
    return PageRecord(
        http_status=200,
        content_type="text/html",
        title="Title",
        description="Description",
        headings="Heading",
        text="Some text",
        byte_size=1234,
        fetch_duration_ms=12,
        noindex=noindex,
        nofollow=nofollow,
        links=links,
    )


def link(
    path: str, position: int, *, internal: bool = True, nofollow: bool = False, host: str = ""
) -> LinkRecord:
    return LinkRecord(
        url=f"https://{host or 'example.test'}{path}",
        position=position,
        anchor_text=f"anchor {position}",
        nofollow=nofollow,
        is_internal=internal,
    )


def stored(*links: LinkRecord, **flags: bool) -> UrlResult:
    return UrlResult(
        state=UrlState.FETCHED,
        outcome="html_stored",
        http_status=200,
        page=html_page(*links, **flags),
    )


def redirect(target: str, *, follow: bool = True) -> UrlResult:
    return UrlResult(
        state=UrlState.FETCHED,
        outcome="redirect",
        http_status=301,
        redirect_target=target,
        follow_redirect=follow,
    )


async def fetch_level(database: Database, job_id: uuid.UUID, pages: dict[str, UrlResult]) -> None:
    """Dispatch, claim and record a result for every URL named in ``pages``."""
    await dispatch(database, job_id)
    async with database.session() as session:
        ids = dict(
            (
                await session.execute(
                    select(CrawlUrl.normalized_url, CrawlUrl.id).where(CrawlUrl.job_id == job_id)
                )
            ).all()
        )
    for url, result in pages.items():
        lease = await claim(database, ids[url])
        assert lease is not None, url
        assert (await record(database, lease, result)).status is RecordStatus.RECORDED


@contextmanager
def captured_sql(database: Database) -> Iterator[list[tuple[str, Any]]]:
    """Record every SQL statement (and its parameters) sent through the engine."""
    statements: list[tuple[str, Any]] = []

    def before(conn: Any, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        statements.append((statement, parameters))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


@pytest.fixture
def db(database: Database) -> Database:
    """A short name for the test database."""
    return database


# --- T2 activate, T3 dispatch --------------------------------------------------------------------


async def test_activation_moves_a_queued_job_to_running_once(db: Database) -> None:
    job_id, _ = await new_job(db, status=JobStatus.QUEUED)
    async with db.session() as session:
        assert await activate_job(session, job_id) is True
    async with db.session() as session:
        assert await activate_job(session, job_id) is False
    job = await job_row(db, job_id)
    assert job.status == "running"
    assert job.started_at is not None
    assert [e.event_type for e in await events(db, job_id)] == ["job.started"]


async def test_dispatch_releases_only_due_pending_urls_of_the_current_level(db: Database) -> None:
    job_id, ids = await new_job(
        db, extra=(("https://example.test/same", 0), ("https://example.test/next", 1))
    )
    async with db.transaction() as session:  # one URL waits for a later retry time
        await session.execute(
            text(
                "UPDATE crawl_urls SET next_attempt_at = now() + interval '1 hour' WHERE id = :id"
            ),
            {"id": ids["https://example.test/same"]},
        )
    assert await dispatch(db, job_id) == [ids[SEED]]
    assert await dispatch(db, job_id) == []  # already queued
    seed = await url_row(db, ids[SEED])
    assert seed.state == "queued"
    assert seed.dispatched_at is not None
    assert (await url_row(db, ids["https://example.test/next"])).state == "pending"


async def test_dispatch_ignores_jobs_that_are_not_running(db: Database) -> None:
    job_id, ids = await new_job(db, status=JobStatus.QUEUED)
    assert await dispatch(db, job_id) == []
    assert (await url_row(db, ids[SEED])).state == "pending"


async def test_concurrent_dispatchers_never_take_the_same_url(db: Database) -> None:
    extra = tuple((f"https://example.test/p{n}", 0) for n in range(60))
    job_id, _ = await new_job(db, extra=extra)
    batches = await asyncio.gather(*(dispatch(db, job_id, limit=15) for _ in range(6)))
    taken = [url_id for batch in batches for url_id in batch]
    assert len(taken) == len(set(taken)) == 61


# --- T4 claim ------------------------------------------------------------------------------------


async def test_a_claim_issues_a_new_token_and_counts_an_attempt(db: Database) -> None:
    job_id, ids = await new_job(db)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    assert (lease.token, lease.attempt, lease.depth, lease.redirect_hops) == (1, 1, 0, 0)
    assert lease.url == SEED
    assert lease.allowed_hosts == ("example.test",)
    assert lease.takeover is False
    row = await url_row(db, ids[SEED])
    assert (row.state, row.lease_owner, row.lease_epoch) == ("in_progress", "worker-a", 1)
    assert await claim(db, ids[SEED], "worker-b") is None  # a live lease is never taken


async def test_only_queued_urls_of_running_jobs_are_claimed(db: Database) -> None:
    job_id, ids = await new_job(db)
    assert await claim(db, ids[SEED]) is None  # pending: not dispatched yet
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE crawl_urls SET state = 'queued', dispatched_at = now() WHERE id = :id"),
            {"id": ids[SEED]},
        )
        await session.execute(
            text(
                "UPDATE crawl_jobs SET status = 'failed', failure_reason = 'test',"
                " finished_at = now() WHERE id = :id"
            ),
            {"id": job_id},
        )
    assert await claim(db, ids[SEED]) is None


async def test_an_expired_lease_is_taken_over_with_a_new_token(db: Database) -> None:
    job_id, ids = await new_job(db)
    first = await leased(db, job_id, ids[SEED], "worker-a")
    await expire_lease(db, ids[SEED])
    second = await claim(db, ids[SEED], "worker-b")
    assert second is not None
    assert (second.token, second.attempt, second.takeover) == (first.token + 1, 2, True)
    recovered = [e for e in await events(db, job_id) if e.event_type == "url.recovered"]
    assert len(recovered) == 1
    assert recovered[0].worker_id == "worker-b"
    assert recovered[0].lease_epoch == second.token
    assert recovered[0].detail == {"previous_owner": "worker-a", "attempt": 2}


async def test_claims_stop_when_the_attempts_are_used_up(db: Database) -> None:
    job_id, ids = await new_job(db)
    await dispatch(db, job_id)
    for attempt in range(1, 3):
        lease = await claim(db, ids[SEED], f"worker-{attempt}", max_attempts=2)
        assert lease is not None
        assert lease.attempt == attempt
        await expire_lease(db, ids[SEED])
    assert await claim(db, ids[SEED], "worker-3", max_attempts=2) is None


# --- T5 fenced results ---------------------------------------------------------------------------


async def test_a_result_stores_the_page_links_and_search_document_atomically(db: Database) -> None:
    job_id, ids = await new_job(db)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    links = (
        link("/a", 0),
        link("/b", 1, nofollow=True),
        link("/c", 2, internal=False, host="other.test"),
    )
    recorded = await record(db, lease, stored(*links))
    assert recorded.status is RecordStatus.RECORDED
    assert recorded.page_id is not None

    row = await url_row(db, ids[SEED])
    assert (row.state, row.outcome, row.http_status) == ("fetched", "html_stored", 200)
    assert row.result_epoch == row.lease_epoch == lease.token
    assert (row.lease_owner, row.lease_expires_at) == (None, None)
    assert row.finished_at is not None
    async with db.session() as session:
        page = await session.get(Page, recorded.page_id)
        assert page is not None
        assert (page.normalized_url, page.title, page.robots_noindex) == (SEED, "Title", False)
        assert len(page.content_hash) == 32
        stored_links = (
            await session.execute(
                select(
                    PageLink.target_normalized_url,
                    PageLink.position,
                    PageLink.nofollow,
                    PageLink.is_internal,
                )
                .where(PageLink.source_page_id == page.id)
                .order_by(PageLink.position)
            )
        ).all()
        assert [tuple(r) for r in stored_links] == [
            ("https://example.test/a", 0, False, True),
            ("https://example.test/b", 1, True, True),
            ("https://other.test/c", 2, False, False),
        ]
        document = await session.get(SearchDocument, page.id)
        assert document is not None
        assert document.status == "pending"


async def test_a_noindex_page_gets_no_search_document(db: Database) -> None:
    job_id, ids = await new_job(db)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    recorded = await record(db, lease, stored(link("/a", 0), noindex=True, nofollow=True))
    async with db.session() as session:
        page = await session.get(Page, recorded.page_id)
        assert page is not None
        assert (page.robots_noindex, page.robots_nofollow) == (True, True)
    assert await count(db, SearchDocument, job_id=job_id) == 0
    assert await count(db, PageLink, job_id=job_id) == 1  # links are still recorded


async def test_a_stale_worker_is_fenced_out_and_writes_nothing(db: Database) -> None:
    job_id, ids = await new_job(db)
    stale = await leased(db, job_id, ids[SEED], "worker-a")
    await expire_lease(db, ids[SEED])
    current = await claim(db, ids[SEED], "worker-b")
    assert current is not None

    rejected = await record(db, stale, stored(link("/a", 0)))
    assert rejected.status is RecordStatus.FENCED
    assert await count(db, Page, job_id=job_id) == 0
    assert await count(db, PageLink, job_id=job_id) == 0
    assert await count(db, SearchDocument, job_id=job_id) == 0
    row = await url_row(db, ids[SEED])
    assert (row.state, row.lease_owner, row.result_epoch) == ("in_progress", "worker-b", None)

    assert (await record(db, current, stored())).status is RecordStatus.RECORDED
    assert (await record(db, current, stored())).status is RecordStatus.FENCED  # only once


async def test_a_wrong_token_is_rejected_even_from_the_lease_owner(db: Database) -> None:
    job_id, ids = await new_job(db)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    for token in (lease.token - 1, lease.token + 1):
        forged = dataclasses.replace(lease, token=token)
        assert (await record(db, forged, stored(link("/a", 0)))).status is RecordStatus.FENCED
    assert await count(db, Page, job_id=job_id) == 0
    assert (await record(db, lease, stored())).status is RecordStatus.RECORDED


async def test_the_result_statement_sends_the_workers_own_token(db: Database) -> None:
    """ADR-0011: result_epoch is bound to the claimant's token, never copied from the row."""
    job_id, ids = await new_job(db)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    with captured_sql(db) as statements:
        await record(db, lease, stored())
    updates = [
        (sql, params) for sql, params in statements if sql.lstrip().startswith("UPDATE crawl_urls")
    ]
    assert len(updates) == 1
    sql, params = updates[0]
    assert not re.search(r"result_epoch\s*=\s*(crawl_urls\.)?lease_epoch", sql)
    set_clause, where_clause = sql.split(" WHERE ", 1)
    result_param = re.search(r"result_epoch=\$(\d+)", set_clause.replace(" ", ""))
    fence_param = re.search(r"crawl_urls\.lease_epoch=\$(\d+)", where_clause.replace(" ", ""))
    assert result_param is not None
    assert fence_param is not None
    assert params[int(result_param.group(1)) - 1] == lease.token
    assert params[int(fence_param.group(1)) - 1] == lease.token


async def test_a_failing_write_rolls_back_the_whole_result(db: Database) -> None:
    job_id, ids = await new_job(db)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    broken = LinkRecord(
        url="not a url", position=0, anchor_text=None, nofollow=False, is_internal=True
    )
    with pytest.raises(IntegrityError):
        await record(db, lease, stored(broken))
    row = await url_row(db, ids[SEED])
    assert (row.state, row.lease_epoch, row.result_epoch) == ("in_progress", lease.token, None)
    assert await count(db, Page, job_id=job_id) == 0
    # The lease is intact: the worker can still record a valid result.
    assert (await record(db, lease, stored(link("/a", 0)))).status is RecordStatus.RECORDED


async def test_text_is_made_storable_and_odd_statuses_are_not_stored(db: Database) -> None:
    job_id, ids = await new_job(db)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    page = PageRecord(
        http_status=200,
        content_type="text/html",
        title="a\x00b",
        description=None,
        headings="",
        text="text\x00with NUL",
        byte_size=10,
        fetch_duration_ms=None,
        noindex=False,
        nofollow=False,
        links=(LinkRecord("https://example.test/x", 0, "x\x00", False, True),),
    )
    recorded = await record(db, lease, UrlResult(UrlState.FETCHED, "html_stored", 200, page=page))
    async with db.session() as session:
        stored_page = await session.get(Page, recorded.page_id)
        assert stored_page is not None
        assert (stored_page.title, stored_page.extracted_text) == ("a�b", "text�with NUL")

    other_job, other_ids = await new_job(db)
    lease = await leased(db, other_job, other_ids[SEED], "worker-a")
    error = UrlResult(UrlState.FETCHED, "http_error", 999, error="HTTP 999 " + "x" * 3000)
    await record(db, lease, error)
    row = await url_row(db, other_ids[SEED])
    assert row.http_status is None
    assert row.last_error is not None
    assert len(row.last_error) == 2000


# --- Redirect admission --------------------------------------------------------------------------


async def test_a_redirect_target_is_admitted_at_the_same_depth(db: Database) -> None:
    job_id, ids = await new_job(db)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    recorded = await record(db, lease, redirect("https://example.test/new"))
    assert recorded.redirect_url_id is not None
    target = await url_row(db, recorded.redirect_url_id)
    assert (target.normalized_url, target.depth, target.redirect_hops) == (
        "https://example.test/new",
        0,
        1,
    )
    assert (target.state, target.discovered_from_url_id) == ("pending", ids[SEED])
    seed = await url_row(db, ids[SEED])
    assert (seed.outcome, seed.http_status, seed.redirect_target) == (
        "redirect",
        301,
        "https://example.test/new",
    )
    assert (await job_row(db, job_id)).admitted_count == 2


@pytest.mark.parametrize(
    ("case", "result"),
    [
        ("not followed", redirect("https://example.test/new", follow=False)),
        ("other host", redirect("https://other.test/new")),  # the service re-checks the scope
        ("already known", redirect(SEED)),
    ],
)
async def test_redirect_targets_that_are_not_admitted(
    db: Database, case: str, result: UrlResult
) -> None:
    job_id, ids = await new_job(db)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    recorded = await record(db, lease, result)
    assert recorded.status is RecordStatus.RECORDED
    assert recorded.redirect_url_id is None
    assert await count(db, CrawlUrl, job_id=job_id) == 1
    assert (await job_row(db, job_id)).admitted_count == 1
    assert (await url_row(db, ids[SEED])).redirect_target == result.redirect_target


async def test_a_redirect_to_a_url_being_recorded_never_waits_for_it(db: Database) -> None:
    """Regression (deadlock): A redirects to B while B's own result transaction is still open.

    INSERT ... ON CONFLICT waits for a transaction that is updating the conflicting row. A's
    transaction holds the job row lock, so if B's transaction then asked for that lock (to admit
    a redirect of its own), the two would deadlock. A known target must be skipped without
    waiting.
    """
    b_url = "https://example.test/b"
    job_id, ids = await new_job(db, extra=((b_url, 0),))
    await dispatch(db, job_id)
    a = await claim(db, ids[SEED], "worker-a")
    b = await claim(db, ids[b_url], "worker-b")
    assert a is not None
    assert b is not None
    async with db.engine.connect() as other:
        transaction = await other.begin()
        # worker-b's fenced update, not committed yet.
        await other.execute(
            text(
                "UPDATE crawl_urls SET state = 'fetched', outcome = 'redirect', http_status = 301,"
                " result_epoch = :token, lease_owner = NULL, lease_expires_at = NULL,"
                " finished_at = clock_timestamp()"
                " WHERE id = :id AND state = 'in_progress' AND lease_epoch = :token"
            ),
            {"id": b.url_id, "token": b.token},
        )
        recorded = await asyncio.wait_for(record(db, a, redirect(b_url)), timeout=5)
        assert recorded.status is RecordStatus.RECORDED
        assert recorded.redirect_url_id is None  # B is already known to the job
        # worker-b can take the job row lock: A's transaction has finished.
        await asyncio.wait_for(
            other.execute(
                text("SELECT 1 FROM crawl_jobs WHERE id = :id FOR UPDATE"), {"id": job_id}
            ),
            timeout=5,
        )
        await transaction.rollback()
    assert (await job_row(db, job_id)).admitted_count == 2


async def test_redirects_stop_at_the_hop_limit(db: Database) -> None:
    job_id, ids = await new_job(db, extra=(("https://example.test/hop5", 0),))
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE crawl_urls SET redirect_hops = :hops WHERE id = :id"),
            {"hops": HOPS, "id": ids["https://example.test/hop5"]},
        )
    lease = await leased(db, job_id, ids["https://example.test/hop5"], "worker-a")
    recorded = await record(db, lease, redirect("https://example.test/hop6"))
    assert recorded.redirect_url_id is None
    assert await count(db, CrawlUrl, job_id=job_id) == 2


async def test_a_redirect_never_exceeds_the_page_budget(db: Database) -> None:
    job_id, ids = await new_job(db, max_pages=1)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    recorded = await record(db, lease, redirect("https://example.test/new"))
    assert recorded.redirect_url_id is None
    assert (await job_row(db, job_id)).admitted_count == 1


async def test_concurrent_redirects_fill_the_last_budget_slot_exactly_once(db: Database) -> None:
    sources = [f"https://example.test/s{n}" for n in range(8)]
    job_id, ids = await new_job(
        db, max_pages=len(sources) + 2, extra=tuple((s, 0) for s in sources)
    )
    await dispatch(db, job_id)
    leases = [await claim(db, ids[source], f"worker-{n}") for n, source in enumerate(sources)]
    results = await asyncio.gather(
        *(
            record(db, lease, redirect(f"https://example.test/target{n}"))
            for n, lease in enumerate(leases)
            if lease is not None
        )
    )
    assert all(r.status is RecordStatus.RECORDED for r in results)
    assert sum(r.redirect_url_id is not None for r in results) == 1
    job = await job_row(db, job_id)
    assert job.admitted_count == job.max_pages == await count(db, CrawlUrl, job_id=job_id)


async def test_concurrent_redirects_to_one_target_admit_it_once(db: Database) -> None:
    sources = [f"https://example.test/s{n}" for n in range(8)]
    job_id, ids = await new_job(db, extra=tuple((s, 0) for s in sources))
    await dispatch(db, job_id)
    leases = [await claim(db, ids[source], f"worker-{n}") for n, source in enumerate(sources)]
    results = await asyncio.gather(
        *(record(db, lease, redirect("https://example.test/same")) for lease in leases if lease)
    )
    assert sum(r.redirect_url_id is not None for r in results) == 1
    assert await count(db, CrawlUrl, job_id=job_id, normalized_url="https://example.test/same") == 1
    assert (await job_row(db, job_id)).admitted_count == len(sources) + 2


# --- T6 level advancement ------------------------------------------------------------------------


async def test_the_next_level_waits_until_the_current_one_is_done(db: Database) -> None:
    job_id, ids = await new_job(db)
    result = await advance(db, job_id)
    assert result.action is AdvanceAction.LEVEL_IN_PROGRESS
    await leased(db, job_id, ids[SEED], "worker-a")
    assert (await advance(db, job_id)).action is AdvanceAction.LEVEL_IN_PROGRESS
    assert (await job_row(db, job_id)).current_level == 0


async def test_advancement_admits_internal_followable_links_once(db: Database) -> None:
    job_id, _ = await new_job(db)
    await fetch_level(
        db,
        job_id,
        {
            SEED: stored(
                link("/b", 0),
                link("/", 1),  # the seed itself: already known
                link("/x", 2, internal=False, host="other.test"),
                link("/nofollow", 3, nofollow=True),
                link("/a", 4),
            )
        },
    )
    result = await advance(db, job_id)
    assert (result.action, result.level, result.admitted) == (AdvanceAction.ADVANCED, 1, 2)
    assert await urls_by_depth(db, job_id) == {
        SEED: 0,
        "https://example.test/b": 1,
        "https://example.test/a": 1,
    }
    job = await job_row(db, job_id)
    assert (job.current_level, job.admitted_count) == (1, 3)
    level_events = [e for e in await events(db, job_id) if e.event_type == "job.level_advanced"]
    assert [e.detail for e in level_events] == [{"level": 1, "admitted": 2, "budget_left": 97}]


async def test_the_budget_admits_a_deterministic_bfs_prefix(db: Database) -> None:
    # Two pages at depth 0: the seed and a redirect target. BFS order is (source URL, position).
    job_id, ids = await new_job(db, max_pages=5)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    await record(db, lease, redirect("https://example.test/a"))
    await fetch_level(
        db,
        job_id,
        {"https://example.test/a": stored(link("/b", 0), link("/c", 1), link("/z", 2))},
    )
    assert (await advance(db, job_id)).action is AdvanceAction.ADVANCED  # level 1: b, c, z
    await fetch_level(
        db,
        job_id,
        {
            "https://example.test/b": stored(link("/b2", 0), link("/b1", 1)),
            "https://example.test/c": stored(link("/c1", 0)),
            "https://example.test/z": stored(link("/b1", 0), link("/z1", 1)),
        },
    )
    result = await advance(db, job_id)
    assert result.admitted == 0  # the budget (5) is full: seed, a, b, c, z
    assert result.action is AdvanceAction.COMPLETED

    job2, ids2 = await new_job(db, max_pages=6)
    await fetch_level(db, job2, {SEED: stored(link("/b", 0), link("/c", 1), link("/z", 2))})
    await advance(db, job2)
    await fetch_level(
        db,
        job2,
        {
            "https://example.test/z": stored(link("/b1", 0), link("/z1", 1)),
            "https://example.test/c": stored(link("/c1", 0)),
            "https://example.test/b": stored(link("/b2", 0), link("/b1", 1)),
        },
    )
    result = await advance(db, job2)
    # Remaining budget 2. Candidates in BFS order: b2 (b, 0), b1 (b, 1), c1 (c, 0), z1 (z, 1).
    assert result.admitted == 2
    depths = await urls_by_depth(db, job2)
    assert {url for url, depth in depths.items() if depth == 2} == {
        "https://example.test/b2",
        "https://example.test/b1",
    }
    async with db.session() as session:
        b1 = (
            await session.execute(
                select(CrawlUrl.discovered_from_url_id).where(
                    CrawlUrl.job_id == job2, CrawlUrl.normalized_url == "https://example.test/b1"
                )
            )
        ).scalar_one()
        b = (
            await session.execute(
                select(CrawlUrl.id).where(
                    CrawlUrl.job_id == job2, CrawlUrl.normalized_url == "https://example.test/b"
                )
            )
        ).scalar_one()
    assert b1 == b  # its first source in BFS order, although z also links to it
    assert (await job_row(db, job2)).admitted_count == 6
    del ids2


async def test_a_nofollow_page_admits_none_of_its_links(db: Database) -> None:
    job_id, _ = await new_job(db)
    await fetch_level(db, job_id, {SEED: stored(link("/a", 0), nofollow=True)})
    result = await advance(db, job_id)
    assert result.action is AdvanceAction.COMPLETED
    assert await count(db, CrawlUrl, job_id=job_id) == 1


async def test_pages_at_max_depth_record_links_without_admitting_them(db: Database) -> None:
    job_id, _ = await new_job(db, max_depth=0)
    await fetch_level(db, job_id, {SEED: stored(link("/a", 0))})
    result = await advance(db, job_id)
    assert (result.action, result.level) == (AdvanceAction.COMPLETED, 0)
    assert await count(db, PageLink, job_id=job_id) == 1
    assert await count(db, CrawlUrl, job_id=job_id) == 1


async def test_a_job_completes_and_stays_completed(db: Database) -> None:
    job_id, _ = await new_job(db)
    await fetch_level(db, job_id, {SEED: stored()})
    result = await advance(db, job_id)
    assert (result.action, result.status) == (AdvanceAction.COMPLETED, JobStatus.COMPLETED)
    job = await job_row(db, job_id)
    assert (job.status, job.failure_reason) == ("completed", None)
    assert job.finished_at is not None
    assert (await advance(db, job_id)).action is AdvanceAction.NOT_RUNNING
    finished = [e for e in await events(db, job_id) if e.event_type == "job.completed"]
    assert [e.detail for e in finished] == [{"levels": 1, "admitted": 1, "pages": 1}]


async def test_a_job_fails_when_its_seed_was_not_fetched(db: Database) -> None:
    job_id, _ = await new_job(db)
    await fetch_level(
        db, job_id, {SEED: UrlResult(UrlState.SKIPPED, "robots_disallowed", error="robots.txt")}
    )
    result = await advance(db, job_id)
    assert result.action is AdvanceAction.FAILED
    job = await job_row(db, job_id)
    assert job.status == "failed"
    assert job.failure_reason == "the seed URL was not fetched (skipped: robots_disallowed)"


async def test_concurrent_advances_admit_a_level_exactly_once(db: Database) -> None:
    job_id, _ = await new_job(db)
    await fetch_level(db, job_id, {SEED: stored(*(link(f"/p{n}", n) for n in range(20)))})
    results = await asyncio.gather(*(advance(db, job_id) for _ in range(8)))
    actions = sorted(r.action.value for r in results)
    assert actions.count("advanced") == 1
    assert set(actions) == {"advanced", "level_in_progress"}
    job = await job_row(db, job_id)
    assert job.admitted_count == 21 == await count(db, CrawlUrl, job_id=job_id)
    assert job.current_level == 1


async def test_a_reused_session_advances_from_the_current_job_row(db: Database) -> None:
    """Regression (review): the session's identity map holds an old copy of the job.

    Before the fix, advance_job read the job through the ORM entity, got that stale copy (level
    0) although the row was locked, and completed the job instead of admitting level 2.
    """
    job_id, _ = await new_job(db)
    await fetch_level(db, job_id, {SEED: stored(link("/a", 0))})
    async with db.session() as session:
        stale = await session.get(CrawlJob, job_id)  # now in this session's identity map
        assert stale is not None
        assert stale.current_level == 0
        await session.commit()  # objects survive the commit (expire_on_commit=False)
        first = await advance_job(session, job_id)
        assert (first.action, first.level) == (AdvanceAction.ADVANCED, 1)
        await fetch_level(db, job_id, {"https://example.test/a": stored(link("/b", 0))})
        second = await advance_job(session, job_id)
        assert (second.action, second.level, second.admitted) == (AdvanceAction.ADVANCED, 2, 1)
    assert await urls_by_depth(db, job_id) == {
        SEED: 0,
        "https://example.test/a": 1,
        "https://example.test/b": 2,
    }


async def test_job_timestamps_survive_a_backward_clock_step(db: Database) -> None:
    """Regression: the database clock stepped back about 1 s during a test run (WSL 2), and
    finishing the job violated ck_crawl_jobs_timestamps_ordered. Simulated here by a creation
    time in the future, i.e. a clock that has since stepped back by an hour."""
    job_id, _ = await new_job(db, status=JobStatus.QUEUED)
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE crawl_jobs SET created_at = now() + interval '1 hour' WHERE id = :id"),
            {"id": job_id},
        )
    async with db.session() as session:
        assert await activate_job(session, job_id)
    await fetch_level(db, job_id, {SEED: stored()})
    assert (await advance(db, job_id)).action is AdvanceAction.COMPLETED
    job = await job_row(db, job_id)
    assert job.started_at is not None
    assert job.finished_at is not None
    assert job.created_at <= job.started_at <= job.finished_at


async def test_advancing_an_unknown_job_is_an_error(db: Database) -> None:
    with pytest.raises(NotFoundError):
        await advance(db, uuid.uuid4())


# --- Recovery ------------------------------------------------------------------------------------


async def test_actionable_urls_are_queued_or_expired_at_the_current_level(db: Database) -> None:
    job_id, ids = await new_job(
        db, extra=(("https://example.test/live", 0), ("https://example.test/later", 1))
    )
    await dispatch(db, job_id)
    live = await claim(db, ids["https://example.test/live"])
    assert live is not None
    async with db.session() as session:
        found = await find_actionable_urls(session, job_id, max_attempts=3, limit=10)
    assert [(u.url_id, u.exhausted) for u in found] == [(ids[SEED], False)]
    await expire_lease(db, ids["https://example.test/live"])
    async with db.session() as session:
        found = await find_actionable_urls(session, job_id, max_attempts=1, limit=10)
    assert {(u.url_id, u.exhausted) for u in found} == {
        (ids[SEED], False),
        (ids["https://example.test/live"], True),
    }


async def test_an_exhausted_url_fails_through_a_fenced_recovery(db: Database) -> None:
    job_id, ids = await new_job(db)
    await dispatch(db, job_id)
    stale = None
    for attempt in range(1, 4):
        stale = await claim(db, ids[SEED], f"worker-{attempt}", max_attempts=3)
        assert stale is not None
        await expire_lease(db, ids[SEED])
    assert stale is not None
    async with db.session() as session:
        assert await recover_exhausted_url(session, ids[SEED], owner="recovery", max_attempts=3)
    row = await url_row(db, ids[SEED])
    assert (row.state, row.outcome, row.attempt_count) == ("failed", EXHAUSTED_OUTCOME, 3)
    assert row.lease_epoch == row.result_epoch == stale.token + 1
    failed = [e for e in await events(db, job_id) if e.event_type == "url.failed"]
    assert len(failed) == 1
    assert failed[0].detail["previous_owner"] == "worker-3"
    # The last holder resumes after the recovery: its result is rejected.
    assert (await record(db, stale, stored(link("/a", 0)))).status is RecordStatus.FENCED
    assert await count(db, Page, job_id=job_id) == 0


async def test_a_live_or_unexhausted_lease_is_never_recovered(db: Database) -> None:
    job_id, ids = await new_job(db)
    lease = await leased(db, job_id, ids[SEED], "worker-a")
    async with db.session() as session:
        assert not await recover_exhausted_url(session, ids[SEED], owner="r", max_attempts=1)
    await expire_lease(db, ids[SEED])
    async with db.session() as session:
        assert not await recover_exhausted_url(session, ids[SEED], owner="r", max_attempts=2)
    row = await url_row(db, ids[SEED])
    assert (row.state, row.lease_epoch) == ("in_progress", lease.token)
