r"""Indexer process: takes index tasks from the Redis stream and builds search vectors (Phase 7).

Start as many as needed, on one machine or several. Settings come from the environment and
backend\.env::

    .\.venv\Scripts\python.exe -m app.workers.indexer [--concurrency N]

Each process is one consumer of the group ``indexers`` on the stream ``<prefix>:index``. Its name
is unique per process start (``indexer-<host>-<pid>-<random>``) and is also the owner of the
leases it takes (``search_documents.lease_owner``), so log lines, ``crawl_events`` and the
consumer group all show which process did what. It runs up to ``SITESCOPE_INDEX_CONCURRENCY``
tasks at once; each task opens one session per transaction, so it holds at most one database
connection at a time, and no session is ever shared between tasks. The consumer loop (reading,
``XAUTOCLAIM``, settling, draining) is the crawler workers' (``app.workers.consumer``).

**Handling a message** ``{v, job_id, page_id, content_version}`` (ADR-0018):

1. A malformed message can never be processed: it is copied to the dead-letter stream and
   acknowledged.
2. The indexer starts an attempt (``search_index.start_index_attempt``): it counts the attempt in
   PostgreSQL and takes the document's lease, *before* any work, so an attempt that crashes its
   process counts too. The attempt waits for a dispatch of the document still in flight.
3. With the attempt: ``index_document`` builds the vector inside PostgreSQL from the page's
   current content, under the page row lock, and marks the document ``indexed``; the message is
   acknowledged after the commit. A vector that exceeds PostgreSQL's size limit is built again
   from a quarter of the body; if it still does not fit, the document fails
   (``tsvector_too_large``), which is final. The attempt has
   ``SITESCOPE_INDEX_TASK_DEADLINE_SECONDS`` in all: PostgreSQL stops each statement when the
   budget is spent, and the indexer abandons the transaction then too. Such an attempt failed
   (``deadline_exceeded``): its error is recorded, its lease released, and the message stays
   pending to be retried after ``SITESCOPE_INDEX_RECLAIM_MIN_IDLE_SECONDS``. The lease outlasts
   the deadline, so no other indexer takes over an attempt that may still run.
4. Without an attempt: the indexer reads the document's state (``inspect_document``) and decides
   (``judge_unstarted``). It acknowledges when nothing is left to do: the document is indexed or
   failed already, or it is ``pending`` again (the dispatch that published this message rolled
   back, or a reindex or a content change reset it: it will be published again). It leaves the
   message pending while another attempt holds a live lease. A document whose attempts are used
   up, with no attempt running, fails as ``max_attempts_exceeded``. A message naming an unknown
   page, a page of another job, or a content version the page never had is dead-lettered. A
   message for an older content version of a queued document starts an attempt like the current
   one: the attempt indexes the current content either way.

Delivery counts are never used to give up on a document: only attempts counted in PostgreSQL
are, so an outage that makes messages bounce does not fail documents that were never tried.

**Errors.** An error leaves the message pending: an uncertain outcome is never acknowledged. A
PostgreSQL or Redis error before the attempt started is retried here a few times, after a
backoff; after it, the message waits for ``XAUTOCLAIM`` (the attempt's lease expires first).
Errors also pause reading (exponential backoff with full jitter).

**Connections.** A task holds one database connection at a time, so an indexer never runs more
tasks than its connection pool holds (``SITESCOPE_DB_POOL_SIZE + SITESCOPE_DB_MAX_OVERFLOW``,
checked at startup): no task can wait for a connection after its attempt was counted.

**Stopping.** The first SIGINT or SIGTERM (Windows: Ctrl+C or CTRL_BREAK_EVENT) stops reading
and waits up to ``SITESCOPE_SHUTDOWN_GRACE_SECONDS`` for running tasks, then cancels the rest; a
second signal cancels them at once. A cancelled task's transaction rolls back and its message
stays pending, to be recovered like a crashed one; killing the process is safe for the same
reason.

**Observability** (Phase 10, ADR-0021). A message's ``task.handled`` line has its stage times
(``start_ms``: the attempt's start; ``index_ms``: building and storing the vector) and, for a
first attempt, its queue delay (``queue_delay_ms``: dispatch to start, by the database clock).
The same go to the process's metrics, with results by outcome and lease takeovers; the process
writes a heartbeat.
"""

