"""Process-local metrics: counters and histograms with bounded labels (Phase 10, ADR-0021).

Every process (API, scheduler, crawler worker, index dispatcher, indexer) keeps its own values
in a ``Registry``. They leave the process only as a snapshot inside its heartbeat
(``app.observability.heartbeat``); the API's ``GET /metrics`` renders every live process's
snapshot with that process's ``role`` and ``process`` labels (``app.observability.exposition``).

So a counter is monotonic for the life of one process, and each process is its own series:
nothing is ever added up across processes before it is exposed. A query sums processes after
taking rates (``sum by (role) (rate(sitescope_tasks_handled_total[5m]))``), which treats a
restarted process as a counter reset. Adding the processes' totals first would turn the exit of
one process into a drop of the total, which a rate would read as a reset and count again.

Labels must come from small, fixed sets (a stage, an outcome, an HTTP status class): never a
URL, a job ID or a process name. A family refuses series beyond its ``max_series`` (the
observation is dropped and logged once), so a mistake cannot grow a heartbeat without bound.
"""

import bisect
import logging
import math
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

logger = logging.getLogger(__name__)

# Seconds: from a millisecond to a minute.
DURATION_BUCKETS: Final = (
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)
# Seconds a dispatched task waits for its claim: up to ten minutes (a backlog).
QUEUE_DELAY_BUCKETS: Final = (
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)
# Labels the exposition adds to every process series; families may not define them.
RESERVED_LABELS: Final = frozenset({"role", "process", "le"})
DEFAULT_MAX_SERIES: Final = 64

_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

Kind = Literal["counter", "histogram"]


class MetricError(ValueError):
    """A family was defined or used inconsistently (a programming error)."""


@dataclass
class _Series:
    value: float = 0.0  # counters
    buckets: list[int] = field(default_factory=list)  # histograms: per bucket, not cumulative
    total: float = 0.0  # histograms: the sum of observations
    count: int = 0  # histograms: the number of observations


class _Family:
    def __init__(
        self,
        registry: "Registry",
        name: str,
        documentation: str,
        kind: Kind,
        labels: Sequence[str],
        buckets: Sequence[float] = (),
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> None:
        if not _NAME.fullmatch(name):
            raise MetricError(f"invalid metric name {name!r}")
        for label in labels:
            if not _LABEL.fullmatch(label) or label.startswith("__"):
                raise MetricError(f"invalid label name {label!r}")
            if label in RESERVED_LABELS:
                raise MetricError(f"label {label!r} is reserved")
        if len(set(labels)) != len(labels):
            raise MetricError("duplicate label names")
        if kind == "histogram" and (
            not buckets
            or list(buckets) != sorted(set(buckets))
            or not all(math.isfinite(bound) for bound in buckets)
        ):
            raise MetricError("histogram buckets must be finite and strictly increasing")
        self._registry = registry
        self.name = name
        self.documentation = documentation
        self.kind: Kind = kind
        self.labels = tuple(labels)
        self.buckets = tuple(float(bound) for bound in buckets)
        self.max_series = max_series
        self._series: dict[tuple[str, ...], _Series] = {}
        self._overflowed = False

    def _key(self, values: Mapping[str, object]) -> tuple[str, ...]:
        if set(values) != set(self.labels):
            raise MetricError(f"{self.name} takes the labels {self.labels}, got {sorted(values)}")
        return tuple(str(values[label]) for label in self.labels)

    def _get(self, key: tuple[str, ...]) -> _Series | None:
        series = self._series.get(key)
        if series is not None:
            return series
        if len(self._series) >= self.max_series:
            if not self._overflowed:
                self._overflowed = True
                logger.warning(
                    "A metric family reached its series limit; new label values are dropped",
                    extra={"event": "metrics.series_limit", "metric": self.name},
                )
            return None
        series = _Series(buckets=[0] * (len(self.buckets) + 1) if self.kind == "histogram" else [])
        self._series[key] = series
        return series

    def snapshot(self) -> dict[str, Any]:
        """JSON-ready: cumulative bucket counts (the ``le`` bounds of ``buckets``, then +Inf)."""
        series: list[list[Any]] = []
        for key, item in sorted(self._series.items()):
            if self.kind == "counter":
                series.append([list(key), item.value])
            else:
                cumulative, running = [], 0
                for count in item.buckets:
                    running += count
                    cumulative.append(running)
                series.append([list(key), cumulative, item.total, item.count])
        result: dict[str, Any] = {
            "name": self.name,
            "type": self.kind,
            "help": self.documentation,
            "labels": list(self.labels),
            "series": series,
        }
        if self.kind == "histogram":
            result["buckets"] = list(self.buckets)
        return result


class Counter(_Family):
    def inc(self, amount: float = 1.0, /, **labels: object) -> None:
        if amount < 0 or not math.isfinite(amount):
            raise MetricError("a counter only goes up, by a finite amount")
        key = self._key(labels)
        with self._registry.lock:
            series = self._get(key)
            if series is not None:
                series.value += amount


class Histogram(_Family):
    def observe(self, value: float, /, **labels: object) -> None:
        if not math.isfinite(value):
            return  # a NaN or infinite duration is a measurement error, never data
        key = self._key(labels)
        with self._registry.lock:
            series = self._get(key)
            if series is not None:
                series.buckets[bisect.bisect_left(self.buckets, value)] += 1
                series.total += value
                series.count += 1


class Registry:
    """The metric families of one process. Getting a family twice returns the same one, so
    every component can declare what it records; a conflicting declaration is an error."""

    def __init__(self) -> None:
        self.lock = threading.Lock()  # observations may come from worker threads
        self._families: dict[str, _Family] = {}

    def counter(
        self,
        name: str,
        documentation: str,
        labels: Sequence[str] = (),
        *,
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> Counter:
        return self._declare(Counter, name, documentation, "counter", labels, (), max_series)

    def histogram(
        self,
        name: str,
        documentation: str,
        labels: Sequence[str] = (),
        *,
        buckets: Sequence[float] = DURATION_BUCKETS,
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> Histogram:
        return self._declare(
            Histogram, name, documentation, "histogram", labels, buckets, max_series
        )

    def _declare[F: _Family](
        self,
        cls: type[F],
        name: str,
        documentation: str,
        kind: Kind,
        labels: Sequence[str],
        buckets: Sequence[float],
        max_series: int,
    ) -> F:
        with self.lock:
            existing = self._families.get(name)
            if existing is not None:
                if (
                    not isinstance(existing, cls)
                    or existing.labels != tuple(labels)
                    or existing.buckets != tuple(float(bound) for bound in buckets)
                ):
                    raise MetricError(f"{name} is already declared differently")
                return existing
            family = cls(self, name, documentation, kind, labels, buckets, max_series)
            self._families[name] = family
            return family

    def snapshot(self) -> list[dict[str, Any]]:
        """Every family with its series, ready for JSON."""
        with self.lock:
            return [family.snapshot() for _, family in sorted(self._families.items())]


# The registry of this process: components record here unless a test gives them their own.
REGISTRY: Final = Registry()
