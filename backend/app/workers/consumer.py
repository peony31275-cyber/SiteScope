"""The consumer loop shared by every stream worker process: crawler workers and indexers.

A ``StreamConsumer`` is one consumer of one task stream's group (``app.queue.streams``). Its name
is unique per process start (``<role>-<host>-<pid>-<random>``). It runs up to ``concurrency``
tasks at once and reads only as many messages as it has free slots. Subclasses decide what a
message means (``_process``); this class owns everything around it (ADR-0016, ADR-0017,
ADR-0018):

* **Reading.** ``XREADGROUP`` for new messages; every ``reclaim_interval``, ``XAUTOCLAIM`` for
  messages idle at least ``reclaim_min_idle`` (abandoned by a crashed or stalled consumer),
  following the cursor through the pending list. The delivery count of each reclaimed message is
  read with ``XPENDING``; if it cannot be read, the messages are handled as first deliveries.
* **Settling.** A handled message is acknowledged (``ACK``), dead-lettered (``DEAD_LETTER``:
  copied and acknowledged by one Lua script), or left pending (``KEEP``). Only ``_process``
  decides, and it acknowledges only when no work is left that the message stands for. An error
  leaves the message pending: an uncertain outcome is never acknowledged.
* **Errors.** A transient error (PostgreSQL or Redis) before the work was claimed is handled
  again here, after a backoff, within half the reclaim idle time (after that ``XAUTOCLAIM``
  could hand the message out again). Errors also pause reading (exponential backoff with full
  jitter), so an outage does not churn through the stream.
* **Stopping.** ``run`` returns when ``stop`` is set, after the running tasks finish, for at most
  ``grace_seconds`` (or until ``force``); the rest are cancelled and their messages stay pending,
  to be recovered like those of a crashed process.
* **Observability** (Phase 10, ADR-0021). Each message's ``task.handled`` line carries its
  duration, and on an error the stage it failed in. The loop counts messages by reason, their
  durations, reclaimed messages and errors in the process's metrics registry, and, given a
  ``HeartbeatPublisher``, writes a heartbeat with its state (tasks in flight, the time since its
  last finished task, its error backoff) until it stops; a graceful stop removes it.
"""

import abc
import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import ClassVar, Final, Protocol

from app.core.backoff import Backoff
from app.core.logging import task_context_var
from app.core.process import error_type, is_transient
from app.core.tasks import cancel_and_wait
from app.db.session import Database
from app.observability.heartbeat import HeartbeatPublisher
from app.observability.metrics import REGISTRY, Registry
from app.queue.messages import StreamMessage
from app.queue.streams import Delivery, TaskStream

logger = logging.getLogger(__name__)

# How often a message is handled again here after a transient error before its claim.
LOCAL_RETRIES: Final = 5
STATS_INTERVAL_SECONDS: Final = 60.0


class Disposition(StrEnum):
    """What a consumer does with a message once it has handled it."""

    ACK = "ack"  # done: XACK
    KEEP = "keep"  # leave it pending; XAUTOCLAIM hands it to a consumer again later
    DEAD_LETTER = "dead_letter"  # unprocessable: copy it to the dead-letter stream, then XACK


@dataclass(frozen=True, slots=True)
class Handled:
    """What became of one message, and why (``reason``: a stable token, logged).

    ``details`` are extra fields of the message's ``task.handled`` log line (never content).
    """

    disposition: Disposition
    reason: str
    details: Mapping[str, object] | None = None


@dataclass
class Progress:
    """How far one message got: once its work was claimed, an error is not retried here.
    ``stage`` names the step running now (``claim``, ``crawl``, ``record``...): an error's
    ``task.handled`` line reports it."""

    claimed: bool = False
    stage: str = "start"


class ConsumerMetrics:
    """The metric families every consumer records (``app.observability.metrics``)."""

    def __init__(self, registry: Registry) -> None:
        self.handled = registry.counter(
            "sitescope_tasks_handled_total",
            "Messages handled, by reason and disposition (ack, keep, dead_letter).",
            ("reason", "disposition"),
        )
        self.duration = registry.histogram(
            "sitescope_task_duration_seconds",
            "Time to handle one message, from its start to its settlement, by disposition.",
            ("disposition",),
        )
        self.reclaimed = registry.counter(
            "sitescope_messages_reclaimed_total",
            "Messages taken over from the consumer group's pending list (XAUTOCLAIM).",
        )
        self.errors = registry.counter(
            "sitescope_worker_errors_total",
            "Errors that paused the consumer, by source (stream: reading; task: a message).",
            ("source",),
        )
        self.settle_failures = registry.counter(
            "sitescope_settle_failures_total",
            "Messages that could not be acknowledged or dead-lettered (they stay pending).",
        )
        self.db_seconds = registry.histogram(
            "sitescope_db_operation_seconds",
            "Duration of one database transaction, by operation.",
            ("operation",),
        )
        self.recoveries = registry.counter(
            "sitescope_recoveries_total",
            "Recoveries by kind: expired leases taken over, exhausted work failed, poison "
            "messages quarantined.",
            ("kind",),
        )


