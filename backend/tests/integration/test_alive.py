"""The container health check against the real Redis (Phase 11): a process's own heartbeat
makes it healthy; another role's, an expired or a retired one do not."""

import asyncio
import socket

import pytest
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.process import process_name
from app.observability.alive import find_own_heartbeat
from app.observability.heartbeat import HeartbeatPublisher

pytestmark = pytest.mark.anyio


def publisher(redis: Redis, prefix: str, role: str, interval: float = 5.0) -> HeartbeatPublisher:
    return HeartbeatPublisher(
        redis,
        key_prefix=prefix,
        name=process_name(role.replace("_", "-")),
        role=role,
        interval=interval,
        registry=None,
    )


async def test_a_live_heartbeat_of_this_host_is_found_until_it_is_retired(
    service_settings: Settings, redis_client: Redis, redis_prefix: str
) -> None:
    settings = service_settings.model_copy(update={"redis_key_prefix": redis_prefix})
    host = socket.gethostname()
    dispatcher = publisher(redis_client, redis_prefix, "index_dispatcher")
    await dispatcher.beat()

    found = await find_own_heartbeat(settings, role="index_dispatcher", host=host)
    assert found is not None
    assert found.name == dispatcher.name
    assert await find_own_heartbeat(settings, role="crawler", host=host) is None
    assert await find_own_heartbeat(settings, role="index_dispatcher", host="elsewhere") is None

    await dispatcher.retire()
    assert await find_own_heartbeat(settings, role="index_dispatcher", host=host) is None


async def test_an_expired_heartbeat_is_not_alive(
    service_settings: Settings, redis_client: Redis, redis_prefix: str
) -> None:
    settings = service_settings.model_copy(update={"redis_key_prefix": redis_prefix})
    await publisher(redis_client, redis_prefix, "crawler", interval=0.05).beat()
    await asyncio.sleep(0.3)  # three intervals of 0.05 s: the heartbeat expired

    assert await find_own_heartbeat(settings, role="crawler", host=socket.gethostname()) is None
