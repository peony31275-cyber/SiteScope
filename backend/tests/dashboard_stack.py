r"""The whole SiteScope stack, for the dashboard's end-to-end run (Phase 9).

Run from backend\ once PostgreSQL and Redis are up (``wsl docker compose up -d --wait``) and the
frontend's dependencies are installed (``npm ci`` in frontend\)::

    .\.venv\Scripts\python.exe -m tests.dashboard_stack           # serve until Ctrl+C
    .\.venv\Scripts\python.exe -m tests.dashboard_stack --e2e     # run Playwright, then stop

What runs, each as its own process, all on 127.0.0.x:

* the demo site (``demo-site/``) on 127.0.0.1, and a slow tree site on 127.0.0.2 (every page
  takes 1.5 s: a crawl of it runs long enough to be cancelled from the dashboard);
* the API (uvicorn), the scheduler, two crawler workers, the index dispatcher and two indexers;
* the dashboard's production build, served by ``vite preview``, whose /api proxy points at
  this API (the browser talks to one origin, as with nginx in a deployment).

It never touches the development database or the development Redis keys:

* a disposable database ``<SITESCOPE_POSTGRES_DB>_test_ui``, created and migrated to head with
  Alembic, and dropped at the end (``--keep`` keeps it for inspection);
* Redis database 15 under a fresh key prefix, whose keys are deleted at the end (never
  ``FLUSHDB``);
* every process's settings come from ``child_environment``, which refuses any database that is
  not a disposable ``*_test`` one, and any Redis database but 15.

With ``--e2e`` the exit status is Playwright's. The stack's URLs are printed, and written to
``stack.json`` in the log directory, which also holds every process's log.
"""

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.redis import create_redis_client
from tests.fetch_support import LocalHttpServer
from tests.integration import helpers
from tests.integration.test_local_crawler import demo_routes
from tests.process_support import (
    Cluster,
    ManagedProcess,
    child_environment,
    process_group_flags,
    tree_site,
)

FRONTEND_DIR = helpers.BACKEND_DIR.parent / "frontend"
DEMO_HOST = "127.0.0.1"
SLOW_HOST = "127.0.0.2"
SLOW_PAGE_SECONDS = 1.5

# No proxy, ever: these requests go to this machine's own servers.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_status(url: str) -> int:
    """The status of a GET (any HTTP answer counts); raises OSError without one."""
    try:
        with _OPENER.open(url, timeout=2) as response:
            status: int = response.status
            return status
    except urllib.error.HTTPError as error:
        return error.code


