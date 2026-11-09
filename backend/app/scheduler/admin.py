r"""Operator commands that change crawl jobs (Phase 11, ADR-0024).

Run where the deployment's settings and database are reachable: from backend\ in development, or
inside a container of the stack (``docker compose exec api python -m app.scheduler.admin ...``)::

    python -m app.scheduler.admin create --seed URL [--max-depth N] [--max-pages N]
                                         [--idempotency-key KEY]
    python -m app.scheduler.admin cancel JOB_ID
    python -m app.scheduler.admin seed-demo --base-url URL [--wait SECONDS]

The commands call the services the API calls (``services/crawl_jobs.py``,
``services/crawl_cancellation.py``), so every rule of a crawl request still applies:

* the approved targets;
* the depth and page limits;
* the active-jobs cap;
* idempotency keys.

Only the API's write access (``app/api/security.py``) is not checked. Running a command needs a
shell with the deployment's configuration and database credentials, which is the operator's
access already. A public read-only deployment (write access ``disabled``) is populated this way.

``seed-demo`` creates the demo jobs (``DEMO_JOBS``) on the demo site at ``--base-url``. The site
is the repository's ``demo-site/``, synthetic content written for SiteScope. A demo job is
skipped while an equal job exists: the same normalized seed URL, depth and page limit, queued,
running or completed. Running the command again therefore creates nothing new. With ``--wait``,
it then waits until every demo job is finished and its indexing complete. The scheduler, crawler
workers, index dispatcher and indexers do that work; this command only creates jobs and reads
them.

Every command prints one JSON document. The exit status is 0 on success, 1 on a refusal (an
invalid request, an unknown job, the cap), an unavailable dependency, or a wait that ran out.
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from app.core.config import Settings, get_settings
from app.core.errors import DomainError, InvalidInputError
from app.core.process import error_type, int_in_range
from app.crawler.urls import InvalidUrlError, normalize_url
from app.db.models import TERMINAL_JOB_STATUSES, CrawlJob, JobStatus
from app.db.session import Database
from app.services.crawl_cancellation import cancel_crawl_job
from app.services.crawl_jobs import (
    CrawlJobProgress,
    JobPosition,
    get_crawl_job,
    list_crawl_jobs,
    submit_crawl_job,
)


@dataclass(frozen=True, slots=True)
class DemoJob:
    name: str
    path: str  # the seed, relative to the demo site's base URL
    max_depth: int
    max_pages: int
    shows: str


# Created in this order: the full crawl last, so that it is the newest job, the one the
# dashboard's overview shows first.
DEMO_JOBS: Final = (
    DemoJob("docs", "/docs/", 2, 50, "one section, crawled from its index page"),
    DemoJob("blog", "/blog/", 1, 20, "depth 1: the seed and the pages it links to"),
    DemoJob("budget", "/", 3, 6, "a page budget smaller than the site: exactly 6 URLs admitted"),
    DemoJob(
        "site",
        "/",
        3,
        100,
        "the whole site: shared links and cycles, robots.txt rules, a noindex page, a nofollow"
        " page, a broken link",
    ),
)

# An equal job in one of these statuses stands for a demo job; a failed or cancelled one does not.
_REUSABLE: Final = frozenset({JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.COMPLETED})
_POLL_SECONDS: Final = 2.0
_LIST_PAGE: Final = 100


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def describe_job(job: CrawlJob) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "seed_url": job.seed_url,
        "max_depth": job.max_depth,
        "max_pages": job.max_pages,
        "status": job.status,
        "created_at": _iso(job.created_at),
    }


def describe_progress(progress: CrawlJobProgress) -> dict[str, Any]:
    indexing = progress.indexing
    return {
        **describe_job(progress.job),
        "admitted": progress.job.admitted_count,
        "urls_by_state": {state.value: count for state, count in progress.urls_by_state.items()},
        "pages_stored": progress.pages_stored,
        "indexing": {
            "status": indexing.state.value,
            "documents": indexing.documents,
            "indexed": indexing.indexed,
            "failed": indexing.failed,
            "completed_at": _iso(indexing.completed_at),
        },
        "failure_reason": progress.job.failure_reason,
    }


def settled(progress: CrawlJobProgress) -> bool:
    """The job is finished and its indexing is complete: nothing about it changes any more."""
    return (
        JobStatus(progress.job.status) in TERMINAL_JOB_STATUSES
        and progress.indexing.completed_at is not None
    )


def _refusal(exc: DomainError) -> dict[str, Any]:
    if isinstance(exc, InvalidInputError):
        return {"code": exc.code, "violations": exc.problem_extensions()["errors"]}
    return {"code": exc.code, "detail": exc.detail}


async def create(
    settings: Settings,
    *,
    seed_url: str,
    max_depth: int,
    max_pages: int,
    idempotency_key: str | None,
) -> dict[str, Any]:
    database = Database(settings, application_name="sitescope-admin")
    try:
        async with database.session() as session:
            submitted = await submit_crawl_job(
                session,
                settings,
                seed_url=seed_url,
                max_depth=max_depth,
                max_pages=max_pages,
                idempotency_key=idempotency_key,
            )
        return {"job": describe_job(submitted.job), "created": submitted.created}
    finally:
        await database.dispose()


async def cancel(settings: Settings, job_id: uuid.UUID) -> dict[str, Any]:
    database = Database(settings, application_name="sitescope-admin")
    try:
        async with database.session() as session:
            result = await cancel_crawl_job(session, job_id)
        return {
            "job_id": str(job_id),
            "action": result.action.value,
            "status": result.status.value,
            "cancelled_urls": result.cancelled_urls,
        }
    finally:
        await database.dispose()


async def _all_jobs(database: Database) -> list[CrawlJob]:
    jobs: list[CrawlJob] = []
    after: JobPosition | None = None
    while True:
        async with database.session() as session:
            page = await list_crawl_jobs(session, limit=_LIST_PAGE, after=after)
        jobs.extend(page.jobs)
        if page.next_position is None:
            return jobs
        after = page.next_position


def _demo_seed(base_url: str, demo: DemoJob) -> str:
    return base_url.rstrip("/") + demo.path


def _equal_job(jobs: Sequence[CrawlJob], seed: str, demo: DemoJob) -> CrawlJob | None:
    """The oldest equal job that stands for ``demo`` (module docstring), if any."""
    equal = [
        job
        for job in jobs
        if job.seed_url == seed
        and job.max_depth == demo.max_depth
        and job.max_pages == demo.max_pages
        and JobStatus(job.status) in _REUSABLE
    ]
    return min(equal, key=lambda job: job.created_at, default=None)


async def seed_demo(settings: Settings, *, base_url: str, wait_seconds: float) -> dict[str, Any]:
    database = Database(settings, application_name="sitescope-admin")
    try:
        existing = await _all_jobs(database)
        jobs: dict[str, uuid.UUID] = {}
        created: list[str] = []
        for demo in DEMO_JOBS:
            seed_url = _demo_seed(base_url, demo)
            try:
                normalized = normalize_url(seed_url).url
            except InvalidUrlError as exc:
                return {"error": {"code": "invalid_base_url", "detail": str(exc)}}
            equal = _equal_job(existing, normalized, demo)
            if equal is not None:
                jobs[demo.name] = equal.id
                continue
            async with database.session() as session:
                submitted = await submit_crawl_job(
                    session,
                    settings,
                    seed_url=seed_url,
                    max_depth=demo.max_depth,
                    max_pages=demo.max_pages,
                )
            jobs[demo.name] = submitted.job.id
            created.append(demo.name)

        progress = await _read(database, jobs)
        deadline = time.monotonic() + wait_seconds
        while wait_seconds > 0 and not all(settled(item) for item in progress.values()):
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_POLL_SECONDS)
            progress = await _read(database, jobs)
        document: dict[str, Any] = {
            "base_url": base_url,
            "content": "synthetic: the SiteScope demo site (demo-site/), written for this project",
            "created": created,
            "jobs": [
                {"name": demo.name, "shows": demo.shows, **describe_progress(progress[demo.name])}
                for demo in DEMO_JOBS
            ],
            "settled": all(settled(item) for item in progress.values()),
        }
        if wait_seconds > 0 and not document["settled"]:
            document["error"] = {
                "code": "wait_timed_out",
                "detail": f"The demo jobs did not all finish and index within {wait_seconds:g} s;"
                " check that the scheduler, crawler workers, index dispatcher and indexers run.",
            }
        return document
    finally:
        await database.dispose()


async def _read(database: Database, jobs: dict[str, uuid.UUID]) -> dict[str, CrawlJobProgress]:
    progress: dict[str, CrawlJobProgress] = {}
    async with database.session() as session:
        for name, job_id in jobs.items():
            progress[name] = await get_crawl_job(session, job_id)
    return progress


async def _run(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    try:
        if args.command == "create":
            return await create(
                settings,
                seed_url=args.seed,
                max_depth=args.max_depth,
                max_pages=args.max_pages,
                idempotency_key=args.idempotency_key,
            )
        if args.command == "cancel":
            return await cancel(settings, args.job_id)
        return await seed_demo(settings, base_url=args.base_url, wait_seconds=args.wait)
    except DomainError as exc:
        return {"error": _refusal(exc)}
    except Exception as exc:  # PostgreSQL unreachable, or another failure: no stack trace
        return {"error": {"code": "unavailable", "error_type": error_type(exc)}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scheduler.admin",
        description="Create or cancel crawl jobs, or seed the demo jobs, through the service"
        " layer (the operator's access; prints JSON).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create_command = commands.add_parser("create", help="create a crawl job")
    create_command.add_argument("--seed", required=True, help="the seed URL (approved target)")
    create_command.add_argument("--max-depth", type=int_in_range(0, 100), default=2)
    create_command.add_argument("--max-pages", type=int_in_range(1, 1_000_000), default=100)
    create_command.add_argument(
        "--idempotency-key", default=None, help="repeat the command with it to create one job"
    )

    cancel_command = commands.add_parser("cancel", help="cancel a crawl job")
    cancel_command.add_argument("job_id", type=uuid.UUID, help="the crawl job's ID")

    seed_command = commands.add_parser("seed-demo", help="create the demo jobs (idempotent)")
    seed_command.add_argument(
        "--base-url",
        required=True,
        help="the demo site's base URL, an approved target (e.g. http://demo-site:8080)",
    )
    seed_command.add_argument(
        "--wait",
        type=int_in_range(0, 3600),
        default=0,
        help="seconds to wait until every demo job is finished and indexed (default: 0)",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    document = asyncio.run(_run(args, settings or get_settings()))
    sys.stdout.write(json.dumps(document, indent=2) + "\n")
    return 1 if "error" in document else 0


if __name__ == "__main__":
    sys.exit(main())
