"""The local stack's launcher (tests/dashboard_stack.py): infrastructure readiness, supervision,
one stack at a time, and a cleanup that never fails and never touches anything but the stack's
own storage.

No service is needed: the probes, the commands (wsl, docker) and the WSL session are test
doubles. The only real processes are short Python ones standing for a stack process that exits.
"""

import asyncio
import os
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from tests import dashboard_stack as stack
from tests.integration import helpers
from tests.process_support import Cluster, ManagedProcess

pytestmark = pytest.mark.anyio

SETTINGS = stack.stack_settings(Settings(_env_file=None, postgres_db="sitescope"))
DOCKER_VERSION = ["wsl", "-e", "docker", "version", "--format", "{{.Server.Version}}"]
COMPOSE_UP = ["wsl", "-e", "docker", "compose", "up", "-d", "--wait"]


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never the real state directory: a test must not stop, or pass for, a running stack."""
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setattr(stack, "STATE_DIR", directory)
    return directory


def request_a_stop() -> None:
    stack.stop_file().touch()


class FakeSession:
    """A stand-in for the ``wsl.exe -e sleep infinity`` process."""

    def __init__(self, code: int | None = None) -> None:
        self.code = code
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = 1

    def wait(self, timeout: float | None = None) -> int:
        return self.code or 0


class Commands:
    """The command runner: records each command; answers ``docker version`` and ``compose``."""

    def __init__(
        self, docker: tuple[int, str] = (0, "29.9.3"), compose: tuple[int, str] = (0, "")
    ) -> None:
        self.calls: list[list[str]] = []
        self.docker = docker
        self.compose = compose

    def __call__(self, command: Any, timeout: float) -> tuple[int, str]:
        self.calls.append(list(command))
        return self.compose if "compose" in command else self.docker


def probes(*answers: str | None) -> stack.Probe:
    """A probe giving each answer in turn (the last one repeats): None is "it answers"."""
    remaining = list(answers)

    async def probe(settings: Settings) -> str | None:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return probe


ANSWERS = probes(None)


def exited_process(tmp_path: Path, name: str, code: int, output: str) -> ManagedProcess:
    """A stack process that wrote ``output`` to its log and exited with ``code``."""
    log_path = tmp_path / f"{name}.log"
    with log_path.open("wb") as log:
        popen = subprocess.Popen(  # noqa: S603 - this interpreter, fixed code
            [sys.executable, "-c", f"print({output!r}); raise SystemExit({code})"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    popen.wait(30)
    return ManagedProcess(name, popen, log_path)


# --- Infrastructure -----------------------------------------------------------------------


async def test_the_wsl_session_is_held_before_docker_and_the_services_are_awaited(
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands = Commands()
    session = FakeSession()
    started_after: list[int] = []

    def start_session() -> FakeSession:
        started_after.append(len(commands.calls))
        return session

    infrastructure = await stack.prepare_infrastructure(
        SETTINGS,
        manage=True,
        run=commands,
        start_session=start_session,
        probe_postgres=probes("ConnectionRefusedError", None),  # forwarding comes up late
        probe_redis=ANSWERS,
        services_within=10,
    )

    assert started_after == [0]  # the session first: WSL must not stop under the commands
    assert commands.calls == [DOCKER_VERSION, COMPOSE_UP]
    assert infrastructure.session is session
    assert infrastructure.alive()
    assert "Docker Engine 29.9.3 is running in WSL." in capsys.readouterr().out


async def test_docker_not_answering_is_one_clear_error_and_nothing_else_is_touched() -> None:
    commands = Commands(docker=(1, "Cannot connect to the Docker daemon. Is it running?"))
    session = FakeSession()

    with pytest.raises(stack.DemoError, match="Docker Engine does not answer") as error:
        await stack.prepare_infrastructure(
            SETTINGS, manage=True, run=commands, start_session=lambda: session, docker_within=0
        )

    assert "sudo systemctl start docker" in str(error.value)  # what to do; never done for you
    assert commands.calls == [DOCKER_VERSION]  # no compose: the services are not touched
    assert session.terminated  # the session held for the run is released


async def test_docker_missing_in_wsl_is_named() -> None:
    commands = Commands(docker=(1, "execvpe(docker) failed: No such file or directory"))

    with pytest.raises(stack.DemoError, match="Docker Engine is not installed in WSL"):
        await stack.prepare_infrastructure(
            SETTINGS, manage=True, run=commands, start_session=FakeSession, docker_within=60
        )


async def test_a_failed_compose_up_shows_its_output() -> None:
    commands = Commands(compose=(1, "Error response from daemon: port is already allocated"))
    session = FakeSession()

    with pytest.raises(stack.DemoError, match="port is already allocated"):
        await stack.prepare_infrastructure(
            SETTINGS, manage=True, run=commands, start_session=lambda: session
        )
    assert session.terminated


async def test_services_unreachable_before_startup_are_named_with_how_to_start_them() -> None:
    with pytest.raises(stack.DemoError) as error:
        await stack.wait_for_services(
            SETTINGS,
            within=0,
            hint="Start them first.",
            probe_postgres=probes("ConnectionRefusedError"),
            probe_redis=probes("ConnectionError"),
        )

    message = str(error.value)
    assert f"PostgreSQL at 127.0.0.1:{SETTINGS.postgres_port} (ConnectionRefusedError)" in message
    assert f"Redis at 127.0.0.1:{SETTINGS.redis_port} (ConnectionError)" in message
    assert message.endswith("Start them first.")


async def test_without_management_no_command_runs_and_no_session_is_held() -> None:
    commands = Commands()

    infrastructure = await stack.prepare_infrastructure(
        SETTINGS, manage=False, run=commands, probe_postgres=ANSWERS, probe_redis=ANSWERS
    )

    assert commands.calls == []
    assert infrastructure.session is None


# --- Supervision ----------------------------------------------------------------------------


def watcher(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "infrastructure": stack.Infrastructure(),
        "processes": lambda: [],
        "interval": 0.01,
        "probe_postgres": ANSWERS,
        "probe_redis": ANSWERS,
    }
    values.update(overrides)
    return stack.watch(SETTINGS, **values)


async def test_a_dependency_lost_during_startup_stops_it_with_one_message() -> None:
    cancelled = asyncio.Event()

    async def startup() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(stack.DemoError) as error:
        await stack.supervised(startup(), watcher(probe_redis=probes(None, "ConnectionError")))

    assert cancelled.is_set()  # the startup does not run on into a cascade of failures
    assert f"Redis at 127.0.0.1:{SETTINGS.redis_port} (ConnectionError)" in str(error.value)
    assert "Did WSL or Docker stop?" in str(error.value)


async def test_one_failed_probe_is_not_a_lost_dependency() -> None:
    calls = 0

    async def blinking(settings: Settings) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 8:
            await asyncio.to_thread(request_a_stop)
        return "TimeoutError" if calls % 2 else None  # every other probe fails

    with pytest.raises(stack.StopRequested):
        await watcher(probe_postgres=blinking)
    assert calls >= 8


async def test_the_wsl_session_ending_is_reported() -> None:
    lost = stack.Infrastructure(session=FakeSession(code=1))

    with pytest.raises(stack.DemoError, match="WSL session this launcher holds ended"):
        await watcher(infrastructure=lost)


async def test_a_stack_process_that_exits_is_reported_with_its_log(tmp_path: Path) -> None:
    worker = exited_process(tmp_path, "worker-1", 3, "boom: no Redis")

    with pytest.raises(stack.DemoError) as error:
        await watcher(processes=lambda: [worker])

    assert "worker-1 exited (code 3)" in str(error.value)
    assert "boom: no Redis" in str(error.value)


async def test_a_worker_that_fails_to_start_is_one_clear_error(tmp_path: Path) -> None:
    worker = exited_process(tmp_path, "worker-1", 1, "ImportError: no module named x")
    cluster = Cluster({}, tmp_path, processes=[worker])

    with pytest.raises(stack.DemoError, match="did not start: worker-1 exited") as error:
        await stack.expect_started(cluster, [worker])
    assert "ImportError: no module named x" in str(error.value)
    assert error.value.__cause__ is None  # no chained traceback


async def test_a_stop_request_ends_the_watch() -> None:
    await asyncio.to_thread(request_a_stop)

    with pytest.raises(stack.StopRequested):
        await watcher()


async def test_work_that_finishes_first_is_returned_and_the_watcher_stopped() -> None:
    async def work() -> str:
        await asyncio.sleep(0.05)
        return "the stack"

    assert await stack.supervised(work(), watcher()) == "the stack"


# --- Cleanup and storage ---------------------------------------------------------------------


async def test_cleanup_without_the_services_reports_what_is_left_and_does_not_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The original failure: PostgreSQL was gone, and the cleanup's own traceback followed."""

    async def refused(*args: object, **kwargs: object) -> int:
        raise ConnectionRefusedError(1225, "The remote computer refused the network connection")

    monkeypatch.setattr(stack, "delete_keys", refused)
    monkeypatch.setattr(helpers, "drop_database", refused)
    resources = stack.Resources(settings=SETTINGS, storage_touched=True, database_created=True)

    assert await stack.release_storage(resources, keep=False) is False
    out = capsys.readouterr().out
    assert f"database {SETTINGS.postgres_db} (ConnectionRefusedError)" in out
    assert "Redis keys under ui-stack (ConnectionRefusedError)" in out
    assert "The next run removes them." in out


