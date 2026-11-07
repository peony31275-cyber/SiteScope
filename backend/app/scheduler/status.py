r"""Read-only diagnostics of the distributed crawler: jobs, leases, and the task stream.

Run from backend\ (settings come from the environment and backend\.env)::

    .\.venv\Scripts\python.exe -m app.scheduler.status

Prints one JSON document and changes nothing, in PostgreSQL (a read-only transaction) or in
Redis (``XINFO``, ``XLEN``, ``XPENDING``). The exit status is 0 if both were reachable, 1 if not.
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
from app.queue.streams import CrawlStream
from app.services.diagnostics import crawl_overview


async def collect(settings: Settings, *, jobs: int = 20) -> dict[str, Any]:
    """The diagnostics document: a ``database`` and a ``stream`` section (or their errors)."""
    report: dict[str, Any] = {}
    database = Database(settings, application_name="sitescope-status")
    try:
        async with database.session() as session:
            report["database"] = asdict(await crawl_overview(session, limit=jobs))
    except Exception as exc:
        report["database"] = {"error": error_type(exc)}
    finally:
        await database.dispose()
    redis = create_redis_client(settings, client_name="sitescope-status")
    try:
        stream = CrawlStream(redis, key_prefix=settings.redis_key_prefix)
        described = await stream.describe()
        if described.get("exists"):
            described["pending"] = asdict(await stream.pending())
        report["stream"] = described
    except Exception as exc:
        report["stream"] = {"error": error_type(exc)}
    finally:
        await redis.aclose()
    return report


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scheduler.status",
        description="Print crawl jobs, leases and task stream statistics as JSON (read-only).",
    )
    parser.add_argument(
        "--jobs",
        type=int_in_range(1, 1000),
        default=20,
        help="running jobs to list, oldest first (default: 20)",
    )
    args = parser.parse_args(argv)
    report = asyncio.run(collect(settings or get_settings(), jobs=args.jobs))
    sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    failed = any("error" in report[section] for section in ("database", "stream"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
