"""The asynchronous Redis client against the real Redis service (logical database 15)."""

import uuid

import pytest
from pydantic import SecretStr
from redis import exceptions as redis_errors
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.redis import create_redis_client
from tests.integration.helpers import unused_port

pytestmark = pytest.mark.anyio


async def test_ping_and_round_trip(redis_client: Redis) -> None:
    key = f"sitescope:test:{uuid.uuid4()}"

    assert await redis_client.ping() is True
    await redis_client.set(key, "value", ex=30)
    assert await redis_client.get(key) == "value"
    assert await redis_client.delete(key) == 1


async def test_client_identifies_itself_to_the_server(redis_client: Redis) -> None:
    await redis_client.ping()

    clients = await redis_client.client_list()

    assert any(client.get("name") == "sitescope-tests" for client in clients)


async def test_append_only_persistence_is_enabled(redis_client: Redis) -> None:
    config = await redis_client.config_get("append*")

    assert config["appendonly"] == "yes"
    assert config["appendfsync"] == "everysec"


async def test_wrong_password_raises_an_authentication_error(
    service_settings: Settings, services_available: None
) -> None:
    settings = service_settings.model_copy(update={"redis_password": SecretStr("wrong-password")})
    client = create_redis_client(settings, client_name="sitescope-tests")
    try:
        with pytest.raises(redis_errors.AuthenticationError):
            await client.ping()
    finally:
        await client.aclose()


async def test_unreachable_server_raises_a_connection_error(service_settings: Settings) -> None:
    settings = service_settings.model_copy(update={"redis_port": unused_port()})
    client = create_redis_client(settings, client_name="sitescope-tests")
    try:
        with pytest.raises(redis_errors.ConnectionError):
            await client.ping()
    finally:
        await client.aclose()
