r"""Crawler worker process: takes crawl tasks from the Redis stream and processes them (Phase 5).

Start as many as needed, on one machine or several. Settings come from the environment and
backend\.env::

    .\.venv\Scripts\python.exe -m app.workers.crawler [--concurrency N]

Each process is one consumer of the group ``crawlers`` on the stream ``<prefix>:crawl``. Its name
is unique per process start (``crawler-<host>-<pid>-<random>``) and is also the lease owner of
the URLs it claims, so log lines, ``crawl_urls.lease_owner``, ``crawl_events`` and the consumer
group all show which process did what. It runs up to ``SITESCOPE_CRAWL_CONCURRENCY`` tasks at
once, and reads only as many messages as it has free slots.

**Handling a message** (ADR-0016; architecture, section 10.3):

1. A malformed message (unknown version, missing or invalid fields) can never be processed: it
   is copied to the dead-letter stream and acknowledged.
2. The worker claims the URL (T4): a compare-and-set that issues a new fencing token.
3. With the lease: ``crawl_task.crawl_url`` under the task deadline (the lease minus
   ``SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS``), then the fenced result (T5) with the worker's own
   token. The message is acknowledged only after the result is committed (``RECORDED``). A
   fenced result (``FENCED``: the lease was lost to a takeover) is discarded, and the message
   is not acknowledged: ``XACK`` acts on the whole group, and the task may be someone else's
   now.
4. Without a lease: the worker reads the authoritative state (``inspect_url``) and decides
   (``judge_unclaimed``). It acknowledges when nothing is left to do (the URL is finished, the
   job is not running, or the dispatch that published the message rolled back). It leaves the
   message pending when another worker holds a live lease. It takes over an expired lease, and
   fails a URL whose last attempt expired.

Any error (PostgreSQL, Redis, a bug) leaves the message pending: an uncertain outcome is never
acknowledged. Pending messages of crashed or stalled workers are taken over with ``XAUTOCLAIM``
once idle for ``SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS`` (at least the lease). A redelivered
or duplicate message goes through the same steps, so it has no effect beyond a refused claim.

**Errors** pause reading for a while (exponential backoff with full jitter), so a PostgreSQL or
Redis outage does not churn through the stream. The worker resumes on its own.

**Stopping.** The first SIGINT or SIGTERM (Windows: Ctrl+C or CTRL_BREAK_EVENT) stops reading
and waits up to ``SITESCOPE_SHUTDOWN_GRACE_SECONDS`` for running tasks, then cancels the rest; a
second signal cancels them at once. A cancelled task keeps its lease and its pending message and
is recovered like a crashed one; killing the process is safe for the same reason.
"""

import argparse
import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from typing import Final, Self

from app.core.backoff import Backoff
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, task_context_var
from app.core.process import error_type, int_in_range, is_transient, process_name
from app.core.redis import create_redis_client
from app.core.shutdown import StopSignals, install_stop_handlers
from app.db.models import TERMINAL_URL_STATES, JobStatus, UrlState
from app.db.session import Database
from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream, Delivery
from app.services.crawl_state import (
    EXHAUSTED_OUTCOME,
    Lease,
    RecordStatus,
    UrlResult,
    UrlStatus,
    claim_url,
    inspect_url,
    record_result,
    recover_exhausted_url,
)
from app.workers.crawl_task import CrawlContext
from app.workers.runtime import build_crawl_context, crawl_within, worker_database

logger = logging.getLogger(__name__)

# Claims per message: a refused claim is tried again when the URL turns out to be claimable
# (it raced with a dispatch, or its lease just expired), at most this many times in all.
_CLAIM_ROUNDS: Final = 3
_STATS_INTERVAL_SECONDS: Final = 60.0


class Disposition(StrEnum):
    """What a worker does with a message once it has handled it."""

    ACK = "ack"  # done: XACK
    KEEP = "keep"  # leave it pending; XAUTOCLAIM hands it to a worker again later
    DEAD_LETTER = "dead_letter"  # unprocessable: copy it to the dead-letter stream, then XACK


