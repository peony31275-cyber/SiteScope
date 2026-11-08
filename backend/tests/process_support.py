"""Real processes for the distributed tests (tests/integration/test_distributed.py, tests/fault).

``Cluster`` starts the scheduler and crawler workers as separate operating-system processes
(``python -m app.scheduler``, ``python -m app.workers.crawler``), and since Phase 7 the index
dispatcher and indexers (``python -m app.scheduler.index_dispatcher``,
``python -m app.workers.indexer``), each with its JSON log in a file. It can stop them
gracefully (CTRL_BREAK_EVENT on Windows, SIGTERM elsewhere), kill them (TerminateProcess /
SIGKILL), and stall them (NtSuspendProcess / SIGSTOP). ``TcpProxy`` sits between the processes
and PostgreSQL or Redis and can cut them off.

Safety: the children read backend/.env for credentials, and its database is the development
database. ``child_environment`` therefore always sets the database explicitly, and refuses any
name that is not a disposable ``*_test`` database, and any Redis database but 15.

Everything here that waits is asynchronous: the local HTTP servers the children crawl run in the
test's event loop, so blocking it would stall the crawl.
"""

import asyncio
import contextlib
import itertools
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from app.core.config import Settings
from tests.fetch_support import Reply, Served
from tests.integration import helpers

# Short timings for process tests; they satisfy the order checked at startup:
# task deadline (6 s) < lease (7 s) <= reclaim min idle (7 s) <= reconcile after (10 s)
# <= retention (600 s).
FAST_TIMING: Mapping[str, str] = {
    "FETCH_CONNECT_TIMEOUT_SECONDS": "2",
    "FETCH_READ_TIMEOUT_SECONDS": "5",
    "FETCH_DEADLINE_SECONDS": "6",
    "CRAWL_LEASE_MARGIN_SECONDS": "1",
    "CRAWL_LEASE_SECONDS": "7",
    "CRAWL_CONCURRENCY": "2",
    "STREAM_RECLAIM_MIN_IDLE_SECONDS": "7",
    "STREAM_RECLAIM_INTERVAL_SECONDS": "0.5",
    "STREAM_READ_BLOCK_MS": "200",
    "STREAM_RETENTION_SECONDS": "600",
    "SCHEDULER_INTERVAL_SECONDS": "0.1",
    "SCHEDULER_RECONCILE_AFTER_SECONDS": "10",
    "SHUTDOWN_GRACE_SECONDS": "3",
    "POLITENESS_INTERVAL_SECONDS": "0.05",
    "POLITENESS_MAX_CONCURRENT_PER_HOST": "2",
    "POLITENESS_MAX_WAIT_SECONDS": "5",
    "ROBOTS_MAX_CRAWL_DELAY_SECONDS": "1",
    # Phase 6: short retry delays, prompt cancellation checks, short politeness slot leases.
    "RETRY_BASE_DELAY_SECONDS": "0.2",
    "RETRY_MAX_DELAY_SECONDS": "1",
    "CRAWL_CANCEL_CHECK_SECONDS": "0.2",
    "POLITENESS_SLOT_LEASE_SECONDS": "1",
    # Phase 7: index tasks cut off after 3 s, reclaimed after 5 s idle, republished after 10 s.
    "INDEX_TASK_DEADLINE_SECONDS": "3",
    "INDEX_RECLAIM_MIN_IDLE_SECONDS": "5",
    "INDEX_RECONCILE_AFTER_SECONDS": "10",
    "INDEX_DISPATCHER_INTERVAL_SECONDS": "0.1",
}

ALLOW_ALL_ROBOTS = Reply(
    headers=[("Content-Type", "text/plain")], body=b"User-agent: *\nAllow: /\n"
)


# --- Sites ---------------------------------------------------------------------------------------


def page(*links: str, title: str = "Page") -> Reply:
    anchors = "".join(f'<a href="{href}">link</a>' for href in links)
    body = f"<!doctype html><title>{title}</title><p>Some text.</p>{anchors}"
    return Reply(headers=[("Content-Type", "text/html; charset=utf-8")], body=body.encode())


@dataclass(frozen=True)
class Site:
    routes: dict[str, Reply]
    depths: dict[str, int]  # page path -> BFS depth from "/"

    def paths(self, max_depth: int) -> set[str]:
        return {path for path, depth in self.depths.items() if depth <= max_depth}


