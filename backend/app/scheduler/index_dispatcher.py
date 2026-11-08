r"""The index dispatcher process: publishes index tasks and completes job indexing (Phase 7).

Run from backend\ (settings come from the environment and backend\.env)::

    .\.venv\Scripts\python.exe -m app.scheduler.index_dispatcher

It moves search documents through the indexing outbox (ADR-0008, ADR-0018) and nothing else:
unlike the scheduler, it never starts a job, dispatches a crawl task or changes a job's crawl
status. Every ``SITESCOPE_INDEX_DISPATCHER_INTERVAL_SECONDS``, one pass, each step its own short
transaction (``services/search_index.py``):

1. **Dispatch** (I1) ``pending`` documents, oldest change first: lock them
   (``FOR UPDATE SKIP LOCKED``), publish one ``{v, job_id, page_id, content_version}`` message
   each to ``<prefix>:index`` (``XADD``), and mark them ``queued`` with their entry IDs, in one
   transaction. At most ``SITESCOPE_INDEX_DISPATCH_BATCH_SIZE`` per pass, and at most
   ``SITESCOPE_INDEX_MAX_OUTSTANDING`` queued at any time (backpressure).
2. **Complete** (I5) the indexing of finished jobs (``completed``, ``failed`` or ``cancelled``)
   none of whose documents is ``pending`` or ``queued``: ``indexing_completed_at`` is set under
   the job row lock, and a ``job.indexing_completed`` event is written. Only such jobs are looked
   at, up to ``COMPLETION_SCAN_LIMIT`` per pass, so jobs still being indexed never delay the
   completion of others.
3. **Reconcile**, every ``reconcile_after / 4`` seconds (at most 30 s): publish again the queued
   documents without progress for ``SITESCOPE_INDEX_RECONCILE_AFTER_SECONDS`` whose stream entry
   is gone (Redis lost it, or it was trimmed). Lost or abandoned attempts need nothing here: the
   indexers' ``XAUTOCLAIM`` and the attempt count recover them.
4. **Maintain**, every ``retention / 4`` seconds (at most 60 s): trim the index stream (never an
   undelivered or pending entry), remove idle indexer consumers that have nothing pending, and
   log statistics counted from PostgreSQL and Redis (``index_dispatcher.stats``).

**Failures.** A failed pass (PostgreSQL or Redis unreachable) is logged and retried with
exponential backoff and full jitter; a job whose completion step fails for a reason of its own
is logged (``index_dispatcher.job_failed``) and skipped for that pass. A dispatcher that crashes
after an ``XADD`` and before its commit leaves the documents ``pending``: they are published
again, and the earlier message finds nothing to do. One that stalls inside a dispatch loses its
session after ``idle_in_transaction_session_timeout`` (twice the Redis timeout, plus 5 s),
which releases its row locks.

**Several dispatchers** may run at once, without a leader: dispatch and reconciliation skip rows
another one has locked, completion is serialized by the job row lock, and trimming is
idempotent. The backpressure limit is then approximate.

**Stopping.** SIGINT or SIGTERM (Windows: Ctrl+C or CTRL_BREAK_EVENT) ends the loop after the
current pass; a second signal cancels the pass, and an unfinished dispatch rolls back.
"""

import argparse
import asyncio
import logging
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
from app.queue.messages import IndexTask
from app.queue.streams import IndexStream
from app.scheduler.passes import PassLoop, run_until_stopped
from app.scheduler.scheduler import scheduler_database
from app.services.crawl_state import ReconcileCursor
from app.services.search_index import (
    IndexTaskRef,
    completable_indexing_jobs,
    complete_job_indexing,
    dispatch_index_tasks,
    indexing_overview,
    queued_index_tasks,
    republish_stale_index_tasks,
)

logger = logging.getLogger(__name__)

# Finished jobs with nothing left to index that are completed per pass, oldest finish first.
COMPLETION_SCAN_LIMIT: Final = 100


@dataclass(frozen=True, slots=True)
class IndexDispatcherConfig:
    interval: float = 0.5  # seconds between passes
    dispatch_batch: int = 200  # tasks per pass; also the reconciliation batch
    max_outstanding: int = 1000  # documents queued at most
    reconcile_after: float = 300.0
    retention_seconds: int = 3600
    consumer_max_idle_seconds: int = 3600
    name: str = field(default_factory=lambda: process_name("index-dispatcher"))

    @property
    def reconcile_interval(self) -> float:
        return min(30.0, self.reconcile_after / 4)

    @property
    def maintenance_interval(self) -> float:
        return min(60.0, self.retention_seconds / 4)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            interval=settings.index_dispatcher_interval_seconds,
            dispatch_batch=settings.index_dispatch_batch_size,
            max_outstanding=settings.index_max_outstanding,
            reconcile_after=settings.index_reconcile_after_seconds,
            retention_seconds=settings.stream_retention_seconds,
            consumer_max_idle_seconds=settings.stream_consumer_max_idle_seconds,
        )