@dataclass(frozen=True, slots=True)
class Handled:
    """What became of one message, and why (``reason``: a stable token, logged)."""

    disposition: Disposition
    reason: str
    lease: Lease | None = None  # the claim this worker made, if any
    result: UrlResult | None = None  # the result it produced, if any


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
        # means the dispatch that published this message rolled back; the next dispatch
        # publishes the task again.
        return Handled(Disposition.ACK, "not_dispatched")
    if status.state is UrlState.QUEUED:
        if status.attempt_count >= max_attempts:  # not reachable before Phase 6 retries
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
    reclaim_min_idle: float = 120.0  # at least the lease
    reclaim_batch: int = 50
    grace_seconds: float = 30.0
    name: str = field(default_factory=lambda: process_name("crawler"))

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if not 0 < self.task_deadline < self.lease_seconds:
            raise ValueError("the task deadline must be positive and shorter than the lease")
        if self.reclaim_min_idle < self.lease_seconds:
            raise ValueError("the reclaim idle time must be at least the lease")

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
        )


class CrawlWorker:
    """The consumer loop of one worker process. See the module docstring."""

    def __init__(
        self,
        database: Database,
        stream: CrawlStream,
        context: CrawlContext,
        config: WorkerConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._database = database
        self._stream = stream
        self._context = context
        self._config = config
        self._clock = clock
        self._tasks: dict[str, asyncio.Task[None]] = {}  # by stream entry ID
        self._stream_backoff = Backoff()
        self._task_backoff = Backoff()
        self._paused_until = 0.0
        self.handled: Counter[str] = Counter()  # messages handled, by reason

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    async def run(self, stop: asyncio.Event, force: asyncio.Event | None = None) -> None:
        """Consume messages until ``stop`` is set, then drain (see the module docstring)."""
        config = self._config
        force = force if force is not None else asyncio.Event()
        if not await self._prepare(stop):
            return
        logger.info(
            "Crawler worker started",
            extra={
                "event": "worker.started",
                "worker_id": self.name,
                "consumer": self.name,
                "stream": self._stream.key,
                "group": self._stream.group,
                "concurrency": config.concurrency,
                "lease_seconds": config.lease_seconds,
                "task_deadline_seconds": config.task_deadline,
                "reclaim_min_idle_seconds": config.reclaim_min_idle,
            },
        )
        cursor = "0-0"
        next_reclaim = self._clock()  # look for abandoned messages at once
        next_stats = self._clock() + _STATS_INTERVAL_SECONDS
        try:
            while await self._ready(stop):
                now = self._clock()
                if now >= next_stats:
                    self._log_stats()
                    next_stats = now + _STATS_INTERVAL_SECONDS
                try:
                    if now >= next_reclaim:
                        cursor = await self._reclaim(cursor)
                        if cursor == "0-0":  # the whole pending list was scanned
                            next_reclaim = self._clock() + config.reclaim_interval
                    else:
                        deliveries = await self._stream.read(
                            self.name, count=self._free(), block_ms=config.read_block_ms
                        )
                        self._start(deliveries, reclaimed=False)
                except Exception as exc:  # Redis unreachable, or an unexpected reply
                    self._pause(self._stream_backoff, "stream", exc)
                    continue
                if self._stream_backoff.reset():
                    logger.info(
                        "The task stream is reachable again",
                        extra={"event": "worker.recovered", "source": "stream"},
                    )
        finally:
            await self._drain(force)
        logger.info(
            "Crawler worker stopped",
            extra={
                "event": "worker.stopped",
                "worker_id": self.name,
                "handled": dict(self.handled),
            },
        )

    # --- The consumer loop ---------------------------------------------------------------------

    async def _prepare(self, stop: asyncio.Event) -> bool:
        """Create the stream and its group if needed; retried until it works or ``stop``."""
        while not stop.is_set():
            try:
                await self._stream.ensure_group()
            except Exception as exc:
                self._pause(self._stream_backoff, "stream", exc)
                await _sleep_unless(stop, self._paused_until - self._clock())
                continue
            self._stream_backoff.reset()
            return True
        return False

    async def _ready(self, stop: asyncio.Event) -> bool:
        """Wait for a free task slot and for any error pause to end; False once ``stop`` is set."""
        while not stop.is_set():
            pause = self._paused_until - self._clock()
            if pause > 0:
                await _sleep_unless(stop, pause)
            elif self._free() <= 0:
                await _wait_any(self._tasks.values(), stop)
            else:
                return True
        return False

    def _free(self) -> int:
        return self._config.concurrency - len(self._tasks)

    async def _reclaim(self, cursor: str) -> str:
        """One ``XAUTOCLAIM`` step from ``cursor``; returns where to continue."""
        config = self._config
        reclaimed = await self._stream.reclaim(
            self.name,
            min_idle_ms=round(config.reclaim_min_idle * 1000),
            start=cursor,
            count=min(self._free(), config.reclaim_batch),
        )
        if reclaimed.deleted:
            # Pending entries that no longer exist; the reconciler publishes their tasks again.
            logger.warning(
                "Dropped pending messages whose entries no longer exist",
                extra={
                    "event": "task.reclaim_deleted",
                    "count": len(reclaimed.deleted),
                    "message_ids": reclaimed.deleted[:20],
                },
            )
        if reclaimed.deliveries:
            logger.info(
                "Took over messages idle in the consumer group",
                extra={
                    "event": "task.reclaimed",
                    "consumer": self.name,
                    "count": len(reclaimed.deliveries),
                    "message_ids": [item.entry_id for item in reclaimed.deliveries][:20],
                },
            )
        self._start(reclaimed.deliveries, reclaimed=True)
        return reclaimed.next_start

    def _start(self, deliveries: Iterable[Delivery], *, reclaimed: bool) -> None:
        for delivery in deliveries:
            if delivery.entry_id in self._tasks:  # this worker is processing it already
                continue
            task = asyncio.create_task(self._handle(delivery, reclaimed=reclaimed))
            self._tasks[delivery.entry_id] = task
            task.add_done_callback(partial(self._finished, delivery.entry_id))

    def _finished(self, entry_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(entry_id) is task:
            del self._tasks[entry_id]
        if not task.cancelled() and (error := task.exception()) is not None:
            # _handle catches everything but cancellation; this is a bug.
            logger.error(
                "A message handler failed",
                exc_info=error,
                extra={"event": "task.crashed", "message_id": entry_id},
            )

    def _pause(self, backoff: Backoff, source: str, error: BaseException) -> None:
        """Stop reading for a backoff delay after an error."""
        delay = backoff.next_delay()
        self._paused_until = max(self._paused_until, self._clock() + delay)
        transient = is_transient(error)
        logger.log(
            logging.WARNING if transient else logging.ERROR,
            "Infrastructure error; pausing" if transient else "Unexpected error; pausing",
            # A traceback only for unexpected errors (bugs), not for every outage retry.
            exc_info=None if transient else error,
            extra={
                "event": "worker.error",
                "source": source,
                "error_type": error_type(error),
                "failures": backoff.failures,
                "retry_in_ms": round(delay * 1000),
            },
        )

    async def _drain(self, force: asyncio.Event) -> None:
        """Let running tasks finish within the grace period (or until ``force``), then cancel."""
        if not self._tasks:
            return
        grace = self._config.grace_seconds
        logger.info(
            "Stopping: waiting for running tasks",
            extra={
                "event": "worker.stopping",
                "worker_id": self.name,
                "in_flight": len(self._tasks),
                "grace_seconds": grace,
            },
        )
        deadline = self._clock() + grace
        while self._tasks and not force.is_set():
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            await _wait_any(self._tasks.values(), force, within=remaining)
        unfinished = list(self._tasks.values())
        if unfinished:
            logger.warning(
                "Cancelled unfinished tasks; their messages stay pending and leased",
                extra={"event": "worker.tasks_cancelled", "count": len(unfinished)},
            )
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)

    def _log_stats(self) -> None:
        logger.info(
            "Worker statistics",
            extra={
                "event": "worker.stats",
                "worker_id": self.name,
                "in_flight": len(self._tasks),
                "handled": dict(self.handled),
            },
        )

    # --- One message ---------------------------------------------------------------------------

    async def _handle(self, delivery: Delivery, *, reclaimed: bool) -> None:
        """Handle one message, settle it (ack, dead-letter or keep), and log one line."""
        started = self._clock()
        context: dict[str, str | int] = {
            "worker_id": self.name,
            "consumer": self.name,
            "message_id": delivery.entry_id,
        }
        if delivery.task is not None:
            context["job_id"] = str(delivery.task.job_id)
            context["url_id"] = str(delivery.task.url_id)
        task_context_var.set(context)  # this asyncio task's own copy of the context
        try:
            handled = await self._process(delivery)
        except asyncio.CancelledError:
            logger.warning(
                "Task cancelled; its message stays pending",
                extra={"event": "task.cancelled", "reclaimed": reclaimed},
            )
            raise
        except Exception as exc:  # the outcome is uncertain: never acknowledge
            handled = Handled(Disposition.KEEP, "error")
            self._pause(self._task_backoff, "task", exc)
        else:
            if self._task_backoff.reset():
                logger.info(
                    "Tasks succeed again", extra={"event": "worker.recovered", "source": "task"}
                )
        settled = await self._settle(delivery, handled)
        self.handled[handled.reason] += 1
        extra: dict[str, object] = {
            "event": "task.handled",
            "disposition": handled.disposition.value,
            "reason": handled.reason,
            "settled": settled,
            "reclaimed": reclaimed,
            "duration_ms": round((self._clock() - started) * 1000),
        }
        if handled.result is not None:
            extra["state"] = handled.result.state.value
            extra["outcome"] = handled.result.outcome
            extra["http_status"] = handled.result.http_status
        if delivery.error is not None:
            extra["error"] = delivery.error
        quiet = handled.disposition is not Disposition.KEEP or handled.reason == "leased"
        logger.log(logging.INFO if quiet else logging.WARNING, "Message handled", extra=extra)

    async def _process(self, delivery: Delivery) -> Handled:
        task = delivery.task
        if task is None:
            return Handled(Disposition.DEAD_LETTER, "malformed")
        config = self._config
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
                return await self._crawl(lease)
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

    async def _crawl(self, lease: Lease) -> Handled:
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
        result = await crawl_within(self._context, lease, deadline=config.task_deadline)
        async with self._database.session() as session:
            recorded = await record_result(
                session, lease, result, max_redirect_hops=config.max_redirect_hops
            )
        if recorded.status is RecordStatus.RECORDED:
            return Handled(Disposition.ACK, "completed", lease, result)
        # Fenced: another claim took the URL over. Its message is not ours to acknowledge.
        return Handled(Disposition.KEEP, "fenced", lease, result)

    async def _settle(self, delivery: Delivery, handled: Handled) -> bool:
        """Acknowledge or dead-letter the message as decided; False if it stays pending."""
        try:
            if handled.disposition is Disposition.ACK:
                await self._stream.ack(delivery.entry_id)
            elif handled.disposition is Disposition.DEAD_LETTER:
                await self._stream.dead_letter(delivery, delivery.error or handled.reason)
            else:
                return False
        except Exception as exc:
            # The message stays pending. Its redelivery finds the work done and is acknowledged
            # then (the claim is refused: already_finished).
            logger.warning(
                "Could not settle a message; it stays pending",
                extra={
                    "event": "task.settle_failed",
                    "disposition": handled.disposition.value,
                    "error_type": error_type(exc),
                },
            )
            return False
        return True


async def _sleep_unless(event: asyncio.Event, seconds: float) -> None:
    """Sleep for ``seconds``, or until ``event`` is set."""
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), timeout=seconds)


async def _wait_any(
    tasks: Iterable[asyncio.Task[None]], event: asyncio.Event, *, within: float | None = None
) -> None:
    """Wait until one of ``tasks`` finishes, ``event`` is set, or ``within`` seconds pass."""
    waiter = asyncio.ensure_future(event.wait())
    try:
        await asyncio.wait([*tasks, waiter], timeout=within, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()


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
