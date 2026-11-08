r"""The scheduler process: starts jobs, advances BFS levels, publishes crawl tasks (Phase 5).

Run from backend\ (settings come from the environment and backend\.env)::

    .\.venv\Scripts\python.exe -m app.scheduler

**It starts every queued job** of its database, oldest first. Never point it at a database whose
queued jobs must not run.

Every ``SITESCOPE_SCHEDULER_INTERVAL_SECONDS``, one pass, each step its own short transaction:

1. **Activate** (T2) queued jobs, oldest first, while fewer than
   ``SITESCOPE_SCHEDULER_MAX_RUNNING_JOBS`` are running. An advisory lock makes the cap exact
   across schedulers.
2. **Advance** (T6) each running job that has no unfinished URL: admit its next BFS level, or
   finish it (``completed`` or ``failed``). The job row lock serializes schedulers. Every
   running job is looked at (up to ``FRONTIER_SCAN_LIMIT``), not only as many as the cap.
3. **Dispatch** (T3) pending URLs of each job's current level. The rows are locked
   (``FOR UPDATE SKIP LOCKED``), published (``XADD``, one message per URL), and marked
   ``queued`` with their entry IDs in the same transaction. Backpressure: at most
   ``SITESCOPE_SCHEDULER_MAX_IN_FLIGHT_PER_JOB`` URLs of a job are queued or in progress, and
   at most ``SITESCOPE_SCHEDULER_TASKS_PER_HOST_SLOT`` per politeness slot of its host (Phase 10,
   ``in_flight_per_job``), at most ``SITESCOPE_SCHEDULER_MAX_OUTSTANDING`` over all jobs, and at
   most ``SITESCOPE_SCHEDULER_DISPATCH_BATCH_SIZE`` per job and pass. The jobs share the free
   room fairly, oldest first.
4. **Settle cancellations** (Phase 6) of each ``cancelling`` job: cancel its pending and queued
   URLs, and its in-progress URLs whose lease expired, then mark it ``cancelled`` once none is
   unfinished (``crawl_cancellation.settle_cancellation``).
5. **Reconcile**, every ``reconcile_after / 4`` seconds (at most 30 s): publish again the
   tasks without progress whose stream entry is gone (``republish_stale``).
6. **Maintain**, every ``retention / 4`` seconds (at most 60 s): trim the stream (never
   undelivered or pending entries), remove idle consumers that have nothing pending, forget
   expired idempotency keys, and log statistics.

Retries need no step of their own: a URL waiting for a retry is ``pending`` with a future
``next_attempt_at``, which dispatch skips until it is due; it also keeps its BFS level open.

**Failures.** A failed pass (PostgreSQL or Redis unreachable) is logged and retried with
exponential backoff and full jitter. A job whose step fails for a reason of its own is logged
(``scheduler.job_failed``) and skipped for that pass; the other jobs go on. Nothing is lost:
every step is a transaction on PostgreSQL state, and a message published by a dispatch that did
not commit is a harmless duplicate. A
scheduler that stalls inside a dispatch transaction (between its ``XADD`` and its commit) loses
its session after ``idle_in_transaction_session_timeout`` (twice the Redis timeout, plus 5 s),
which releases the row locks it held.

**Several schedulers** may run at once, without a leader (ADR-0016): activation is serialized
by the advisory lock, level advancement by the job row lock, dispatch and reconciliation skip
rows another scheduler has locked, and trimming is idempotent. The backpressure limits are then
approximate, because each scheduler may fill the room it saw.

**Stopping.** SIGINT or SIGTERM (Windows: Ctrl+C or CTRL_BREAK_EVENT) ends the loop after the
current pass. A second signal cancels the pass; an unfinished dispatch then rolls back.
"""

import argparse
import asyncio
import logging
import math
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Self

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.process import process_name
from app.core.redis import create_redis_client
from app.core.shutdown import StopSignals, install_stop_handlers
from app.db.session import Database
from app.observability.heartbeat import HeartbeatPublisher
from app.queue.messages import CrawlTask
from app.queue.streams import CrawlStream
from app.scheduler.passes import PassLoop, run_until_stopped
from app.services.crawl_cancellation import Settlement, cancelling_jobs, settle_cancellation
from app.services.crawl_jobs import expire_idempotency_keys
from app.services.crawl_state import (
    AdvanceAction,
    AdvanceResult,
    JobFrontier,
    ReconcileCursor,
    activate_jobs,
    advance_job,
    dispatch_urls,
    outstanding_tasks,
    republish_stale,
    running_frontiers,
)

