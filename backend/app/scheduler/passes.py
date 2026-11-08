"""The pass loop shared by the scheduler and the index dispatcher (Phase 7).

A ``PassLoop`` runs ``run_pass`` every ``interval`` seconds until it is told to stop. A failed
pass (PostgreSQL or Redis unreachable, or a bug) is logged and retried after an exponential
backoff with full jitter; every step of a pass is a transaction on PostgreSQL state, so a
failed pass loses nothing. Its log events are named after the process role: ``<role>.started``,
``<role>.pass`` (only for a pass that did something), ``<role>.pass_failed``,
``<role>.recovered`` and ``<role>.stopped``.
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

logger = logging.getLogger(__name__)


class Report(Protocol):
    """What one pass did: a dataclass (its fields are logged) that knows if it was busy."""

    __dataclass_fields__: ClassVar[dict[str, Any]]

    @property
    def busy(self) -> bool: ...


class PassLoop[R: Report](abc.ABC):
    """Passes until stopped (see the module docstring)."""

    ROLE: ClassVar[str]  # the prefix of the log events, e.g. "scheduler"
    ID_FIELD: ClassVar[str]  # the log field that names this process, e.g. "scheduler_id"
    DISPLAY_NAME: ClassVar[str]  # e.g. "Scheduler"

    def __init__(self, *, interval: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval
        self._clock = clock
        self._backoff = Backoff(base=0.5, cap=30.0)

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

    async def run(self, stop: asyncio.Event) -> None:
        """Run passes until ``stop`` is set."""
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
            if self._backoff.reset():
                logger.info(
                    f"{self.DISPLAY_NAME} passes succeed again",
                    extra={"event": f"{role}.recovered", self.ID_FIELD: self.name},
                )
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


async def run_until_stopped(loop: PassLoop[Any], signals: StopSignals) -> None:
    """Run ``loop`` until the first stop signal; a second signal abandons the current pass."""
    task = asyncio.create_task(loop.run(signals.stop))
    forced = asyncio.create_task(signals.force.wait())
    try:
        await asyncio.wait([task, forced], return_when=asyncio.FIRST_COMPLETED)
    finally:
        forced.cancel()
        if not task.done():  # a second signal: abandon the current pass
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
