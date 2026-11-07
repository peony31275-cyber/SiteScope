"""Test doubles and a local HTTP server for the safe fetcher tests.

Nothing here is used by the application. ``ScriptedResolver`` and ``RecordingBackend`` are test
doubles; ``LocalHttpServer`` is a real HTTP/1.1 server on 127.0.0.1 that answers with scripted
bytes, so tests can exercise malformed, truncated, slow and hostile responses. Tests never
contact the public internet.
"""

import asyncio
import contextlib
import ipaddress
import ssl
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import httpcore2

from app.crawler.destinations import DestinationPolicy, IPAddress, Origin
from app.crawler.fetcher import FetchConfig, SafeFetcher
from app.crawler.resolver import ResolutionError
from app.crawler.scope import CrawlTargetPolicy

# Answers for one host: a list of addresses per lookup (the last one repeats), or an error.
type Answer = Sequence[str] | ResolutionError


class ScriptedResolver:
    """Test double for DNS: answers from a script and records every lookup.

    ``answers[host]`` is a list of answers, one per lookup; the last answer repeats. This lets
    a test change the answer between lookups, as a DNS rebinding attacker would.
    """

    def __init__(self, answers: Mapping[str, Sequence[Answer]]) -> None:
        self._answers = {host: list(script) for host, script in answers.items()}
        self.calls: list[str] = []

    async def resolve(self, host: str) -> list[IPAddress]:
        self.calls.append(host)
        script = self._answers.get(host)
        if not script:
            raise ResolutionError(f"{host} does not exist", temporary=False)
        answer = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(answer, ResolutionError):
            raise answer
        return [ipaddress.ip_address(address) for address in answer]


class RecordingBackend(httpcore2.AnyIOBackend):
    """Test double around the real network backend: records every TCP connection target."""

    def __init__(self) -> None:
        super().__init__()
        self.targets: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore2's interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        self.targets.append((host, port))
        return await super().connect_tcp(host, port, timeout, local_address, socket_options)


class FailingBackend(httpcore2.AsyncNetworkBackend):
    """Test double: every connection attempt raises the given error, and is recorded."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.targets: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore2's interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        self.targets.append((host, port))
        raise self.error

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass
class Reply:
    """What the local server sends for one path."""

    status: int = 200
    reason: str = "OK"
    headers: list[tuple[str, str]] = field(
        default_factory=lambda: [("Content-Type", "text/html; charset=utf-8")]
    )
    body: bytes = b""
    content_length: bool = True  # add Content-Length for the body
    raw: bytes | None = None  # send exactly these bytes instead of a built response
    delay: float = 0.0  # seconds to wait before answering
    drip: float = 0.0  # seconds to wait between body bytes (a slow-drip response)
    hold: bool = False  # never answer; wait until the client gives up

    def serialize_head(self) -> bytes:
        lines = [f"HTTP/1.1 {self.status} {self.reason}"]
        lines += [f"{name}: {value}" for name, value in self.headers]
        if self.content_length:
            lines.append(f"Content-Length: {len(self.body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


@dataclass(frozen=True)
class ReceivedRequest:
    method: str
    target: str
    headers: dict[str, str]  # lowercase names
    peer: str  # the client's IP address
    sni: str | None  # the TLS server name the client sent, if TLS


class LocalHttpServer:
    """A scripted HTTP/1.1 server on 127.0.0.1 (optionally TLS). One request per connection."""

    def __init__(
        self, routes: Mapping[str, Reply] | None = None, *, tls: ssl.SSLContext | None = None
    ) -> None:
        self.routes: dict[str, Reply] = dict(routes or {})
        self.requests: list[ReceivedRequest] = []
        self.connections = 0
        self._tls = tls
        self._sni: dict[int, str | None] = {}
        self._server: asyncio.Server | None = None
        self._port: int | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        if tls is not None:
            tls.sni_callback = self._record_sni

    @property
    def port(self) -> int:
        assert self._port is not None, "the server has not been started"
        return self._port

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, ssl=self._tls)
        self._port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._server is not None
        self._server.close()
        self._server.close_clients()
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._server.wait_closed()

    def _record_sni(
        self, ssl_object: ssl.SSLObject, server_name: str | None, context: ssl.SSLContext
    ) -> None:
        self._sni[id(ssl_object)] = server_name

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # the client went away, as several tests make it do
        finally:
            self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        head = await reader.readuntil(b"\r\n\r\n")
        request_line, *header_lines = head.decode("latin-1").split("\r\n")
        method, target, _ = request_line.split(" ", 2)
        headers: dict[str, str] = {}
        for line in header_lines:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        ssl_object = writer.get_extra_info("ssl_object")
        self.requests.append(
            ReceivedRequest(
                method=method,
                target=target,
                headers=headers,
                peer=writer.get_extra_info("peername")[0],
                sni=self._sni.get(id(ssl_object)) if ssl_object is not None else None,
            )
        )
        reply = self.routes.get(target.split("?")[0], Reply(status=404, reason="Not Found"))
        if reply.hold:
            await reader.read()  # until the client closes the connection
            return
        if reply.delay:
            await asyncio.sleep(reply.delay)
        if reply.raw is not None:
            writer.write(reply.raw)
            await writer.drain()
            return
        writer.write(reply.serialize_head())
        if reply.drip:
            for index in range(len(reply.body)):
                writer.write(reply.body[index : index + 1])
                await writer.drain()
                await asyncio.sleep(reply.drip)
        else:
            writer.write(reply.body)
        await writer.drain()


def make_fetcher(
    *,
    resolver: ScriptedResolver,
    port: int,
    hosts: Sequence[str] = ("fixture.test",),
    internal: Sequence[str] = ("fixture.test",),
    allow_loopback: bool = True,
    schemes: Sequence[str] = ("http", "https"),
    backend: httpcore2.AsyncNetworkBackend | None = None,
    tls_context: ssl.SSLContext | None = None,
    **config: Any,
) -> SafeFetcher:
    """A fetcher for local test servers: approved ``hosts`` on ``port``, short timeouts.

    Hosts in ``internal`` are internal origins (for both schemes) and may resolve to loopback.
    """
    settings: dict[str, Any] = {"connect_timeout": 2.0, "read_timeout": 2.0, "deadline": 10.0}
    settings.update(config)
    return SafeFetcher(
        config=FetchConfig(**settings),
        targets=CrawlTargetPolicy.from_config(hosts=hosts, schemes=schemes, ports=[port]),
        destinations=DestinationPolicy(
            internal_origins=frozenset(
                Origin(scheme, host, port) for host in internal for scheme in ("http", "https")
            ),
            allow_loopback=allow_loopback,
        ),
        resolver=resolver,
        network_backend=backend,
        tls_context=tls_context,
    )
