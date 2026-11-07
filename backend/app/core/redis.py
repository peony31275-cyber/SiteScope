"""Process-wide asynchronous Redis client.

One client (and therefore one connection pool) per process, created at startup and closed at
shutdown. The client is safe to share between concurrent tasks; each command borrows a pooled
connection. Connections are opened lazily, so creating the client never fails because Redis is
down; the readiness probe reports that instead.

Automatic retries are disabled. redis-py retries ConnectionError by default, with exponential
backoff, and AuthenticationError is a subclass of it: a wrong password became a multi-second
hang, and outages were hidden from the readiness probe (observed in Phase 2B). Callers apply
their own bounded retry policy to transient failures (docs/architecture.md, section 10.5).

Replies are decoded leniently: bytes that are not UTF-8 become U+FFFD instead of failing the
whole reply. SiteScope writes only UTF-8, so only a foreign or damaged value can contain such
bytes, and with strict decoding a single one of them in a stream entry failed every
``XREADGROUP`` and ``XAUTOCLAIM`` batch it was in: the valid tasks of the batch stayed pending
forever (found by the Phase 5 review). Now the entry decodes, fails the message contract, and
is dead-lettered.
"""

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.core.config import Settings


def create_redis_client(settings: Settings, *, client_name: str) -> Redis:
    """Build a Redis client from settings. ``client_name`` shows up in ``CLIENT LIST``."""
    password = settings.redis_password.get_secret_value() if settings.redis_password else None
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=password,
        client_name=client_name,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        # Ping connections that sat idle in the pool before reusing them.
        health_check_interval=30,
        retry=Retry(NoBackoff(), retries=0),
        decode_responses=True,
        encoding_errors="replace",
    )
