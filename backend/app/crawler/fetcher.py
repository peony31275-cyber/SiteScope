"""The safe fetcher: HTTP(S) GET requests to approved crawl targets only (ADR-0006, ADR-0013).

Every request hop goes through the same steps, in this order:

1. normalize the URL (``urls.normalize_url``);
2. check it against the approved crawl targets (``scope.CrawlTargetPolicy``);
3. resolve the host once (``resolver``) and check *every* address it resolves to
   (``destinations.DestinationPolicy``);
4. connect only to a validated address: ``PinnedNetworkBackend`` replaces name resolution in the
   HTTP stack, so nothing resolves the host a second time and a changed DNS answer (DNS
   rebinding) cannot redirect the connection;
5. send the request over HTTP/1.1. TLS SNI and certificate verification use the *host name*, and
   the ``Host`` header carries the original authority;
6. read the response within the time and size limits.

The HTTP stack never follows redirects. The fetcher follows them itself, up to
``max_redirects`` hops, and every target goes through steps 1-6 again: a valid first URL never
authorizes an unsafe redirect.

Networking is explicit. The fetcher uses httpcore2, the transport layer of httpx2, directly: it
supports no proxies, reads no environment configuration (``HTTP_PROXY``, ``.netrc``, ...), and
uses a TLS context built in code (``create_tls_context``). Connections are never reused between
requests.
"""

import asyncio
import logging
import re
import ssl
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, NoReturn

import httpcore2
import truststore

from app import __version__
from app.crawler.content import (
    ACCEPT_ENCODING,
    HTML_MEDIA_TYPES,
    BodyReader,
    BodyTooLargeError,
    ContentDecodingError,
    parse_content_coding,
    parse_media_type,
)
from app.crawler.destinations import (
    BlockedDestinationError,
    DestinationPolicy,
    IPAddress,
    classify_address,
)
from app.crawler.errors import FailureKind, FetchError
from app.crawler.resolver import ResolutionError, Resolver, SystemResolver
from app.crawler.results import FetchResult, Redirect
from app.crawler.scope import CrawlTargetPolicy, TargetNotAllowedError
from app.crawler.urls import InvalidUrlError, NormalizedUrl, ip_address_of, join_url, normalize_url

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

USER_AGENT: Final = f"SiteScopeBot/{__version__}"
ACCEPT: Final = "text/html,application/xhtml+xml;q=0.9"
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
# Response headers kept in a FetchResult (lowercase names); each value is cut to MAX_HEADER_VALUE.
KEPT_HEADERS: Final = (
    "content-type",
    "content-length",
    "content-encoding",
    "content-language",
    "last-modified",
    "etag",
    "cache-control",
    "x-robots-tag",
)
MAX_HEADER_VALUE: Final = 1024
# h11's message when the connection closes before the declared body is complete.
_TRUNCATED_BODY: Final = "peer closed connection without sending complete message body"
# httpcore2's message when the connection closes before any response.
_NO_RESPONSE: Final = "Server disconnected without sending a response"
_CONTENT_LENGTH: Final = re.compile(r"[0-9]{1,18}")


@dataclass(frozen=True, slots=True)
class FetchConfig:
    """Time and size limits of the safe fetcher (settings: ``SITESCOPE_FETCH_*``)."""

    connect_timeout: float = 10.0  # DNS resolution, TCP connect, and the TLS handshake
    read_timeout: float = 10.0  # each read or write of the request and response
    deadline: float = 60.0  # the whole fetch, all redirects included
    max_body_bytes: int = 2 * 1024 * 1024  # after removing gzip or deflate
    max_redirects: int = 5
    user_agent: str = USER_AGENT

    @property
    def max_wire_bytes(self) -> int:
        """Limit on received body bytes: coded data is never much larger than its content."""
        return self.max_body_bytes + self.max_body_bytes // 64 + 4096


class UnvalidatedDestinationError(httpcore2.ConnectError):
    """The HTTP stack asked for a connection that the fetcher did not validate."""