class ConsumerConfig(Protocol):
    """The settings every consumer needs; a subclass's configuration provides more."""

    @property
    def concurrency(self) -> int: ...  # tasks in flight

    @property
    def read_block_ms(self) -> int: ...

    @property
    def reclaim_interval(self) -> float: ...

    @property
    def reclaim_min_idle(self) -> float: ...

    @property
    def reclaim_batch(self) -> int: ...

    @property
    def grace_seconds(self) -> float: ...

    @property
    def name(self) -> str: ...  # unique per process start: consumer name and worker_id


class StreamConsumer[T: StreamMessage, C: ConsumerConfig](abc.ABC):
    """The consumer loop of one worker process. See the module docstring."""

    # Shown in the start and stop log lines.
    DISPLAY_NAME: ClassVar[str] = "Worker"

    def __init__(
        self,
        database: Database,
        stream: TaskStream[T],
        config: C,
        *,
        clock: Callable[[], float] = time.monotonic,
        registry: Registry = REGISTRY,
    ) -> None:
        self._database = database
        self._stream = stream
        self._config = config
        self._clock = clock
        self._tasks: dict[str, asyncio.Task[None]] = {}  # by stream entry ID
        self._stream_backoff = Backoff()
        self._task_backoff = Backoff()
        self._paused_until = 0.0
        self._last_progress: float | None = None  # when a message was last settled
        self._last_recovered: float | None = None  # when an outage last ended
        self.handled: Counter[str] = Counter()  # messages handled, by reason
        self._metrics = ConsumerMetrics(registry)

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def heartbeat_state(self) -> dict[str, object]:
        """This consumer's part of its process's heartbeat (``app.observability.heartbeat``)."""
        now = self._clock()

        def ago(moment: float | None) -> float | None:
            return round(now - moment, 3) if moment is not None else None

        return {
            "in_flight": len(self._tasks),
            "concurrency": self._config.concurrency,
            "handled": dict(self.handled),
            "last_progress_seconds_ago": ago(self._last_progress),
            "last_recovered_seconds_ago": ago(self._last_recovered),
            "paused_seconds": round(max(0.0, self._paused_until - now), 3),
            "stream_failures": self._stream_backoff.failures,
            "task_failures": self._task_backoff.failures,
        }

    @contextlib.contextmanager
    def _timed(self, operation: str) -> Iterator[None]:
        """Record the duration of a database transaction (``sitescope_db_operation_seconds``)."""
        started = self._clock()
        try:
            yield
        finally:
            self._metrics.db_seconds.observe(self._clock() - started, operation=operation)

    async def run(
        self,
        stop: asyncio.Event,
        force: asyncio.Event | None = None,
        *,
        heartbeat: HeartbeatPublisher | None = None,
    ) -> None:
        """Consume messages until ``stop`` is set, then drain (see the module docstring)."""
        beating = asyncio.create_task(heartbeat.run()) if heartbeat is not None else None
        try:
            await self._consume(stop, force)
        finally:
            if beating is not None:
                await cancel_and_wait(beating)
            if heartbeat is not None:
                await heartbeat.retire()

    async def _consume(self, stop: asyncio.Event, force: asyncio.Event | None) -> None:
        config = self._config
        force = force if force is not None else asyncio.Event()
        if not await self._prepare(stop):
            return
        logger.info(
            f"{self.DISPLAY_NAME} started",
            extra={
                "event": "worker.started",
                "worker_id": self.name,
                "consumer": self.name,
                "stream": self._stream.key,
                "group": self._stream.group,
                "concurrency": config.concurrency,
                "reclaim_min_idle_seconds": config.reclaim_min_idle,
                **self._started_fields(),
            },
        )
        background = asyncio.create_task(self._background())
        cursor = "0-0"
        next_reclaim = self._clock()  # look for abandoned messages at once
        next_stats = self._clock() + STATS_INTERVAL_SECONDS
        try:
            while await self._ready(stop):
                now = self._clock()
                if now >= next_stats:
                    self._log_stats()
                    next_stats = now + STATS_INTERVAL_SECONDS
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
                    self._last_recovered = self._clock()
                    logger.info(
                        "The task stream is reachable again",
                        extra={"event": "worker.recovered", "source": "stream"},
                    )
        finally:
            await self._drain(force)
            await cancel_and_wait(background)
        logger.info(
            f"{self.DISPLAY_NAME} stopped",
            extra={
                "event": "worker.stopped",
                "worker_id": self.name,
                "handled": dict(self.handled),
            },
        )

    # --- What subclasses provide ---------------------------------------------------------------

    @abc.abstractmethod
    async def _process(self, delivery: Delivery[T], deliveries: int, progress: Progress) -> Handled:
        """Handle one message and decide its disposition. Set ``progress.claimed`` as soon as
        the work is claimed: an error after that is not retried here. ``deliveries`` is the
        message's delivery count (1 unless it was reclaimed)."""

    async def _background(self) -> None:
        """Work that runs beside the loop until it stops (for example, a watcher)."""
        return

    def _started_fields(self) -> dict[str, object]:
        """Extra fields of the ``worker.started`` line."""
        return {}

    # --- The consumer loop ---------------------------------------------------------------------

    async def _prepare(self, stop: asyncio.Event) -> bool:
        """Create the stream and its group if needed; retried until it works or ``stop``."""
        while not stop.is_set():
            try:
                await self._stream.ensure_group()
            except Exception as exc:
                self._pause(self._stream_backoff, "stream", exc)
                await sleep_unless(stop, self._paused_until - self._clock())
                continue
            self._stream_backoff.reset()
            return True
        return False

    async def _ready(self, stop: asyncio.Event) -> bool:
        """Wait for a free task slot and for any error pause to end; False once ``stop`` is set."""
        while not stop.is_set():
            pause = self._paused_until - self._clock()
            if pause > 0:
                await sleep_unless(stop, pause)
            elif self._free() <= 0:
                await wait_any(self._tasks.values(), stop)
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
            self._metrics.reclaimed.inc(len(reclaimed.deliveries))
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
        deliveries: Iterable[Delivery[T]],
        *,
        reclaimed: bool,
        delivery_counts: Mapping[str, int] | None = None,
    ) -> None:
        for delivery in deliveries:
            if delivery.entry_id in self._tasks:  # this consumer is processing it already
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
        self._metrics.errors.inc(source=source)
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
            await wait_any(self._tasks.values(), force, within=remaining)
        unfinished = list(self._tasks.values())
        if unfinished:
            logger.warning(
                "Cancelled unfinished tasks; their messages stay pending",
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

    async def _handle(self, delivery: Delivery[T], *, reclaimed: bool, deliveries: int = 1) -> None:
        """Handle one message, settle it (ack, dead-letter or keep), and log one line."""
        started = self._clock()
        context: dict[str, str | int] = {
            "worker_id": self.name,
            "consumer": self.name,
            "message_id": delivery.entry_id,
        }
        if delivery.task is not None:
            context.update(delivery.task.identifiers())
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
        elapsed = self._clock() - started
        self.handled[handled.reason] += 1
        disposition = handled.disposition.value
        self._metrics.handled.inc(reason=handled.reason, disposition=disposition)
        self._metrics.duration.observe(elapsed, disposition=disposition)
        if settled:
            self._last_progress = self._clock()
        extra: dict[str, object] = {
            "event": "task.handled",
            "disposition": disposition,
            "reason": handled.reason,
            "settled": settled,
            "reclaimed": reclaimed,
            "deliveries": deliveries,
            "duration_ms": round(elapsed * 1000),
            **(handled.details or {}),
        }
        if delivery.error is not None:
            extra["error"] = delivery.error
        quiet = handled.disposition is not Disposition.KEEP or handled.reason == "leased"
        logger.log(logging.INFO if quiet else logging.WARNING, "Message handled", extra=extra)

    async def _handle_with_local_retries(self, delivery: Delivery[T], deliveries: int) -> Handled:
        """``_process``, tried again after a transient error that happened before the claim.

        Only within half the reclaim idle time: the message is not acknowledged meanwhile, so
        after that time ``XAUTOCLAIM`` may hand it out again (and count another delivery).
        """
        give_up_at = self._clock() + self._config.reclaim_min_idle / 2
        for local_try in range(LOCAL_RETRIES + 1):
            progress = Progress()
            try:
                handled = await self._process(delivery, deliveries, progress)
            except Exception as exc:  # the outcome may be uncertain: never acknowledge
                self._pause(self._task_backoff, "task", exc)
                wait = max(0.0, self._paused_until - self._clock())
                if (
                    progress.claimed
                    or not is_transient(exc)
                    or local_try == LOCAL_RETRIES
                    or self._clock() + wait >= give_up_at
                ):
                    return Handled(
                        Disposition.KEEP,
                        "error",
                        {"stage": progress.stage, "error_type": error_type(exc)},
                    )
                await asyncio.sleep(wait)
                continue
            if self._task_backoff.reset():
                self._last_recovered = self._clock()
                logger.info(
                    "Tasks succeed again", extra={"event": "worker.recovered", "source": "task"}
                )
            return handled
        raise AssertionError("unreachable")  # pragma: no cover - the loop always returns

    async def _settle(self, delivery: Delivery[T], handled: Handled, deliveries: int) -> bool:
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
            # then.
            self._metrics.settle_failures.inc()
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


async def sleep_unless(event: asyncio.Event, seconds: float) -> None:
    """Sleep for ``seconds``, or until ``event`` is set."""
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), timeout=seconds)


async def wait_any(
    tasks: Iterable[asyncio.Task[None]], event: asyncio.Event, *, within: float | None = None
) -> None:
    """Wait until one of ``tasks`` finishes, ``event`` is set, or ``within`` seconds pass."""
    waiter = asyncio.ensure_future(event.wait())
    try:
        await asyncio.wait([*tasks, waiter], timeout=within, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