async def test_nothing_is_released_when_the_storage_was_never_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected(*args: object, **kwargs: object) -> int:
        raise AssertionError("must not be called")

    monkeypatch.setattr(stack, "delete_keys", unexpected)
    resources = stack.Resources(settings=SETTINGS)  # it failed before the storage

    assert await stack.release_storage(resources, keep=False) is True


def test_the_stack_uses_only_its_own_disposable_storage() -> None:
    assert SETTINGS.postgres_db == "sitescope_test_ui"
    assert SETTINGS.redis_db == 15
    assert SETTINGS.admin_token is None
    assert stack.REDIS_PREFIX == "ui-stack"


async def test_the_key_cleanup_matches_the_stacks_prefix_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class FakeRedis:
        async def scan_iter(self, *, match: str, count: int) -> AsyncIterator[str]:
            seen["match"] = match
            for key in ("ui-stack:crawl", "ui-stack:index"):
                yield key

        async def delete(self, *keys: str) -> int:
            seen["deleted"] = keys
            return len(keys)

        async def aclose(self) -> None:
            seen["closed"] = True

    def fake_client(settings: Settings, *, client_name: str) -> FakeRedis:
        seen["db"] = settings.redis_db
        return FakeRedis()

    monkeypatch.setattr(stack, "create_redis_client", fake_client)

    assert await stack.delete_keys(SETTINGS, stack.REDIS_PREFIX) == 2
    assert seen == {
        "db": 15,
        "match": "ui-stack:*",
        "deleted": ("ui-stack:crawl", "ui-stack:index"),
        "closed": True,
    }


