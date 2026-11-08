"""Helpers for the indexing tests (Phase 7): pages and search documents built directly.

Pages are inserted as a crawl would store them (a fetched crawl URL, its page, and a ``pending``
search document unless the page is ``noindex``), so the index services see realistic rows.
``change_content`` performs a content change the way ADR-0008 prescribes; no production code
changes stored content yet (there is no re-crawl), so the version tests drive it from here.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text

from app.db.models import SearchDocument
from app.db.session import Database
from app.queue.messages import IndexTask
from app.queue.streams import IndexStream
from app.services.search_index import (
    IndexOutcome,
    IndexPublisher,
    IndexTaskRef,
    dispatch_index_tasks,
    index_document,
    start_index_attempt,
)

INSERT_URL = text(
    """
    INSERT INTO crawl_urls (job_id, normalized_url, depth, state, result_epoch, outcome,
                            http_status, finished_at)
    VALUES (:job_id, :url, :depth, 'fetched', 0, 'html_stored', 200, clock_timestamp())
    RETURNING id
    """
)

INSERT_PAGE = text(
    """
    INSERT INTO pages (job_id, crawl_url_id, normalized_url, http_status, content_type, title,
                       meta_description, headings, extracted_text, content_hash, byte_size,
                       fetched_at, content_version, robots_noindex, robots_nofollow)
    VALUES (:job_id, :url_id, :url, 200, 'text/html', :title, :description, :headings, :body,
            :hash, :size, clock_timestamp(), :version, :noindex, :nofollow)
    RETURNING id
    """
)


async def add_page(
    database: Database,
    job_id: uuid.UUID,
    *,
    path: str | None = None,
    title: str | None = None,
    description: str | None = None,
    headings: str = "",
    body: str = "",
    noindex: bool = False,
    nofollow: bool = False,
    content_version: int = 1,
    host: str = "example.test",
    depth: int = 0,
) -> uuid.UUID:
    """A stored page of ``job_id`` at ``https://<host>/<path>`` (its crawl URL at ``depth``),
    with a pending search document (none for a noindex page); returns the page ID."""
    url = f"https://{host}/{path if path is not None else uuid.uuid4().hex}"
    digest = hashlib.sha256(f"{title}|{description}|{headings}|{body}".encode()).digest()
    async with database.transaction() as session:
        url_id = (
            await session.execute(INSERT_URL, {"job_id": job_id, "url": url, "depth": depth})
        ).scalar_one()
        page_id: uuid.UUID = (
            await session.execute(
                INSERT_PAGE,
                {
                    "job_id": job_id,
                    "url_id": url_id,
                    "url": url,
                    "title": title,
                    "description": description,
                    "headings": headings,
                    "body": body,
                    "hash": digest,
                    "size": len(body.encode()),
                    "version": content_version,
                    "noindex": noindex,
                    "nofollow": nofollow,
                },
            )
        ).scalar_one()
        if not noindex:
            await session.execute(
                text("INSERT INTO search_documents (page_id, job_id) VALUES (:page, :job)"),
                {"page": page_id, "job": job_id},
            )
    return page_id


CHANGE_PAGE = text(
    """
    UPDATE pages
    SET content_version = content_version + 1,
        title = coalesce(:title, title),
        extracted_text = coalesce(:body, extracted_text)
    WHERE id = :page_id
    RETURNING content_version
    """
)

RESET_DOCUMENT = text(
    """
    UPDATE search_documents
    SET status = 'pending', attempt_count = 0, last_error = NULL,
        lease_owner = NULL, lease_expires_at = NULL
    WHERE page_id = :page_id
    """
)


async def change_content(
    database: Database, page_id: uuid.UUID, *, title: str | None = None, body: str | None = None
) -> int:
    """A content change as ADR-0008 prescribes: one transaction increments the page's content
    version and sets its search document back to ``pending``. Returns the new version."""
    async with database.transaction() as session:
        version: int = (
            await session.execute(CHANGE_PAGE, {"page_id": page_id, "title": title, "body": body})
        ).scalar_one()
        await session.execute(RESET_DOCUMENT, {"page_id": page_id})
    return version


async def document(database: Database, page_id: uuid.UUID) -> SearchDocument:
    async with database.session() as session:
        row = await session.get(SearchDocument, page_id)
        assert row is not None
        await session.refresh(row)
        return row


async def lexemes(database: Database, page_id: uuid.UUID, weight: str | None = None) -> set[str]:
    """The lexemes of a document's vector (only those with ``weight``, if given)."""
    async with database.session() as session:
        rows = await session.execute(
            text(
                "SELECT lexeme FROM search_documents, unnest(search_vector) AS v"
                " WHERE page_id = :page_id"
                " AND (CAST(:weight AS text) IS NULL OR CAST(:weight AS text) = ANY(v.weights))"
            ),
            {"page_id": page_id, "weight": weight},
        )
        return {row.lexeme for row in rows}


async def finish_job(database: Database, job_id: uuid.UUID, status: str = "completed") -> None:
    """Mark a job finished (completed, failed or cancelled), as the scheduler would."""
    await execute(
        database,
        "UPDATE crawl_jobs SET status = :status, finished_at = clock_timestamp(),"
        " failure_reason = CASE WHEN :status = 'failed' THEN 'test' END,"
        " cancel_requested_at = CASE WHEN :status = 'cancelled' THEN clock_timestamp() END"
        " WHERE id = :id",
        id=job_id,
        status=status,
    )


async def execute(database: Database, sql: str, **params: Any) -> None:
    async with database.transaction() as session:
        await session.execute(text(sql), params)


async def scalar(database: Database, sql: str, **params: Any) -> Any:
    async with database.session() as session:
        return await session.scalar(text(sql), params)


def publisher(stream: IndexStream) -> IndexPublisher:
    """Publishes index task references as messages on ``stream``."""

    async def publish(tasks: Sequence[IndexTaskRef]) -> list[str]:
        return await stream.publish(
            [IndexTask(task.job_id, task.page_id, task.content_version) for task in tasks]
        )

    return publish


def ref(task: IndexTask) -> IndexTaskRef:
    return IndexTaskRef(task.job_id, task.page_id, task.content_version)


async def until_sessions_wait_for_locks(
    database: Database, count: int = 1, *, within: float = 5.0
) -> None:
    """Until PostgreSQL shows ``count`` sessions of the test database blocked on a lock: proof
    that the transactions wait, rather than merely not being scheduled yet."""
    async with asyncio.timeout(within):
        while True:
            async with database.session() as session:
                waiting = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            if waiting is not None and waiting >= count:
                return
            await asyncio.sleep(0.02)


async def index_everything(database: Database, stream: IndexStream) -> int:
    """Dispatch every pending document, then index each through the services, as one indexer
    would (without its process). Returns the documents indexed."""
    async with database.session() as session:
        await dispatch_index_tasks(session, limit=10_000, publish=publisher(stream))
    deliveries = await stream.read(f"indexer-{uuid.uuid4().hex[:6]}", count=10_000, block_ms=10)
    indexed = 0
    for delivery in deliveries:
        if delivery.task is None:
            continue
        task = ref(delivery.task)
        async with database.session() as session:
            attempt = await start_index_attempt(
                session, task, owner="indexer-test", lease_seconds=60, max_attempts=3
            )
        if attempt is None:
            continue
        async with database.session() as session:
            result = await index_document(session, task, max_body_chars=100_000, budget_seconds=20)
        if result.outcome is IndexOutcome.INDEXED:
            indexed += 1
        await stream.ack(delivery.entry_id)
    return indexed
