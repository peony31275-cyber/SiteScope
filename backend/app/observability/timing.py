"""Timings of one task that code deep inside it can add to (Phase 10).

A crawl task waits for politeness slots inside the safe fetcher, several calls below the worker.
The worker sets ``task_timings_var`` to a fresh ``TaskTimings`` before the task's crawl phase;
code running in that task, or in tasks it creates (they copy the context, and so share the same
object), adds to it. Only the worker that set it reads it, after the phase, for the task's log
line and metrics. Timings are observability only; nothing decides anything from them.
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class TaskTimings:
    politeness_wait: float = 0.0  # seconds spent waiting for politeness slots
    slots: int = 0  # politeness slots requested (request hops, robots.txt included)


task_timings_var: ContextVar[TaskTimings | None] = ContextVar("task_timings", default=None)
