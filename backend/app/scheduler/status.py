r"""Read-only diagnostics of the distributed crawler and indexer: jobs, leases, streams.

Run from backend\ (settings come from the environment and backend\.env)::

    .\.venv\Scripts\python.exe -m app.scheduler.status

Prints one JSON document and changes nothing, in PostgreSQL (read-only transactions) or in
Redis (``XINFO``, ``XLEN``, ``XPENDING``, ``XREVRANGE``, heartbeats). Its sections: ``database``
(crawl jobs, URL states, crawl leases), ``stream`` (the crawl stream), ``indexing`` (search
documents by status, index attempts running per indexer, the oldest waiting documents, finished
jobs whose indexing is not complete; Phase 7) and ``index_stream`` (the index stream). Each stream
section ends with its newest dead letters (``--dead-letters``). Since Phase 10: ``processes``
(every live process, from its heartbeat: role, heartbeat age, tasks in flight, the time since its
last progress, error backoff) and ``assessment`` (``services.diagnostics.assess``: PostgreSQL,
Redis, the API, crawl progress, crawler workers, indexing, repeated failures, recovery; each
``ok``, ``warning``, ``critical`` or ``unknown`` with its reason). The exit status is 0 if
PostgreSQL and Redis were reachable, 1 if not.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from app.core.config import Settings, get_settings
from app.core.process import error_type, int_in_range
from app.core.redis import create_redis_client
from app.db.session import Database
from app.observability.heartbeat import Heartbeat, read_heartbeats
from app.queue.streams import CrawlStream, IndexStream, TaskStream
from app.services.diagnostics import (
    CrawlOverview,
    IndexOverview,
    ProcessView,
    assess,
    crawl_overview,
    index_overview,
)

SECTIONS = ("database", "stream", "indexing", "index_stream")
_PROCESS_STATE_FIELDS = (
    "in_flight",
    "concurrency",
    "last_progress_seconds_ago",
    "last_pass_seconds_ago",
    "paused_seconds",
    "stream_failures",
    "task_failures",
    "consecutive_failures",
    "failed_jobs_last_pass",
    "last_recovered_seconds_ago",
)


def _rounded(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _process_entry(heartbeat: Heartbeat) -> dict[str, Any]:
    view = ProcessView.from_heartbeat(heartbeat)
    entry: dict[str, Any] = {
        "role": heartbeat.role,
        "name": heartbeat.name,
        "version": heartbeat.payload.get("version"),
        "heartbeat_age_seconds": round(heartbeat.age_seconds, 3),
        "uptime_seconds": heartbeat.payload.get("uptime_seconds"),
        "progress_age_seconds": _rounded(view.progress_age_seconds),
        "failing": view.failing,
    }
    if (passed := view.pass_age_seconds) is not None:
        entry["pass_age_seconds"] = _rounded(passed)
    state = heartbeat.state
    entry.update({key: state[key] for key in _PROCESS_STATE_FIELDS if key in state})
    if "handled" in state:
        entry["handled"] = state["handled"]
    return entry


async def collect(
    settings: Settings, *, jobs: int = 20, dead_letters: int = 10, stall_seconds: float = 120.0
) -> dict[str, Any]:
    """The diagnostics document: one section per entry of ``SECTIONS`` (or its error), then
    ``processes`` and ``assessment``."""
    report: dict[str, Any] = {}
    crawl: CrawlOverview | None = None
    index: IndexOverview | None = None
    database = Database(settings, application_name="sitescope-status")
    try:
        async with database.session() as session:
            crawl = await crawl_overview(session, limit=jobs)
            report["database"] = asdict(crawl)
        async with database.session() as session:
            index = await index_overview(session)
            report["indexing"] = asdict(index)
    except Exception as exc:
        report.setdefault("database", {"error": error_type(exc)})
        report["indexing"] = {"error": error_type(exc)}
    finally:
        await database.dispose()
    redis = create_redis_client(settings, client_name="sitescope-status")
    heartbeats: list[Heartbeat] = []
    redis_ok = False
    try:
        prefix = settings.redis_key_prefix
        report["stream"] = await _describe(CrawlStream(redis, key_prefix=prefix), dead_letters)
        report["index_stream"] = await _describe(
            IndexStream(redis, key_prefix=prefix), dead_letters
        )
        heartbeats = await read_heartbeats(redis, key_prefix=prefix)
        report["processes"] = [_process_entry(heartbeat) for heartbeat in heartbeats]
        redis_ok = True
    except Exception as exc:
        report.setdefault("stream", {"error": error_type(exc)})
        report.setdefault("index_stream", {"error": error_type(exc)})
        report["processes"] = {"error": error_type(exc)}
    finally:
        await redis.aclose()
    findings = assess(
        crawl=crawl,
        index=index,
        redis_ok=redis_ok,
        processes=[ProcessView.from_heartbeat(heartbeat) for heartbeat in heartbeats],
        stall_seconds=stall_seconds,
    )
    report["assessment"] = {name: asdict(finding) for name, finding in findings.items()}
    return report


async def _describe(stream: TaskStream[Any], dead_letters: int) -> dict[str, Any]:
    described = await stream.describe()
    if described.get("exists"):
        described["pending"] = asdict(await stream.pending())
    described["recent_dead_letters"] = await stream.recent_dead_letters(dead_letters)
    return described


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scheduler.status",
        description=(
            "Print crawl jobs, leases, indexing progress and stream statistics as JSON (read-only)."
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int_in_range(1, 1000),
        default=20,
        help="running jobs to list, oldest first (default: 20)",
    )
    parser.add_argument(
        "--dead-letters",
        type=int_in_range(0, 1000),
        default=10,
        help="newest dead letters to list (default: 10)",
    )
    parser.add_argument(
        "--stall-seconds",
        type=int_in_range(1, 86_400),
        default=120,
        help="how long work may wait, or a busy process go without progress, before the "
        "assessment reports it (default: 120)",
    )
    args = parser.parse_args(argv)
    report = asyncio.run(
        collect(
            settings or get_settings(),
            jobs=args.jobs,
            dead_letters=args.dead_letters,
            stall_seconds=args.stall_seconds,
        )
    )
    sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    failed = any("error" in report[section] for section in SECTIONS)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
