"""The Prometheus text exposition format (version 0.0.4), for ``GET /metrics`` (Phase 10).

An ``Exposition`` collects gauges counted by the API itself (from PostgreSQL and Redis) and the
metric snapshots of every live process (from their heartbeats), then renders each family once:
its ``# HELP`` and ``# TYPE`` lines, then all its samples. A process snapshot's series get the
labels ``role`` and ``process``. Snapshots come from Redis, written by processes that may run
another version of the code, so they are checked: a family with an invalid name, label or shape,
or whose type differs from a family already collected, is skipped, and label values are escaped.
"""

import logging
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

logger = logging.getLogger(__name__)

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_TYPES: Final = frozenset({"counter", "gauge", "histogram"})


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def format_number(value: float) -> str:
    """A sample value: integral values without a fraction, infinities as ``+Inf``/``-Inf``."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer() and abs(value) < 2**53:
        return str(int(value))
    return repr(float(value))


def _sample(name: str, labels: Mapping[str, str], value: float) -> str:
    if not labels:
        return f"{name} {format_number(value)}"
    rendered = ",".join(f'{key}="{_escape_label(str(item))}"' for key, item in labels.items())
    return f"{name}{{{rendered}}} {format_number(value)}"


@dataclass
class _Family:
    name: str
    kind: str
    documentation: str
    lines: list[str] = field(default_factory=list)


class Exposition:
    """Families to render, in the order they were first added."""

    def __init__(self) -> None:
        self._families: dict[str, _Family] = {}
        self.skipped = 0  # snapshot families that could not be rendered

    def _family(self, name: str, kind: str, documentation: str) -> _Family | None:
        family = self._families.get(name)
        if family is None:
            family = _Family(name, kind, documentation)
            self._families[name] = family
        elif family.kind != kind:
            return None
        return family

    def gauge(self, name: str, documentation: str, value: float, /, **labels: str) -> None:
        """One gauge sample, counted by the caller (the families are fixed in code)."""
        family = self._family(name, "gauge", documentation)
        if family is None:
            raise ValueError(f"{name} is not a gauge")
        family.lines.append(_sample(name, labels, value))

    def declare(self, name: str, kind: str, documentation: str) -> None:
        """Declare a family (its HELP and TYPE lines are written even without samples)."""
        self._family(name, kind, documentation)

    def add_snapshot(
        self, snapshot: Iterable[Mapping[str, Any]], *, role: str, process: str
    ) -> None:
        """Every family of one process's metric snapshot, with its ``role`` and ``process``."""
        for item in snapshot:
            try:
                lines = self._snapshot_lines(item, role=role, process=process)
            except (KeyError, TypeError, ValueError, IndexError):
                lines = None
            if lines is None:
                self.skipped += 1
                continue
            name, kind, documentation, rendered = lines
            family = self._family(name, kind, documentation)
            if family is None:
                self.skipped += 1
                continue
            family.lines.extend(rendered)

    def _snapshot_lines(
        self, item: Mapping[str, Any], *, role: str, process: str
    ) -> tuple[str, str, str, list[str]] | None:
        name, kind, labels = str(item["name"]), str(item["type"]), list(item["labels"])
        if kind not in ("counter", "histogram") or not _NAME.fullmatch(name):
            return None
        if any(not _LABEL.fullmatch(str(label)) for label in labels):
            return None
        if {"role", "process", "le"} & {str(label) for label in labels}:
            return None
        rendered: list[str] = []
        for series in item["series"]:
            values = [str(value) for value in series[0]]
            if len(values) != len(labels):
                return None
            base = {"role": role, "process": process, **dict(zip(labels, values, strict=True))}
            if kind == "counter":
                rendered.append(_sample(name, base, float(series[1])))
                continue
            bounds = [float(bound) for bound in item["buckets"]]
            cumulative = [float(count) for count in series[1]]
            if len(cumulative) != len(bounds) + 1:
                return None
            for bound, count in zip(bounds, cumulative, strict=False):
                rendered.append(
                    _sample(f"{name}_bucket", {**base, "le": format_number(bound)}, count)
                )
            rendered.append(_sample(f"{name}_bucket", {**base, "le": "+Inf"}, cumulative[-1]))
            rendered.append(_sample(f"{name}_sum", base, float(series[2])))
            rendered.append(_sample(f"{name}_count", base, float(series[3])))
        return name, kind, str(item.get("help", "")), rendered

    def render(self) -> str:
        lines: list[str] = []
        for family in self._families.values():
            lines.append(f"# HELP {family.name} {_escape_help(family.documentation)}")
            lines.append(f"# TYPE {family.name} {family.kind}")
            lines.extend(family.lines)
        return "\n".join(lines) + "\n"
