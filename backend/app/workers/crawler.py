r"""Crawler worker process: takes crawl tasks from the Redis stream and processes them.

Start as many as needed, on one machine or several. Settings come from the environment and
backend\.env::

    .\.venv\Scripts\python.exe -m app.workers.crawler [--concurrency N]

Each process is one consumer of the group ``crawlers`` on the stream ``<prefix>:crawl``. Its name
is unique per process start (``crawler-<host>-<pid>-<random>``) and is also the lease owner of
the URLs it claims, so log lines, ``crawl_urls.lease_owner``, ``crawl_events`` and the consumer
group all show which process did what. It runs up to ``SITESCOPE_CRAWL_CONCURRENCY`` tasks at
once, and reads only as many messages as it has free slots.

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
import contextlib
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
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
from app.core.tasks import cancel_and_wait
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
from app.workers.crawl_task import CrawlContext
from app.workers.retry import INFRASTRUCTURE_ERROR, RetryPolicy
from app.workers.runtime import build_crawl_context, crawl_within, worker_database

logger = logging.getLogger(__name__)

# Claims per message: a refused claim is tried again when the URL turns out to be claimable
# (it raced with a dispatch, or its lease just expired), at most this many times in all.
_CLAIM_ROUNDS: Final = 3
# How often a message is handled again here after a transient error before its claim.
_LOCAL_RETRIES: Final = 5
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
class _Progress:
    """How far one message got: once its URL was claimed, an error is not retried here."""

    claimed: bool = False


@dataclass
class _Watched:
    """A running crawl phase, which the cancellation watcher may stop."""

    job_id: uuid.UUID
    task: "asyncio.Task[UrlResult]"
    stopped_for_cancellation: bool = False


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
        self._crawling: dict[str, _Watched] = {}  # crawl phases in progress, by entry ID
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
                "max_attempts": config.max_attempts,
            },
        )
        watcher = asyncio.create_task(self._watch_cancellations())
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
            await cancel_and_wait(watcher)
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
        counts: dict[str, int] = {}
        if reclaimed.deliveries:
            try:
                counts = await self._stream.delivery_counts(
                    [item.entry_id for item in reclaimed.deliveries]
                )
            except Exception as exc:
                # The XAUTOCLAIM already made these messages ours (and reset their idle time):
                # handle them now, as first deliveries, rather than hide them for another
                # reclaim idle time (Phase 6 independent review).
                logger.warning(
                    "Could not read the delivery counts of reclaimed messages",
                    extra={
                        "event": "task.delivery_counts_failed",
                        "count": len(reclaimed.deliveries),
                        "error_type": error_type(exc),
                    },
                )
            logger.info(
                "Took over messages idle in the consumer group",
                extra={
                    "event": "task.reclaimed",
                    "consumer": self.name,
                    "count": len(reclaimed.deliveries),
                    "message_ids": [item.entry_id for item in reclaimed.deliveries][:20],
                },
            )
        self._start(reclaimed.deliveries, reclaimed=True, delivery_counts=counts)
        return reclaimed.next_start

    def _start(
        self,
        deliveries: Iterable[Delivery],
        *,
        reclaimed: bool,
        delivery_counts: Mapping[str, int] | None = None,
    ) -> None:
        for delivery in deliveries:
            if delivery.entry_id in self._tasks:  # this worker is processing it already
                continue
            count = (delivery_counts or {}).get(delivery.entry_id, 1)
            task = asyncio.create_task(
                self._handle(delivery, reclaimed=reclaimed, deliveries=count)
            )
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

    async def _handle(self, delivery: Delivery, *, reclaimed: bool, deliveries: int = 1) -> None:
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
            handled = await self._handle_with_local_retries(delivery, deliveries)
        except asyncio.CancelledError:
            logger.warning(
                "Task cancelled; its message stays pending",
                extra={"event": "task.cancelled", "reclaimed": reclaimed},
            )
            raise
        settled = await self._settle(delivery, handled, deliveries)
        self.handled[handled.reason] += 1
        extra: dict[str, object] = {
            "event": "task.handled",
            "disposition": handled.disposition.value,
            "reason": handled.reason,
            "settled": settled,
            "reclaimed": reclaimed,
            "deliveries": deliveries,
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

    async def _handle_with_local_retries(self, delivery: Delivery, deliveries: int) -> Handled:
        """``_process``, tried again after a transient error that happened before the claim.

        Only within half the reclaim idle time: the message is not acknowledged meanwhile, so
        after that time ``XAUTOCLAIM`` may hand it out again (and count another delivery).
        """
        give_up_at = self._clock() + self._config.reclaim_min_idle / 2
        for local_try in range(_LOCAL_RETRIES + 1):
            progress = _Progress()
            try:
                handled = await self._process(delivery, deliveries, progress)
            except Exception as exc:  # the outcome may be uncertain: never acknowledge
                self._pause(self._task_backoff, "task", exc)
                wait = max(0.0, self._paused_until - self._clock())
                if (
                    progress.claimed
                    or not is_transient(exc)
                    or local_try == _LOCAL_RETRIES
                    or self._clock() + wait >= give_up_at
                ):
                    return Handled(Disposition.KEEP, "error")
                await asyncio.sleep(wait)
                continue
            if self._task_backoff.reset():
                logger.info(
                    "Tasks succeed again", extra={"event": "worker.recovered", "source": "task"}
                )
            return handled
        raise AssertionError("unreachable")  # pragma: no cover - the loop always returns

    async def _process(self, delivery: Delivery, deliveries: int, progress: _Progress) -> Handled:
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
            return Handled(Disposition.ACK, "completed", lease, result)
        if recorded.status is RecordStatus.RETRY_SCHEDULED:
            return Handled(Disposition.ACK, "retry_scheduled", lease, result)
        # Fenced: another claim took the URL over. Its message is not ours to acknowledge.
        return Handled(Disposition.KEEP, "fenced", lease, result)

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
            return Handled(Disposition.ACK, "cancelled", lease)
        return Handled(Disposition.KEEP, "fenced", lease)  # the lease was taken over

    async def _settle(self, delivery: Delivery, handled: Handled, deliveries: int) -> bool:
        """Acknowledge or dead-letter the message as decided; False if it stays pending."""
        try:
            if handled.disposition is Disposition.ACK:
                await self._stream.ack(delivery.entry_id)
            elif handled.disposition is Disposition.DEAD_LETTER:
                await self._stream.dead_letter(
                    delivery,
                    delivery.error or handled.reason,
                    kind=handled.reason,
                    consumer=self.name,
                    deliveries=deliveries,
                )
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
