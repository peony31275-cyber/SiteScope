r"""Shared machinery of the benchmark scripts (Phase 10).

* **Isolation.** A disposable database ``<SITESCOPE_POSTGRES_DB>_test_bench`` (created, migrated and
  dropped by each script), Redis database 15 under a unique key prefix per run (deleted
  afterwards), HTTP servers on loopback addresses only. The development database and the
  development Redis keys are never touched; nothing leaves the machine.
* **Code variants.** The processes of a run can come from another checkout of the backend
  (``--variant name=path``, for example a ``git worktree`` of an earlier commit), so that two
  versions of the code are measured in one session with their runs interleaved: drift of the
  machine (heat, background load) then affects both alike.
* **Measurements.** Environment capture, samplers (the machine's CPU load and effective clock,
  the WSL 2 VM and the PostgreSQL and Redis containers, every benchmark process), PostgreSQL's
  cumulative statistics, and summary statistics. Every sampler reads real counters; nothing is
  estimated.

Percentiles use linear interpolation between the closest ranks (the method of numpy's default
and of PostgreSQL's ``percentile_cont``). A p95 is reported only from at least 20 samples, a p99
only from at least 100.
"""

import asyncio
import contextlib
import csv
import ctypes
import hashlib
import io
import itertools
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    # After the benchmarks directory (the script's, sys.path[0]): its modules come first.
    sys.path.insert(1, str(BACKEND))

from sqlalchemy import text  # noqa: E402 - after the path setup

from app.core.config import Settings  # noqa: E402
from app.core.redis import create_redis_client  # noqa: E402
from app.db.session import Database  # noqa: E402
from tests.fetch_support import Served  # noqa: E402
from tests.integration import helpers  # noqa: E402
from tests.process_support import Cluster, ManagedProcess, child_environment  # noqa: E402

# BENCHMARK_DATABASE_SUFFIX (not a SITESCOPE_ setting) lets a smoke test run beside a benchmark;
# the name must still be a disposable "*_test_*" one.
BENCH_SUFFIX = os.environ.get("BENCHMARK_DATABASE_SUFFIX", "_test_bench")
POSTGRES_CONTAINER = "sitescope-postgres-1"
REDIS_CONTAINER = "sitescope-redis-1"


# --- Settings and code variants -------------------------------------------------------------------


def bench_settings() -> Settings:
    """The settings of the benchmark: the disposable database and Redis database 15."""
    base = Settings()
    settings = base.model_copy(
        update={"postgres_db": f"{base.postgres_db}{BENCH_SUFFIX}", "redis_db": 15}
    )
    helpers.require_disposable(settings.postgres_db)
    return settings


async def fresh_database(settings: Settings, revision: str = "head") -> None:
    """Create the disposable database again, migrated to ``revision``."""
    await helpers.recreate_database(settings)
    await helpers.upgrade(settings, revision)


@dataclass(frozen=True)
class Variant:
    """A checkout of the backend whose processes a run starts."""

    name: str
    backend_dir: Path


def parse_variants(values: Sequence[str] | None) -> list[Variant]:
    """``name=path`` pairs (path: a backend directory); default: this checkout as "current"."""
    if not values:
        return [Variant("current", BACKEND)]
    variants = []
    for value in values:
        name, separator, path = value.partition("=")
        backend = Path(path).resolve()
        if not separator or not name or not (backend / "app" / "__init__.py").is_file():
            raise SystemExit(f"--variant {value!r}: expected name=<backend directory>")
        variants.append(Variant(name, backend))
    if len({variant.name for variant in variants}) != len(variants):
        raise SystemExit("variant names must be unique")
    return variants