import argparse
import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Self

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, task_context_var
from app.core.process import error_type, int_in_range, process_name
from app.core.redis import create_redis_client
from app.core.shutdown import StopSignals, install_stop_handlers
from app.db.models import IndexStatus
from app.db.session import Database
from app.indexing.documents import DEADLINE_EXCEEDED, MAX_ATTEMPTS_EXCEEDED
from app.observability.heartbeat import HeartbeatPublisher
from app.observability.metrics import QUEUE_DELAY_BUCKETS, REGISTRY, Registry
from app.queue.messages import IndexTask
from app.queue.streams import Delivery, IndexStream
from app.services.search_index import (
    DocumentStatus,
    IndexAttempt,
    IndexOutcome,
    IndexTaskRef,
    fail_exhausted_document,
    index_document,
    inspect_document,
    note_attempt_error,
    sqlstate,
    start_index_attempt,
)
from app.workers.consumer import Disposition, Handled, Progress, StreamConsumer

__all__ = [
    "IndexWorker",
    "IndexerConfig",
    "Retry",
    "indexer_database",
    "judge_unstarted",
    "main",
    "pool_capacity",
    "run",
    "serve",
]

logger = logging.getLogger(__name__)

# Starts per message: a refused start is tried again when the document turns out to be startable
# (it raced with a dispatch, or a lease just expired), at most this many times in all.
_START_ROUNDS: Final = 3
# SQLSTATE of a statement cut off by statement_timeout.
_QUERY_CANCELED: Final = "57014"
# An indexer's transactions run their statements back to back and never wait on the network, so
# a session idle inside one for this long belongs to a frozen process: PostgreSQL ends it, which
# rolls the transaction back and releases its row locks.
IDLE_TRANSACTION_TIMEOUT_MS: Final = 5000


class Retry(StrEnum):
    """A refused start that is worth another round."""

    START = "start"  # startable now: queued, attempts left, no live lease
    FAIL = "fail"  # attempts used up and no attempt running: fail the document


def judge_unstarted(
    status: DocumentStatus | None, task: IndexTask, *, max_attempts: int
) -> Handled | Retry:
    """What to do with a message whose attempt could not start.

    ``status`` is the document's authoritative state, read after the refused start. Acknowledge
    only when no work is left that this message stands for; leave the message pending while an
    attempt runs that may still need it.
    """
    if status is None:  # no search document for this page: not a task of this database
        return Handled(Disposition.DEAD_LETTER, "unknown_document")
    if status.job_id != task.job_id:
        return Handled(Disposition.DEAD_LETTER, "job_mismatch")
    if task.content_version > status.content_version:  # a version the page never had
        return Handled(Disposition.DEAD_LETTER, "version_ahead")
    if status.status is IndexStatus.INDEXED:  # a duplicate, or a redelivery after the commit
        return Handled(Disposition.ACK, "already_indexed")
    if status.status is IndexStatus.FAILED:
        return Handled(Disposition.ACK, "already_failed")
    if status.status is IndexStatus.PENDING:
        # The start waited for any dispatch of the row in flight. Still pending means the
        # dispatch that published this message rolled back, or the document was reset for
        # reindexing; either way the dispatcher publishes it again.
        return Handled(Disposition.ACK, "not_dispatched")
    # Queued. A message for an older content version stands for the same work: an attempt
    # indexes the current content. (Acknowledging it as outdated relied on the current version
    # having a message of its own, which a dispatch racing a content change does not guarantee:
    # independent review.)
    if status.lease_live:  # another attempt runs; if its indexer dies, this message recovers it
        return Handled(Disposition.KEEP, "leased")
    if status.attempt_count >= max_attempts:
        return Retry.FAIL
    return Retry.START


@dataclass(frozen=True, slots=True)
class IndexerConfig:
    concurrency: int = 4  # tasks in flight
    max_attempts: int = 3
    max_body_chars: int = 100_000
    task_deadline: float = 20.0  # the whole attempt's deadline (all its statements)
    reclaim_min_idle: float = 60.0  # longer than the task deadline
    read_block_ms: int = 2000
    reclaim_interval: float = 15.0
    reclaim_batch: int = 50
    grace_seconds: float = 30.0
    name: str = field(default_factory=lambda: process_name("indexer"))

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if not 0 < self.task_deadline < self.reclaim_min_idle:
            raise ValueError(
                "the task deadline must be positive and shorter than the reclaim idle time"
            )
        if self.reclaim_min_idle > 86_400:
            raise ValueError("the reclaim idle time must be at most one day")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_body_chars < 1:
            raise ValueError("max_body_chars must be at least 1")

    @property
    def lease_seconds(self) -> float:
        """Halfway between the task deadline and the reclaim idle time: an attempt, which the
        deadline ends, keeps its lease while it may still run, and a message reclaimed from a
        crashed indexer finds the lease expired."""
        return (self.task_deadline + self.reclaim_min_idle) / 2

    @classmethod
    def from_settings(cls, settings: Settings, *, concurrency: int | None = None) -> Self:
        concurrency = concurrency if concurrency is not None else settings.index_concurrency
        capacity = pool_capacity(settings)
        if concurrency > capacity:
            raise ValueError(
                f"an indexer runs at most {capacity} tasks at once, one per database connection"
                " (SITESCOPE_DB_POOL_SIZE + SITESCOPE_DB_MAX_OVERFLOW)"
            )
        return cls(
            concurrency=concurrency,
            max_attempts=settings.index_max_attempts,
            max_body_chars=settings.index_max_body_chars,
            task_deadline=settings.index_task_deadline_seconds,
            reclaim_min_idle=settings.index_reclaim_min_idle_seconds,
            read_block_ms=settings.stream_read_block_ms,
            reclaim_interval=settings.stream_reclaim_interval_seconds,
            reclaim_batch=settings.stream_reclaim_batch_size,
            grace_seconds=settings.shutdown_grace_seconds,
        )


