"""The pass loop's heartbeat state (``app.scheduler.passes``; Phase 10 review)."""

import asyncio
from dataclasses import dataclass

import pytest

from app.observability.metrics import Registry
from app.scheduler.passes import PassLoop

pytestmark = pytest.mark.anyio


@dataclass(frozen=True, slots=True)
class Report:
    worked: int = 0
    failed_jobs: int = 0

    @property
    def progressed(self) -> bool:
        return bool(self.worked)

    @property
    def busy(self) -> bool:
        return self.progressed or bool(self.failed_jobs)


class ScriptedLoop(PassLoop[Report]):
    """Returns the scripted reports, one per pass, then stops."""

    ROLE = "scripted"
    ID_FIELD = "scripted_id"
    DISPLAY_NAME = "Scripted loop"
    COUNTED_FIELDS = ("worked", "failed_jobs")

    def __init__(self, reports: list[Report], stop: asyncio.Event) -> None:
        self._now = 100.0
        super().__init__(interval=0, clock=self._tick, registry=Registry())
        self._reports = reports
        self._stop = stop

    def _tick(self) -> float:
        self._now += 1.0  # every reading is one second later
        return self._now

    @property
    def name(self) -> str:
        return "scripted-h-1-aa"

    async def run_pass(self) -> Report:
        report = self._reports.pop(0)
        if not self._reports:
            self._stop.set()
        return report


async def passes(*reports: Report) -> dict[str, object]:
    stop = asyncio.Event()
    loop = ScriptedLoop(list(reports), stop)
    await loop.run(stop)
    return loop.heartbeat_state()


async def test_a_job_failing_in_every_pass_is_not_progress_and_shows_in_the_heartbeat() -> None:
    state = await passes(Report(failed_jobs=1), Report(failed_jobs=1), Report(failed_jobs=1))

    assert state["passes"] == 3
    assert state["consecutive_failures"] == 0  # the passes themselves succeed
    assert state["last_progress_seconds_ago"] is None
    assert state["failed_jobs_last_pass"] == 1
    assert state["last_pass_seconds_ago"] is not None


async def test_progress_is_the_last_pass_that_did_work_and_failures_clear() -> None:
    state = await passes(Report(worked=2), Report(failed_jobs=1), Report())

    assert state["failed_jobs_last_pass"] == 0  # the last pass had none
    progress = state["last_progress_seconds_ago"]
    last_pass = state["last_pass_seconds_ago"]
    assert isinstance(progress, float)
    assert isinstance(last_pass, float)
    assert progress > last_pass  # the first pass did the work, the third was the last