def tree_site(
    depth: int, branching: int, *, delays: Mapping[str, float] | None = None, delay: float = 0.0
) -> Site:
    """A tree of HTML pages: "/" links to "/0".."/<b-1>", "/0" to "/0/0".., down to ``depth``.

    Every page also links back to "/" and to its first sibling, so deduplication matters.
    ``delays`` slows down single pages (seconds); ``delay`` slows down every page.
    """
    routes: dict[str, Reply] = {"/robots.txt": ALLOW_ALL_ROBOTS}
    depths: dict[str, int] = {}

    def add(path: str, level: int) -> None:
        children = (
            [f"{path.rstrip('/')}/{index}" for index in range(branching)] if level < depth else []
        )
        sibling = path.rsplit("/", 1)[0] + "/0" if level > 0 else "/"
        reply = page(*children, "/", sibling)
        reply.delay = (delays or {}).get(path, delay)
        routes[path] = reply
        depths[path] = level
        for child in children:
            add(child, level + 1)

    add("/", 0)
    return Site(routes, depths)


# --- Server-side checks --------------------------------------------------------------------------


def assert_strict_bfs(served: Iterable[Served], depths: Mapping[str, int]) -> None:
    """Every request for a page of depth d started after every page of depth d - 1 was served:
    the crawler admits a level only once the previous one is finished (ADR-0004)."""
    by_depth: dict[int, list[Served]] = {}
    for item in served:
        if item.target in depths:
            by_depth.setdefault(depths[item.target], []).append(item)
    for previous, level in itertools.pairwise(sorted(by_depth)):
        last_finished = max(item.finished for item in by_depth[previous])
        first_started = min(item.started for item in by_depth[level])
        assert first_started >= last_finished, (previous, level, first_started - last_finished)


def assert_polite(
    served: Sequence[Served], *, interval: float, max_concurrent: int, tolerance: float = 0.05
) -> None:
    """Requests to one host: never more than ``max_concurrent`` at once, and starts spaced by
    at least ``interval`` less ``tolerance``. The gate spaces the grants; a process that sends
    one request a little late (scheduling on a loaded Windows host with many processes) shrinks
    the gap the server sees, hence the tolerance. Without the gate the gaps would be near 0."""
    starts = sorted(item.started for item in served)
    gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert not gaps or min(gaps) >= interval - tolerance, min(gaps)
    events = sorted(
        [(item.started, 1) for item in served] + [(item.finished, -1) for item in served]
    )
    active = peak = 0
    for _, change in events:
        active += change
        peak = max(peak, active)
    assert peak <= max_concurrent, peak


# --- Child processes -----------------------------------------------------------------------------


# Settings ``extra`` may not change: where the children's data lives.
PROTECTED_SETTINGS: frozenset[str] = frozenset(
    {"ENVIRONMENT", "POSTGRES_DB", "REDIS_DB", "REDIS_KEY_PREFIX"}
)


