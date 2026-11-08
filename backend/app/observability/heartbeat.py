"""Process heartbeats in Redis (Phase 10, ADR-0021).

Every long-running process (the API, the scheduler, crawler workers, the index dispatcher,
indexers) writes a heartbeat every ``SITESCOPE_HEARTBEAT_INTERVAL_SECONDS``: its role, version and
uptime, a small state of its own (for a worker: tasks in flight, messages handled, the time since
its last finished task, its error backoff) and a snapshot of its metrics
(``app.observability.metrics``). A heartbeat expires three intervals after it was written, so a
process that stops writing (it crashed, froze, or cannot reach Redis) leaves the view within three
intervals. A process that stops gracefully removes its heartbeat at once.

Keys (``<prefix>`` is ``SITESCOPE_REDIS_KEY_PREFIX``):

* ``<prefix>:process:<name>``: a hash of ``payload`` (JSON) and ``seen_ms`` (Redis ``TIME``),
  with the time to live;
* ``<prefix>:processes``: a sorted set of process names scored by their last ``seen_ms``, so
  that readers find heartbeats without ``SCAN``. Writers remove entries older than an hour.

Heartbeats are observability only (ADR-0021). No claim, lease, acknowledgment, dispatch or other
decision reads them, and losing them (Redis restarted empty) loses only the view, until each
process's next heartbeat. Time is Redis ``TIME``, never a process's clock.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from redis.asyncio import Redis

from app import __version__
from app.core.process import error_type
from app.observability.metrics import REGISTRY, Registry

logger = logging.getLogger(__name__)

TTL_INTERVALS: Final = 3  # a heartbeat lives this many intervals
INDEX_RETENTION_MS: Final = 3_600_000  # names older than this leave the index
MAX_PROCESSES: Final = 500  # heartbeats read at most (the most recent)

# KEYS: the index, the process's hash. ARGV: name, payload, time to live (ms), index retention
# (ms). Returns the Redis time of the heartbeat (ms).
_BEAT: Final = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('HSET', KEYS[2], 'payload', ARGV[2], 'seen_ms', now)
redis.call('PEXPIRE', KEYS[2], ARGV[3])
redis.call('ZADD', KEYS[1], now, ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - tonumber(ARGV[4]))
return now
"""


def index_key(prefix: str) -> str:
    return f"{prefix}:processes"


def process_key(prefix: str, name: str) -> str:
    return f"{prefix}:process:{name}"


def role_of(name: str) -> str:
    """The role part of a process name (``<role>-<host>-<pid>-<random>``)."""
    return name.split("-", 1)[0]


class HeartbeatPublisher:
    """Writes one process's heartbeat every ``interval`` seconds while ``run`` runs."""

    def __init__(
        self,
        redis: Redis,
        *,
        key_prefix: str,
        name: str,
        role: str,
        interval: float,
        state: Callable[[], Mapping[str, object]] | None = None,
        registry: Registry | None = REGISTRY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("the heartbeat interval must be positive")
        self._redis = redis
        self._prefix = key_prefix
        self.name = name
        self.role = role
        self.interval = interval
        self._state = state
        self._registry = registry
        self._clock = clock
        self._started = clock()
        self._script = redis.register_script(_BEAT)

    def payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "version": __version__,
            "interval_seconds": self.interval,
            "uptime_seconds": round(self._clock() - self._started, 3),
            "state": dict(self._state()) if self._state is not None else {},
            "metrics": self._registry.snapshot() if self._registry is not None else [],
        }

    async def beat(self) -> int:
        """Write one heartbeat; returns its Redis time (ms). Raises if Redis fails."""
        now = await self._script(
            keys=[index_key(self._prefix), process_key(self._prefix, self.name)],
            args=[
                self.name,
                json.dumps(self.payload(), separators=(",", ":"), default=str),
                round(self.interval * TTL_INTERVALS * 1000),
                INDEX_RETENTION_MS,
            ],
        )
        return int(now)

    async def run(self) -> None:
        """Heartbeats until cancelled: one at once, then every interval. Never raises (but
        ``CancelledError``): a failed heartbeat is logged once per outage, and retried."""
        failing = False
        while True:
            try:
                await self.beat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not failing:
                    failing = True
                    logger.warning(
                        "Could not write the process heartbeat",
                        extra={"event": "heartbeat.failed", "error_type": error_type(exc)},
                    )
            else:
                if failing:
                    failing = False
                    logger.info(
                        "The process heartbeat is written again",
                        extra={"event": "heartbeat.recovered"},
                    )
            await asyncio.sleep(self.interval)

    async def retire(self) -> None:
        """Remove this process's heartbeat (a graceful stop). Errors are ignored: the heartbeat
        then expires on its own."""
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.delete(process_key(self._prefix, self.name))
                pipe.zrem(index_key(self._prefix), self.name)
                await pipe.execute()
        except Exception as exc:
            logger.debug(
                "Could not remove the process heartbeat",
                extra={"event": "heartbeat.retire_failed", "error_type": error_type(exc)},
            )


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """A live process, as its last heartbeat described it."""

    name: str
    role: str
    age_seconds: float  # since the heartbeat was written, by the Redis clock
    payload: Mapping[str, Any]

    @property
    def state(self) -> Mapping[str, Any]:
        state = self.payload.get("state")
        return state if isinstance(state, Mapping) else {}


async def read_heartbeats(redis: Redis, *, key_prefix: str) -> list[Heartbeat]:
    """The live heartbeats (at most ``MAX_PROCESSES``, the most recent), by role and name.

    An expired or unreadable heartbeat is left out.
    """
    seconds, microseconds = await redis.time()
    now_ms = seconds * 1000 + microseconds // 1000
    names = await redis.zrevrangebyscore(
        index_key(key_prefix), "+inf", now_ms - INDEX_RETENTION_MS, start=0, num=MAX_PROCESSES
    )
    if not names:
        return []
    decoded = [name.decode() if isinstance(name, bytes) else str(name) for name in names]
    async with redis.pipeline(transaction=False) as pipe:
        for name in decoded:
            pipe.hgetall(process_key(key_prefix, name))
        hashes = await pipe.execute()
    heartbeats = []
    for name, fields in zip(decoded, hashes, strict=True):
        if not fields:
            continue  # expired
        values = {
            (key.decode() if isinstance(key, bytes) else key): value
            for key, value in fields.items()
        }
        try:
            payload = json.loads(values["payload"])
            seen_ms = int(values["seen_ms"])
        except (KeyError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        heartbeats.append(
            Heartbeat(
                name=name,
                role=str(payload.get("role") or role_of(name)),
                age_seconds=max(0.0, (now_ms - seen_ms) / 1000),
                payload=payload,
            )
        )
    heartbeats.sort(key=lambda item: (item.role, item.name))
    return heartbeats