class PinnedNetworkBackend(httpcore2.AsyncNetworkBackend):
    """Connects to the validated addresses of one origin, and to nothing else.

    httpcore2 passes the URL's host name to ``connect_tcp``, then uses the same name for TLS SNI
    and certificate verification. This backend never resolves that name. It connects to the
    validated IP addresses, in order, and refuses any other host, port or socket.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        addresses: Sequence[IPAddress],
        inner: httpcore2.AsyncNetworkBackend | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._addresses = tuple(addresses)
        self._inner = inner if inner is not None else httpcore2.AnyIOBackend()
        self.connected_address: IPAddress | None = None

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore2's interface
        local_address: str | None = None,
        # httpcore2's socket-option type is not exported; Any keeps this override compatible.
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        if host != self._host or port != self._port or not self._addresses:
            raise UnvalidatedDestinationError("refusing to connect to an unvalidated destination")
        last = len(self._addresses) - 1
        for index, address in enumerate(self._addresses):
            try:
                stream = await self._inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout):
                if index == last:
                    raise  # every validated address failed: report the last error
                continue
            self.connected_address = address
            return stream
        raise UnvalidatedDestinationError("no validated address")  # pragma: no cover

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore2's interface
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        raise UnvalidatedDestinationError("Unix sockets are never crawl destinations")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def create_tls_context() -> ssl.SSLContext:
    """TLS 1.2 or newer; certificate and host name verified by the operating system's verifier.

    ``truststore`` verifies with the platform's own API (CryptoAPI on Windows, Security.framework
    on macOS, the system store through OpenSSL on Linux), as browsers do. OpenSSL alone,
    through ``ssl.load_default_certs()``, failed on the Windows development machine for valid
    public sites (example.com): Windows installs most root certificates only when its own
    verifier first needs them, so OpenSSL never sees them. The context is built explicitly,
    never with ``ssl.create_default_context()``, which also reads ``SSLKEYLOGFILE``.
    """
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # CERT_REQUIRED, check_hostname
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


@dataclass(frozen=True, slots=True)
class _RedirectResponse:
    status_code: int
    location: NormalizedUrl
    headers: Mapping[str, str]
    remote_address: str


class SafeFetcher:
    """Fetches approved URLs over validated, pinned connections. See the module docstring.

    One instance can serve many concurrent fetches: each fetch creates its own connection.
    """

    def __init__(
        self,
        *,
        config: FetchConfig,
        targets: CrawlTargetPolicy,
        destinations: DestinationPolicy,
        resolver: Resolver | None = None,
        tls_context: ssl.SSLContext | None = None,
        network_backend: httpcore2.AsyncNetworkBackend | None = None,
    ) -> None:
        self._config = config
        self._targets = targets
        self._destinations = destinations
        self._resolver = resolver if resolver is not None else SystemResolver()
        self._tls_context = tls_context if tls_context is not None else create_tls_context()
        # The backend that opens TCP connections to validated addresses (tests record them).
        self._network_backend = network_backend

    async def fetch(self, url: str, *, max_redirects: int | None = None) -> FetchResult:
        """Fetch an HTML document, following up to ``max_redirects`` validated redirects.

        ``max_redirects=0`` returns a redirect response instead of following it. Raises
        ``FetchError`` for every failure; ``FetchError.retryable`` classifies it.
        """
        limit = self._config.max_redirects if max_redirects is None else max_redirects
        if limit < 0:
            raise ValueError("max_redirects must not be negative")
        started = time.monotonic()
        try:
            target = normalize_url(url)
        except InvalidUrlError as exc:
            raise FetchError(FailureKind.INVALID_URL, str(exc), url=url) from None
        requested = target.url
        redirects: list[Redirect] = []
        visited = {target.url}
        try:
            async with asyncio.timeout(self._config.deadline):
                while True:
                    response = await self._request(target, tuple(redirects), requested, started)
                    if isinstance(response, FetchResult):
                        return response
                    location = response.location
                    if limit == 0:
                        return self._unfollowed_redirect(target, response, requested, started)
                    if location.url in visited:
                        raise FetchError(
                            FailureKind.REDIRECT_LOOP,
                            f"redirect loop at {target.host}",
                            url=target.url,
                            http_status=response.status_code,
                            redirects=tuple(redirects),
                        )
                    if len(redirects) >= limit:
                        raise FetchError(
                            FailureKind.TOO_MANY_REDIRECTS,
                            f"more than {limit} redirects",
                            url=target.url,
                            http_status=response.status_code,
                            redirects=tuple(redirects),
                        )
                    redirects.append(Redirect(target.url, response.status_code, location.url))
                    visited.add(location.url)
                    target = location
        except TimeoutError:
            raise FetchError(
                FailureKind.DEADLINE_EXCEEDED,
                f"the fetch did not finish within {self._config.deadline:g} s",
                url=target.url,
                redirects=tuple(redirects),
            ) from None

    async def _request(
        self,
        target: NormalizedUrl,
        redirects: tuple[Redirect, ...],
        requested: str,
        started: float,
    ) -> FetchResult | _RedirectResponse:
        """One request hop: validate, resolve, connect to a validated address, read."""

        def fail(kind: FailureKind, message: str, status: int | None = None) -> NoReturn:
            raise FetchError(kind, message, url=target.url, http_status=status, redirects=redirects)

        try:
            self._targets.check(target)
        except TargetNotAllowedError as exc:
            fail(FailureKind.TARGET_NOT_ALLOWED, str(exc))
        addresses = await self._resolve(target, fail)
        try:
            self._destinations.check(target, addresses)
        except BlockedDestinationError as exc:
            logger.warning(
                "Refused a crawl destination",
                extra={
                    "event": "fetch.destination_blocked",
                    "host": target.host,
                    "address_classes": sorted({classify_address(a).value for a in addresses}),
                },
            )
            fail(FailureKind.BLOCKED_DESTINATION, str(exc))

        connect_host = _connect_host(target)
        backend = PinnedNetworkBackend(
            host=connect_host, port=target.port, addresses=addresses, inner=self._network_backend
        )
        config = self._config
        timeouts = {
            "connect": config.connect_timeout,
            "read": config.read_timeout,
            "write": config.read_timeout,
            "pool": config.connect_timeout,
        }
        request_url = httpcore2.URL(
            scheme=target.scheme.encode("ascii"),
            host=connect_host.encode("ascii"),
            port=target.port,
            target=_request_target(target).encode("ascii"),
        )
        headers = [
            (b"Host", _authority(target).encode("ascii")),
            (b"User-Agent", config.user_agent.encode("ascii")),
            (b"Accept", ACCEPT.encode("ascii")),
            (b"Accept-Encoding", ACCEPT_ENCODING.encode("ascii")),
            (b"Connection", b"close"),
        ]
        try:
            async with (
                httpcore2.AsyncConnectionPool(
                    ssl_context=self._tls_context,
                    max_connections=1,
                    max_keepalive_connections=0,
                    http1=True,
                    http2=False,
                    retries=0,
                    network_backend=backend,
                ) as pool,
                pool.stream(
                    "GET", request_url, headers=headers, extensions={"timeout": timeouts}
                ) as response,
            ):
                remote = str(backend.connected_address)
                return await self._read_response(
                    target, response, remote, redirects, requested, started, fail
                )
        except UnvalidatedDestinationError as exc:
            fail(FailureKind.BLOCKED_DESTINATION, str(exc))
        except httpcore2.ConnectTimeout:
            fail(FailureKind.CONNECT_TIMEOUT, f"connecting to {target.host} timed out")
        except httpcore2.ConnectError as exc:
            tls_error = _tls_error(exc)
            if tls_error is not None:
                fail(FailureKind.TLS_ERROR, f"TLS with {target.host} failed: {tls_error}")
            fail(FailureKind.CONNECTION_ERROR, f"could not connect to {target.host}")
        except (httpcore2.ReadTimeout, httpcore2.WriteTimeout, httpcore2.PoolTimeout):
            fail(FailureKind.READ_TIMEOUT, f"{target.host} stopped responding")
        except httpcore2.RemoteProtocolError as exc:
            message = str(exc)
            if message.startswith(_TRUNCATED_BODY):
                fail(FailureKind.TRUNCATED, f"{target.host} closed the connection mid-body")
            if message.startswith(_NO_RESPONSE):
                fail(FailureKind.CONNECTION_ERROR, f"{target.host} closed without a response")
            fail(FailureKind.MALFORMED_RESPONSE, f"invalid HTTP response from {target.host}")
        except (httpcore2.ReadError, httpcore2.WriteError):
            fail(FailureKind.CONNECTION_ERROR, f"the connection to {target.host} was reset")
        except httpcore2.ProtocolError:
            fail(FailureKind.MALFORMED_RESPONSE, f"HTTP protocol error with {target.host}")

    async def _resolve(
        self, target: NormalizedUrl, fail: Callable[[FailureKind, str], NoReturn]
    ) -> list[IPAddress]:
        literal = ip_address_of(target.host)
        if literal is not None:
            return [literal]
        try:
            async with asyncio.timeout(self._config.connect_timeout):
                return await self._resolver.resolve(target.host)
        except TimeoutError:
            fail(FailureKind.DNS_ERROR, f"resolving {target.host} timed out")
        except ResolutionError as exc:
            kind = FailureKind.DNS_ERROR if exc.temporary else FailureKind.DNS_NOT_FOUND
            fail(kind, str(exc))

    async def _read_response(
        self,
        target: NormalizedUrl,
        response: httpcore2.Response,
        remote: str,
        redirects: tuple[Redirect, ...],
        requested: str,
        started: float,
        fail: Callable[..., NoReturn],
    ) -> FetchResult | _RedirectResponse:
        status = response.status
        headers = _kept_headers(response.headers)
        if status in REDIRECT_STATUSES:
            location = _header(response.headers, b"location")
            if not location:
                fail(FailureKind.INVALID_REDIRECT, f"{status} response without a Location", status)
            try:
                destination = join_url(target.url, location)
            except InvalidUrlError as exc:
                fail(FailureKind.INVALID_REDIRECT, f"unusable redirect target: {exc}", status)
            return _RedirectResponse(status, destination, headers, remote)
        if not 200 <= status < 300:
            fail(FailureKind.HTTP_ERROR, f"HTTP {status} from {target.host}", status)

        media_type, parameters = parse_media_type(headers.get("content-type"))
        if media_type not in HTML_MEDIA_TYPES:
            fail(
                FailureKind.UNSUPPORTED_CONTENT_TYPE,
                f"unsupported content type: {(media_type or 'none')[:64]}",
                status,
            )
        try:
            coding = parse_content_coding(headers.get("content-encoding"))
        except ContentDecodingError as exc:
            fail(FailureKind.UNSUPPORTED_CONTENT_ENCODING, str(exc), status)
        declared_length = _content_length(headers.get("content-length"))
        if declared_length is not None and declared_length > self._config.max_wire_bytes:
            fail(
                FailureKind.TOO_LARGE,
                f"the declared body of {declared_length} bytes exceeds the limit",
                status,
            )

        reader = BodyReader(
            coding,
            max_bytes=self._config.max_body_bytes,
            max_wire_bytes=self._config.max_wire_bytes,
        )
        try:
            async for chunk in response.aiter_stream():
                reader.feed(chunk)
            body = reader.finish()
        except BodyTooLargeError as exc:
            fail(FailureKind.TOO_LARGE, str(exc), status)
        except ContentDecodingError as exc:
            fail(FailureKind.MALFORMED_RESPONSE, str(exc), status)

        elapsed = time.monotonic() - started
        logger.debug(
            "Fetched a page",
            extra={
                "event": "fetch.completed",
                "host": target.host,
                "status_code": status,
                "body_bytes": len(body),
                "redirects": len(redirects),
                "elapsed_ms": round(elapsed * 1000, 1),
            },
        )
        return FetchResult(
            requested_url=requested,
            url=target.url,
            status_code=status,
            content_type=media_type,
            charset=parameters.get("charset"),
            headers=headers,
            body=body,
            redirects=redirects,
            redirect_location=None,
            remote_address=remote,
            wire_bytes=reader.wire_bytes,
            elapsed_seconds=elapsed,
        )

    def _unfollowed_redirect(
        self, target: NormalizedUrl, response: _RedirectResponse, requested: str, started: float
    ) -> FetchResult:
        return FetchResult(
            requested_url=requested,
            url=target.url,
            status_code=response.status_code,
            content_type=None,
            charset=None,
            headers=response.headers,
            body=b"",
            redirects=(),
            redirect_location=response.location.url,
            remote_address=response.remote_address,
            wire_bytes=0,
            elapsed_seconds=time.monotonic() - started,
        )


def build_fetcher(settings: "Settings", *, resolver: Resolver | None = None) -> SafeFetcher:
    """A fetcher configured from the application settings (used by the worker, Phase 5)."""
    return SafeFetcher(
        config=FetchConfig(
            connect_timeout=settings.fetch_connect_timeout_seconds,
            read_timeout=settings.fetch_read_timeout_seconds,
            deadline=settings.fetch_deadline_seconds,
            max_body_bytes=settings.fetch_max_body_bytes,
            max_redirects=settings.fetch_max_redirects,
        ),
        targets=CrawlTargetPolicy.from_config(
            hosts=settings.crawl_allowed_hosts,
            schemes=settings.crawl_allowed_schemes,
            ports=settings.crawl_allowed_ports,
        ),
        destinations=DestinationPolicy.from_config(
            internal_origins=settings.fetch_internal_origins,
            allow_loopback=settings.environment != "production",
        ),
        resolver=resolver,
    )


def _connect_host(url: NormalizedUrl) -> str:
    """The host as the HTTP stack sees it: IPv6 literals without their brackets."""
    return url.host[1:-1] if url.host.startswith("[") else url.host


def _authority(url: NormalizedUrl) -> str:
    """The Host header: the normalized host, with the port unless it is the default."""
    default = 443 if url.scheme == "https" else 80
    return url.host if url.port == default else f"{url.host}:{url.port}"


def _request_target(url: NormalizedUrl) -> str:
    """The path and query of a normalized URL (the HTTP request target)."""
    prefix = f"{url.scheme}://{_authority(url)}"
    return url.url[len(prefix) :]


def _header(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> str | None:
    """The first value of a header; UTF-8 if valid, else Latin-1 (never fails)."""
    for key, value in headers:
        if key.lower() == name:
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return value.decode("latin-1")
    return None


def _kept_headers(headers: Sequence[tuple[bytes, bytes]]) -> dict[str, str]:
    """The response headers listed in KEPT_HEADERS; repeated headers are joined with ", "."""
    kept: dict[str, str] = {}
    for key, value in headers:
        name = key.decode("latin-1").lower()
        if name in KEPT_HEADERS:
            text = value.decode("latin-1")
            kept[name] = f"{kept[name]}, {text}" if name in kept else text
    return {name: value[:MAX_HEADER_VALUE] for name, value in kept.items()}


def _content_length(value: str | None) -> int | None:
    """The declared body length, or None if absent (h11 rejects malformed values itself)."""
    match = _CONTENT_LENGTH.fullmatch(value.strip()) if value is not None else None
    return int(match.group()) if match is not None else None


def _tls_error(exc: BaseException) -> str | None:
    """The TLS failure behind a connect error, if there is one.

    Follows ``__context__`` as well as ``__cause__``: httpcore2's connection pool re-raises
    connection errors with ``from None``, which clears ``__cause__`` but keeps ``__context__``.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return f"certificate verification failed ({current.verify_message})"
        if isinstance(current, ssl.SSLError):
            return current.reason or type(current).__name__
        current = current.__cause__ or current.__context__
    return None