def child_environment(
    settings: Settings,
    *,
    prefix: str,
    hosts: Sequence[str],
    ports: Sequence[int],
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment of a scheduler or worker process of a test: the test database, Redis
    database 15 under ``prefix``, approved local targets, and fast timings.

    Refuses a database that is not a disposable ``*_test`` one, any Redis database but 15, and
    any ``extra`` setting that would change either (names are case-insensitive on Windows).
    """
    helpers.require_disposable(settings.postgres_db)  # never the development database
    if settings.redis_db != 15:
        raise RuntimeError("process tests use Redis database 15 only")
    extra_values = {name.upper(): value for name, value in (extra or {}).items()}
    if len(extra_values) != len(extra or {}) or PROTECTED_SETTINGS & set(extra_values):
        raise RuntimeError(f"extra settings must not set {sorted(PROTECTED_SETTINGS)}")
    values: dict[str, str] = {
        "ENVIRONMENT": "test",
        "LOG_LEVEL": "INFO",
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": settings.postgres_db,
        "REDIS_HOST": settings.redis_host,
        "REDIS_PORT": str(settings.redis_port),
        "REDIS_DB": str(settings.redis_db),
        "REDIS_KEY_PREFIX": prefix,
        "CRAWL_ALLOWED_HOSTS": ",".join(hosts),
        "CRAWL_ALLOWED_SCHEMES": "http",
        "CRAWL_ALLOWED_PORTS": ",".join(str(port) for port in ports),
        "FETCH_INTERNAL_ORIGINS": ",".join(
            f"http://{host}:{port}" for host in hosts for port in ports
        ),
        **FAST_TIMING,
        **extra_values,
    }
    if settings.redis_password is not None:
        # Normally the same as in backend/.env; a disposable Redis has its own (Phase 6).
        values["REDIS_PASSWORD"] = settings.redis_password.get_secret_value()
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("SITESCOPE_")
    }
    environment.update({f"SITESCOPE_{name}": value for name, value in values.items()})
    environment["PYTHONUTF8"] = "1"
    # The final word, checked on what the child will actually see.
    sitescope = {name.upper(): value for name, value in environment.items()}
    helpers.require_disposable(sitescope["SITESCOPE_POSTGRES_DB"])
    if sitescope["SITESCOPE_REDIS_DB"] != "15":
        raise RuntimeError("process tests use Redis database 15 only")
    return environment


@dataclass
class ManagedProcess:
    name: str
    popen: "subprocess.Popen[bytes]"
    log_path: Path

    def lines(self) -> list[dict[str, Any]]:
        """The JSON log lines written so far (a partial last line is skipped)."""
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        parsed = []
        for line in content.splitlines():
            if line.startswith("{"):
                with contextlib.suppress(json.JSONDecodeError):
                    parsed.append(json.loads(line))
        return parsed

    def events(self, event: str) -> list[dict[str, Any]]:
        return [line for line in self.lines() if line.get("event") == event]

    def tail(self, count: int = 30) -> str:
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        return "\n".join(content.splitlines()[-count:])

    @property
    def alive(self) -> bool:
        return self.popen.poll() is None

    @property
    def identity(self) -> str:
        """The process's unique name: its consumer name and lease owner (worker_id)."""
        for line in self.lines():
            if line.get("event") == "worker.started":
                return str(line["worker_id"])
            if line.get("event") == "scheduler.started":
                return str(line["scheduler_id"])
        raise AssertionError(f"{self.name} has not started:\n{self.tail()}")

    @property
    def interpreter_pid(self) -> int:
        """The PID of the Python interpreter running the service, from its unique name.

        On Windows, ``.venv\\Scripts\\python.exe`` is a launcher that runs the real interpreter
        as a child process (in a job object that ends the child with the launcher), so
        ``popen.pid`` is the launcher's PID. Suspending the launcher would not freeze the
        service: freeze and kill the interpreter itself.
        """
        return int(self.identity.rsplit("-", 2)[1])

    def interrupt(self) -> None:
        """Ask for a graceful stop: CTRL_BREAK_EVENT (Windows) or SIGTERM."""
        if sys.platform == "win32":
            # Sent to the process group: the launcher and the interpreter both receive it.
            os.kill(self.popen.pid, signal.CTRL_BREAK_EVENT)
        else:  # pragma: no cover - POSIX only
            self.popen.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        """A crash: TerminateProcess (Windows) or SIGKILL of the interpreter; nothing runs in
        it any more (no cleanup, no finally blocks). The launcher then exits too."""
        try:
            pid = self.interpreter_pid
        except AssertionError:  # not started yet: kill whatever runs
            self.popen.kill()
            return
        if sys.platform == "win32":
            os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        else:  # pragma: no cover - POSIX only
            os.kill(pid, signal.SIGKILL)

    def suspend(self) -> None:
        """Freeze every thread of the interpreter (NtSuspendProcess / SIGSTOP)."""
        if sys.platform == "win32":
            _nt_process_call("NtSuspendProcess", self.interpreter_pid)
        else:  # pragma: no cover - POSIX only
            os.kill(self.interpreter_pid, signal.SIGSTOP)

    def resume(self) -> None:
        if sys.platform == "win32":
            _nt_process_call("NtResumeProcess", self.interpreter_pid)
        else:  # pragma: no cover - POSIX only
            os.kill(self.interpreter_pid, signal.SIGCONT)

    async def exited(self, *, within: float = 30.0) -> int:
        deadline = time.monotonic() + within
        while (code := self.popen.poll()) is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"{self.name} did not exit:\n{self.tail()}")
            await asyncio.sleep(0.05)
        return code

    async def stop(self, *, within: float = 30.0) -> int:
        """Graceful stop; returns the exit code."""
        if self.alive:
            self.interrupt()
        return await self.exited(within=within)


