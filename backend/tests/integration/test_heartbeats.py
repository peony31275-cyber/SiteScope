"""Process heartbeats in the real Redis (Phase 10, ADR-0021): writing, reading, expiry, removal,
and the API process's own heartbeat."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from app.core.config import Settings
from app.main import create_app
from app.observability.heartbeat import (
    INDEX_RETENTION_MS,
    HeartbeatPublisher,
    index_key,
    process_key,
    read_heartbeats,
)
from app.observability.metrics import Registry

pytestmark = pytest.mark.anyio


def publisher(redis: Redis, prefix: str, name: str, *, interval: float = 5.0) -> HeartbeatPublisher:
    registry = Registry()
    registry.counter("sitescope_test_total", "Test.").inc(4)
    return HeartbeatPublisher(
        redis,
        key_prefix=prefix,
        name=name,
        role="crawler",
        interval=interval,
        state=lambda: {"in_flight": 2},
        registry=registry,
    )


async def test_a_heartbeat_is_read_back_with_its_state_and_metrics(
    redis_client: Redis, redis_prefix: str
) -> None:
    beat = publisher(redis_client, redis_prefix, "crawler-h-1-aa")

    written = await beat.beat()

    [heartbeat] = await read_heartbeats(redis_client, key_prefix=redis_prefix)
    assert (heartbeat.name, heartbeat.role) == ("crawler-h-1-aa", "crawler")
    assert heartbeat.state == {"in_flight": 2}
    assert heartbeat.payload["metrics"][0]["series"] == [[[], 4.0]]
    assert heartbeat.payload["interval_seconds"] == 5.0
    assert 0 <= heartbeat.age_seconds < 5
    # Stamped with Redis TIME, and kept for three intervals.
    stored = await redis_client.hgetall(process_key(redis_prefix, "crawler-h-1-aa"))
    assert int(stored["seen_ms"]) == written
    assert 13_000 < await redis_client.pttl(process_key(redis_prefix, "crawler-h-1-aa")) <= 15_000
    assert json.loads(stored["payload"])["role"] == "crawler"


async def test_an_expired_heartbeat_is_not_read_even_while_its_name_is_indexed(
    redis_client: Redis, redis_prefix: str
) -> None:
    await publisher(redis_client, redis_prefix, "crawler-h-1-aa", interval=0.05).beat()
    await asyncio.sleep(0.3)  # three intervals of 0.05 s

    assert await redis_client.zscore(index_key(redis_prefix), "crawler-h-1-aa") is not None
    assert await read_heartbeats(redis_client, key_prefix=redis_prefix) == []


async def test_names_older_than_the_retention_leave_the_index(
    redis_client: Redis, redis_prefix: str
) -> None:
    seconds, _ = await redis_client.time()
    stale = seconds * 1000 - INDEX_RETENTION_MS - 60_000
    await redis_client.zadd(index_key(redis_prefix), {"crawler-gone": stale})

    await publisher(redis_client, redis_prefix, "crawler-h-1-aa").beat()

    assert await redis_client.zrange(index_key(redis_prefix), 0, -1) == ["crawler-h-1-aa"]


async def test_retire_removes_the_heartbeat_at_once(redis_client: Redis, redis_prefix: str) -> None:
    beat = publisher(redis_client, redis_prefix, "crawler-h-1-aa")
    await beat.beat()

    await beat.retire()

    assert await read_heartbeats(redis_client, key_prefix=redis_prefix) == []
    assert await redis_client.exists(process_key(redis_prefix, "crawler-h-1-aa")) == 0


async def test_run_keeps_writing_until_it_is_cancelled(
    redis_client: Redis, redis_prefix: str
) -> None:
    beat = publisher(redis_client, redis_prefix, "crawler-h-1-aa", interval=0.05)
    running = asyncio.create_task(beat.run())
    try:
        await asyncio.sleep(0.3)
        first = int(await redis_client.hget(process_key(redis_prefix, beat.name), "seen_ms") or 0)
        await asyncio.sleep(0.2)
        second = int(await redis_client.hget(process_key(redis_prefix, beat.name), "seen_ms") or 0)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert second > first > 0


async def test_the_api_process_publishes_a_heartbeat_only_when_asked(
    test_database: Settings, redis_client: Redis, redis_prefix: str
) -> None:
    """The applications tests build publish none; the server's (``app.main.app``) does. The
    TestClient runs the application in a thread of its own, so this test's loop can watch."""
    settings = test_database.model_copy(update={"redis_key_prefix": redis_prefix})

    async def roles() -> list[str]:
        return [beat.role for beat in await read_heartbeats(redis_client, key_prefix=redis_prefix)]

    with TestClient(create_app(settings)):
        await asyncio.sleep(0.3)
        assert await roles() == []
    with TestClient(create_app(settings, publish_heartbeat=True)):
        for _ in range(100):  # the heartbeat task's first write, within 5 s
            if await roles() == ["api"]:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the API process wrote no heartbeat")
    assert await roles() == []  # the application's shutdown removed it
