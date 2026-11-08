"""The pass loop shared by the scheduler and the index dispatcher (Phase 7).

A ``PassLoop`` runs ``run_pass`` every ``interval`` seconds until it is told to stop. A failed
pass (PostgreSQL or Redis unreachable, or a bug) is logged and retried after an exponential
backoff with full jitter; every step of a pass is a transaction on PostgreSQL state, so a
failed pass loses nothing. Its log events are named after the process role: ``<role>.started``,
``<role>.pass`` (only for a pass that did something), ``<role>.pass_failed``,
``<role>.recovered`` and ``<role>.stopped``.

Since Phase 10 (ADR-0021) the loop records each pass's duration and what it did
(``COUNTED_FIELDS`` of its report) in the process's metrics, and, given a
``HeartbeatPublisher``, writes a heartbeat with its state until it stops.
"""

import abc
import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any, ClassVar, Protocol

from app.core.backoff import Backoff
from app.core.process import error_type, is_transient
from app.core.shutdown import StopSignals
from app.core.tasks import cancel_and_wait
from app.observability.heartbeat import HeartbeatPublisher
from app.observability.metrics import REGISTRY, Registry

logger = logging.getLogger(__name__)


class Report(Protocol):
    """What one pass did: a dataclass (its fields are logged) that knows if it did work
    (``progressed``) and if it is worth a log line (``busy``: work, or failed jobs)."""

    __dataclass_fields__: ClassVar[dict[str, Any]]

    @property
    def failed_jobs(self) -> int:
        """Jobs whose step failed for a reason of their own in this pass."""
        ...

    @property
    def progressed(self) -> bool: ...

    @property
    def busy(self) -> bool: ...