def process_environment(
    settings: Settings,
    *,
    prefix: str,
    hosts: Sequence[str],
    ports: Sequence[int],
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """``child_environment`` (the test database, Redis database 15 under ``prefix``), with the
    database credentials passed explicitly: a variant's checkout has no ``backend/.env``."""
    values = {"POSTGRES_USER": settings.postgres_user, **(extra or {})}
    if settings.postgres_password is not None:
        values["POSTGRES_PASSWORD"] = settings.postgres_password.get_secret_value()
    return child_environment(settings, prefix=prefix, hosts=hosts, ports=ports, extra=values)


async def delete_keys(settings: Settings, prefix: str) -> None:
    """Delete a run's Redis keys (streams, politeness state, heartbeats and their index, which
    has no expiry) from database 15. Only keys under the run's own prefix."""
    if not prefix.startswith("bench-"):
        raise ValueError(f"not a benchmark prefix: {prefix!r}")
    redis = create_redis_client(settings, client_name="sitescope-benchmark")
    try:
        keys = [key async for key in redis.scan_iter(match=f"{prefix}:*", count=1000)]
        if keys:
            await redis.delete(*keys)
    finally:
        await redis.aclose()


@dataclass
class VariantCluster(Cluster):
    """A ``Cluster`` whose processes run the code of ``backend_dir``."""

    backend_dir: Path = BACKEND

    def _spawn(self, role: str, module: str, *arguments: str) -> ManagedProcess:
        self._counts[role] += 1
        name = f"{role}-{self._counts[role]}"
        log_path = self.log_dir / f"{name}.log"
        with log_path.open("wb") as log:
            popen = subprocess.Popen(  # noqa: S603 - this interpreter and a fixed module name
                [sys.executable, "-m", module, *arguments],
                cwd=self.backend_dir,
                env=self.environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        process = ManagedProcess(name, popen, log_path)
        self.processes.append(process)
        return process


def git_state(path: Path) -> dict[str, Any]:
    """The commit of a checkout, whether its files differ from it, and a digest of the
    differences (tracked changes and untracked files' contents), so that two runs of
    uncommitted code can be told apart."""

    def git_bytes(*arguments: str) -> bytes:
        completed = subprocess.run(  # noqa: S603 - git with fixed arguments
            ["git", "-C", str(path), *arguments],  # noqa: S607 - git from PATH
            capture_output=True,
            check=False,
        )
        return completed.stdout

    def git(*arguments: str) -> str:
        # Git writes UTF-8; the console's code page (GBK here) cannot decode every diff.
        return git_bytes(*arguments).decode("utf-8", errors="replace").strip()

    untracked = sorted(git("ls-files", "--others", "--exclude-standard").splitlines())
    digest = hashlib.sha256(git_bytes("diff", "HEAD"))
    root = Path(git("rev-parse", "--show-toplevel") or path)
    for name in untracked:
        digest.update(name.encode())
        with contextlib.suppress(OSError):
            digest.update((root / name).read_bytes())
    return {
        "commit": git("rev-parse", "HEAD"),
        "tracked_changes": bool(git("status", "--porcelain", "--untracked-files=no")),
        "untracked_files": len(untracked),
        "changes_sha256": digest.hexdigest()[:16],
    }


# --- Environment ----------------------------------------------------------------------------------


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _PowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _windows_hardware() -> dict[str, Any]:
    if sys.platform != "win32":  # pragma: no cover - the benchmarks run on Windows
        return {}
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    ) as key:
        cpu = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    memory = _MemoryStatus()
    memory.dwLength = ctypes.sizeof(_MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
    power = _PowerStatus()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power))
    scheme = subprocess.run(
        ["powercfg", "/getactivescheme"],  # noqa: S607 - a Windows tool from PATH
        capture_output=True,
        check=False,
    ).stdout.decode("mbcs", errors="replace")
    guid = re.search(r"[0-9a-f]{8}-[0-9a-f-]{27}", scheme)
    return {
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "ram_gb": round(memory.ullTotalPhys / 2**30, 1),
        "on_ac_power": power.ACLineStatus == 1,
        "power_scheme_guid": guid.group(0) if guid else None,
    }


