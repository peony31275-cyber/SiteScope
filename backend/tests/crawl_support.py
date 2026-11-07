"""Helpers for tests that build crawl jobs directly in the test database (Phase 5 tests)."""

import uuid
from collections.abc import Sequence
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import func, select, text

from app.core.config import Settings
from app.db.models import (
    UNFINISHED_URL_STATES,
    WORKER_OUTCOME_STATES,
    CrawlEvent,
    CrawlJob,
    CrawlUrl,
    JobStatus,
    Page,
    SearchDocument,
    UrlState,
)
from app.db.session import Database
from app.services.crawl_jobs import create_crawl_job
from app.services.crawl_state import PageRecord, UrlResult

SEED = "https://example.test/"


async def create_local_job(
    database: Database,
    settings: Settings,
    *,
    host: str,
    ports: Sequence[int],
    port: int,
    max_depth: int,
    max_pages: int = 1000,
) -> uuid.UUID:
    """A queued job for a local site, created through the service the API uses."""
    approved = settings.model_copy(
        update={
            "crawl_allowed_hosts": (host,),
            "crawl_allowed_schemes": ("http",),
            "crawl_allowed_ports": tuple(ports),
            "crawl_max_depth_limit": 10,
            "crawl_max_pages_limit": 100_000,
        }
    )
    async with database.session() as session:
        job = await create_crawl_job(
            session,
            approved,
            seed_url=f"http://{host}:{port}/",
            max_depth=max_depth,
            max_pages=max_pages,
        )
    return job.id


async def assert_job_invariants(database: Database, job_id: uuid.UUID) -> None:
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
    # 3. Depths: a URL descends from a stored page one level up, or from a redirect at its own
    #    level.
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


async def stream_entries(redis: Redis, key: str) -> list[tuple[str, dict[str, str]]]:
    """A whole stream (``XRANGE - +``), typed: the client decodes replies to ``str``."""
    return cast(list[tuple[str, dict[str, str]]], await redis.xrange(key))


async def url_rows(database: Database, job_id: uuid.UUID) -> list[CrawlUrl]:
    async with database.session() as session:
        return list(await session.scalars(select(CrawlUrl).where(CrawlUrl.job_id == job_id)))


async def new_job(
    database: Database,
    *,
    status: JobStatus = JobStatus.RUNNING,
    urls: int = 0,
    max_pages: int = 100,
    max_depth: int = 3,
    host: str = "example.test",
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """A job with its seed and ``urls`` more pending URLs at depth 0; returns (job, URL IDs)."""
    seed = f"https://{host}/"
    async with database.transaction() as session:
        job = CrawlJob(
            seed_url=seed,
            allowed_hosts=[host],
            max_depth=max_depth,
            max_pages=max(max_pages, 1 + urls),
            status=status.value,
            admitted_count=1 + urls,
            started_at=func.now() if status is not JobStatus.QUEUED else None,
            cancel_requested_at=func.now() if status is JobStatus.CANCELLING else None,
        )
        session.add(job)
        await session.flush()
        rows = [CrawlUrl(job_id=job.id, normalized_url=seed, depth=0)]
        rows += [
            CrawlUrl(job_id=job.id, normalized_url=f"{seed}page-{index}", depth=0)
            for index in range(urls)
        ]
        session.add_all(rows)
        await session.flush()
        return job.id, [row.id for row in rows]


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


async def states(database: Database, job_id: uuid.UUID) -> dict[str, int]:
    async with database.session() as session:
        rows = await session.execute(
            select(CrawlUrl.state, func.count())
            .where(CrawlUrl.job_id == job_id)
            .group_by(CrawlUrl.state)
        )
        # Not dict(rows): a Result has keys(), so dict() would treat it as a mapping.
        return dict(rows.all())


async def events(database: Database, job_id: uuid.UUID, event_type: str) -> list[CrawlEvent]:
    async with database.session() as session:
        rows = await session.scalars(
            select(CrawlEvent)
            .where(CrawlEvent.job_id == job_id, CrawlEvent.event_type == event_type)
            .order_by(CrawlEvent.id)
        )
        return list(rows)


async def execute(database: Database, sql: str, **params: Any) -> None:
    async with database.transaction() as session:
        await session.execute(text(sql), params)


async def expire_lease(database: Database, url_id: uuid.UUID, *, seconds_ago: float = 1) -> None:
    """Move the lease expiry into the past by the database clock (a lease change, allowed)."""
    await execute(
        database,
        "UPDATE crawl_urls SET lease_expires_at = clock_timestamp() - make_interval(secs => :s)"
        " WHERE id = :id AND state = 'in_progress'",
        id=url_id,
        s=seconds_ago,
    )


async def age_dispatch(database: Database, url_id: uuid.UUID, *, seconds: float) -> None:
    """Pretend a URL was dispatched ``seconds`` ago (and, if in progress, its lease expired
    as long ago)."""
    await execute(
        database,
        "UPDATE crawl_urls SET dispatched_at = clock_timestamp() - make_interval(secs => :s),"
        " lease_expires_at = CASE WHEN state = 'in_progress'"
        " THEN clock_timestamp() - make_interval(secs => :s) END"
        " WHERE id = :id",
        id=url_id,
        s=seconds,
    )


def html_result(text_value: str = "Some text") -> UrlResult:
    return UrlResult(
        state=UrlState.FETCHED,
        outcome="html_stored",
        http_status=200,
        page=PageRecord(
            http_status=200,
            content_type="text/html",
            title="Title",
            description=None,
            headings="",
            text=text_value,
            byte_size=len(text_value),
            fetch_duration_ms=1,
            noindex=False,
            nofollow=False,
        ),
    )
