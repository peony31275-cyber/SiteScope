"""Helpers for the website-analysis tests (Phase 8): crawl results and links built directly.

Crawl URLs are inserted in the final states a crawl leaves them in (the same columns a fenced
result writes), so the analysis queries see realistic rows without running a crawl. Pages come
from ``tests/index_support.add_page``.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text

from app.db.models import TERMINAL_URL_STATES, WORKER_OUTCOME_STATES, UrlState
from app.db.session import Database

HOST = "example.test"


def url_of(path: str, host: str = HOST) -> str:
    return f"https://{host}/{path}"


async def add_url(
    database: Database,
    job_id: uuid.UUID,
    path: str,
    *,
    state: UrlState,
    outcome: str | None = None,
    http_status: int | None = None,
    redirect_to: str | None = None,
    depth: int = 1,
    host: str = HOST,
) -> uuid.UUID:
    """A crawl URL of ``job_id`` in ``state`` (a terminal state carries its result epoch and
    ``finished_at``, as a fenced result writes them); ``redirect_to`` is a path or a full URL."""
    target = None
    if redirect_to is not None:
        target = redirect_to if "://" in redirect_to else url_of(redirect_to, host)
    worker_outcome = state in WORKER_OUTCOME_STATES
    async with database.transaction() as session:
        url_id: uuid.UUID = (
            await session.execute(
                text(
                    """
                    INSERT INTO crawl_urls (job_id, normalized_url, depth, state, outcome,
                                            http_status, redirect_target, result_epoch,
                                            dispatched_at, finished_at)
                    VALUES (:job_id, :url, :depth, :state, :outcome, :http_status, :target,
                            :epoch,
                            CASE WHEN :queued THEN clock_timestamp() END,
                            CASE WHEN :terminal THEN clock_timestamp() END)
                    RETURNING id
                    """
                ),
                {
                    "job_id": job_id,
                    "url": url_of(path, host),
                    "depth": depth,
                    "state": state.value,
                    "outcome": outcome,
                    "http_status": http_status,
                    "target": target,
                    "epoch": 0 if worker_outcome else None,
                    "queued": state is UrlState.QUEUED,
                    "terminal": state in TERMINAL_URL_STATES,
                },
            )
        ).scalar_one()
    return url_id


@dataclass(frozen=True)
class Link:
    target: str  # a path (on HOST) or a full URL
    internal: bool = True
    nofollow: bool = False
    anchor: str | None = None


async def add_links(
    database: Database, job_id: uuid.UUID, page_id: uuid.UUID, links: Sequence[Link]
) -> None:
    """The page's links, in order (position 0, 1, ...)."""
    async with database.transaction() as session:
        for position, link in enumerate(links):
            target = link.target if "://" in link.target else url_of(link.target)
            await session.execute(
                text(
                    """
                    INSERT INTO page_links (source_page_id, target_normalized_url, job_id,
                                            anchor_text, position, is_internal, nofollow)
                    VALUES (:page_id, :target, :job_id, :anchor, :position, :internal,
                            :nofollow)
                    """
                ),
                {
                    "page_id": page_id,
                    "target": target,
                    "job_id": job_id,
                    "anchor": link.anchor,
                    "position": position,
                    "internal": link.internal,
                    "nofollow": link.nofollow,
                },
            )


async def set_job(database: Database, job_id: uuid.UUID, **columns: object) -> None:
    """Set columns of a crawl job (test setup only)."""
    assignments = ", ".join(f"{name} = :{name}" for name in columns)
    async with database.transaction() as session:
        await session.execute(
            text(f"UPDATE crawl_jobs SET {assignments} WHERE id = :job_id"),  # noqa: S608
            {"job_id": job_id, **columns},
        )