def _wsl_vm() -> dict[str, Any]:
    completed = subprocess.run(
        ["wsl", "-e", "sh", "-c", "nproc; grep MemTotal /proc/meminfo; uname -r"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    lines = completed.stdout.split("\n")
    if completed.returncode != 0 or len(lines) < 3:
        return {"error": completed.stderr.strip()[:200]}
    return {
        "cpus": int(lines[0]),
        "memory_gb": round(int(lines[1].split()[1]) / 2**20, 1),
        "kernel": lines[2].strip(),
    }


async def environment(
    settings: Settings, database: Database, variants: Sequence[Variant] = ()
) -> dict[str, Any]:
    """Where and how the benchmark ran (no credentials)."""
    from app.core.redis import create_redis_client

    async with database.session() as session:
        version = await session.scalar(text("SELECT version()"))
        rows = await session.execute(
            text(
                "SELECT name, setting, unit FROM pg_settings WHERE name = ANY(:names) ORDER BY name"
            ),
            {
                "names": [
                    "shared_buffers",
                    "effective_cache_size",
                    "work_mem",
                    "max_connections",
                    "synchronous_commit",
                    "fsync",
                    "jit",
                    "max_parallel_workers_per_gather",
                    "random_page_cost",
                ]
            },
        )
        postgres_settings = {
            row.name: f"{row.setting}{' ' + row.unit if row.unit else ''}" for row in rows
        }
    redis = create_redis_client(settings, client_name="sitescope-benchmark")
    try:
        server = await redis.info("server")
        persistence = await redis.info("persistence")
        memory = await redis.info("memory")
    finally:
        await redis.aclose()
    return {
        "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": " ".join(sys.argv),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": _windows_hardware(),
        "wsl_vm": _wsl_vm(),
        "postgresql": {"version": version, "settings": postgres_settings},
        "redis": {
            "version": server.get("redis_version"),
            "aof_enabled": persistence.get("aof_enabled"),
            "maxmemory": memory.get("maxmemory"),
        },
        "code": {variant.name: git_state(variant.backend_dir) for variant in variants},
    }


# --- Statistics -----------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q`` quantile (0..1) of ``values``, by linear interpolation between closest ranks."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower))


def distribution(values: Sequence[float], *, digits: int = 2) -> dict[str, Any]:
    """Count, min, p50, p95 (from 20 samples on), p99 (from 100 samples on), max and mean of
    ``values``. With fewer samples a high percentile would only restate the maximum."""
    if not values:
        return {"n": 0}
    result: dict[str, Any] = {
        "n": len(values),
        "min": round(min(values), digits),
        "p50": round(percentile(values, 0.50), digits),
        "p95": round(percentile(values, 0.95), digits) if len(values) >= 20 else None,
        "p99": round(percentile(values, 0.99), digits) if len(values) >= 100 else None,
        "max": round(max(values), digits),
        "mean": round(statistics.fmean(values), digits),
    }
    return result


def trials(values: Sequence[float], *, digits: int = 2) -> dict[str, Any]:
    """Median of repeated trials with their range; ``spread_pct`` is (max - min) / median."""
    if not values:
        return {"n": 0}
    median = statistics.median(values)
    return {
        "n": len(values),
        "values": [round(value, digits) for value in values],
        "median": round(median, digits),
        "min": round(min(values), digits),
        "max": round(max(values), digits),
        "spread_pct": round((max(values) - min(values)) / median * 100, 1) if median else None,
    }


# --- Server-side request timelines ----------------------------------------------------------------


def timeline(
    served: Iterable[Served], start: float, end: float, *, paths: set[str] | None = None
) -> dict[str, Any]:
    """Requests in flight at one server (one host) over ``[start, end]`` (monotonic seconds):
    the time-weighted mean, the peak, and the share of the window with none in flight."""
    events: list[tuple[float, int]] = []
    count = 0
    for item in served:
        if paths is not None and item.target.split("?")[0] not in paths:
            continue
        begin, finish = max(item.started, start), min(item.finished, end)
        count += 1
        if finish > begin:
            events += [(begin, 1), (finish, -1)]
    events.sort()
    window = end - start
    if window <= 0:
        return {"requests": count}
    active = peak = 0
    area = idle = 0.0
    previous = start
    for moment, change in events:
        span = moment - previous
        area += active * span
        if active == 0:
            idle += span
        previous = moment
        active += change
        peak = max(peak, active)
    span = end - previous
    area += active * span
    if active == 0:
        idle += span
    return {
        "requests": count,
        "mean_in_flight": round(area / window, 2),
        "peak_in_flight": peak,
        "idle_share": round(idle / window, 3),
    }


# --- Samplers -------------------------------------------------------------------------------------

TYPEPERF_COUNTERS = (
    r"\Processor Information(_Total)\% Processor Performance",
    r"\Processor Information(_Total)\% Processor Utility",
    r"\Processor Information(_Total)\% Performance Limit",
    r"\Processor Information(_Total)\% Processor Time",
)
_COUNTER_KEYS = (
    "processor_performance_pct",  # effective clock as % of the nominal one (boost > 100)
    "processor_utility_pct",
    "performance_limit_pct",  # < 100: the clock is limited (power, heat)
    "processor_time_pct",
)


def _container_cgroup(name: str) -> str | None:
    completed = subprocess.run(  # noqa: S603 - docker in WSL with fixed arguments
        ["wsl", "-e", "docker", "inspect", "-f", "{{.Id}}", name],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    container = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{64}", container):
        return None
    return f"/sys/fs/cgroup/system.slice/docker-{container}.scope"


@dataclass
class MachineSampler:
    """Once a second: the machine's CPU counters (typeperf), the WSL 2 VM's CPU (/proc/stat),
    and the CPU time of the PostgreSQL and Redis containers (their cgroups' cpu.stat)."""

    containers: Mapping[str, str] = field(
        default_factory=lambda: {"postgres": POSTGRES_CONTAINER, "redis": REDIS_CONTAINER}
    )
    _windows: list[dict[str, float]] = field(default_factory=list)
    _vm: list[tuple[float, int, int]] = field(default_factory=list)  # (time, total, idle) jiffies
    _cgroups: dict[str, list[tuple[float, int]]] = field(default_factory=dict)  # usage_usec
    _processes: list[asyncio.subprocess.Process] = field(default_factory=list)
    _readers: list[asyncio.Task[None]] = field(default_factory=list)

    async def start(self) -> None:
        typeperf = await asyncio.create_subprocess_exec(
            "typeperf",
            *TYPEPERF_COUNTERS,
            "-si",
            "1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._processes.append(typeperf)
        self._readers.append(asyncio.create_task(self._read_typeperf(typeperf)))
        cgroups = {
            label: path
            for label, name in self.containers.items()
            if (path := _container_cgroup(name)) is not None
        }
        loop_body = "printf 'T %s ' \"$(date +%s.%N)\"; head -1 /proc/stat || exit 0; " + "".join(
            f"printf 'C {label} %s ' \"$(date +%s.%N)\"; head -1 {path}/cpu.stat || exit 0; "
            for label, path in cgroups.items()
        )
        # At most an hour; the loop also ends when its output pipe breaks (the reader stopped).
        script = f"for i in $(seq 1 3600); do {loop_body} sleep 1; done"
        vm = await asyncio.create_subprocess_exec(
            "wsl",
            "-e",
            "sh",
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._processes.append(vm)
        self._readers.append(asyncio.create_task(self._read_vm(vm)))

    async def _read_typeperf(self, process: asyncio.subprocess.Process) -> None:
        if process.stdout is None:
            return
        while line := await process.stdout.readline():
            row = next(csv.reader(io.StringIO(line.decode("mbcs", errors="replace"))), [])
            if len(row) != len(TYPEPERF_COUNTERS) + 1 or row[0].startswith("(PDH"):
                continue
            with contextlib.suppress(ValueError):
                self._windows.append(
                    {key: float(value) for key, value in zip(_COUNTER_KEYS, row[1:], strict=True)}
                )

    async def _read_vm(self, process: asyncio.subprocess.Process) -> None:
        if process.stdout is None:
            return
        while line := await process.stdout.readline():
            parts = line.decode(errors="replace").split()
            with contextlib.suppress(ValueError, IndexError):
                if parts[0] == "T" and parts[2] == "cpu":
                    jiffies = [int(value) for value in parts[3:]]
                    idle = jiffies[3] + jiffies[4]  # idle + iowait
                    self._vm.append((float(parts[1]), sum(jiffies[:8]), idle))
                elif parts[0] == "C" and parts[3] == "usage_usec":
                    self._cgroups.setdefault(parts[1], []).append((float(parts[2]), int(parts[4])))

    async def stop(self) -> dict[str, Any]:
        for process in self._processes:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        for process in self._processes:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=10)
        for reader in self._readers:
            reader.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)
        return self.summary()

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {"windows_samples": len(self._windows)}
        for key in _COUNTER_KEYS:
            values = [sample[key] for sample in self._windows]
            if values:
                result[key] = {
                    "mean": round(statistics.fmean(values), 1),
                    "min": round(min(values), 1),
                    "max": round(max(values), 1),
                }
        busy = []
        for (t0, total0, idle0), (t1, total1, idle1) in itertools.pairwise(self._vm):
            if total1 > total0 and t1 > t0:
                busy.append(100 * (1 - (idle1 - idle0) / (total1 - total0)))
        if busy:
            result["wsl_vm_busy_pct"] = {
                "mean": round(statistics.fmean(busy), 1),
                "max": round(max(busy), 1),
            }
        for label, samples in self._cgroups.items():
            cores = [
                (u1 - u0) / 1e6 / (t1 - t0)
                for (t0, u0), (t1, u1) in itertools.pairwise(samples)
                if t1 > t0
            ]
            if cores:
                result[f"{label}_cpu_cores"] = {
                    "mean": round(statistics.fmean(cores), 2),
                    "max": round(max(cores), 2),
                    "cpu_seconds": round((samples[-1][1] - samples[0][1]) / 1e6, 2),
                }
        return result


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    @property
    def seconds(self) -> float:
        return ((self.high << 32) | self.low) / 1e7


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_KERNEL32: Any = None


def _kernel32() -> Any:
    """A private kernel32 handle with exact signatures (64-bit handles are pointers)."""
    global _KERNEL32
    if _KERNEL32 is None:
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        library.OpenProcess.restype = ctypes.c_void_p
        library.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        library.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
        library.K32GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        library.CloseHandle.argtypes = [ctypes.c_void_p]
        _KERNEL32 = library
    return _KERNEL32


def process_usage(pid: int) -> dict[str, float] | None:
    """CPU seconds (user + kernel) and working set (MB) of a Windows process, or None."""
    if sys.platform != "win32":  # pragma: no cover - the benchmarks run on Windows
        return None
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED | VM_READ
    if not handle:
        return None
    try:
        creation, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        memory = _MemoryCounters()
        memory.cb = ctypes.sizeof(_MemoryCounters)
        kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb)
        return {
            "cpu_seconds": kernel.seconds + user.seconds,
            "working_set_mb": memory.WorkingSetSize / 2**20,
            "peak_working_set_mb": memory.PeakWorkingSetSize / 2**20,
            "private_mb": memory.PagefileUsage / 2**20,
        }
    finally:
        kernel32.CloseHandle(handle)


@dataclass
class ProcessSampler:
    """CPU time and memory of named processes over a measured window (sampled every second for
    the peak working set inside the window)."""

    pids: Mapping[str, int]
    _start: dict[str, dict[str, float]] = field(default_factory=dict)
    _peak: dict[str, float] = field(default_factory=dict)
    _started_at: float = 0.0
    _task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._started_at = time.perf_counter()
        for name, pid in self.pids.items():
            usage = process_usage(pid)
            if usage is not None:
                self._start[name] = usage
                self._peak[name] = usage["working_set_mb"]
        self._task = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1)
            for name, pid in self.pids.items():
                usage = process_usage(pid)
                if usage is not None:
                    self._peak[name] = max(self._peak.get(name, 0.0), usage["working_set_mb"])

    async def stop(self) -> dict[str, Any]:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        elapsed = time.perf_counter() - self._started_at
        result = {}
        for name, pid in self.pids.items():
            end = process_usage(pid)
            begin = self._start.get(name)
            if end is None or begin is None:
                result[name] = {"error": "not readable"}
                continue
            cpu = end["cpu_seconds"] - begin["cpu_seconds"]
            result[name] = {
                "cpu_seconds": round(cpu, 2),
                "cpu_cores": round(cpu / elapsed, 3) if elapsed > 0 else None,
                "working_set_mb_start": round(begin["working_set_mb"], 1),
                "working_set_mb_end": round(end["working_set_mb"], 1),
                "working_set_mb_peak": round(self._peak.get(name, 0.0), 1),
                "private_mb_end": round(end["private_mb"], 1),
            }
        return result


_PG_DATABASE_STATS = text(
    """
    SELECT xact_commit, xact_rollback, blks_read, blks_hit, tup_returned, tup_fetched,
           tup_inserted, tup_updated, tup_deleted, conflicts, deadlocks, temp_files, temp_bytes
    FROM pg_stat_database WHERE datname = current_database()
    """
)

_PG_ACTIVITY = text(
    """
    SELECT count(*) AS connections,
           count(*) FILTER (WHERE wait_event_type = 'Lock') AS lock_waits,
           count(*) FILTER (WHERE state = 'active') AS active
    FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()
    """
)


async def pg_database_stats(database: Database) -> dict[str, int]:
    """PostgreSQL's cumulative statistics of the benchmark database."""
    async with database.session() as session:
        row = (await session.execute(_PG_DATABASE_STATS)).one()
    return {key: int(value) for key, value in row._mapping.items()}


def stats_delta(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, Any]:
    delta: dict[str, Any] = {key: after[key] - before[key] for key in after}
    reads = delta["blks_read"] + delta["blks_hit"]
    delta["cache_hit_pct"] = round(100 * delta["blks_hit"] / reads, 2) if reads else None
    return delta


@dataclass
class ActivitySampler:
    """``pg_stat_activity`` of the benchmark database every ``interval`` seconds: connections,
    active sessions, and sessions waiting for a lock."""

    database: Database
    interval: float = 0.5
    _samples: list[tuple[int, int, int]] = field(default_factory=list)
    _task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            with contextlib.suppress(Exception):  # a sample lost to an outage is just skipped
                async with self.database.session() as session:
                    row = (await session.execute(_PG_ACTIVITY)).one()
                self._samples.append((row.connections, row.active, row.lock_waits))
            await asyncio.sleep(self.interval)

    async def stop(self) -> dict[str, Any]:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if not self._samples:
            return {"samples": 0}
        connections, active, waits = zip(*self._samples, strict=True)
        return {
            "samples": len(self._samples),
            "connections_max": max(connections),
            "active_mean": round(statistics.fmean(active), 2),
            "active_max": max(active),
            "lock_waits_max": max(waits),
            "samples_with_lock_waits": sum(1 for value in waits if value),
        }


# --- Logs and output ------------------------------------------------------------------------------


def handled_lines(processes: Iterable[ManagedProcess]) -> list[dict[str, Any]]:
    """Every ``task.handled`` log line of the given processes."""
    return [line for process in processes for line in process.events("task.handled")]


def write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )


def emit(record: Mapping[str, Any]) -> None:
    """Print one result line (the benchmark's own output)."""
    print(json.dumps(record, default=str), flush=True)  # noqa: T201 - the benchmark's output
