"""Container health check of one long-running process (Phase 11, ADR-0025).

``python -m app.observability.alive <role>`` exits 0 while Redis holds a live heartbeat that a
``<role>`` process on this host wrote, and 1 otherwise:

* there is none: the process has not started yet, or it stopped;
* it expired: the process froze, or could not reach Redis, for three heartbeat intervals;
* Redis cannot be read.

A process writes its heartbeat from its own event loop (``app.observability.heartbeat``), so a
blocked loop shows as well.

"This host" is the container's hostname. Every process name contains it
(``<role>-<host>-<pid>-<random>``, ``app.core.process``), so each replica of a service checks its
own process, never a sibling's. Docker only reports the result (``docker compose ps``). No
SiteScope decision reads it, as for every heartbeat (ADR-0021).
"""

import argparse
import asyncio
import socket
import sys
from collections.abc import Iterable, Sequence
from typing import Final

from app.core.config import Settings, get_settings
from app.core.process import error_type
from app.core.redis import create_redis_client
from app.observability.heartbeat import Heartbeat, read_heartbeats

# The roles of the long-running processes, as their heartbeats name them.
ROLES: Final = ("api", "scheduler", "crawler", "index_dispatcher", "indexer")

_HEX: Final = frozenset("0123456789abcdef")


def own_heartbeat(heartbeats: Iterable[Heartbeat], *, role: str, host: str) -> Heartbeat | None:
    """The live heartbeat of a ``role`` process on ``host`` (the most recent), if any.

    The name must be exactly ``<role>-<host>-<pid>-<8 hex digits>``, so that a host whose name
    starts with this one (``abc`` and ``abc-def``) is never mistaken for it. Process names spell
    a role with hyphens (``index-dispatcher``) where the heartbeat's role has underscores.
    """
    prefix = f"{role.replace('_', '-')}-{host}-"
    mine = [
        heartbeat
        for heartbeat in heartbeats
        if heartbeat.role == role
        and heartbeat.name.startswith(prefix)
        and _is_pid_and_suffix(heartbeat.name.removeprefix(prefix))
    ]
    return min(mine, key=lambda heartbeat: heartbeat.age_seconds, default=None)


def _is_pid_and_suffix(rest: str) -> bool:
    pid, _, suffix = rest.partition("-")
    return pid.isdecimal() and len(suffix) == 8 and set(suffix) <= _HEX


async def find_own_heartbeat(settings: Settings, *, role: str, host: str) -> Heartbeat | None:
    """Read the live heartbeats and return this host's ``role`` process's, if any."""
    redis = create_redis_client(settings, client_name=f"sitescope-alive-{role}")
    try:
        heartbeats = await read_heartbeats(redis, key_prefix=settings.redis_key_prefix)
    finally:
        await redis.aclose()
    return own_heartbeat(heartbeats, role=role, host=host)


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    host: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.observability.alive",
        description=(
            "Exit 0 while a live heartbeat of ROLE written from this host is in Redis, 1 "
            "otherwise (a container health check; read-only)."
        ),
    )
    parser.add_argument("role", choices=ROLES, help="the process role to look for")
    args = parser.parse_args(argv)
    role: str = args.role
    try:
        heartbeat = asyncio.run(
            find_own_heartbeat(
                settings or get_settings(), role=role, host=host or socket.gethostname()
            )
        )
    except Exception as exc:  # settings, Redis: any failure to read is "not known alive"
        sys.stderr.write(f"unhealthy: cannot read the heartbeats ({error_type(exc)})\n")
        return 1
    if heartbeat is None:
        sys.stderr.write(f"unhealthy: no live {role} heartbeat from this host\n")
        return 1
    age = f"{heartbeat.age_seconds:.1f}"
    sys.stdout.write(f"healthy: {heartbeat.name}, last heartbeat {age} s ago\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