async def test_the_cleanup_refuses_any_database_but_a_disposable_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_keys(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(stack, "delete_keys", no_keys)
    development = SETTINGS.model_copy(update={"postgres_db": "sitescope"})
    resources = stack.Resources(settings=development, storage_touched=True, database_created=True)

    with pytest.raises(RuntimeError, match="refusing to use non-test database 'sitescope'"):
        await stack.release_storage(resources, keep=False)


# --- One stack at a time --------------------------------------------------------------------


def test_a_running_stack_is_found_and_a_second_start_only_points_to_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stack.write_state(pid=os.getpid(), dashboard="http://127.0.0.1:4173")

    assert stack.main([]) == 0  # nothing started, nothing dropped

    out = capsys.readouterr().out
    assert f"already running (pid {os.getpid()}): http://127.0.0.1:4173" in out
    assert stack.state_file().exists()  # the running stack's state is left alone
    assert stack.main(["--e2e"]) == 3  # an end-to-end run never shares that database


def test_a_stale_state_is_removed() -> None:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait(30)
    stack.write_state(pid=finished.pid)

    assert stack.running_instance() is None
    assert not stack.state_file().exists()


def test_stop_asks_the_running_stack_and_waits_for_its_cleanup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stack.write_state(pid=os.getpid())
    asked: list[bool] = []

    def the_running_stack() -> None:
        while not stack.stop_file().exists():
            time.sleep(0.01)
        asked.append(True)
        time.sleep(0.2)  # its cleanup
        stack.release_state()

    thread = threading.Thread(target=the_running_stack)
    thread.start()
    try:
        assert stack.request_stop(within=10, interval=0.02) == 0
    finally:
        thread.join(10)

    assert asked == [True]
    assert "Stopped." in capsys.readouterr().out


def test_stop_without_a_running_stack_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert stack.request_stop() == 0
    assert "No SiteScope stack is running." in capsys.readouterr().out


def test_process_alive_never_signals_the_process() -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert stack.process_alive(sleeper.pid)
        assert sleeper.poll() is None  # still running: checked, not killed
    finally:
        sleeper.kill()
        sleeper.wait(30)
    assert not stack.process_alive(sleeper.pid)
    assert not stack.process_alive(0)