async def wait_until_serving(url: str, *, within: float, process: ManagedProcess) -> None:
    deadline = time.monotonic() + within
    while True:
        with contextlib.suppress(OSError):
            if await asyncio.to_thread(http_status, url) < 500:
                return
        if not process.alive:
            raise RuntimeError(f"{process.name} exited:\n{process.tail()}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"{url} did not answer:\n{process.tail()}")
        await asyncio.sleep(0.2)


def node() -> str:
    path = shutil.which("node")
    if path is None:
        raise RuntimeError("Node.js is not on PATH: install Node.js 22.12 or newer")
    return path


def frontend_process(
    cluster: Cluster, name: str, arguments: Sequence[str], environment: dict[str, str]
) -> ManagedProcess:
    """A Node process in frontend\\ (vite), logged like the other processes."""
    log_path = cluster.log_dir / f"{name}.log"
    with log_path.open("wb") as log:
        popen = subprocess.Popen(  # noqa: S603 - this machine's node and the project's own CLIs
            [node(), *arguments],
            cwd=FRONTEND_DIR,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=process_group_flags(),
        )
    process = ManagedProcess(name, popen, log_path)
    cluster.processes.append(process)
    return process


async def delete_keys(settings: Settings, prefix: str) -> int:
    client = create_redis_client(settings, client_name="sitescope-dashboard-stack")
    try:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*", count=500)]
        if keys:
            await client.delete(*keys)
        return len(keys)
    finally:
        await client.aclose()


@dataclass
class Resources:
    """What the run created, for its cleanup, and for the last-resort cleanup in ``main`` when a
    second Ctrl+C interrupts the graceful one."""

    settings: Settings | None = None
    prefix: str = ""
    cluster: Cluster | None = None
    database_created: bool = False
    cleaned: bool = False


async def run(args: argparse.Namespace, resources: Resources) -> int:
    base = Settings()
    if missing := base.missing_credentials():
        sys.stderr.write(f"Set {', '.join(missing)} in backend/.env first.\n")
        return 2
    # Writes open and the default cap whatever backend/.env says; the processes get the same
    # from child_environment.
    settings = base.model_copy(
        update={
            "environment": "test",
            "postgres_db": f"{base.postgres_db}_test_ui",
            "redis_db": 15,
            "admin_token": None,
            "crawl_max_active_jobs": Settings.model_fields["crawl_max_active_jobs"].default,
        }
    )
    helpers.require_disposable(settings.postgres_db)
    for problem in (await helpers.probe_postgres(settings), await helpers.probe_redis(settings)):
        if problem is not None:
            sys.stderr.write(
                f"PostgreSQL or Redis is unavailable ({problem}): "
                "run `wsl docker compose up -d --wait` in backend/.\n"
            )
            return 2
    if not (FRONTEND_DIR / "node_modules").is_dir():
        sys.stderr.write("Run `npm ci` in frontend/ first.\n")
        return 2

    resources.settings = settings
    resources.prefix = f"ui-{uuid.uuid4().hex[:12]}"
    log_dir = Path(tempfile.mkdtemp(prefix="sitescope-dashboard-"))
    exit_code = 0
    try:
        resources.database_created = True  # from here on, a failure still drops it
        await helpers.recreate_database(settings)
        await helpers.upgrade(settings, "head")
        exit_code = await serve(args, settings, resources, log_dir)
    finally:
        exit_code = await stop(resources, log_dir, keep=args.keep) or exit_code
    return exit_code


async def serve(
    args: argparse.Namespace, settings: Settings, resources: Resources, log_dir: Path
) -> int:
    slow = tree_site(2, 5, delay=SLOW_PAGE_SECONDS)
    async with (
        LocalHttpServer(demo_routes(), host=DEMO_HOST) as demo_site,
        LocalHttpServer(slow.routes, host=SLOW_HOST) as slow_site,
    ):
        environment = child_environment(
            settings,
            prefix=resources.prefix,
            hosts=[DEMO_HOST, SLOW_HOST],
            ports=[demo_site.port, slow_site.port],
        )
        cluster = Cluster(environment, log_dir)
        resources.cluster = cluster
        api_port = helpers.unused_port()
        api = cluster.api(api_port)
        workers = [
            cluster.scheduler(),
            cluster.worker(),
            cluster.worker(),
            cluster.index_dispatcher(),
            cluster.indexer(),
            cluster.indexer(),
        ]
        await cluster.started(*workers)
        api_url = f"http://127.0.0.1:{api_port}"
        await wait_until_serving(f"{api_url}/health", within=60, process=api)

        frontend_environment = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().startswith(("SITESCOPE_", "VITE_"))
        }
        # The browser under test talks to this stack only: the API path and the polling
        # interval are pinned, whatever a developer's frontend/.env.local says (process
        # variables take precedence over Vite's .env files).
        frontend_environment.update(
            {
                "SITESCOPE_API_URL": api_url,
                "VITE_API_BASE_URL": "/api/v1",
                "VITE_POLL_INTERVAL_MS": "3000",
            }
        )
        vite = ["node_modules/vite/bin/vite.js"]
        if not args.no_build:
            builder = frontend_process(
                cluster, "frontend-build", [*vite, "build"], frontend_environment
            )
            if await asyncio.to_thread(builder.popen.wait) != 0:
                raise RuntimeError(f"the frontend build failed:\n{builder.tail()}")
        preview_port = helpers.unused_port()
        preview = frontend_process(
            cluster,
            "frontend",
            [*vite, "preview", "--host", "127.0.0.1", "--port", str(preview_port), "--strictPort"],
            frontend_environment,
        )
        dashboard_url = f"http://127.0.0.1:{preview_port}"
        await wait_until_serving(f"{dashboard_url}/", within=60, process=preview)
        await wait_until_serving(f"{dashboard_url}/api/v1/health/ready", within=30, process=preview)

        stack: dict[str, Any] = {
            "dashboard": dashboard_url,
            "api": api_url,
            "demo_seed": f"http://{DEMO_HOST}:{demo_site.port}/",
            "slow_seed": f"http://{SLOW_HOST}:{slow_site.port}/",
            "database": settings.postgres_db,
            "redis": f"db {settings.redis_db}, prefix {resources.prefix}",
            "logs": str(log_dir),
        }
        (log_dir / "stack.json").write_text(json.dumps(stack, indent=2) + "\n", encoding="utf-8")
        sys.stdout.write(json.dumps(stack, indent=2) + "\n")
        sys.stdout.flush()

        if args.e2e:
            return await playwright(stack, args.playwright_args)
        sys.stdout.write("Serving; press Ctrl+C to stop.\n")
        sys.stdout.flush()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        return 0


