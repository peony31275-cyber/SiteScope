"""Helpers of the long-running processes: backoff, names, error classes, stop signals."""

import argparse
import asyncio
import signal
import sys

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.backoff import Backoff
from app.core.config import Settings
from app.core.process import error_type, int_in_range, is_transient, process_name
from app.core.shutdown import StopSignals, install_stop_handlers
from app.scheduler.scheduler import PassReport, SchedulerConfig, idle_transaction_timeout_ms
from app.workers.runtime import (
    idle_transaction_timeout_ms as worker_idle_transaction_timeout_ms,
)

# --- Backoff -------------------------------------------------------------------------------------


def test_backoff_grows_exponentially_up_to_the_cap() -> None:
    backoff = Backoff(base=0.5, cap=4.0)
    ceilings = []
    for _ in range(6):
        delay = backoff.next_delay()
        ceilings.append(backoff.ceiling())
        assert 0 <= delay <= backoff.ceiling()

    assert ceilings == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0]


def test_backoff_reset_reports_the_end_of_a_failure_series() -> None:
    backoff = Backoff()

    assert backoff.reset() is False
    backoff.next_delay()
    assert backoff.reset() is True
    assert backoff.failures == 0
    assert backoff.ceiling() == 0


def test_backoff_uses_full_jitter() -> None:
    """Delays spread over the whole range, so restarted processes do not retry in lockstep."""
    delays = []
    for _ in range(200):
        backoff = Backoff(base=1.0, cap=1.0)
        delays.append(backoff.next_delay())

    assert min(delays) < 0.25
    assert max(delays) > 0.75


# --- Process names and errors --------------------------------------------------------------------


def test_process_names_are_unique_per_start() -> None:
    names = {process_name("crawler") for _ in range(100)}

    assert len(names) == 100
    assert all(name.startswith("crawler-") for name in names)


def test_transient_errors() -> None:
    assert is_transient(RedisConnectionError("refused"))
    assert is_transient(ConnectionRefusedError())
    assert is_transient(TimeoutError())
    assert is_transient(OperationalError("SELECT 1", {}, OSError("refused")))
    assert not is_transient(IntegrityError("INSERT", {}, ValueError("duplicate")))
    assert not is_transient(ValueError("a bug"))


def test_error_type_names_the_driver_error_but_never_the_message() -> None:
    error = OperationalError("SELECT secret_column", {"url": "https://x/?token=1"}, OSError("x"))

    assert error_type(error) == "OperationalError(OSError)"
    assert error_type(ValueError("https://example.com/?token=secret")) == "ValueError"


def test_int_in_range() -> None:
    parse = int_in_range(1, 64)

    assert parse("64") == 64
    for value in ("0", "65", "x", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            parse(value)


# --- Stop signals --------------------------------------------------------------------------------


def test_the_first_request_stops_and_the_second_forces() -> None:
    signals = StopSignals()

    signals.request()
    assert signals.stop.is_set()
    assert not signals.force.is_set()

    signals.request()
    assert signals.force.is_set()


@pytest.mark.anyio
async def test_sigint_requests_a_stop() -> None:
    """The handler runs in the main thread and hands the stop to the event loop."""
    names = ["SIGINT", "SIGTERM"] + (["SIGBREAK"] if sys.platform == "win32" else [])
    previous = {name: signal.getsignal(getattr(signal, name)) for name in names}
    signals = StopSignals()
    try:
        install_stop_handlers(signals, process="test")
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(signals.stop.wait(), timeout=5)
    finally:
        loop = asyncio.get_running_loop()
        for name, handler in previous.items():
            if sys.platform != "win32":
                loop.remove_signal_handler(getattr(signal, name))
            signal.signal(getattr(signal, name), handler)

    assert signals.stop.is_set()
    assert not signals.force.is_set()


# --- Scheduler configuration ---------------------------------------------------------------------


def test_scheduler_config_from_settings() -> None:
    settings = Settings(_env_file=None)

    config = SchedulerConfig.from_settings(settings)

    assert config.max_running_jobs == 10
    assert config.reconcile_after == 300  # 600 until Phase 6
    assert config.reconcile_interval == 30  # reconcile_after / 4, at most 30 s
    assert config.maintenance_interval == 60  # retention / 4, at most 60 s
    assert config.name.startswith("scheduler-")
    short = SchedulerConfig(reconcile_after=8, retention_seconds=60)
    assert (short.reconcile_interval, short.maintenance_interval) == (2, 15)


def test_pass_report_busy() -> None:
    assert not PassReport(outstanding=5).busy
    assert PassReport(dispatched=1).busy


def test_a_stalled_scheduler_session_ends_after_two_redis_timeouts_and_a_margin() -> None:
    settings = Settings(_env_file=None)

    assert idle_transaction_timeout_ms(settings) == 15_000  # 2 * 5 s + 5 s


def test_a_frozen_crawl_process_session_ends_after_the_lease_margin() -> None:
    settings = Settings(_env_file=None)

    assert worker_idle_transaction_timeout_ms(settings) == 10_000
    short = settings.model_copy(update={"crawl_lease_margin_seconds": 0.2})
    assert worker_idle_transaction_timeout_ms(short) == 1_000  # never below 1 s
