r"""The single-process crawl driver: runs one crawl job to completion in this process.

It drives the Phase 4 use cases (``services.crawl_state``) the way the scheduler and the
distributed workers will in Phase 5, in one process and without Redis Streams: the IDs of
dispatched URLs go straight to in-process tasks instead of through a stream. It is a tool for
development, tests and demonstrations, not the production worker. Redis is still required:
politeness slots and the robots.txt cache are shared through it.

The loop, for one job:

1. activate the job (``queued`` -> ``running``) if it is still queued;
2. whenever no task of this process is in flight, try to advance the job (T6), and stop once it
   is completed or failed (or no longer running);
3. fill free task slots with URLs of the current level: first ``queued`` URLs and URLs whose
   lease expired (left behind by a stopped run; they are taken over with a new fencing token),
   then newly dispatched ``pending`` URLs (T3);
4. each task claims its URL (T4), runs ``crawl_task.crawl_url`` under a deadline that ends
   ``LEASE_MARGIN_SECONDS`` before its lease, and records the result (T5). A fenced-out result
   is discarded;
5. a URL whose expired lease used its last attempt is failed (``recover_exhausted_url``);
6. if the level has unfinished URLs that this process cannot act on (live leases of another
   process), wait and look again.

**Crash safety.** Everything is in PostgreSQL. If the process stops (a crash, Ctrl+C, or a
PostgreSQL or Redis error, which ends the run), the URLs it had claimed stay ``in_progress``
until their leases expire by the database clock. The next run takes them over with a new token,
so a result from the stopped process can never be recorded after the takeover. Nothing ever
resets a live lease.

Run from backend\ against an existing job (settings come from the environment and backend\.env):

    .\.venv\Scripts\python.exe -m app.workers.local_crawler <job-id>

The job must be ``queued`` or ``running``; create it with ``POST /api/v1/crawls``. It prints one
JSON summary. The exit status is 0 if the job completed, 1 if it ended otherwise, and 2 if the
job does not exist.
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import socket
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Self

from app.core.config import LEASE_MARGIN_SECONDS, Settings, get_settings
from app.core.errors import NotFoundError
from app.core.logging import configure_logging, task_context_var
from app.core.redis import create_redis_client
from app.crawler.fetcher import build_fetcher
from app.crawler.html_parser import ParseLimits
from app.crawler.politeness import build_gate
from app.crawler.robots import build_robots_policy
from app.db.models import UrlState
from app.db.session import Database
from app.services.crawl_jobs import CrawlJobProgress, get_crawl_job, target_policy
from app.services.crawl_state import (
    AdvanceAction,
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
from app.workers.crawl_task import CrawlContext, crawl_url

logger = logging.getLogger(__name__)

_FINISHED = (AdvanceAction.COMPLETED, AdvanceAction.FAILED, AdvanceAction.NOT_RUNNING)


def worker_name() -> str:
    """A lease owner name unique per process start (ADR-0011)."""
    return f"local-{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"


@dataclass(frozen=True, slots=True)
class DriverConfig:
    concurrency: int = 4  # tasks in flight
    lease_seconds: float = 90.0
    task_deadline: float = 80.0  # must end before the lease does
    max_attempts: int = 3
    max_redirect_hops: int = 5  # redirect targets admitted in a row from one URL
    idle_poll_seconds: float = 1.0  # wait when only other processes' leases remain
    owner: str = field(default_factory=worker_name)

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if not 0 < self.task_deadline < self.lease_seconds:
            raise ValueError("the task deadline must be positive and shorter than the lease")

    @classmethod
    def from_settings(cls, settings: Settings, *, concurrency: int | None = None) -> Self:
        return cls(
            concurrency=concurrency if concurrency is not None else settings.crawl_concurrency,
            lease_seconds=settings.crawl_lease_seconds,
            task_deadline=settings.crawl_lease_seconds - LEASE_MARGIN_SECONDS,
            max_attempts=settings.crawl_max_attempts,
            max_redirect_hops=settings.fetch_max_redirects,
        )


class LocalCrawler:
    """Runs crawl jobs in this process. See the module docstring."""

    def __init__(
        self,
        database: Database,
        context: CrawlContext,
        config: DriverConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._database = database
        self._context = context
        self._config = config
        self._sleep = sleep

    async def run(self, job_id: uuid.UUID) -> CrawlJobProgress:
        """Run ``job_id`` until it is completed or failed; return its final progress.

        Raises ``NotFoundError`` for an unknown job. PostgreSQL and Redis errors end the run;
        a later run continues where it stopped.
        """
        async with self._database.session() as session:
            await activate_job(session, job_id)
        logger.info(
            "Local crawl started",
            extra={
                "event": "crawl.run_started",
                "job_id": str(job_id),
                "worker_id": self._config.owner,
                "concurrency": self._config.concurrency,
            },
        )
        in_flight: dict[uuid.UUID, asyncio.Task[bool]] = {}
        try:
            while True:
                if not in_flight:
                    async with self._database.session() as session:
                        advanced = await advance_job(session, job_id)
                    if advanced.action in _FINISHED:
                        break
                started = await self._fill(job_id, in_flight)
                if not in_flight:
                    if not started:
                        # The level's remaining URLs are leased by another process (or wait
                        # for their retry time): look again later.
                        await self._sleep(self._config.idle_poll_seconds)
                    continue
                done, _ = await asyncio.wait(
                    in_flight.values(), return_when=asyncio.FIRST_COMPLETED
                )
                claimed = False
                for url_id, task in list(in_flight.items()):
                    if task in done:
                        del in_flight[url_id]
                        claimed |= task.result()  # re-raises infrastructure failures
                if not claimed and not in_flight:
                    # Every finished task lost its claim to another process: do not spin.
                    await self._sleep(self._config.idle_poll_seconds)
        finally:
            for task in in_flight.values():
                task.cancel()
            await asyncio.gather(*in_flight.values(), return_exceptions=True)
        async with self._database.session() as session:
            progress = await get_crawl_job(session, job_id)
        logger.info(
            "Local crawl finished",
            extra={
                "event": "crawl.run_finished",
                "job_id": str(job_id),
                "status": progress.job.status,
                "admitted": progress.job.admitted_count,
            },
        )
        return progress

    async def _fill(self, job_id: uuid.UUID, in_flight: dict[uuid.UUID, asyncio.Task[bool]]) -> int:
        """Start tasks for free slots; return how many URLs were started or recovered."""
        free = self._config.concurrency - len(in_flight)
        if free <= 0:
            return 0
        started = recovered = 0
        async with self._database.session() as session:
            actionable = await find_actionable_urls(
                session,
                job_id,
                max_attempts=self._config.max_attempts,
                limit=free + len(in_flight),
            )
        for item in actionable:
            if item.url_id in in_flight:
                continue
            if item.exhausted:  # needs no task slot: one transaction fails it
                async with self._database.session() as session:
                    if await recover_exhausted_url(
                        session,
                        item.url_id,
                        owner=self._config.owner,
                        max_attempts=self._config.max_attempts,
                    ):
                        recovered += 1
                continue
            if started < free:
                in_flight[item.url_id] = asyncio.create_task(self._process(item.url_id))
                started += 1
        if started < free:
            async with self._database.session() as session:
                dispatched = await dispatch_urls(session, job_id, limit=free - started)
            for url_id in dispatched:
                in_flight[url_id] = asyncio.create_task(self._process(url_id))
                started += 1
        return started + recovered

    async def _process(self, url_id: uuid.UUID) -> bool:
        """One task: claim, crawl under the task deadline, record the fenced result.

        Returns whether the claim succeeded.
        """
        config = self._config
        async with self._database.session() as session:
            lease = await claim_url(
                session,
                url_id,
                owner=config.owner,
                lease_seconds=config.lease_seconds,
                max_attempts=config.max_attempts,
            )
        if lease is None:
            return False  # claimed by another process meanwhile, or no longer claimable
        task_context_var.set(
            {
                "job_id": str(lease.job_id),
                "url_id": str(url_id),
                "worker_id": config.owner,
                "lease_epoch": lease.token,
            }
        )
        try:
            async with asyncio.timeout(config.task_deadline):
                result = await crawl_url(self._context, lease)
        except TimeoutError:
            result = UrlResult(
                UrlState.FAILED,
                "deadline_exceeded",
                error=f"the task did not finish within {config.task_deadline:g} s",
            )
        async with self._database.session() as session:
            recorded = await record_result(
                session, lease, result, max_redirect_hops=config.max_redirect_hops
            )
        if recorded.status is RecordStatus.RECORDED:
            logger.info(
                "URL crawled",
                extra={
                    "event": "task.completed",
                    "depth": lease.depth,
                    "state": result.state.value,
                    "outcome": result.outcome,
                    "http_status": result.http_status,
                    "redirect_admitted": recorded.redirect_url_id is not None,
                },
            )
        return True


async def run(
    settings: Settings, job_id: uuid.UUID, *, concurrency: int | None = None
) -> CrawlJobProgress:
    """Build the crawl context from settings and run one job (the CLI's work)."""
    database = Database(settings, application_name="sitescope-local-crawler")
    redis = create_redis_client(settings, client_name="sitescope-local-crawler")
    try:
        gate = build_gate(settings, redis)
        fetcher = build_fetcher(settings, gate=gate)
        context = CrawlContext(
            fetcher=fetcher,
            robots=build_robots_policy(settings, fetcher=fetcher, redis=redis, delay_sink=gate),
            targets=target_policy(settings),
            parse_limits=ParseLimits.from_settings(settings),
            crawler_name=settings.crawler_name,
        )
        crawler = LocalCrawler(
            database, context, DriverConfig.from_settings(settings, concurrency=concurrency)
        )
        return await crawler.run(job_id)
    finally:
        await redis.aclose()
        await database.dispose()


def summary(progress: CrawlJobProgress) -> dict[str, Any]:
    job = progress.job
    return {
        "job_id": str(job.id),
        "status": job.status,
        "failure_reason": job.failure_reason,
        "current_level": job.current_level,
        "max_depth": job.max_depth,
        "admitted_count": job.admitted_count,
        "max_pages": job.max_pages,
        "urls_by_state": {state.value: n for state, n in progress.urls_by_state.items()},
    }


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.local_crawler",
        description="Run one existing crawl job to completion in this process (development tool).",
    )
    parser.add_argument("job_id", type=uuid.UUID, help="the ID of a queued or running crawl job")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="tasks in flight (default: SITESCOPE_CRAWL_CONCURRENCY)",
    )
    args = parser.parse_args(argv)
    settings = settings or get_settings()
    configure_logging(settings.log_level, service="local-crawler")
    try:
        progress = asyncio.run(run(settings, args.job_id, concurrency=args.concurrency))
    except NotFoundError as exc:
        sys.stderr.write(f"error: {exc.detail}\n")
        return 2
    sys.stdout.write(json.dumps(summary(progress), indent=2) + "\n")
    return 0 if progress.job.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
