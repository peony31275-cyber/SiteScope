"""The crawl task stream: a thin adapter over Redis Streams and one consumer group.

Keys (prefix ``SITESCOPE_REDIS_KEY_PREFIX``):

* ``<prefix>:crawl``: the task stream, read by the consumer group ``crawlers``;
* ``<prefix>:crawl:dlq``: dead letters, i.e. malformed entries that can never be processed,
  kept (capped) for inspection.

Delivery semantics are Redis Streams' own: a message read with ``XREADGROUP`` stays in the
group's pending entries list (PEL) until it is acknowledged (``XACK``). A message whose consumer
crashed or stalled stays pending, and ``XAUTOCLAIM`` hands it to another consumer once it has
been idle long enough. Delivery is therefore at least once: a message can be delivered again,
and the same task can be published more than once. Consumers make every effect idempotent
through PostgreSQL (ADR-0016).

The adapter never decides *whether* to acknowledge; workers do (``app.workers.crawler``).

**Trimming.** ``trim`` removes entries older than the retention time, but never an entry that a
group still holds pending, and never one that has not been delivered yet: the trim point is
capped by the oldest pending entry and by every group's last-delivered ID. The reconciler
(``services.crawl_state.republish_stale``) covers entries lost in any other way, for example a
Redis restart without persistence: it asks ``delivering`` whether a stale task's entry is still
owed to a consumer, and publishes the task again only if it is not.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.queue.messages import CrawlTask, MalformedMessageError

logger = logging.getLogger(__name__)

GROUP: Final = "crawlers"
DEAD_LETTER_MAX_LENGTH: Final = 10_000  # approximate cap of the dead-letter stream

# KEYS[1] the stream, ARGV[1] the group, ARGV[2] the idle time (ms). Deletes the consumers with
# nothing pending that have been idle longer, and returns their names. (XINFO replies are flat
# field/value arrays inside a script.)
_REMOVE_IDLE_CONSUMERS: Final = """
local removed = {}
for _, consumer in ipairs(redis.call('XINFO', 'CONSUMERS', KEYS[1], ARGV[1])) do
    local fields = {}
    for i = 1, #consumer, 2 do
        fields[consumer[i]] = consumer[i + 1]
    end
    if tonumber(fields['pending']) == 0 and tonumber(fields['idle']) > tonumber(ARGV[2]) then
        redis.call('XGROUP', 'DELCONSUMER', KEYS[1], ARGV[1], fields['name'])
        table.insert(removed, fields['name'])
    end
