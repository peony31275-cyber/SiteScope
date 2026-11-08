r"""Crawler worker process: takes crawl tasks from the Redis stream and processes them.

Start as many as needed, on one machine or several. Settings come from the environment and
backend\.env::

    .\.venv\Scripts\python.exe -m app.workers.crawler [--concurrency N]

Each process is one consumer of the group ``crawlers`` on the stream ``<prefix>:crawl``. Its name
is unique per process start (``crawler-<host>-<pid>-<random>``) and is also the lease owner of
the URLs it claims, so log lines, ``crawl_urls.lease_owner``, ``crawl_events`` and the consumer
group all show which process did what. It runs up to ``SITESCOPE_CRAWL_CONCURRENCY`` tasks at
once, and reads only as many messages as it has free slots.

The consumer loop itself (reading, ``XAUTOCLAIM``, settling, draining) is shared with the
indexer (``app.workers.consumer``, Phase 7); this module adds what a crawl task means.

**Handling a message** (ADR-0016, ADR-0017; architecture, section 10.3):

1. A malformed message (unknown version, missing or invalid fields, bytes that are not UTF-8)
   can never be processed: it is copied to the dead-letter stream and acknowledged.
2. A message delivered more than ``SITESCOPE_STREAM_MAX_DELIVERIES`` times without being settled
   is poison if its URL can never be claimed again (queued, its attempts used up): the URL is
   failed as ``max_deliveries_exceeded`` (``crawl_state.quarantine_url``), then the message is
   dead-lettered. Any other URL goes on to step 3: delivery counts also grow while PostgreSQL is
   down, and the attempt limit bounds the work of a URL that can be claimed.
3. The worker claims the URL (T4): a compare-and-set that issues a new fencing token.
4. With the lease: ``crawl_task.crawl_url`` under the task deadline (the lease minus
   ``SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS``). The retry policy (``app/workers/retry.py``) then
   decides: a retryable failure with attempts left goes back to ``pending`` with a backoff
   (``RETRY_SCHEDULED``); anything else is the URL's result (``RECORDED``). Both are the fenced
   update with the worker's own token, and both are acknowledged only after the commit. A fenced
   result (``FENCED``: the lease was lost to a takeover) is discarded, and the message is not
   acknowledged: ``XACK`` acts on the whole group, and the task may be someone else's now.
5. Without a lease: the worker reads the authoritative state (``inspect_url``) and decides
   (``judge_unclaimed``). It acknowledges when nothing is left to do (the URL is finished, the
   job is not running, or the URL is ``pending``: the dispatch that published the message rolled
   back, or the URL waits for a retry). It leaves the message pending when another worker holds
   a live lease. It takes over an expired lease, and fails a URL whose last attempt expired.

**Errors.** An error leaves the message pending: an uncertain outcome is never acknowledged.

* A PostgreSQL or Redis error *before* the claim is retried here a few times, after a backoff,
  so that a short outage does not leave the message waiting for the reclaim idle time.
* A Redis error *during* the task (the politeness gate, the robots.txt cache) becomes a
  retryable ``infrastructure_error`` attempt, recorded like any failed attempt.
* Anything else after the claim (a failed or uncertain commit) waits for the lease to expire
  and for ``XAUTOCLAIM``.

Errors also pause reading for a while (exponential backoff with full jitter), so an outage does
not churn through the stream.

**Cancellation.** Every ``SITESCOPE_CRAWL_CANCEL_CHECK_SECONDS``, the worker asks PostgreSQL
whether the jobs of its running tasks are being cancelled (``jobs_being_cancelled``). It stops
those tasks and records their URLs ``cancelled`` under its own token (a fenced update). A task
that is already committing its result is left to finish. Other job statuses never stop a task:
a task whose job has completed holds a stale lease, and is fenced out when it records.

**Stopping.** The first SIGINT or SIGTERM (Windows: Ctrl+C or CTRL_BREAK_EVENT) stops reading
and waits up to ``SITESCOPE_SHUTDOWN_GRACE_SECONDS`` for running tasks, then cancels the rest; a
second signal cancels them at once. A cancelled task keeps its lease and its pending message and
is recovered like a crashed one; killing the process is safe for the same reason.
"""

import argparse
import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Self

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, task_context_var
from app.core.process import error_type, int_in_range, is_transient, process_name
from app.core.redis import create_redis_client
from app.core.shutdown import StopSignals, install_stop_handlers
from app.db.models import TERMINAL_URL_STATES, JobStatus, UrlState
from app.db.session import Database
from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream, Delivery
from app.services.crawl_cancellation import cancel_claimed_url, jobs_being_cancelled
from app.services.crawl_state import (
    EXHAUSTED_OUTCOME,
    MAX_DELIVERIES_OUTCOME,
    Lease,
    RecordStatus,
    UrlResult,
    UrlStatus,
    claim_url,
    inspect_url,
    quarantine_url,
    record_result,
    recover_exhausted_url,
)
from app.workers.consumer import Disposition, Handled, Progress, StreamConsumer
from app.workers.crawl_task import CrawlContext
from app.workers.retry import INFRASTRUCTURE_ERROR, RetryPolicy
from app.workers.runtime import build_crawl_context, crawl_within, worker_database