logger = logging.getLogger(__name__)

# Running jobs looked at per pass, oldest first. Not the running-jobs cap: the cap limits
# activation only, and every running job must make progress. The same bound applies to the
# cancelling jobs settled per pass.
FRONTIER_SCAN_LIMIT: Final = 1000
_EXPIRED_KEYS_PER_PASS: Final = 500


def in_flight_per_job(settings: Settings) -> int:
    """A job's tasks queued or in progress at most: ``SITESCOPE_SCHEDULER_MAX_IN_FLIGHT_PER_JOB``,
    and at most ``SITESCOPE_SCHEDULER_TASKS_PER_HOST_SLOT`` per politeness slot of its host.

    A job crawls one host (its seed's), which serves at most
    ``SITESCOPE_POLITENESS_MAX_CONCURRENT_PER_HOST`` requests at once. Tasks beyond a few per slot
    only wait for a slot inside a worker, holding one of its task slots, while the stream's next
    messages (other jobs, other hosts) wait behind them: Phase 10 measured five hosts crawled
    about one at a time with the old limit of 100 (ADR-0022, docs/performance.md).
    """
    per_host = settings.scheduler_tasks_per_host_slot * settings.politeness_max_concurrent_per_host
    return min(settings.scheduler_max_in_flight_per_job, per_host)


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    interval: float = 0.5  # seconds between passes
    dispatch_batch: int = 100  # tasks per job and pass; also the reconciliation batch
    max_in_flight_per_job: int = 100
    max_outstanding: int = 1000
    max_running_jobs: int = 10
    reconcile_after: float = 300.0
    retention_seconds: int = 3600
    consumer_max_idle_seconds: int = 3600
    idempotency_key_ttl_seconds: int = 86_400
    name: str = field(default_factory=lambda: process_name("scheduler"))

    @property
    def reconcile_interval(self) -> float:
        return min(30.0, self.reconcile_after / 4)

    @property
    def maintenance_interval(self) -> float:
        return min(60.0, self.retention_seconds / 4)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            interval=settings.scheduler_interval_seconds,
            dispatch_batch=settings.scheduler_dispatch_batch_size,
            max_in_flight_per_job=in_flight_per_job(settings),
            max_outstanding=settings.scheduler_max_outstanding,
            max_running_jobs=settings.scheduler_max_running_jobs,
            reconcile_after=settings.scheduler_reconcile_after_seconds,
            retention_seconds=settings.stream_retention_seconds,
            consumer_max_idle_seconds=settings.stream_consumer_max_idle_seconds,
            idempotency_key_ttl_seconds=settings.idempotency_key_ttl_seconds,
        )


@dataclass(frozen=True, slots=True)
class PassReport:
    """What one scheduler pass did."""

    activated: int = 0  # jobs started
    advanced: int = 0  # jobs whose next level was admitted
    finished: int = 0  # jobs completed or failed
    dispatched: int = 0  # tasks published
    republished: int = 0  # tasks published again by the reconciliation
    outstanding: int = 0  # tasks queued or in progress before this pass dispatched
    failed_jobs: int = 0  # jobs skipped in this pass after an error of their own
    cancelled: int = 0  # cancelling jobs whose cancellation finished in this pass

    @property
    def progressed(self) -> bool:
        """It did work. A job's failed step is not work: a job failing in every pass must not
        look like progress (found by the Phase 10 review)."""
        return any(
            (
                self.activated,
                self.advanced,
                self.finished,
                self.dispatched,
                self.republished,
                self.cancelled,
            )
        )

    @property
    def busy(self) -> bool:
        """Worth a log line: it did work, or a job's step failed."""
        return self.progressed or bool(self.failed_jobs)


