r"""The whole SiteScope stack on this machine, for the dashboard: the local demo, and the
dashboard's end-to-end run (Phase 9; the local startup made reliable after Phase 11).

From the repository root, on Windows::

    .\demo.cmd                 # start; open the printed URL; Ctrl+C stops and cleans up
    .\demo.cmd --stop          # stop a demo running in another terminal
    .\demo.cmd --e2e           # the Playwright suite against a fresh stack, then stop

or from backend\: ``.\.venv\Scripts\python.exe -m tests.dashboard_stack [options]``. The
frontend's dependencies must be installed (``npm ci`` in frontend\).

**Infrastructure.** Docker Engine runs inside WSL, and WSL stops about 20 s after its last
session closes: Docker and its containers stop with it. A Windows process connected to the
forwarded ports does not count as a session, so ``wsl docker compose up -d --wait`` followed
by a launcher used to lose PostgreSQL and Redis mid-startup. On Windows the launcher therefore:

1. holds a WSL session open (``wsl.exe -e sleep infinity``) for as long as it runs;
2. waits for Docker Engine. It never starts, stops or restarts Docker;
3. runs ``docker compose up -d --wait`` in backend\ (backend/compose.yaml): containers already
   running stay as they are, and volumes are never touched;
4. waits until PostgreSQL and Redis answer from Windows.

``--no-infra`` skips steps 1 to 3, for services started some other way. On Linux (CI) only step 4
runs.

**Supervision.** While it starts and while it serves, the launcher checks every few seconds
that PostgreSQL, Redis, the WSL session and every process of the stack are alive. When one is
not, it stops everything with one clear message instead of a cascade of tracebacks.

**What runs**, each as its own process, all on 127.0.0.x:

* the demo site (``demo-site/``) on 127.0.0.1, and a slow tree site on 127.0.0.2 (every page
  takes 1.5 s: a crawl of it runs long enough to be cancelled from the dashboard);
* the API (uvicorn), the scheduler, two crawler workers, the index dispatcher and two indexers;
* the dashboard's production build, served by ``vite preview`` on http://127.0.0.1:4173
  (``--port``), whose /api proxy points at this API: one origin, as with nginx in a deployment.

Unless ``--no-seed`` or ``--e2e`` is given, the demo jobs of ``app.scheduler.admin seed-demo``
are created on the demo site, so the dashboard has real crawls to show.

**Storage.** The launcher never touches the development database or the development Redis keys:

* it uses a disposable database ``<SITESCOPE_POSTGRES_DB>_test_ui``. That database is recreated
  at the start, which also removes what an interrupted run left, migrated to head with Alembic,
  and dropped at the end (``--keep`` keeps it);
* it uses Redis database 15 under the prefix ``ui-stack``, whose keys are deleted at the start
  and at the end (never ``FLUSHDB``);
* every process's settings come from ``child_environment``, which refuses any database that is
  not a disposable ``*_test`` one, and any Redis database but 15.

**One stack at a time.** They would share that database. A second start prints the running
stack's URL and exits. The running stack's state is kept in ``<temp>/sitescope-dashboard-stack``.

With ``--e2e`` the exit status is Playwright's. The stack's URLs are printed, and written to
``stack.json`` in the log directory, which also holds every process's log.
"""

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

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
DEFAULT_PORT: Final = 4173
# Redis database 15, under a prefix of this launcher's own: an interrupted run's keys are found
# and deleted by the next run.
REDIS_PREFIX: Final = "ui-stack"
STATE_DIR: Final = Path(tempfile.gettempdir()) / "sitescope-dashboard-stack"
WATCH_SECONDS: Final = 3.0
# Consecutive failed probes before a dependency counts as lost (one may be a blip).
LOST_AFTER: Final = 2
# Errors of an unreachable PostgreSQL or Redis, in the cleanup.
UNREACHABLE: Final = (OSError, TimeoutError, SQLAlchemyError, RedisError)

# No proxy, ever: these requests go to this machine's own servers.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DemoError(Exception):
    """A failure the launcher explains in one message, without a traceback."""