class IndexMetrics:
    """The metric families of an indexer, beyond the consumer's."""

    def __init__(self, registry: Registry) -> None:
        self.queue_delay = registry.histogram(
            "sitescope_queue_delay_seconds",
            "Time from a task's last publish (its dispatch, or a republish by the reconciliation) "
            "to its claim or first attempt, by the database clock.",
            buckets=QUEUE_DELAY_BUCKETS,
        )
        self.results = registry.counter(
            "sitescope_index_results_total",
            "Results of index attempts, by outcome (indexed, failed, not_queued, "
            "deadline_exceeded).",
            ("outcome",),
        )


class IndexWorker(StreamConsumer[IndexTask, IndexerConfig]):
    """The consumer loop of one indexer process. See the module docstring."""

    DISPLAY_NAME = "Indexer"

    def __init__(
        self,
        database: Database,
        stream: IndexStream,
        config: IndexerConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        registry: Registry = REGISTRY,
    ) -> None:
        super().__init__(database, stream, config, clock=clock, registry=registry)
        self._index_metrics = IndexMetrics(registry)

    def _started_fields(self) -> dict[str, object]:
        config = self._config
        return {
            "role": "indexer",
            "lease_seconds": config.lease_seconds,
            "task_deadline_seconds": config.task_deadline,
            "max_attempts": config.max_attempts,
            "max_body_chars": config.max_body_chars,
        }

    async def _process(
        self, delivery: Delivery[IndexTask], deliveries: int, progress: Progress
    ) -> Handled:
        task = delivery.task
        if task is None:
            return Handled(Disposition.DEAD_LETTER, "malformed")
        config = self._config
        ref = IndexTaskRef(task.job_id, task.page_id, task.content_version)
        for _ in range(_START_ROUNDS):
            progress.stage = "start_attempt"
            start_started = self._clock()
            with self._timed("start_attempt"):
                async with self._database.session() as session:
                    attempt = await start_index_attempt(
                        session,
                        ref,
                        owner=self.name,
                        lease_seconds=config.lease_seconds,
                        max_attempts=config.max_attempts,
                    )
            if attempt is not None:
                progress.claimed = True
                return await self._index(
                    ref, attempt, progress, start_seconds=self._clock() - start_started
                )
            progress.stage = "inspect"
            with self._timed("inspect"):
                async with self._database.session() as session:
                    status = await inspect_document(session, task.page_id)
            verdict = judge_unstarted(status, task, max_attempts=config.max_attempts)
            if isinstance(verdict, Handled):
                return verdict
            if verdict is Retry.FAIL:
                progress.stage = "fail_exhausted"
                with self._timed("fail_exhausted"):
                    async with self._database.session() as session:
                        failed = await fail_exhausted_document(
                            session, ref, max_attempts=config.max_attempts, worker_id=self.name
                        )
                if failed:
                    self._metrics.recoveries.inc(kind="exhausted_document_failed")
                    return Handled(Disposition.ACK, MAX_ATTEMPTS_EXCEEDED)
        return Handled(Disposition.KEEP, "start_contended")

    async def _index(
        self, ref: IndexTaskRef, attempt: IndexAttempt, progress: Progress, *, start_seconds: float
    ) -> Handled:
        """The started attempt: build and store the vector, then decide the message's fate."""
        config = self._config
        task_context_var.set(
            {
                **(task_context_var.get() or {}),
                "attempt": attempt.attempt,
                "content_version": attempt.content_version,
            }
        )
        details: dict[str, object] = {
            "attempt": attempt.attempt,
            "takeover": attempt.takeover,
            "start_ms": round(start_seconds * 1000, 1),
        }
        if attempt.takeover:
            self._metrics.recoveries.inc(kind="index_lease_takeover")
        if attempt.queued_seconds is not None:
            self._index_metrics.queue_delay.observe(attempt.queued_seconds)
            details["queue_delay_ms"] = round(attempt.queued_seconds * 1000, 1)
        progress.stage = "index_document"
        index_started = self._clock()
        try:
            # The whole attempt ends by its deadline: PostgreSQL stops each statement when the
            # budget is spent, and this timeout abandons the transaction (it rolls back) if the
            # server does not answer in time. The lease lasts longer.
            with self._timed("index_document"):
                async with asyncio.timeout(config.task_deadline):
                    async with self._database.session() as session:
                        result = await index_document(
                            session,
                            ref,
                            max_body_chars=config.max_body_chars,
                            budget_seconds=config.task_deadline,
                            worker_id=self.name,
                        )
        except Exception as exc:
            if not isinstance(exc, TimeoutError) and sqlstate(exc) != _QUERY_CANCELED:
                raise  # transient or unexpected: the message stays pending
            self._index_metrics.results.inc(outcome=DEADLINE_EXCEEDED)
            await self._note_error(ref, DEADLINE_EXCEEDED)
            return Handled(Disposition.KEEP, DEADLINE_EXCEEDED, details)
        details["index_ms"] = round((self._clock() - index_started) * 1000, 1)
        self._index_metrics.results.inc(outcome=result.outcome.value)
        if result.outcome is IndexOutcome.INDEXED:
            return Handled(
                Disposition.ACK,
                "indexed",
                {
                    **details,
                    "indexed_version": result.content_version,
                    "lexemes": result.lexemes,
                    "body_chars": result.body_chars,
                    "truncated": result.truncated,
                },
            )
        if result.outcome is IndexOutcome.FAILED:
            return Handled(Disposition.ACK, "index_failed", {**details, "error": result.reason})
        status = result.status.value if result.status is not None else None
        return Handled(Disposition.ACK, "not_queued", {**details, "status": status})

    async def _note_error(self, ref: IndexTaskRef, reason: str) -> None:
        """Record why the attempt failed and release its lease; best effort (the lease also
        expires on its own)."""
        try:
            with self._timed("note_error"):
                async with self._database.session() as session:
                    await note_attempt_error(session, ref.page_id, owner=self.name, reason=reason)
        except Exception as exc:
            logger.debug(
                "Could not record an attempt's error",
                extra={"event": "index.note_error_failed", "error_type": error_type(exc)},
            )


