r"""Read-only diagnostics of the distributed crawler and indexer: jobs, leases, streams.

Run from backend\ (settings come from the environment and backend\.env)::

    .\.venv\Scripts\python.exe -m app.scheduler.status

Prints one JSON document and changes nothing, in PostgreSQL (read-only transactions) or in
Redis (``XINFO``, ``XLEN``, ``XPENDING``, ``XREVRANGE``). Its sections: ``database`` (crawl jobs,
URL states, crawl leases), ``stream`` (the crawl stream), ``indexing`` (search documents by
status, index attempts running per indexer, the oldest waiting documents, finished jobs whose
indexing is not complete; Phase 7) and ``index_stream`` (the index stream). Each stream section
ends with its newest dead letters (``--dead-letters``). The exit status is 0 if PostgreSQL and
Redis were reachable, 1 if not.
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
from app.queue.streams import CrawlStream, IndexStream, TaskStream
from app.services.diagnostics import crawl_overview, index_overview

SECTIONS = ("database", "stream", "indexing", "index_stream")


async def collect(settings: Settings, *, jobs: int = 20, dead_letters: int = 10) -> dict[str, Any]:
    """The diagnostics document: one section per entry of ``SECTIONS`` (or its error)."""
    report: dict[str, Any] = {}
    database = Database(settings, application_name="sitescope-status")
    try:
        async with database.session() as session:
            report["database"] = asdict(await crawl_overview(session, limit=jobs))
        async with database.session() as session:
            report["indexing"] = asdict(await index_overview(session))
    except Exception as exc:
        report.setdefault("database", {"error": error_type(exc)})
        report["indexing"] = {"error": error_type(exc)}
    finally:
        await database.dispose()
    redis = create_redis_client(settings, client_name="sitescope-status")
    try:
        prefix = settings.redis_key_prefix
        report["stream"] = await _describe(CrawlStream(redis, key_prefix=prefix), dead_letters)
        report["index_stream"] = await _describe(
            IndexStream(redis, key_prefix=prefix), dead_letters
        )
    except Exception as exc:
        report.setdefault("stream", {"error": error_type(exc)})
        report["index_stream"] = {"error": error_type(exc)}
    finally:
        await redis.aclose()
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
    args = parser.parse_args(argv)
    report = asyncio.run(
        collect(settings or get_settings(), jobs=args.jobs, dead_letters=args.dead_letters)
    )
    sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    failed = any("error" in report[section] for section in SECTIONS)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
