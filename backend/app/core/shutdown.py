"""Graceful shutdown for long-running processes (scheduler, crawler workers).

The first SIGINT or SIGTERM (on Windows: Ctrl+C, or CTRL_BREAK_EVENT, which Python reports as
SIGBREAK) sets ``stop``: the process stops taking new work and lets running work finish within
its grace period. A second signal sets ``force``: the process cancels what is left and exits.
Forced termination (``kill -9``, ``TerminateProcess``) is always safe too: PostgreSQL holds the
state, unfinished tasks stay pending in Redis, and expired leases are taken over (ADR-0016).

On Windows the event loop cannot install signal handlers, so plain ``signal.signal`` handlers
hand the event to the loop with ``call_soon_threadsafe``.
"""

import asyncio
import logging
import signal
import sys
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class StopSignals:
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    force: asyncio.Event = field(default_factory=asyncio.Event)

    def request(self) -> None:
        if self.stop.is_set():
            self.force.set()
        self.stop.set()


def install_stop_handlers(signals: StopSignals, *, process: str) -> None:
    """Route SIGINT, SIGTERM (and SIGBREAK on Windows) to ``signals``; call inside the loop."""
    loop = asyncio.get_running_loop()

    def received(signal_name: str) -> None:
        logger.info(
            "Stop requested",
            extra={"event": f"{process}.stop_requested", "signal": signal_name},
        )
        signals.request()

    if sys.platform == "win32":
        names = ["SIGINT", "SIGBREAK", "SIGTERM"]
        for name in names:
            number = getattr(signal, name)

            def handler(signum: int, frame: object, name: str = name) -> None:
                loop.call_soon_threadsafe(received, name)

            signal.signal(number, handler)
    else:  # pragma: no cover - exercised on POSIX systems only
        for name in ("SIGINT", "SIGTERM"):
            loop.add_signal_handler(getattr(signal, name), received, name)
