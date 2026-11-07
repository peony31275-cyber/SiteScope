"""Request and response bodies of the crawl job endpoints, and the listing cursor."""

import base64
import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import FieldViolation, InvalidInputError
from app.db.models import JobStatus, UrlState
from app.services.crawl_jobs import CrawlJobProgress, JobPosition

# As submitted; the normalized URL must also fit in 2048 bytes (app/crawler/urls.py).
SEED_URL_MAX_LENGTH = 4096


class CreateCrawlJobRequest(BaseModel):
    """A request to crawl one approved site, breadth-first from a seed URL."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"seed_url": "https://docs.example.com/", "max_depth": 3, "max_pages": 500}
            ]
        },
    )

    seed_url: Annotated[
        str,
        Field(
            strict=True,
            min_length=1,
            max_length=SEED_URL_MAX_LENGTH,
            description="Absolute http(s) URL on an approved host. It is normalized before "
            "it is stored (docs/architecture.md, section 8.1).",
        ),
    ]
    max_depth: Annotated[
        int,
        Field(
            strict=True,
            ge=0,
            description="Link hops to follow from the seed (0 crawls only the seed). At most "
            "the server's SITESCOPE_CRAWL_MAX_DEPTH_LIMIT.",
        ),
    ]
    max_pages: Annotated[
        int,
        Field(
            strict=True,
            ge=1,
            description="Most URLs the job may admit, seed included. At most the server's "
            "SITESCOPE_CRAWL_MAX_PAGES_LIMIT.",
        ),
    ]


class CrawlJobSummary(BaseModel):
    """A crawl job as stored in PostgreSQL."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: JobStatus = Field(
        description="`queued` until the scheduler (Phase 5) starts the job; nothing is crawled "
        "while a job is queued."
    )
    seed_url: str = Field(description="The normalized seed URL.")
    allowed_hosts: list[str] = Field(description="Hosts this job may crawl (exact match).")
    max_depth: int
    max_pages: int
    current_level: int = Field(description="The BFS level being crawled.")
    admitted_count: int = Field(description="URLs admitted so far, seed included.")
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested_at: datetime | None


class UrlStateCounts(BaseModel):
    """Number of the job's URLs in each state."""

    pending: int
    queued: int
    in_progress: int
    fetched: int
    failed: int
    skipped: int
    cancelled: int


class CrawlProgress(BaseModel):
    """Progress counted from the job's URL rows when the request was served."""

    urls_total: int = Field(description="URL rows of the job; equals `admitted_count`.")
    urls_finished: int = Field(description="URLs in a terminal state.")
    urls_by_state: UrlStateCounts


class CrawlJobDetail(CrawlJobSummary):
    """A crawl job with its progress."""

    progress: CrawlProgress

    @classmethod
    def from_progress(cls, progress: CrawlJobProgress) -> Self:
        summary = CrawlJobSummary.model_validate(progress.job)
        counts = {state.value: progress.urls_by_state[state] for state in UrlState}
        return cls(
            **summary.model_dump(),
            progress=CrawlProgress(
                urls_total=progress.urls_total,
                urls_finished=progress.urls_finished,
                urls_by_state=UrlStateCounts(**counts),
            ),
        )


class CrawlJobList(BaseModel):
    """One page of crawl jobs, newest first."""

    items: list[CrawlJobSummary]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page."
    )


def encode_cursor(position: JobPosition) -> str:
    """An opaque cursor for the listing position (URL-safe base64, no padding)."""
    raw = f"{position.created_at.isoformat()}|{position.id}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> JobPosition:
    """Parse a cursor from ``encode_cursor``; raise ``InvalidInputError`` if it is not one."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        created_at_text, separator, job_id = raw.partition("|")
        created_at = datetime.fromisoformat(created_at_text)
        if not separator or created_at.tzinfo is None:
            raise ValueError("not a listing cursor")
        return JobPosition(created_at=created_at, id=uuid.UUID(job_id))
    except ValueError:  # includes binascii.Error and UnicodeError
        raise InvalidInputError(
            [
                FieldViolation(
                    ("query", "cursor"),
                    "cursor is not valid; use the next_cursor value of a previous page",
                    "invalid_cursor",
                )
            ]
        ) from None
