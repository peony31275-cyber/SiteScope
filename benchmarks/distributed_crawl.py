r"""Distributed crawl throughput with 1, 2 and 4 crawler worker processes (Phase 5).

What it measures: the wall time from creating the crawl jobs until every job is completed,
with one scheduler process and N worker processes (separate operating-system processes, each
running SITESCOPE_CRAWL_CONCURRENCY tasks), crawling generated sites served on this machine.
Process start-up is not included. It also reports how many pages each worker recorded (from the
workers' own logs).

Isolation: a disposable database ``<SITESCOPE_POSTGRES_DB>_test_bench`` (created, migrated and
dropped by this script), Redis database 15 under a unique key prefix (deleted at the end), and
HTTP servers on loopback addresses 127.0.0.1-127.0.0.5. Nothing leaves the machine.

The local servers answer every page after a fixed delay (default 100 ms), standing in for a
remote server's latency. Politeness is relaxed for the measurement (no spacing, 8 requests in
flight per host); production defaults (1 s, 1 request per host) would make the per-host limit
the only thing measured. These numbers describe this development machine only.

Run from backend\ (PostgreSQL and Redis up, as for the tests):

    .\.venv\Scripts\python.exe ..\benchmarks\distributed_crawl.py `
        --output ..\benchmarks\results\phase5-distributed.json

Every run is printed and saved as raw JSON; nothing is averaged away.
"""

import argparse
import asyncio
import json
import platform
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.config import Settings  # noqa: E402 - after the path setup
from app.core.redis import create_redis_client  # noqa: E402
from app.db.session import Database  # noqa: E402
from tests.crawl_support import create_local_job, job_row  # noqa: E402
from tests.fetch_support import LocalHttpServer  # noqa: E402
from tests.integration import helpers  # noqa: E402
from tests.process_support import Cluster, child_environment, tree_site  # noqa: E402

HOSTS = ["127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5"]


async def one_run(
    settings: Settings,
    database: Database,
    *,
    workers: int,
    hosts: list[str],
    depth: int,
    branching: int,
    delay: float,
    concurrency: int,
    per_host: int,
    log_dir: Path,
) -> dict[str, Any]:
    site = tree_site(depth=depth, branching=branching, delay=delay)
    prefix = f"bench-{uuid.uuid4().hex[:10]}"
    redis = create_redis_client(settings, client_name="sitescope-benchmark")
    servers = [LocalHttpServer(site.routes, host=host) for host in hosts]
    try:
        for server in servers:
            await server.__aenter__()
        ports = [server.port for server in servers]
        environment = child_environment(
            settings,
            prefix=prefix,
            hosts=hosts,
            ports=ports,
            extra={
                "LOG_LEVEL": "INFO",
                "CRAWL_CONCURRENCY": str(concurrency),
                "POLITENESS_INTERVAL_SECONDS": "0",
                "POLITENESS_MAX_CONCURRENT_PER_HOST": str(per_host),
                "SCHEDULER_MAX_RUNNING_JOBS": "10",
                "SCHEDULER_MAX_IN_FLIGHT_PER_JOB": "200",
            },
        )
        run_dir = log_dir / f"w{workers}-h{len(hosts)}-{prefix}"
        run_dir.mkdir(parents=True)
        cluster = Cluster(environment, run_dir)
        try:
            processes = [cluster.scheduler()] + [cluster.worker() for _ in range(workers)]
            await cluster.started(*processes)
            started = time.perf_counter()
            jobs = [
                await create_local_job(
                    database,
                    settings,
                    host=host,
                    ports=ports,
                    port=server.port,
                    max_depth=depth,
                    max_pages=100_000,
                )
                for host, server in zip(hosts, servers, strict=True)
            ]
            while True:
                statuses = [(await job_row(database, job_id)).status for job_id in jobs]
                if all(status in ("completed", "failed") for status in statuses):
                    break
                if time.perf_counter() - started > 600:
                    raise RuntimeError("a run took more than 10 minutes")
                await asyncio.sleep(0.05)
            elapsed = time.perf_counter() - started
            exit_codes = await cluster.stop_all()
        finally:
            cluster.kill_all()
        per_worker = {
            process.name: sum(
                1 for line in process.events("task.handled") if line["reason"] == "completed"
            )
            for process in cluster.workers()
        }
        reasons = Counter(
            line["reason"] for line in cluster.all_lines() if line.get("event") == "task.handled"
        )
        pages = sum(
            1
            for server in servers
            for request in server.requests
            if request.target != "/robots.txt"
        )
        return {
            "workers": workers,
            "hosts": len(hosts),
            "concurrency_per_worker": concurrency,
            "requests_in_flight_per_host": per_host,
            "pages_per_host": len(site.depths),
            "page_delay_seconds": delay,
            "statuses": statuses,
            "page_requests": pages,
            "elapsed_seconds": round(elapsed, 3),
            "pages_per_second": round(pages / elapsed, 1),
            "recorded_per_worker": per_worker,
            "handled_by_reason": dict(reasons),
            "exit_codes": exit_codes,
        }
    finally:
        for server in servers:
            await server.__aexit__(None, None, None)
        keys = [key async for key in redis.scan_iter(match=f"{prefix}:*", count=1000)]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workers", default="1,2,4", help="worker counts (comma-separated)")
    parser.add_argument("--hosts", default="1,4", help="host counts, 1 to 4 (comma-separated)")
    parser.add_argument("--per-host", type=int, default=8, help="requests in flight per host")
    parser.add_argument("--repeat", type=int, default=3, help="runs per configuration")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--branching", type=int, default=6)
    parser.add_argument("--delay", type=float, default=0.1, help="seconds per page response")
    parser.add_argument("--concurrency", type=int, default=4, help="tasks per worker process")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--logs", type=Path, default=Path("benchmark-logs"))
    args = parser.parse_args()

    base = Settings()
    settings = base.model_copy(
        update={"postgres_db": f"{base.postgres_db}_test_bench", "redis_db": 15}
    )
    helpers.require_disposable(settings.postgres_db)
    await helpers.recreate_database(settings)
    await helpers.upgrade(settings, "head")
    database = Database(settings, application_name="sitescope-benchmark")
    runs: list[dict[str, Any]] = []
    try:
        for host_set in (HOSTS[: int(count)] for count in args.hosts.split(",")):
            for workers in (int(value) for value in args.workers.split(",")):
                for repeat in range(args.repeat):
                    result = await one_run(
                        settings,
                        database,
                        workers=workers,
                        hosts=host_set,
                        depth=args.depth,
                        branching=args.branching,
                        delay=args.delay,
                        concurrency=args.concurrency,
                        per_host=args.per_host,
                        log_dir=args.logs,
                    )
                    result["repeat"] = repeat + 1
                    runs.append(result)
                    print(json.dumps(result), flush=True)  # noqa: T201 - the benchmark's output
    finally:
        await database.dispose()
        await helpers.drop_database(settings)
    environment = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor(),
        "command": " ".join(sys.argv),
        "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"environment": environment, "runs": runs}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    asyncio.run(main())
