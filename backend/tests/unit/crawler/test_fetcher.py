"""Safe fetcher pieces that need no sockets: the pinned backend, failure classification, setup.

Behavior over real connections is tested in tests/integration/test_fetcher.py.
"""

import asyncio
import ssl
from collections.abc import Iterable
from typing import Any

import httpcore2
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.crawler.errors import RETRYABLE_HTTP_STATUSES, FailureKind, FetchError
from app.crawler.fetcher import (
    USER_AGENT,
    FetchConfig,
    PinnedNetworkBackend,
    UnvalidatedDestinationError,
    build_fetcher,
    create_tls_context,
)
from tests.fetch_support import FailingBackend, ScriptedResolver, make_fetcher

pytestmark = pytest.mark.anyio


class FakeStream(httpcore2.AsyncNetworkStream):
    """Test double: a network stream that is never read."""


class ScriptedBackend(httpcore2.AsyncNetworkBackend):
    """Test double: refuses the listed addresses, "connects" to the others, records attempts."""

    def __init__(self, refused: set[str]) -> None:
        self.refused = refused
        self.attempts: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore2's interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        self.attempts.append(host)
        if host in self.refused:
            raise httpcore2.ConnectError(f"refused by {host}")
        return FakeStream()


def pinned(inner: httpcore2.AsyncNetworkBackend, *addresses: str) -> PinnedNetworkBackend:
    import ipaddress

    return PinnedNetworkBackend(
        host="fixture.test",
        port=443,
        addresses=[ipaddress.ip_address(address) for address in addresses],
        inner=inner,
    )


async def test_the_pinned_backend_connects_only_to_validated_addresses_in_order() -> None:
    inner = ScriptedBackend(refused={"192.0.2.1"})
    backend = pinned(inner, "192.0.2.1", "192.0.2.2", "192.0.2.3")

    stream = await backend.connect_tcp("fixture.test", 443)

    assert isinstance(stream, FakeStream)
    assert inner.attempts == ["192.0.2.1", "192.0.2.2"]
    assert str(backend.connected_address) == "192.0.2.2"


async def test_the_pinned_backend_reports_the_last_failure() -> None:
    inner = ScriptedBackend(refused={"192.0.2.1", "192.0.2.2"})
    backend = pinned(inner, "192.0.2.1", "192.0.2.2")

    with pytest.raises(httpcore2.ConnectError, match=r"refused by 192\.0\.2\.2"):
        await backend.connect_tcp("fixture.test", 443)
    assert backend.connected_address is None


@pytest.mark.parametrize(("host", "port"), [("other.test", 443), ("fixture.test", 80)])
async def test_the_pinned_backend_refuses_any_other_destination(host: str, port: int) -> None:
    inner = ScriptedBackend(refused=set())
    backend = pinned(inner, "192.0.2.1")

    with pytest.raises(UnvalidatedDestinationError):
        await backend.connect_tcp(host, port)
    with pytest.raises(UnvalidatedDestinationError):
        await backend.connect_unix_socket("/var/run/docker.sock")
    assert inner.attempts == []


def failing_fetcher(error: Exception, **config: Any) -> tuple[Any, FailingBackend]:
    backend = FailingBackend(error)
    resolver = ScriptedResolver({"fixture.test": [["192.0.2.10"]]})
    fetcher = make_fetcher(resolver=resolver, port=443, internal=[], backend=backend, **config)
    return fetcher, backend


async def expect(fetcher: Any, url: str) -> FetchError:
    with pytest.raises(FetchError) as caught:
        await fetcher.fetch(url)
    return caught.value


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (httpcore2.ConnectTimeout("slow"), FailureKind.CONNECT_TIMEOUT),
        (httpcore2.ConnectError("refused"), FailureKind.CONNECTION_ERROR),
    ],
)
async def test_connection_failures_are_classified(error: Exception, kind: FailureKind) -> None:
    # 192.0.2.10 is a documentation address, so the destination policy refuses it: allow the
    # test's single address by classifying with a policy that treats it as global.
    fetcher, backend = failing_fetcher(error)
    fetcher._destinations = _AllowAll()

    failure = await expect(fetcher, "https://fixture.test/")

    assert (failure.kind, failure.retryable) == (kind, True)
    assert backend.targets == [("192.0.2.10", 443)]