def pool_capacity(settings: Settings) -> int:
    """The most database connections one process's pool opens."""
    return settings.db_pool_size + settings.db_max_overflow


def indexer_database(settings: Settings) -> Database:
    """An indexer's PostgreSQL access (see ``IDLE_TRANSACTION_TIMEOUT_MS``)."""
    return Database(
        settings,
        application_name="sitescope-indexer",
        server_settings={"idle_in_transaction_session_timeout": str(IDLE_TRANSACTION_TIMEOUT_MS)},
    )


async def run(settings: Settings, signals: StopSignals, *, concurrency: int | None = None) -> None:
    """Build an indexer from settings and run it until ``signals`` stop it."""
    config = IndexerConfig.from_settings(settings, concurrency=concurrency)
    database = indexer_database(settings)
    redis = create_redis_client(settings, client_name="sitescope-indexer")
    try:
        worker = IndexWorker(
            database, IndexStream(redis, key_prefix=settings.redis_key_prefix), config
        )
        heartbeat = HeartbeatPublisher(
            redis,
            key_prefix=settings.redis_key_prefix,
            name=config.name,
            role="indexer",
            interval=settings.heartbeat_interval_seconds,
            state=worker.heartbeat_state,
        )
        await worker.run(signals.stop, signals.force, heartbeat=heartbeat)
    finally:
        await redis.aclose()
        await database.dispose()


async def serve(settings: Settings, *, concurrency: int | None = None) -> None:
    """The command's work: run an indexer until SIGINT or SIGTERM."""
    signals = StopSignals()
    install_stop_handlers(signals, process="indexer")
    await run(settings, signals, concurrency=concurrency)


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.indexer",
        description=(
            "Run an indexer: build the search vectors of crawled pages from the index stream"
            " until stopped (Ctrl+C, or SIGTERM)."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int_in_range(1, 64),
        default=None,
        help="index tasks processed at once, 1 to 64 (default: SITESCOPE_INDEX_CONCURRENCY)",
    )
    args = parser.parse_args(argv)
    settings = settings or get_settings()
    if args.concurrency is not None and args.concurrency > pool_capacity(settings):
        parser.error(
            f"--concurrency must not exceed the connection pool ({pool_capacity(settings)}:"
            " SITESCOPE_DB_POOL_SIZE + SITESCOPE_DB_MAX_OVERFLOW)"
        )
    configure_logging(settings.log_level, service="indexer")
    asyncio.run(serve(settings, concurrency=args.concurrency))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