if sys.platform == "win32":
    import ctypes

    def _nt_process_call(function: str, pid: int) -> None:
        """NtSuspendProcess / NtResumeProcess: (un)freeze every thread of a process."""
        process_suspend_resume = 0x0800
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        handle = kernel32.OpenProcess(process_suspend_resume, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            status = getattr(ntdll, function)(handle)
            if status != 0:
                raise OSError(f"{function} failed with NTSTATUS {status:#x}")
        finally:
            kernel32.CloseHandle(handle)


@dataclass
class Cluster:
    """Scheduler and worker processes of one test; every one is stopped or killed at the end."""

    environment: dict[str, str]
    log_dir: Path
    processes: list[ManagedProcess] = field(default_factory=list)
    _counts: Counter[str] = field(default_factory=Counter)

    def scheduler(self) -> ManagedProcess:
        return self._spawn("scheduler", "app.scheduler")

    def worker(self, *arguments: str) -> ManagedProcess:
        return self._spawn("worker", "app.workers.crawler", *arguments)

    def workers(self) -> list[ManagedProcess]:
        return [process for process in self.processes if process.name.startswith("worker")]

    def index_dispatcher(self) -> ManagedProcess:
        return self._spawn("index-dispatcher", "app.scheduler.index_dispatcher")

    def indexer(self, *arguments: str) -> ManagedProcess:
        return self._spawn("indexer", "app.workers.indexer", *arguments)

    def indexers(self) -> list[ManagedProcess]:
        return [process for process in self.processes if process.name.startswith("indexer")]

    def api(self, port: int) -> ManagedProcess:
        """The API server (uvicorn) on 127.0.0.1:``port`` (Phase 9: the dashboard's end-to-end
        run, ``tests/dashboard_stack.py``). It logs no start event: wait for its /health."""
        return self._spawn(
            "api",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        )

    def _spawn(self, role: str, module: str, *arguments: str) -> ManagedProcess:
        self._counts[role] += 1
        name = f"{role}-{self._counts[role]}"
        log_path = self.log_dir / f"{name}.log"
        with log_path.open("wb") as log:
            popen = subprocess.Popen(  # noqa: S603 - this interpreter and a fixed module name
                [sys.executable, "-m", module, *arguments],
                cwd=helpers.BACKEND_DIR,
                env=self.environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        process = ManagedProcess(name, popen, log_path)
        self.processes.append(process)
        return process

    async def started(self, *processes: ManagedProcess, within: float = 60.0) -> None:
        """Wait until each process logged that it started (its stop handlers are installed)."""
        deadline = time.monotonic() + within
        for process in processes:
            while not any(process.events(event) for event in STARTED_EVENTS):
                if not process.alive:
                    raise AssertionError(f"{process.name} exited:\n{process.tail()}")
                if time.monotonic() > deadline:
                    raise AssertionError(f"{process.name} did not start:\n{process.tail()}")
                await asyncio.sleep(0.1)

    async def stop_all(self, *, within: float = 30.0) -> dict[str, int]:
        """Stop every live process gracefully; returns each one's exit code."""
        for process in self.processes:
            if process.alive:
                with contextlib.suppress(OSError):
                    process.interrupt()
        return {process.name: await process.exited(within=within) for process in self.processes}

    def kill_all(self) -> None:
        for process in self.processes:
            if process.alive:
                with contextlib.suppress(OSError, AssertionError):
                    process.resume()
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(OSError):
                    process.popen.kill()  # the launcher, if it is still there
        for process in self.processes:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.popen.wait(10)

    def all_lines(self) -> list[dict[str, Any]]:
        return [line for process in self.processes for line in process.lines()]


async def wait_for(
    condition: Callable[[], Awaitable[bool]],
    *,
    within: float,
    what: str,
    cluster: Cluster | None = None,
) -> None:
    """Poll ``condition`` until it holds; on timeout, fail with the processes' log tails."""
    deadline = time.monotonic() + within
    while not await condition():
        if time.monotonic() > deadline:
            tails = ""
            if cluster is not None:
                tails = "\n".join(
                    f"--- {process.name} ---\n{process.tail(15)}" for process in cluster.processes
                )
            raise AssertionError(f"timed out waiting for {what}\n{tails}")
        await asyncio.sleep(0.1)


# The line each kind of process logs once its stop handlers are installed.
STARTED_EVENTS = ("worker.started", "scheduler.started", "index_dispatcher.started")


# --- A TCP proxy that can cut a service off ------------------------------------------------------


class TcpProxy:
    """Forwards 127.0.0.1:<port> to ``target``. ``cut()`` resets every connection and refuses
    new ones (they are accepted and reset at once) until ``restore()``: a service outage as the
    clients see it, without touching the service itself."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.cut_off = False
        self.port = 0

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._server is not None
        self._server.close()
        self._reset_all()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._server.wait_closed()

    def cut(self) -> None:
        self.cut_off = True
        self._reset_all()

    def restore(self) -> None:
        self.cut_off = False

    def _reset_all(self) -> None:
        for writer in list(self._writers):
            writer.transport.abort()
        self._writers.clear()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.cut_off:
            writer.transport.abort()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        except OSError:
            writer.transport.abort()
            return
        self._writers.update((writer, upstream_writer))
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            await asyncio.gather(
                self._pipe(reader, upstream_writer), self._pipe(upstream_reader, writer)
            )
        finally:
            self._tasks.discard(task)
            for end in (writer, upstream_writer):
                self._writers.discard(end)
                end.transport.abort()

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                if self.cut_off:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.transport.abort()