end
return removed
"""


@dataclass(frozen=True, slots=True)
class Delivery:
    """One stream entry handed to this consumer, decoded if it is a valid crawl task."""

    entry_id: str
    fields: Mapping[str, str]
    task: CrawlTask | None
    error: str | None = None  # why ``task`` is None

    @classmethod
    def of(cls, entry_id: str, fields: Mapping[str, str] | None) -> "Delivery":
        fields = dict(fields or {})
        try:
            return cls(entry_id, fields, CrawlTask.decode(fields))
        except MalformedMessageError as exc:
            return cls(entry_id, fields, None, str(exc))


@dataclass(frozen=True, slots=True)
class PendingSummary:
    count: int
    oldest: str | None  # the smallest pending entry ID
    per_consumer: dict[str, int]


@dataclass(frozen=True, slots=True)
class Reclaimed:
    """One ``XAUTOCLAIM`` call: deliveries now owned by the caller, and where to continue."""

    next_start: str  # "0-0" once the whole pending list has been scanned
    deliveries: list[Delivery]
    deleted: list[str]  # pending IDs whose entries no longer exist (trimmed or lost)


def parse_entry_id(entry_id: str) -> tuple[int, int]:
    milliseconds, _, sequence = entry_id.partition("-")
    return int(milliseconds), int(sequence or 0)


def safe_trim_point(
    *, older_than_ms: int, oldest_pending: str | None, last_delivered: Sequence[str]
) -> str:
    """The MINID for XTRIM (which keeps entries at or after it): entries before it are old,
    delivered, and acknowledged.

    It is the smallest of the time threshold, the oldest pending entry (still owed to a
    consumer), and the ID just after each group's last-delivered ID (later entries are
    undelivered; the last-delivered entry itself is either acknowledged or pending).
    """
    candidates = [(older_than_ms, 0)]
    if oldest_pending is not None:
        candidates.append(parse_entry_id(oldest_pending))
    for entry in last_delivered:
        milliseconds, sequence = parse_entry_id(entry)
        candidates.append((milliseconds, sequence + 1))
    milliseconds, sequence = min(candidates)
    return f"{milliseconds}-{sequence}"


class CrawlStream:
    """The crawl task stream and its consumer group. Safe for concurrent use by one process."""

    def __init__(self, redis: Redis, *, key_prefix: str, group: str = GROUP) -> None:
        self._redis = redis
        self.key = f"{key_prefix}:crawl"
        self.dead_letter_key = f"{key_prefix}:crawl:dlq"
        self.group = group
        self._remove_idle = redis.register_script(_REMOVE_IDLE_CONSUMERS)

    async def ensure_group(self) -> None:
        """Create the stream and its group if needed (idempotent; every process calls it)."""
        try:
            await self._redis.xgroup_create(self.key, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def publish(self, tasks: Sequence[CrawlTask]) -> list[str]:
        """``XADD`` each task, in one round trip; returns the entry IDs in order."""
        if not tasks:
            return []
        async with self._redis.pipeline(transaction=False) as pipe:
            for task in tasks:
                pipe.xadd(self.key, task.encode())  # type: ignore[arg-type]  # str fields
            ids: list[str] = await pipe.execute()
        return ids

    async def read(self, consumer: str, *, count: int, block_ms: int) -> list[Delivery]:
        """New messages for ``consumer`` (``XREADGROUP ... >``), waiting up to ``block_ms``.

        Recreates the group if it vanished (for example after Redis lost its data).
        """
        try:
            # RESP2 reply: [[stream, [(entry_id, fields), ...]]], or [] after the block time.
            reply = cast(
                list[Any],
                await self._redis.xreadgroup(
                    self.group, consumer, {self.key: ">"}, count=count, block=block_ms
                ),
            )
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            logger.warning(
                "The consumer group was missing and is created again",
                extra={"event": "stream.group_recreated", "stream": self.key},
            )
            await self.ensure_group()
            return []
        return [
            Delivery.of(entry_id, fields)
            for _, entries in reply or []
            for entry_id, fields in entries
        ]

    async def ack(self, entry_id: str) -> bool:
        """``XACK``: True if the entry was pending (False: acknowledged before, or unknown)."""
        return bool(await self._redis.xack(self.key, self.group, entry_id))

    async def length(self) -> int:
        return int(await self._redis.xlen(self.key))

    async def reclaim(
        self, consumer: str, *, min_idle_ms: int, start: str, count: int
    ) -> Reclaimed:
        """Take over messages idle for at least ``min_idle_ms`` (``XAUTOCLAIM``)."""
        try:
            reply: list[Any] = await self._redis.xautoclaim(
                self.key, self.group, consumer, min_idle_ms, start_id=start, count=count
            )
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            await self.ensure_group()
            return Reclaimed("0-0", [], [])
        next_start, entries = reply[0], reply[1]
        deleted = list(reply[2]) if len(reply) > 2 else []
        return Reclaimed(
            next_start,
            [Delivery.of(entry_id, fields) for entry_id, fields in entries],
            deleted,
        )

    async def dead_letter(self, delivery: Delivery, reason: str) -> None:
        """Copy an unprocessable entry to the dead-letter stream, then acknowledge it."""
        fields = {f"original.{name}"[:64]: value[:512] for name, value in delivery.fields.items()}
        fields.update({"original_id": delivery.entry_id, "reason": reason[:200]})
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xadd(
                self.dead_letter_key,
                fields,  # type: ignore[arg-type]  # str fields
                maxlen=DEAD_LETTER_MAX_LENGTH,
                approximate=True,
            )
            pipe.xack(self.key, self.group, delivery.entry_id)
            await pipe.execute()

    async def delivering(self, entry_ids: Sequence[str | None]) -> list[bool]:
        """For each entry ID, whether the group still owes that entry to a consumer.

        True if the entry exists and is either not delivered yet (after the group's
        last-delivered ID) or pending (delivered, not acknowledged): then a worker reads it, or
        takes it over with ``XAUTOCLAIM``, without help. False if it was acknowledged, trimmed
        or lost (for example with Redis data), or for ``None`` (never published). The
        reconciler publishes again only the tasks whose entry is no longer delivering.
        """
        result = [False] * len(entry_ids)
        known = [(index, entry) for index, entry in enumerate(entry_ids) if entry is not None]
        if not known:
            return result
        # One round trip (the reconciler calls this inside its transaction): the group's
        # last-delivered ID, then each entry's presence and pending state.
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.xinfo_groups(self.key)
            for _, entry in known:
                pipe.xrange(self.key, min=entry, max=entry, count=1)
                pipe.xpending_range(self.key, self.group, min=entry, max=entry, count=1)
            replies = await pipe.execute(raise_on_error=False)
        groups = replies[0]
        if isinstance(groups, Exception):  # the stream does not exist
            return result
        group = next((item for item in groups if item["name"] == self.group), None)
        if group is None:
            return result
        last_delivered = parse_entry_id(group["last-delivered-id"])
        for position, (index, entry) in enumerate(known):
            exists, pending = replies[1 + 2 * position], replies[2 + 2 * position]
            if isinstance(exists, Exception) or isinstance(pending, Exception):
                continue
            result[index] = bool(exists) and (
                parse_entry_id(entry) > last_delivered or bool(pending)
            )
        return result

    async def pending(self) -> PendingSummary:
        try:
            summary = await self._redis.xpending(self.key, self.group)
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            return PendingSummary(0, None, {})
        return PendingSummary(
            count=int(summary["pending"]),
            oldest=summary["min"],
            per_consumer={item["name"]: int(item["pending"]) for item in summary["consumers"]},
        )

    async def trim(self, *, retention_ms: int, exact: bool = False) -> int:
        """Remove old, delivered and acknowledged entries (see the module docstring).

        By default the trim is approximate (``MINID ~``: whole internal nodes only, which is
        cheaper); it never removes an entry at or after the trim point either way.
        """
        if not await self._redis.exists(self.key):
            return 0
        seconds, microseconds = await self._redis.time()
        now_ms = seconds * 1000 + microseconds // 1000
        groups = await self._redis.xinfo_groups(self.key)
        pending = await self.pending()
        point = safe_trim_point(
            older_than_ms=now_ms - retention_ms,
            oldest_pending=pending.oldest,
            last_delivered=[group["last-delivered-id"] for group in groups],
        )
        removed = int(await self._redis.xtrim(self.key, minid=point, approximate=not exact))
        await self._redis.xtrim(
            self.dead_letter_key, maxlen=DEAD_LETTER_MAX_LENGTH, approximate=True
        )
        return removed

    async def remove_idle_consumers(self, *, max_idle_ms: int) -> list[str]:
        """Delete consumers (gone worker processes) with nothing pending, idle for long.

        ``XGROUP DELCONSUMER`` drops a consumer's pending entries too, so only consumers with
        none may go. The check and the deletion run in one Lua script, atomically: done in two
        round trips, a consumer resuming in between could read entries and lose them (found by
        the Phase 5 review).
        """
        try:
            removed = await self._remove_idle(keys=[self.key], args=[self.group, max_idle_ms])
        except ResponseError as exc:
            if "NOGROUP" not in str(exc) and "no such key" not in str(exc):
                raise
            return []
        return [str(name) for name in removed]

    async def describe(self) -> dict[str, Any]:
        """Stream, group and consumer statistics, for diagnostics (approximate)."""
        if not await self._redis.exists(self.key):
            return {"stream": self.key, "exists": False}
        groups = await self._redis.xinfo_groups(self.key)
        mine = next((group for group in groups if group["name"] == self.group), None)
        consumers = (
            await self._redis.xinfo_consumers(self.key, self.group) if mine is not None else []
        )
        return {
            "stream": self.key,
            "exists": True,
            "length": await self._redis.xlen(self.key),
            "dead_letters": await self._redis.xlen(self.dead_letter_key),
            "group": mine,
            "consumers": consumers,
        }