@dataclass(frozen=True, slots=True)
class IndexPassReport:
    """What one index dispatcher pass did."""

    dispatched: int = 0  # tasks published
    republished: int = 0  # tasks published again by the reconciliation
    completed_jobs: int = 0  # jobs whose indexing was completed
    queued: int = 0  # documents queued before this pass dispatched
    failed_jobs: int = 0  # jobs skipped in this pass after an error of their own

    @property
    def busy(self) -> bool:
        return any((self.dispatched, self.republished, self.completed_jobs, self.failed_jobs))


class IndexDispatcher(PassLoop[IndexPassReport]):
    """The index dispatcher's loop. See the module docstring."""

    ROLE = "index_dispatcher"
    ID_FIELD = "dispatcher_id"
    DISPLAY_NAME = "Index dispatcher"

    def __init__(
        self,
        database: Database,
        stream: IndexStream,
        config: IndexDispatcherConfig,
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
            "dispatch_batch": config.dispatch_batch,
            "max_outstanding": config.max_outstanding,
            "reconcile_after_seconds": config.reconcile_after,
        }

    async def run_pass(self) -> IndexPassReport:
        """One pass: dispatch, complete jobs, and from time to time reconcile and maintain."""
        config = self._config
        async with self._database.session() as session:
            queued = await queued_index_tasks(session)
        room = min(config.dispatch_batch, config.max_outstanding - queued)
        dispatched = 0
        if room > 0:
            async with self._database.session() as session:
                dispatched = len(
                    await dispatch_index_tasks(session, limit=room, publish=self._publish)
                )

        async with self._database.session() as session:
            jobs = await completable_indexing_jobs(session, limit=COMPLETION_SCAN_LIMIT)
        completed = failed_jobs = 0
        for job_id in jobs:
            done = await self._for_job(job_id, self._complete(job_id))
            if done is None:
                failed_jobs += 1
            elif done:
                completed += 1

        republished = 0
        if self._clock() >= self._next_reconcile:
            republished = await self._reconcile()
            self._next_reconcile = self._clock() + config.reconcile_interval
        if self._clock() >= self._next_maintenance:
            await self._maintain(queued)
            self._next_maintenance = self._clock() + config.maintenance_interval
        return IndexPassReport(
            dispatched=dispatched,
            republished=republished,
            completed_jobs=completed,
            queued=queued,
            failed_jobs=failed_jobs,
        )

    async def _complete(self, job_id: uuid.UUID) -> bool:
        async with self._database.session() as session:
            return await complete_job_indexing(session, job_id)

    async def _publish(self, tasks: Sequence[IndexTaskRef]) -> list[str]:
        return await self._stream.publish(
            [IndexTask(task.job_id, task.page_id, task.content_version) for task in tasks]
        )

    async def _reconcile(self) -> int:
        config = self._config
        # The group must exist before entries can count as undelivered (TaskStream.delivering);
        # this also recreates it after Redis lost its data.
        await self._ensure_group()
        async with self._database.session() as session:
            result = await republish_stale_index_tasks(
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

    async def _maintain(self, queued: int) -> None:
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
            overview = await indexing_overview(session)
        pending = await self._stream.pending()
        logger.info(
            "Index dispatcher statistics",
            extra={
                "event": "index_dispatcher.stats",
                "dispatcher_id": self.name,
                "documents_pending": overview.pending,
                "documents_queued": overview.queued,
                "documents_active": overview.active,
                "documents_indexed": overview.indexed,
                "documents_failed": overview.failed,
                "oldest_pending_seconds": overview.oldest_pending_seconds,
                "oldest_queued_seconds": overview.oldest_queued_seconds,
                "jobs_awaiting_completion": overview.jobs_awaiting_completion,
                "queued_at_pass_start": queued,
                "stream_length": await self._stream.length(),
                "pending_messages": pending.count,
                "pending_by_consumer": pending.per_consumer,
                "trimmed": trimmed,
            },
        )


def index_dispatcher_database(settings: Settings) -> Database:
    """The index dispatcher's PostgreSQL access: like the scheduler's, it publishes inside
    transactions (``scheduler.idle_transaction_timeout_ms``)."""
    return scheduler_database(settings, application_name="sitescope-index-dispatcher")


async def run(settings: Settings, signals: StopSignals) -> None:
    """Build an index dispatcher from settings and run it until ``signals`` stop it."""
    database = index_dispatcher_database(settings)
    redis = create_redis_client(settings, client_name="sitescope-index-dispatcher")
    try:
        dispatcher = IndexDispatcher(
            database,
            IndexStream(redis, key_prefix=settings.redis_key_prefix),
            IndexDispatcherConfig.from_settings(settings),
        )
        await run_until_stopped(dispatcher, signals)
    finally:
        await redis.aclose()
        await database.dispose()


async def serve(settings: Settings) -> None:
    """The command's work: run the index dispatcher until SIGINT or SIGTERM."""
    signals = StopSignals()
    install_stop_handlers(signals, process="index_dispatcher")
    await run(settings, signals)


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scheduler.index_dispatcher",
        description=(
            "Run the index dispatcher until stopped (Ctrl+C, or SIGTERM): publish the pages"
            " waiting to be indexed to the index stream, and mark the indexing of finished jobs"
            " complete. It starts no crawl job."
        ),
    )
    parser.parse_args(argv)
    settings = settings or get_settings()
    configure_logging(settings.log_level, service="index-dispatcher")
    asyncio.run(serve(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