async def stop(resources: Resources, log_dir: Path, *, keep: bool) -> int:
    """Stop every process (gracefully first), delete the Redis keys, drop the database. Each
    step runs even when an earlier one fails. Returns 1 if a crawl role did not stop cleanly."""
    exit_code = 0
    cluster = resources.cluster
    if cluster is not None:
        for process in cluster.processes:
            if process.name.startswith("frontend") and process.alive:
                process.popen.terminate()
        try:
            codes = await cluster.stop_all(within=20)
        except AssertionError as error:  # a process did not stop in time
            sys.stderr.write(f"{error}\n")
            codes = {}
            exit_code = 1
        # The API and the frontend are stopped, not drained; the crawl roles must exit 0.
        failed = {
            name: code
            for name, code in codes.items()
            if code != 0 and not name.startswith(("api", "frontend"))
        }
        if failed:
            sys.stderr.write(f"Processes that did not stop cleanly: {failed} (logs: {log_dir})\n")
            exit_code = 1
        cluster.kill_all()
    await release_storage(resources, keep=keep)
    resources.cleaned = True
    return exit_code


async def release_storage(resources: Resources, *, keep: bool) -> None:
    settings = resources.settings
    if settings is None:
        return
    deleted: int | str
    try:
        deleted = await delete_keys(settings, resources.prefix)
    except Exception as error:  # still drop the database
        deleted = f"no ({type(error).__name__})"
    if not resources.database_created:
        return
    if keep:
        sys.stdout.write(f"Kept database {settings.postgres_db}; deleted {deleted} Redis keys.\n")
    else:
        await helpers.drop_database(settings)
        sys.stdout.write(
            f"Dropped database {settings.postgres_db}; deleted {deleted} Redis keys.\n"
        )


async def playwright(stack: dict[str, Any], extra: Sequence[str]) -> int:
    environment = dict(os.environ)
    environment.update(
        {
            "E2E_BASE_URL": stack["dashboard"],
            "E2E_DEMO_SEED": stack["demo_seed"],
            "E2E_SLOW_SEED": stack["slow_seed"],
        }
    )
    process = await asyncio.create_subprocess_exec(
        node(),
        "node_modules/@playwright/test/cli.js",
        "test",
        *extra,
        cwd=FRONTEND_DIR,
        env=environment,
    )
    return await process.wait()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.dashboard_stack",
        description="Run the whole SiteScope stack on a disposable database for the dashboard.",
    )
    parser.add_argument("--e2e", action="store_true", help="run the Playwright suite, then stop")
    parser.add_argument("--keep", action="store_true", help="keep the disposable database")
    parser.add_argument("--no-build", action="store_true", help="serve the existing frontend build")
    parser.add_argument("playwright_args", nargs="*", help="passed to `playwright test` (after --)")
    args = parser.parse_args(argv)
    resources = Resources()
    try:
        return asyncio.run(run(args, resources))
    except KeyboardInterrupt:
        # A second Ctrl+C interrupted the graceful stop: finish the cleanup without waiting.
        if not resources.cleaned:
            if resources.cluster is not None:
                resources.cluster.kill_all()
            asyncio.run(release_storage(resources, keep=args.keep))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