__all__ = [
    "CrawlWorker",
    "Disposition",
    "Handled",
    "Retry",
    "WorkerConfig",
    "judge_unclaimed",
    "main",
    "run",
    "serve",
]

logger = logging.getLogger(__name__)

# Claims per message: a refused claim is tried again when the URL turns out to be claimable
# (it raced with a dispatch, or its lease just expired), at most this many times in all.
_CLAIM_ROUNDS: Final = 3


class Retry(StrEnum):
    """A refused claim that is worth another round."""

    CLAIM = "claim"  # claimable now: queued, or an expired lease with attempts left
    RECOVER = "recover"  # an expired lease after the last attempt: fail the URL


def judge_unclaimed(
    status: UrlStatus | None, task: CrawlTask, *, max_attempts: int
) -> Handled | Retry:
    """What to do with a message whose URL this worker could not claim.

    ``status`` is the URL's authoritative state, read after the refused claim. Acknowledge only
    when no work is left that this message stands for; leave the message pending when the work
    may still need it.
    """
    if status is None:  # no such URL: not a task of this database
        return Handled(Disposition.DEAD_LETTER, "unknown_url")
    if status.job_id != task.job_id:
        return Handled(Disposition.DEAD_LETTER, "job_mismatch")
    if status.state in TERMINAL_URL_STATES:  # a duplicate, or a redelivery after the commit
        return Handled(Disposition.ACK, "already_finished")
    if status.job_status is not JobStatus.RUNNING:
        return Handled(Disposition.ACK, "job_not_running")
    if status.state is UrlState.PENDING:
        # The claim waited for any dispatch of the row in flight (claim_url). Still pending
        # means the dispatch that published this message rolled back, or the URL waits for a
        # retry; either way the dispatcher publishes the task again when it is due.
        return Handled(Disposition.ACK, "not_dispatched")
    if status.state is UrlState.QUEUED:
        # Only after SITESCOPE_CRAWL_MAX_ATTEMPTS was lowered: the message turns poison and
        # its URL is failed once it was delivered SITESCOPE_STREAM_MAX_DELIVERIES times.
        if status.attempt_count >= max_attempts:
            return Handled(Disposition.KEEP, "attempts_exhausted")
        return Retry.CLAIM
    if status.lease_live:  # another worker holds the lease; if it dies, this message recovers it
        return Handled(Disposition.KEEP, "leased")
    if status.attempt_count >= max_attempts:
        return Retry.RECOVER
    return Retry.CLAIM


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    concurrency: int = 4  # tasks in flight
    lease_seconds: float = 90.0
    task_deadline: float = 80.0  # must end before the lease does
    max_attempts: int = 3
    max_redirect_hops: int = 5
    read_block_ms: int = 2000
    reclaim_interval: float = 15.0
    reclaim_min_idle: float = 100.0  # at least the lease
    reclaim_batch: int = 50
    grace_seconds: float = 30.0
    max_deliveries: int = 10  # a message delivered more often is poison
    cancel_check_interval: float = 2.0
    retry: RetryPolicy | None = None  # None: the default policy, with max_attempts
    name: str = field(default_factory=lambda: process_name("crawler"))

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if not 0 < self.task_deadline < self.lease_seconds:
            raise ValueError("the task deadline must be positive and shorter than the lease")
        if self.reclaim_min_idle < self.lease_seconds:
            raise ValueError("the reclaim idle time must be at least the lease")
        if self.max_deliveries < 2:
            raise ValueError("max_deliveries must be at least 2")
        if self.retry is None:
            object.__setattr__(self, "retry", RetryPolicy(max_attempts=self.max_attempts))
        elif self.retry.max_attempts != self.max_attempts:
            raise ValueError("the retry policy must use the same max_attempts")

    @property
    def retry_policy(self) -> RetryPolicy:
        if self.retry is None:  # pragma: no cover - __post_init__ always sets it
            raise RuntimeError("the worker has no retry policy")
        return self.retry

    @classmethod
    def from_settings(cls, settings: Settings, *, concurrency: int | None = None) -> Self:
        return cls(
            concurrency=concurrency if concurrency is not None else settings.crawl_concurrency,
            lease_seconds=settings.crawl_lease_seconds,
            task_deadline=settings.crawl_lease_seconds - settings.crawl_lease_margin_seconds,
            max_attempts=settings.crawl_max_attempts,
            max_redirect_hops=settings.fetch_max_redirects,
            read_block_ms=settings.stream_read_block_ms,
            reclaim_interval=settings.stream_reclaim_interval_seconds,
            reclaim_min_idle=settings.stream_reclaim_min_idle_seconds,
            reclaim_batch=settings.stream_reclaim_batch_size,
            grace_seconds=settings.shutdown_grace_seconds,
            max_deliveries=settings.stream_max_deliveries,
            cancel_check_interval=settings.crawl_cancel_check_seconds,
            retry=RetryPolicy.from_settings(settings),
        )