class Scheduler(PassLoop[PassReport]):
    """The scheduler's loop. See the module docstring."""

    ROLE = "scheduler"
    ID_FIELD = "scheduler_id"
    DISPLAY_NAME = "Scheduler"
    COUNTED_FIELDS = (
        "activated",
        "advanced",
        "finished",
        "dispatched",
        "republished",
        "failed_jobs",
        "cancelled",
    )

    def __init__(
        self,
        database: Database,
        stream: CrawlStream,
        config: SchedulerConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(interval=config.interval, clock=clock)
        self._database = database
        self._stream = stream
        self._config = config
        self._next_reconcile = 0.0  # at once: a restart may follow a loss of Redis data
        self._next_maintenance = 0.0
        self._reconcile_cursor: ReconcileCursor | None = None

    @property
    def name(self) -> str:
        return self._config.name

    def _started_fields(self) -> dict[str, object]:
        config = self._config
        return {
            "stream": self._stream.key,
            "max_running_jobs": config.max_running_jobs,
            "max_outstanding": config.max_outstanding,
            "max_in_flight_per_job": config.max_in_flight_per_job,
        }

    async def run_pass(self) -> PassReport:
        """One pass: activate, advance, dispatch, and from time to time reconcile and maintain."""
        config = self._config
        async with self._database.session() as session:
            activated = await activate_jobs(
                session, max_running=config.max_running_jobs, limit=config.max_running_jobs
            )
        # Every running job, not only as many as the cap: jobs started another way (the
        # single-process driver) or before the cap was lowered must not be starved.
        async with self._database.session() as session:
            frontiers = await running_frontiers(session, limit=FRONTIER_SCAN_LIMIT)

        ready: list[JobFrontier] = []  # jobs with pending URLs to dispatch
        advanced = finished = failed_jobs = 0
        for frontier in frontiers:
            if frontier.unfinished == 0:
                result = await self._for_job(frontier.job_id, self._advance(frontier.job_id))
                if result is None:
                    failed_jobs += 1
                elif result.action is AdvanceAction.ADVANCED:
                    advanced += 1
                    ready.append(JobFrontier(frontier.job_id, result.admitted, 0))
                elif result.action in (AdvanceAction.COMPLETED, AdvanceAction.FAILED):
                    finished += 1
                # LEVEL_IN_PROGRESS (another scheduler advanced it first) or NOT_RUNNING: the
                # next pass sees the job's new state.
            elif frontier.unfinished > frontier.in_flight:
                ready.append(frontier)

        async with self._database.session() as session:
            outstanding = await outstanding_tasks(session)
        room = config.max_outstanding - outstanding
        dispatched = 0
        for index, frontier in enumerate(ready):
            if room <= 0:
                break
            share = math.ceil(room / (len(ready) - index))  # fair share of what is left
            window = min(
                config.max_in_flight_per_job - frontier.in_flight, config.dispatch_batch, share
            )
            if window <= 0:
                continue
            published = await self._for_job(
                frontier.job_id, self._dispatch(frontier.job_id, window)
            )
            if published is None:
                failed_jobs += 1
                continue
            room -= len(published)
            dispatched += len(published)

        cancelled = 0
        async with self._database.session() as session:
            to_settle = await cancelling_jobs(session, limit=FRONTIER_SCAN_LIMIT)
        for job_id in to_settle:
            settlement = await self._for_job(job_id, self._settle_cancellation(job_id))
            if settlement is None:
                failed_jobs += 1
            elif settlement.finalized:
                cancelled += 1

        republished = 0
        if self._clock() >= self._next_reconcile:
            republished = await self._reconcile()
            self._next_reconcile = self._clock() + config.reconcile_interval
        if self._clock() >= self._next_maintenance:
            await self._maintain(outstanding)
            self._next_maintenance = self._clock() + config.maintenance_interval
        return PassReport(
            activated=len(activated),
            advanced=advanced,
            finished=finished,
            dispatched=dispatched,
            republished=republished,
            outstanding=outstanding,
            failed_jobs=failed_jobs,
            cancelled=cancelled,
        )

    async def _advance(self, job_id: uuid.UUID) -> AdvanceResult:
        async with self._database.session() as session:
            return await advance_job(session, job_id)

    async def _settle_cancellation(self, job_id: uuid.UUID) -> Settlement:
        async with self._database.session() as session:
            return await settle_cancellation(session, job_id)

    async def _dispatch(self, job_id: uuid.UUID, window: int) -> list[uuid.UUID]:
        async with self._database.session() as session:
            return await dispatch_urls(session, job_id, limit=window, publish=self._publish)

    async def _publish(self, tasks: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> list[str]:
        return await self._stream.publish([CrawlTask(job_id, url_id) for job_id, url_id in tasks])

    async def _reconcile(self) -> int:
        config = self._config
        # The group must exist before entries can count as undelivered (CrawlStream.delivering);
        # this also recreates it after Redis lost its data.
        await self._ensure_group()
        async with self._database.session() as session:
            result = await republish_stale(
                session,
                older_than=config.reconcile_after,
                limit=config.dispatch_batch,
                publish=self._publish,
                delivering=self._stream.delivering,
                after=self._reconcile_cursor,
            )
        self._reconcile_cursor = result.resume_after
        return result.republished

    async def _ensure_group(self) -> None:
        if await self._stream.ensure_group():
            # The first start on an empty Redis, or Redis lost its data (ADR-0016).
            logger.info(
                "The consumer group did not exist and was created",
                extra={"event": "stream.group_created", "stream": self._stream.key},
            )

    async def _maintain(self, outstanding: int) -> None:
        config = self._config
        # Without the group, trimming would not know which entries are still undelivered.
        await self._ensure_group()
        trimmed = await self._stream.trim(retention_ms=config.retention_seconds * 1000)
        removed = await self._stream.remove_idle_consumers(
            max_idle_ms=config.consumer_max_idle_seconds * 1000
        )
        if removed:
            logger.info(
                "Removed idle consumers without pending messages",
                extra={"event": "stream.consumers_removed", "consumers": removed},
            )
        async with self._database.session() as session:
            expired_keys = await expire_idempotency_keys(
                session, ttl=config.idempotency_key_ttl_seconds, limit=_EXPIRED_KEYS_PER_PASS
            )
        if expired_keys:
            logger.info(
                "Forgot expired idempotency keys",
                extra={"event": "api.idempotency_keys_expired", "count": expired_keys},
            )
        pending = await self._stream.pending()
        logger.info(
            "Scheduler statistics",
            extra={
                "event": "scheduler.stats",
                "scheduler_id": self.name,
                "outstanding": outstanding,
                "stream_length": await self._stream.length(),
                "pending_messages": pending.count,
                "pending_by_consumer": pending.per_consumer,
                "trimmed": trimmed,
            },
        )


def idle_transaction_timeout_ms(settings: Settings) -> int:
    """The scheduler sessions' ``idle_in_transaction_session_timeout``.

    A transaction waits on at most two Redis round trips, each bounded by the Redis socket
    timeout: a dispatch publishes (``XADD``); a reconciliation checks the stale tasks' entries
    (one pipeline, ``CrawlStream.delivering``), then publishes. A session idle in a transaction
    for longer belongs to a stalled scheduler, and PostgreSQL ends it, which releases its row
    locks.
    """
    return round((2 * settings.redis_socket_timeout_seconds + 5) * 1000)


def scheduler_database(
    settings: Settings, *, application_name: str = "sitescope-scheduler"
) -> Database:
    """The PostgreSQL access of a process that publishes inside transactions: the scheduler and
    the index dispatcher (see ``idle_transaction_timeout_ms``)."""
    return Database(
        settings,
        application_name=application_name,
        server_settings={
            "idle_in_transaction_session_timeout": str(idle_transaction_timeout_ms(settings))
        },
    )


async def run(settings: Settings, signals: StopSignals) -> None:
    """Build a scheduler from settings and run it until ``signals`` stop it."""
    database = scheduler_database(settings)
    redis = create_redis_client(settings, client_name="sitescope-scheduler")
    try:
        scheduler = Scheduler(
            database,
            CrawlStream(redis, key_prefix=settings.redis_key_prefix),
            SchedulerConfig.from_settings(settings),
        )
        heartbeat = HeartbeatPublisher(
            redis,
            key_prefix=settings.redis_key_prefix,
            name=scheduler.name,
            role="scheduler",
            interval=settings.heartbeat_interval_seconds,
            state=scheduler.heartbeat_state,
        )
        await run_until_stopped(scheduler, signals, heartbeat=heartbeat)
    finally:
        await redis.aclose()
        await database.dispose()


async def serve(settings: Settings) -> None:
    """The command's work: run the scheduler until SIGINT or SIGTERM."""
    signals = StopSignals()
    install_stop_handlers(signals, process="scheduler")
    await run(settings, signals)


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scheduler",
        description=(
            "Run the crawl scheduler until stopped (Ctrl+C, or SIGTERM). It starts the queued"
            " jobs of its database: never point it at a database whose queued jobs must not run."
        ),
    )
    parser.parse_args(argv)
    settings = settings or get_settings()
    configure_logging(settings.log_level, service="scheduler")
    asyncio.run(serve(settings))
    return 0