def say(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def tail(text_: str, lines: int = 12) -> str:
    return "\n".join(text_.strip().splitlines()[-lines:])


# --- The stack's settings ---------------------------------------------------------------------


def stack_settings(base: Settings) -> Settings:
    """The developer's servers, but the disposable database and Redis database 15, with writes
    open and the default job cap, whatever backend/.env says (children get the same from
    ``child_environment``)."""
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
    return settings


# --- Infrastructure: Docker in WSL, PostgreSQL and Redis ----------------------------------------


class Session(Protocol):
    """The part of ``subprocess.Popen`` the WSL session needs."""

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


Runner = Callable[[Sequence[str], float], tuple[int, str]]
Probe = Callable[[Settings], Awaitable[str | None]]


def run_command(command: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run ``command`` in backend\\ (no shell); returns its exit code and its output."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed programs: wsl.exe and docker
            list(command),
            cwd=helpers.BACKEND_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"{command[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g} s"
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def manages_wsl_docker() -> bool:
    """Docker Engine in WSL, driven from Windows (docs/development.md, "Docker (WSL 2)")."""
    return sys.platform == "win32" and shutil.which("wsl") is not None


def start_wsl_session() -> "subprocess.Popen[bytes]":
    """A WSL session held open: WSL stops about 20 s after its last session closes, and
    Docker Engine with it. Its own process group, so that Ctrl+C does not end it before the
    cleanup, which needs PostgreSQL and Redis."""
    return subprocess.Popen(
        ["wsl", "-e", "sleep", "infinity"],  # noqa: S607 - wsl.exe from PATH (Windows)
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=process_group_flags(),
    )


@dataclass
class Infrastructure:
    """What the launcher holds open for the stack: the WSL session, if it manages one."""

    session: Session | None = None

    def alive(self) -> bool:
        return self.session is None or self.session.poll() is None

    def close(self) -> None:
        if self.session is not None and self.session.poll() is None:
            self.session.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.session.wait(timeout=10)


async def service_problems(
    settings: Settings,
    *,
    probe_postgres: Probe = helpers.probe_postgres,
    probe_redis: Probe = helpers.probe_redis,
) -> list[str]:
    """What does not answer now, one entry per service (empty: both answer)."""
    problems = []
    if (error := await probe_postgres(settings)) is not None:
        problems.append(
            f"PostgreSQL at {settings.postgres_host}:{settings.postgres_port} ({error})"
        )
    if (error := await probe_redis(settings)) is not None:
        problems.append(f"Redis at {settings.redis_host}:{settings.redis_port} ({error})")
    return problems


async def wait_for_services(
    settings: Settings,
    *,
    within: float,
    hint: str,
    interval: float = 1.0,
    probe_postgres: Probe = helpers.probe_postgres,
    probe_redis: Probe = helpers.probe_redis,
) -> None:
    deadline = time.monotonic() + within
    while problems := await service_problems(
        settings, probe_postgres=probe_postgres, probe_redis=probe_redis
    ):
        if time.monotonic() > deadline:
            raise DemoError(f"Not reachable from this machine: {'; '.join(problems)}. {hint}")
        await asyncio.sleep(interval)


async def wait_for_docker(run: Runner, *, within: float, interval: float = 2.0) -> str:
    """Docker Engine's version, once it answers inside WSL. It is never started from here."""
    deadline = time.monotonic() + within
    while True:
        code, output = await asyncio.to_thread(
            run, ["wsl", "-e", "docker", "version", "--format", "{{.Server.Version}}"], 30
        )
        if code == 0 and output:
            return output.splitlines()[-1].strip()
        if "No such file" in output or "not found" in output:
            raise DemoError(
                "Docker Engine is not installed in WSL (wsl -e docker failed). Set it up as "
                'docs/development.md, "Docker (WSL 2)", describes.'
            )
        if time.monotonic() > deadline:
            raise DemoError(
                f"Docker Engine does not answer inside WSL ({tail(output, 3) or 'no output'}). "
                "Start it in an Ubuntu terminal with `sudo systemctl start docker`, then run "
                "this again."
            )
        await asyncio.sleep(interval)


async def prepare_infrastructure(
    settings: Settings,
    *,
    manage: bool,
    run: Runner = run_command,
    start_session: Callable[[], Session] = start_wsl_session,
    probe_postgres: Probe = helpers.probe_postgres,
    probe_redis: Probe = helpers.probe_redis,
    docker_within: float = 90,
    services_within: float = 60,
) -> Infrastructure:
    """PostgreSQL and Redis up and reachable, and (``manage``) WSL held open for the stack."""
    infrastructure = Infrastructure()
    if not manage:
        await wait_for_services(
            settings,
            within=10,
            hint="Start them first (in backend/: docker compose up -d --wait).",
            probe_postgres=probe_postgres,
            probe_redis=probe_redis,
        )
        return infrastructure
    try:
        infrastructure.session = start_session()
        say("WSL: holding a session open while the stack runs (WSL stops Docker otherwise).")
        version = await wait_for_docker(run, within=docker_within)
        say(f"Docker Engine {version} is running in WSL.")
        code, output = await asyncio.to_thread(
            run, ["wsl", "-e", "docker", "compose", "up", "-d", "--wait"], 240
        )
        if code != 0:
            raise DemoError(f"`docker compose up -d --wait` failed in backend/:\n{tail(output)}")
        await wait_for_services(
            settings,
            within=services_within,
            hint="The containers run, but their ports do not answer from Windows: check "
            "`wsl docker compose ps` in backend/ and WSL's localhost forwarding.",
            probe_postgres=probe_postgres,
            probe_redis=probe_redis,
        )
        say(
            f"PostgreSQL {settings.postgres_host}:{settings.postgres_port} and Redis "
            f"{settings.redis_host}:{settings.redis_port} answer (backend/compose.yaml)."
        )
    except BaseException:
        infrastructure.close()
        raise
    return infrastructure


# --- One stack at a time ------------------------------------------------------------------------


def state_file() -> Path:
    return STATE_DIR / "running.json"


def stop_file() -> Path:
    return STATE_DIR / "stop"


def process_alive(pid: int) -> bool:
    """Whether a process with this ID runs (never signals it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_instance() -> dict[str, Any] | None:
    """The running stack's state, if one runs; a stale state (its launcher is gone) is removed."""
    try:
        state: dict[str, Any] = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if process_alive(int(state.get("pid", 0))):
        return state
    with contextlib.suppress(OSError):
        state_file().unlink()
    return None


def write_state(**values: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.update(values)
    temporary = state_file().with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_file())


def release_state() -> None:
    for path in (state_file(), stop_file()):
        with contextlib.suppress(OSError):
            path.unlink()


def request_stop(*, within: float = 120.0, interval: float = 0.5) -> int:
    """``--stop``: ask the running stack to stop, and wait until its cleanup is done."""
    state = running_instance()
    if state is None:
        say("No SiteScope stack is running.")
        return 0
    pid = int(state["pid"])
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stop_file().touch()
    say(f"Asked the stack (pid {pid}) to stop; waiting for its cleanup...")
    deadline = time.monotonic() + within
    while state_file().exists() and process_alive(pid):
        if time.monotonic() > deadline:
            say("It is still stopping; it finishes on its own.")
            return 1
        time.sleep(interval)
    say("Stopped.")
    return 0


# --- Supervision ----------------------------------------------------------------------------


class StopRequested(Exception):
    """``--stop`` from another terminal."""


async def watch(
    settings: Settings,
    *,
    infrastructure: Infrastructure,
    processes: Callable[[], Sequence[ManagedProcess]],
    interval: float = WATCH_SECONDS,
    probe_postgres: Probe = helpers.probe_postgres,
    probe_redis: Probe = helpers.probe_redis,
) -> None:
    """Return never; raise ``StopRequested`` on a stop request, ``DemoError`` when the stack
    cannot go on: PostgreSQL or Redis lost, the WSL session gone, a process exited."""
    failures = 0
    while True:
        if stop_file().exists():
            raise StopRequested
        for process in processes():
            if not process.alive:
                raise DemoError(
                    f"{process.name} exited (code {process.popen.returncode}). Its last log "
                    f"lines:\n{process.tail(15)}"
                )
        if not infrastructure.alive():
            raise DemoError(
                "The WSL session this launcher holds ended (was WSL shut down?): PostgreSQL "
                "and Redis stopped with it."
            )
        problems = await service_problems(
            settings, probe_postgres=probe_postgres, probe_redis=probe_redis
        )
        failures = failures + 1 if problems else 0
        if failures >= LOST_AFTER:
            raise DemoError(
                f"Lost while the stack was running: {'; '.join(problems)}. Did WSL or Docker "
                "stop? `wsl docker compose ps` in backend/ shows the containers."
            )
        await asyncio.sleep(interval)


async def supervised[T](work: Coroutine[Any, Any, T], watcher: Coroutine[Any, Any, None]) -> T:
    """Run ``work`` while ``watcher`` runs; whichever ends first decides (the other is
    cancelled): a watcher's ``DemoError`` or ``StopRequested`` cancels the work."""
    work_task = asyncio.ensure_future(work)
    watch_task = asyncio.ensure_future(watcher)
    try:
        await asyncio.wait({work_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
        if work_task.done():
            return work_task.result()
        watch_task.result()  # raises: the watcher never ends otherwise
        raise AssertionError("the watcher returned")  # pragma: no cover
    finally:
        for task in (work_task, watch_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


# --- Storage ------------------------------------------------------------------------------


async def delete_keys(settings: Settings, prefix: str) -> int:
    client = create_redis_client(settings, client_name="sitescope-dashboard-stack")
    try:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*", count=500)]
        if keys:
            await client.delete(*keys)
        return len(keys)
    finally:
        await client.aclose()


async def database_exists(settings: Settings) -> bool:
    engine = create_async_engine(helpers.maintenance_url(settings), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            found = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": settings.postgres_db},
            )
            return found is not None
    finally:
        await engine.dispose()


async def prepare_storage(settings: Settings, prefix: str) -> None:
    """A fresh disposable database at head and no key under ``prefix``: an interrupted run's
    leftovers are removed first (the names are this launcher's own)."""
    helpers.require_disposable(settings.postgres_db)
    leftover_keys = await delete_keys(settings, prefix)
    leftover_database = await database_exists(settings)
    if leftover_keys or leftover_database:
        say(
            "Removed what an interrupted run left: "
            + ", ".join(
                [f"database {settings.postgres_db}"] * leftover_database
                + [f"{leftover_keys} Redis keys"] * bool(leftover_keys)
            )
            + "."
        )
    await helpers.recreate_database(settings)
    await helpers.upgrade(settings, "head")


# --- The stack ------------------------------------------------------------------------------


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
            raise DemoError(f"{process.name} exited during startup:\n{process.tail(15)}")
        if time.monotonic() > deadline:
            raise DemoError(f"{url} did not answer within {within:g} s:\n{process.tail(15)}")
        await asyncio.sleep(0.2)


def node() -> str:
    path = shutil.which("node")
    if path is None:
        raise DemoError("Node.js is not on PATH: install Node.js 22.12 or newer")
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


def free_port(preferred: int) -> int:
    """``preferred`` if nothing listens there (0: any), else any free port."""
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", preferred))
            except OSError:
                say(f"Port {preferred} is in use: the dashboard gets another one.")
            else:
                return preferred
    return helpers.unused_port()


def long_running(cluster: Cluster) -> list[ManagedProcess]:
    """The processes that must stay alive (the frontend build is a step, not a service)."""
    return [process for process in cluster.processes if process.name != "frontend-build"]


async def expect_started(cluster: Cluster, processes: Sequence[ManagedProcess]) -> None:
    try:
        await cluster.started(*processes)
    except AssertionError as error:  # exited, or no start event in time
        raise DemoError(f"A process of the stack did not start: {error}") from None


def seed_demo_jobs(environment: dict[str, str], base_url: str) -> list[str]:
    """The demo jobs on the demo site (``app.scheduler.admin seed-demo``); returns their names."""
    completed = subprocess.run(  # noqa: S603 - this interpreter and a fixed module
        [
            sys.executable,
            "-m",
            "app.scheduler.admin",
            "seed-demo",
            "--base-url",
            base_url,
            "--wait",
            "0",
        ],
        cwd=helpers.BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise DemoError(
            f"Seeding the demo jobs failed:\n{tail(completed.stdout + completed.stderr)}"
        )
    created: list[str] = json.loads(completed.stdout)["created"]
    return created


@dataclass
class Resources:
    """What the run created, for its cleanup, and for the last-resort cleanup in ``main`` when a
    second Ctrl+C interrupts the graceful one."""

    settings: Settings | None = None
    prefix: str = REDIS_PREFIX
    cluster: Cluster | None = None
    infrastructure: Infrastructure | None = None
    storage_touched: bool = False
    database_created: bool = False
    cleaned: bool = False


async def run(args: argparse.Namespace, resources: Resources) -> int:
    if sys.prefix == sys.base_prefix:
        say("Note: not running in a virtual environment; backend\\.venv's Python is expected.")
    if (state := running_instance()) is not None:
        say(
            f"A SiteScope stack is already running (pid {state['pid']}): "
            f"{state.get('dashboard', 'still starting')}. Stop it first with demo.cmd --stop "
            "(or press Ctrl+C in its terminal)."
        )
        return 3 if args.e2e else 0
    base = Settings()
    if missing := base.missing_credentials():
        sys.stderr.write(f"Set {', '.join(missing)} in backend/.env first.\n")
        return 2
    settings = stack_settings(base)
    if not (FRONTEND_DIR / "node_modules").is_dir():
        sys.stderr.write("Run `npm ci` in frontend/ first.\n")
        return 2

    release_state()  # a stale stop request, from a stack that is gone
    log_dir = Path(tempfile.mkdtemp(prefix="sitescope-dashboard-"))
    write_state(pid=os.getpid(), logs=str(log_dir), started_at=time.time())
    resources.settings = settings
    exit_code = 0
    try:
        resources.infrastructure = await prepare_infrastructure(
            settings, manage=manages_wsl_docker() and not args.no_infra
        )
        resources.storage_touched = True
        resources.database_created = True  # from here on, a failure still drops it
        await prepare_storage(settings, resources.prefix)
        exit_code = await serve(args, settings, resources, log_dir)
    except DemoError as error:
        sys.stderr.write(f"\nThe stack stopped: {error}\nLogs: {log_dir}\n")
        exit_code = 1
    finally:
        exit_code = await stop(resources, log_dir, keep=args.keep) or exit_code
        if resources.infrastructure is not None:
            resources.infrastructure.close()
        release_state()
    return exit_code


async def serve(
    args: argparse.Namespace, settings: Settings, resources: Resources, log_dir: Path
) -> int:
    slow = tree_site(2, 5, delay=SLOW_PAGE_SECONDS)
    infrastructure = resources.infrastructure or Infrastructure()
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

        def watcher() -> Coroutine[Any, Any, None]:
            return watch(
                settings, infrastructure=infrastructure, processes=lambda: long_running(cluster)
            )

        try:
            stack = await supervised(
                start(args, cluster, demo_site.port, slow_site.port, settings, resources, log_dir),
                watcher(),
            )
            if args.e2e:
                announce(stack)
                return await supervised(playwright(stack, args.playwright_args), watcher())
            if not args.no_seed:
                names = await asyncio.to_thread(seed_demo_jobs, environment, stack["demo_seed"])
                say(f"Created the demo jobs ({', '.join(names) or 'already there'}); crawling now.")
            announce(stack)
            await watcher()
        except StopRequested:
            say("Stop requested: stopping the stack.")
        return 0


async def start(
    args: argparse.Namespace,
    cluster: Cluster,
    demo_port: int,
    slow_port: int,
    settings: Settings,
    resources: Resources,
    log_dir: Path,
) -> dict[str, Any]:
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
    await expect_started(cluster, workers)
    api_url = f"http://127.0.0.1:{api_port}"
    await wait_until_serving(f"{api_url}/health", within=60, process=api)
    say("API, scheduler, 2 crawler workers, index dispatcher and 2 indexers: running.")

    frontend_environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("SITESCOPE_", "VITE_"))
    }
    # The browser talks to this stack only: the API path and the polling interval are pinned,
    # whatever a developer's frontend/.env.local says (process variables take precedence over
    # Vite's .env files).
    frontend_environment.update(
        {
            "SITESCOPE_API_URL": api_url,
            "VITE_API_BASE_URL": "/api/v1",
            "VITE_POLL_INTERVAL_MS": "3000",
        }
    )
    vite = ["node_modules/vite/bin/vite.js"]
    if not args.no_build:
        say("Building the dashboard...")
        builder = frontend_process(
            cluster, "frontend-build", [*vite, "build"], frontend_environment
        )
        if await asyncio.to_thread(builder.popen.wait) != 0:
            raise DemoError(f"The dashboard's build failed:\n{builder.tail(15)}")
    preview_port = free_port(args.port)
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
        "demo_seed": f"http://{DEMO_HOST}:{demo_port}/",
        "slow_seed": f"http://{SLOW_HOST}:{slow_port}/",
        "database": settings.postgres_db,
        "redis": f"db {settings.redis_db}, prefix {resources.prefix}",
        "logs": str(log_dir),
    }
    (log_dir / "stack.json").write_text(json.dumps(stack, indent=2) + "\n", encoding="utf-8")
    write_state(dashboard=dashboard_url, api=api_url)
    return stack


def announce(stack: dict[str, Any]) -> None:
    rule = "=" * 72
    say(
        f"\n{rule}\n"
        f"  SiteScope is running. Open the dashboard:  {stack['dashboard']}\n\n"
        f"  API:        {stack['api']}  (OpenAPI docs: {stack['api']}/docs)\n"
        f"  Demo site:  {stack['demo_seed']}  (an approved crawl target)\n"
        f"  Slow site:  {stack['slow_seed']}  (pages take 1.5 s: a crawl to cancel)\n"
        f"  Database:   {stack['database']} (disposable); Redis {stack['redis']}\n"
        f"  Logs:       {stack['logs']}\n\n"
        "  Stop: Ctrl+C here, or demo.cmd --stop from another terminal.\n"
        f"{rule}\n"
    )


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


async def release_storage(resources: Resources, *, keep: bool) -> bool:
    """Delete the stack's Redis keys and drop its database. An unreachable PostgreSQL or Redis
    is reported, never raised: the next run removes what is left. Returns whether all of it
    was released."""
    settings = resources.settings
    if settings is None or not resources.storage_touched:
        return True
    left: list[str] = []
    deleted = 0
    try:
        deleted = await delete_keys(settings, resources.prefix)
    except UNREACHABLE as error:
        left.append(f"Redis keys under {resources.prefix} ({type(error).__name__})")
    if resources.database_created and not keep:
        try:
            await helpers.drop_database(settings)
        except UNREACHABLE as error:
            left.append(f"database {settings.postgres_db} ({type(error).__name__})")
    if left:
        say(f"Could not remove: {'; '.join(left)}. The next run removes them.")
        return False
    if keep:
        say(f"Kept database {settings.postgres_db}; deleted {deleted} Redis keys.")
    else:
        say(f"Dropped database {settings.postgres_db}; deleted {deleted} Redis keys.")
    return True


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
        description="Run the whole SiteScope stack on a disposable database: the local demo "
        "(default), or the dashboard's end-to-end tests (--e2e).",
    )
    parser.add_argument("--e2e", action="store_true", help="run the Playwright suite, then stop")
    parser.add_argument("--stop", action="store_true", help="stop the stack running elsewhere")
    parser.add_argument("--keep", action="store_true", help="keep the disposable database")
    parser.add_argument("--no-build", action="store_true", help="serve the existing frontend build")
    parser.add_argument("--no-seed", action="store_true", help="do not create the demo jobs")
    parser.add_argument(
        "--no-infra",
        action="store_true",
        help="use PostgreSQL and Redis as they are: no WSL session, no docker compose",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"the dashboard's port (default {DEFAULT_PORT}; 0: any free port)",
    )
    parser.add_argument("playwright_args", nargs="*", help="passed to `playwright test` (after --)")
    args = parser.parse_args(argv)
    if args.stop:
        return request_stop()
    resources = Resources()
    try:
        return asyncio.run(run(args, resources))
    except KeyboardInterrupt:
        # A second Ctrl+C interrupted the graceful stop: finish the cleanup without waiting.
        if not resources.cleaned:
            if resources.cluster is not None:
                resources.cluster.kill_all()
            with contextlib.suppress(Exception):
                asyncio.run(release_storage(resources, keep=args.keep))
        if resources.infrastructure is not None:
            resources.infrastructure.close()
        release_state()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