@dataclass
class _Watched:
    """A running crawl phase, which the cancellation watcher may stop."""

    job_id: uuid.UUID
    task: "asyncio.Task[UrlResult]"
    stopped_for_cancellation: bool = False


def _result_details(result: UrlResult) -> dict[str, object]:
    """The fields of a result that the ``task.handled`` line shows."""
    return {
        "state": result.state.value,
        "outcome": result.outcome,
        "http_status": result.http_status,
    }


class CrawlWorker(StreamConsumer[CrawlTask, WorkerConfig]):
    """The consumer loop of one crawler worker process. See the module docstring."""

    DISPLAY_NAME = "Crawler worker"

    def __init__(
        self,
        database: Database,
        stream: CrawlStream,
        context: CrawlContext,
        config: WorkerConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(database, stream, config, clock=clock)
        self._context = context
        self._crawling: dict[str, _Watched] = {}  # crawl phases in progress, by entry ID

    def _started_fields(self) -> dict[str, object]:
        config = self._config
        return {
            "lease_seconds": config.lease_seconds,
            "task_deadline_seconds": config.task_deadline,
            "max_attempts": config.max_attempts,
        }

    async def _background(self) -> None:
        await self._watch_cancellations()

    async def _watch_cancellations(self) -> None:
        """Stop the crawl phases of tasks whose job is being cancelled."""
        while True:
            await asyncio.sleep(self._config.cancel_check_interval)
            watched = list(self._crawling.values())
            if not watched:
                continue
            try:
                async with self._database.session() as session:
                    stopped = await jobs_being_cancelled(
                        session, list({item.job_id for item in watched})
                    )
            except Exception as exc:  # the next check tries again
                logger.debug(
                    "Could not check for cancelled jobs",
                    extra={"event": "worker.cancel_check_failed", "error_type": error_type(exc)},
                )
                continue
            for item in watched:
                if item.job_id in stopped and not item.task.done():
                    item.stopped_for_cancellation = True
                    item.task.cancel()
                    logger.info(
                        "Stopping a task: its job is being cancelled",
                        extra={"event": "task.stopping_cancelled", "job_id": str(item.job_id)},
                    )

    # --- One message ---------------------------------------------------------------------------

    async def _process(
        self, delivery: Delivery[CrawlTask], deliveries: int, progress: Progress
    ) -> Handled:
        task = delivery.task
        if task is None:
            return Handled(Disposition.DEAD_LETTER, "malformed")
        config = self._config
        if deliveries > config.max_deliveries:
            poisoned = await self._quarantine(task, deliveries)
            if poisoned is not None:
                return poisoned
        for _ in range(_CLAIM_ROUNDS):
            async with self._database.session() as session:
                lease = await claim_url(
                    session,
                    task.url_id,
                    owner=self.name,
                    lease_seconds=config.lease_seconds,
                    max_attempts=config.max_attempts,
                    job_id=task.job_id,
                )
            if lease is not None:
                progress.claimed = True
                return await self._crawl(lease, delivery.entry_id)
            async with self._database.session() as session:
                status = await inspect_url(session, task.url_id)
            verdict = judge_unclaimed(status, task, max_attempts=config.max_attempts)
            if isinstance(verdict, Handled):
                return verdict
            if verdict is Retry.RECOVER:
                async with self._database.session() as session:
                    if await recover_exhausted_url(
                        session, task.url_id, owner=self.name, max_attempts=config.max_attempts
                    ):
                        return Handled(Disposition.ACK, EXHAUSTED_OUTCOME)
        return Handled(Disposition.KEEP, "claim_contended")

    async def _quarantine(self, task: CrawlTask, deliveries: int) -> Handled | None:
        """A message delivered too often. Only a URL that can never be claimed again (queued,
        its attempts used up) is failed as ``max_deliveries_exceeded``, and the message
        dead-lettered. A claimable URL, or an expired lease, returns None: it is claimed or
        recovered as usual, because only a claim counts an attempt and ``attempt_count`` bounds
        the work. Deliveries also grow while PostgreSQL is down (every ``XAUTOCLAIM`` counts
        one), and an outage must not fail URLs that were never tried (Phase 6 independent
        review). A URL that needs nothing, or has a live lease, is settled by the usual rules."""
        async with self._database.session() as session:
            status = await inspect_url(session, task.url_id)
        config = self._config
        verdict = judge_unclaimed(status, task, max_attempts=config.max_attempts)
        if not isinstance(verdict, Handled):
            return None  # claimable, or an expired last attempt to recover
        if verdict.reason != "attempts_exhausted":
            return verdict  # finished, not running, pending, unknown, mismatched, or leased
        async with self._database.session() as session:
            failed = await quarantine_url(
                session,
                task.url_id,
                job_id=task.job_id,
                owner=self.name,
                deliveries=deliveries,
                max_attempts=config.max_attempts,
            )
        if failed:
            return Handled(Disposition.DEAD_LETTER, MAX_DELIVERIES_OUTCOME)
        return Handled(Disposition.KEEP, "quarantine_refused")  # it changed meanwhile

    async def _crawl(self, lease: Lease, entry_id: str) -> Handled:
        """The claimed URL: crawl it under the task deadline, then record the fenced result."""
        config = self._config
        task_context_var.set(
            {
                **(task_context_var.get() or {}),
                "lease_epoch": lease.token,
                "depth": lease.depth,
                "attempt": lease.attempt,
            }
        )
        watched = _Watched(lease.job_id, asyncio.create_task(self._crawl_phase(lease)))
        self._crawling[entry_id] = watched
        try:
            result = await watched.task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if not watched.stopped_for_cancellation or (current and current.cancelling()):
                raise  # this whole handler is being cancelled (shutdown)
            return await self._record_cancelled(lease)
        finally:
            self._crawling.pop(entry_id, None)
        decision = config.retry_policy.decide(result, attempt=lease.attempt)
        async with self._database.session() as session:
            recorded = await record_result(
                session,
                lease,
                result,
                max_redirect_hops=config.max_redirect_hops,
                retry_delay=decision.delay,
            )
        if recorded.status is RecordStatus.RECORDED:
            return Handled(Disposition.ACK, "completed", _result_details(result))
        if recorded.status is RecordStatus.RETRY_SCHEDULED:
            return Handled(Disposition.ACK, "retry_scheduled", _result_details(result))
        # Fenced: another claim took the URL over. Its message is not ours to acknowledge.
        return Handled(Disposition.KEEP, "fenced", _result_details(result))

    async def _crawl_phase(self, lease: Lease) -> UrlResult:
        """``crawl_url`` under the task deadline; a transient infrastructure error (Redis, in
        the politeness gate or the robots.txt cache) becomes a retryable failed attempt."""
        try:
            return await crawl_within(self._context, lease, deadline=self._config.task_deadline)
        except Exception as exc:
            if not is_transient(exc):
                raise
            logger.warning(
                "Infrastructure error during a task; it counts as a failed attempt",
                extra={"event": "task.infrastructure_error", "error_type": error_type(exc)},
            )
            return UrlResult(
                UrlState.FAILED,
                INFRASTRUCTURE_ERROR,
                error=f"{error_type(exc)} in the crawler's own infrastructure during the task",
            )

    async def _record_cancelled(self, lease: Lease) -> Handled:
        """The task was stopped because its job is being cancelled: record that, fenced."""
        async with self._database.session() as session:
            cancelled = await cancel_claimed_url(session, lease)
        if cancelled:
            return Handled(Disposition.ACK, "cancelled")
        return Handled(Disposition.KEEP, "fenced")  # the lease was taken over


async def run(settings: Settings, signals: StopSignals, *, concurrency: int | None = None) -> None:
    """Build a worker from settings and run it until ``signals`` stop it."""
    config = WorkerConfig.from_settings(settings, concurrency=concurrency)
    database = worker_database(settings, application_name="sitescope-crawler")
    redis = create_redis_client(settings, client_name="sitescope-crawler")
    try:
        worker = CrawlWorker(
            database,
            CrawlStream(redis, key_prefix=settings.redis_key_prefix),
            build_crawl_context(settings, redis),
            config,
        )
        await worker.run(signals.stop, signals.force)
    finally:
        await redis.aclose()
        await database.dispose()


async def serve(settings: Settings, *, concurrency: int | None = None) -> None:
    """The command's work: run a worker until SIGINT or SIGTERM."""
    signals = StopSignals()
    install_stop_handlers(signals, process="worker")
    await run(settings, signals, concurrency=concurrency)


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.crawler",
        description=(
            "Run a crawler worker: process crawl tasks from the Redis stream until stopped"
            " (Ctrl+C, or SIGTERM)."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int_in_range(1, 64),
        default=None,
        help="tasks processed at once, 1 to 64 (default: SITESCOPE_CRAWL_CONCURRENCY)",
    )
    args = parser.parse_args(argv)
    settings = settings or get_settings()
    configure_logging(settings.log_level, service="crawler")
    asyncio.run(serve(settings, concurrency=args.concurrency))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