async def test_tls_failures_are_recognized_through_the_exception_chain() -> None:
    """The TLS error is chained as AnyIO raises it (raise ConnectError(...) from exc). The
    connection pool re-raises with ``from None``, which clears __cause__ but keeps __context__,
    so the classifier must follow __context__ too."""
    verification = ssl.SSLCertVerificationError(1, "certificate verify failed")
    verification.verify_message = "Hostname mismatch"
    try:
        try:
            raise verification
        except ssl.SSLError as exc:
            raise httpcore2.ConnectError(str(exc)) from exc
    except httpcore2.ConnectError as error:
        wrapped = error
    fetcher, _ = failing_fetcher(wrapped)
    fetcher._destinations = _AllowAll()

    failure = await expect(fetcher, "https://fixture.test/")

    assert (failure.kind, failure.retryable) == (FailureKind.TLS_ERROR, False)
    assert "Hostname mismatch" in failure.message


async def test_a_slow_dns_lookup_is_a_retryable_dns_error() -> None:
    class SlowResolver(ScriptedResolver):
        async def resolve(self, host: str) -> Any:
            await asyncio.sleep(5)
            return await super().resolve(host)

    fetcher = make_fetcher(
        resolver=SlowResolver({"fixture.test": [["127.0.0.1"]]}), port=443, connect_timeout=0.05
    )

    failure = await expect(fetcher, "https://fixture.test/")

    assert (failure.kind, failure.retryable) == (FailureKind.DNS_ERROR, True)


async def test_invalid_urls_and_arguments() -> None:
    fetcher = make_fetcher(resolver=ScriptedResolver({}), port=443)

    failure = await expect(fetcher, "ftp://fixture.test/")

    assert (failure.kind, failure.retryable) == (FailureKind.INVALID_URL, False)
    with pytest.raises(ValueError, match="must not be negative"):
        await fetcher.fetch("https://fixture.test/", max_redirects=-1)


def test_retryable_failures() -> None:
    retryable = {
        FailureKind.DNS_ERROR,
        FailureKind.CONNECTION_ERROR,
        FailureKind.CONNECT_TIMEOUT,
        FailureKind.READ_TIMEOUT,
        FailureKind.DEADLINE_EXCEEDED,
        FailureKind.TRUNCATED,
        FailureKind.RATE_LIMITED,  # Phase 4: no politeness slot in time; nothing was sent
    }
    for kind in FailureKind:
        if kind is not FailureKind.HTTP_ERROR:
            assert FetchError(kind, "m", url="u").retryable is (kind in retryable), kind
    for status in (400, 403, 404, 410, 429, 500, 501, 502, 503, 504, 505):
        error = FetchError(FailureKind.HTTP_ERROR, "m", url="u", http_status=status)
        assert error.retryable is (status in RETRYABLE_HTTP_STATUSES)
    assert {429, 500, 502, 503, 504} == RETRYABLE_HTTP_STATUSES


def test_failure_kinds_fit_the_outcome_column() -> None:
    """Workers store FailureKind values in crawl_urls.outcome (CHECK ^[a-z][a-z0-9_]*$)."""
    import re

    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", kind.value) for kind in FailureKind)


def test_the_tls_context_verifies_certificates_and_host_names() -> None:
    context = create_tls_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2


def test_build_fetcher_uses_the_settings() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        fetch_connect_timeout_seconds=3,
        fetch_read_timeout_seconds=4,
        fetch_deadline_seconds=30,
        fetch_max_body_bytes=4096,
        fetch_max_redirects=2,
    )

    fetcher = build_fetcher(settings)

    assert fetcher._config == FetchConfig(
        connect_timeout=3,
        read_timeout=4,
        deadline=30,
        max_body_bytes=4096,
        max_redirects=2,
        user_agent=USER_AGENT,
    )
    assert fetcher._destinations.allow_loopback


def test_production_fetchers_never_allow_loopback() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        postgres_password=SecretStr("real-postgres-secret"),
        redis_password=SecretStr("real-redis-secret"),
    )

    assert not build_fetcher(settings)._destinations.allow_loopback


class _AllowAll:
    """Test double for the destination policy: allows every address."""

    def check(self, url: object, addresses: object) -> None:
        return None


def test_the_cli_reports_failures_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    """No network: the default settings approve no crawl target."""
    import json

    from app.crawler import cli

    status = cli.main(["https://docs.example.com/"], settings=Settings(_env_file=None))

    report = json.loads(capsys.readouterr().out)
    assert status == 1
    assert (report["ok"], report["failure"], report["retryable"]) == (
        False,
        "target_not_allowed",
        False,
    )