class PassLoop[R: Report](abc.ABC):
    """Passes until stopped (see the module docstring)."""

    ROLE: ClassVar[str]  # the prefix of the log events, e.g. "scheduler"
    ID_FIELD: ClassVar[str]  # the log field that names this process, e.g. "scheduler_id"
    DISPLAY_NAME: ClassVar[str]  # e.g. "Scheduler"
    # Report fields counted in sitescope_pass_items_total (the others are not counts of work).
    COUNTED_FIELDS: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        *,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
        registry: Registry = REGISTRY,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._backoff = Backoff(base=0.5, cap=30.0)
        self._passes = 0
        self._failed_passes = 0
        self._last_pass: float | None = None  # when the last pass succeeded
        self._last_progress: float | None = None  # when a pass last did work
        self._last_recovered: float | None = None  # when passes last succeeded after failures
        self._failed_jobs_last_pass = 0  # jobs whose step failed in the last successful pass
        self._pass_seconds = registry.histogram(
            "sitescope_pass_duration_seconds",
            "Duration of one scheduler or index dispatcher pass, by result (ok, failed).",
            ("result",),
        )
        self._pass_items = registry.counter(
            "sitescope_pass_items_total",
            "Work done by passes, by item: for the scheduler jobs activated, advanced, finished "
            "and cancelled and tasks dispatched and republished; for the index dispatcher tasks "
            "dispatched and republished and jobs whose indexing completed.",
            ("item",),
        )

    def heartbeat_state(self) -> dict[str, object]:
        """This loop's part of its process's heartbeat (``app.observability.heartbeat``).

        ``last_pass_seconds_ago`` shows a pass that hangs (the heartbeat is written by its own
        task and stays fresh); ``last_progress_seconds_ago`` counts passes that did work, not
        failed jobs; ``failed_jobs_last_pass`` shows a job that fails in every pass.
        """
        now = self._clock()

        def ago(moment: float | None) -> float | None:
            return round(now - moment, 3) if moment is not None else None

        return {
            "passes": self._passes,
            "failed_passes": self._failed_passes,
            "consecutive_failures": self._backoff.failures,
            "failed_jobs_last_pass": self._failed_jobs_last_pass,
            "last_pass_seconds_ago": ago(self._last_pass),
            "last_progress_seconds_ago": ago(self._last_progress),
            "last_recovered_seconds_ago": ago(self._last_recovered),
        }

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique per process start."""

    @abc.abstractmethod
    async def run_pass(self) -> R:
        """One pass."""

    def _started_fields(self) -> dict[str, object]:
        """Extra fields of the ``<role>.started`` line."""
        return {}

    async def _for_job[T](self, job_id: uuid.UUID, step: Awaitable[T]) -> T | None:
        """Run one job's step. A job whose step fails for a reason of its own is logged
        (``<role>.job_failed``) and skipped for this pass, so that one broken job cannot stop
        the work of all the others (found by the Phase 5 review). A transient error (PostgreSQL
        or Redis unreachable) fails the whole pass instead: every other job would fail the
        same way."""
        try:
            return await step
        except Exception as exc:
            if is_transient(exc):
                raise
            logger.error(
                "A job's step failed; the other jobs go on",
                exc_info=exc,
                extra={
                    "event": f"{self.ROLE}.job_failed",
                    self.ID_FIELD: self.name,
                    "job_id": str(job_id),
                    "error_type": error_type(exc),
                },
            )
            return None

    async def run(
        self, stop: asyncio.Event, *, heartbeat: HeartbeatPublisher | None = None
    ) -> None:
        """Run passes until ``stop`` is set, writing ``heartbeat`` meanwhile."""
        beating = asyncio.create_task(heartbeat.run()) if heartbeat is not None else None
        try:
            await self._passes_until(stop)
        finally:
            if beating is not None:
                await cancel_and_wait(beating)
            if heartbeat is not None:
                await heartbeat.retire()

    async def _passes_until(self, stop: asyncio.Event) -> None:
        role = self.ROLE
        logger.info(
            f"{self.DISPLAY_NAME} started",
            extra={
                "event": f"{role}.started",
                self.ID_FIELD: self.name,
                "interval_seconds": self._interval,
                **self._started_fields(),
            },
        )
        while not stop.is_set():
            started = self._clock()
            try:
                report = await self.run_pass()
            except Exception as exc:
                self._pass_seconds.observe(self._clock() - started, result="failed")
                self._failed_passes += 1
                delay = self._backoff.next_delay()
                transient = is_transient(exc)
                logger.log(
                    logging.WARNING if transient else logging.ERROR,
                    f"{self.DISPLAY_NAME} pass failed; retrying",
                    exc_info=None if transient else exc,
                    extra={
                        "event": f"{role}.pass_failed",
                        self.ID_FIELD: self.name,
                        "error_type": error_type(exc),
                        "failures": self._backoff.failures,
                        "retry_in_ms": round(delay * 1000),
                    },
                )
                await sleep_unless(stop, delay)
                continue
            finished = self._clock()
            self._pass_seconds.observe(finished - started, result="ok")
            self._passes += 1
            self._last_pass = finished
            self._failed_jobs_last_pass = report.failed_jobs
            for item in self.COUNTED_FIELDS:
                if value := getattr(report, item):
                    self._pass_items.inc(value, item=item)
            if self._backoff.reset():
                self._last_recovered = finished
                logger.info(
                    f"{self.DISPLAY_NAME} passes succeed again",
                    extra={"event": f"{role}.recovered", self.ID_FIELD: self.name},
                )
            if report.progressed:
                self._last_progress = finished
            if report.busy:
                logger.info(
                    f"{self.DISPLAY_NAME} pass",
                    extra={"event": f"{role}.pass", self.ID_FIELD: self.name, **asdict(report)},
                )
            await sleep_unless(stop, self._interval - (self._clock() - started))
        logger.info(
            f"{self.DISPLAY_NAME} stopped",
            extra={"event": f"{role}.stopped", self.ID_FIELD: self.name},
        )


async def sleep_unless(event: asyncio.Event, seconds: float) -> None:
    """Sleep for ``seconds``, or until ``event`` is set."""
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), timeout=seconds)


async def run_until_stopped(
    loop: PassLoop[Any], signals: StopSignals, *, heartbeat: HeartbeatPublisher | None = None
) -> None:
    """Run ``loop`` until the first stop signal; a second signal abandons the current pass."""
    task = asyncio.create_task(loop.run(signals.stop, heartbeat=heartbeat))
    forced = asyncio.create_task(signals.force.wait())
    try:
        await asyncio.wait([task, forced], return_when=asyncio.FIRST_COMPLETED)
    finally:
        forced.cancel()
        if not task.done():  # a second signal: abandon the current pass
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
