"""Phase 4 fetcher features against local servers on 127.0.0.1: content policies (robots.txt),
X-Robots-Tag headers, and the per-hop request gate. No PostgreSQL, Redis or internet.

``RecordingGate`` is a test double of the politeness gate: it records every slot it grants and
can refuse slots. The real gate is tested in test_politeness_redis.py and the unit tests.
"""

import asyncio
import gzip
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from app.crawler.content import HTML_PAGE, ROBOTS_TXT, ROBOTS_TXT_MAX_BYTES
from app.crawler.errors import FailureKind, FetchError, RateLimitedError
from app.crawler.urls import NormalizedUrl
from tests.fetch_support import (
    LocalHttpServer,
    RecordingBackend,
    Reply,
    ScriptedResolver,
    make_fetcher,
)

pytestmark = pytest.mark.anyio

LOOPBACK = ["127.0.0.1"]
ROBOTS = b"User-agent: *\nDisallow: /private/\n"


class RecordingGate:
    """Test double of the politeness gate: records slots, or refuses every slot."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.events: list[str] = []

    @asynccontextmanager
    async def slot(self, url: NormalizedUrl) -> AsyncIterator[None]:
        if self.refuse:
            raise RateLimitedError(f"no request slot for {url.host} within 1 s")
        self.events.append(f"enter {url.url}")
        try:
            yield
        finally:
            self.events.append(f"exit {url.url}")


def text_reply(body: bytes, content_type: str | None = "text/plain", **kwargs: object) -> Reply:
    headers = [("Content-Type", content_type)] if content_type else []
    return Reply(headers=headers, body=body, **kwargs)  # type: ignore[arg-type]  # Reply fields


def html_reply(body: bytes, headers: list[tuple[str, str]] | None = None) -> Reply:
    return Reply(headers=[("Content-Type", "text/html"), *(headers or [])], body=body)


async def fetch_error(call: object) -> FetchError:
    with pytest.raises(FetchError) as caught:
        await call  # type: ignore[misc]  # an awaitable built by the caller
    return caught.value


# --- Content policies ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type", ["text/plain", "text/html", "application/octet-stream", None]
)
async def test_robots_txt_is_accepted_whatever_its_media_type(content_type: str | None) -> None:
    async with LocalHttpServer({"/robots.txt": text_reply(ROBOTS, content_type)}) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}), port=server.port
        )
        result = await fetcher.fetch(
            f"http://fixture.test:{server.port}/robots.txt", content=ROBOTS_TXT
        )
    assert result.body == ROBOTS
    assert result.truncated is False
    [request] = server.requests
    assert request.headers["accept"] == ROBOTS_TXT.accept


async def test_pages_still_accept_only_html() -> None:
    """Regression: the robots.txt policy does not weaken the HTML page rules."""
    async with LocalHttpServer({"/notes.txt": text_reply(b"plain text")}) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}), port=server.port
        )
        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/notes.txt"))
    assert error.kind is FailureKind.UNSUPPORTED_CONTENT_TYPE
    [request] = server.requests
    assert request.headers["accept"] == HTML_PAGE.accept


async def test_a_large_robots_txt_is_truncated_at_500_kib() -> None:
    line = b"Disallow: /p/0123456789abcdef\n"  # 30 bytes
    body = line * (700 * 1024 // len(line))
    async with LocalHttpServer({"/robots.txt": text_reply(body)}) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}), port=server.port
        )
        result = await fetcher.fetch(
            f"http://fixture.test:{server.port}/robots.txt", content=ROBOTS_TXT
        )
    assert result.truncated is True
    assert result.body == body[:ROBOTS_TXT_MAX_BYTES]
    # The declared Content-Length (700 KiB) is no reason to refuse a truncating fetch.
    assert result.wire_bytes < len(body)


async def test_a_large_gzip_robots_txt_is_truncated_after_decoding() -> None:
    body = b"Allow: /a\n" * 200_000  # 2 MB decoded, a few kB compressed
    reply = Reply(
        headers=[("Content-Type", "text/plain"), ("Content-Encoding", "gzip")],
        body=gzip.compress(body),
    )
    async with LocalHttpServer({"/robots.txt": reply}) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}), port=server.port
        )
        result = await fetcher.fetch(
            f"http://fixture.test:{server.port}/robots.txt", content=ROBOTS_TXT
        )
    assert result.truncated is True
    assert result.body == body[:ROBOTS_TXT_MAX_BYTES]


async def test_an_oversized_page_is_still_rejected() -> None:
    page = b"<p>" + b"x" * 5000 + b"</p>"
    async with LocalHttpServer({"/": html_reply(page)}) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}),
            port=server.port,
            max_body_bytes=1024,
        )
        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))
    assert error.kind is FailureKind.TOO_LARGE


async def test_robots_txt_keeps_the_ssrf_checks() -> None:
    # An approved host that resolves to a loopback address, but is not an internal origin.
    backend = RecordingBackend()
    async with LocalHttpServer({"/robots.txt": text_reply(ROBOTS)}) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"public.test": [LOOPBACK]}),
            port=server.port,
            hosts=("public.test",),
            internal=(),
            backend=backend,
        )
        error = await fetch_error(
            fetcher.fetch(f"http://public.test:{server.port}/robots.txt", content=ROBOTS_TXT)
        )
    assert error.kind is FailureKind.BLOCKED_DESTINATION
    assert backend.targets == []
    assert server.connections == 0


# --- X-Robots-Tag --------------------------------------------------------------------------------


async def test_x_robots_tag_headers_are_kept_one_by_one() -> None:
    reply = html_reply(
        b"<p>x</p>",
        headers=[("X-Robots-Tag", "otherbot: noindex"), ("X-Robots-Tag", "nofollow")],
    )
    async with LocalHttpServer({"/": reply}) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}), port=server.port
        )
        result = await fetcher.fetch(f"http://fixture.test:{server.port}/")
    assert result.robots_tags == ("otherbot: noindex", "nofollow")
    assert "x-robots-tag" not in result.headers


# --- The request gate ----------------------------------------------------------------------------


async def test_every_hop_takes_its_own_slot_in_order() -> None:
    routes = {
        "/": Reply(status=301, reason="Moved", headers=[("Location", "/one")]),
        "/one": Reply(status=302, reason="Found", headers=[("Location", "/two")]),
        "/two": html_reply(b"<p>done</p>"),
    }
    gate = RecordingGate()
    async with LocalHttpServer(routes) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}), port=server.port, gate=gate
        )
        base = f"http://fixture.test:{server.port}"
        result = await fetcher.fetch(f"{base}/")
    assert result.url == f"{base}/two"
    assert gate.events == [
        f"enter {base}/",
        f"exit {base}/",
        f"enter {base}/one",
        f"exit {base}/one",
        f"enter {base}/two",
        f"exit {base}/two",
    ]


async def test_no_slot_means_no_dns_lookup_and_no_request() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": html_reply(b"<p>x</p>")}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, gate=RecordingGate(refuse=True))
        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))
    assert error.kind is FailureKind.RATE_LIMITED
    assert error.retryable
    assert (resolver.calls, server.connections) == ([], 0)


async def test_an_unapproved_target_never_takes_a_slot() -> None:
    gate = RecordingGate()
    async with LocalHttpServer({}) as server:
        fetcher = make_fetcher(resolver=ScriptedResolver({}), port=server.port, gate=gate)
        error = await fetch_error(fetcher.fetch(f"http://elsewhere.test:{server.port}/"))
    assert error.kind is FailureKind.TARGET_NOT_ALLOWED
    assert gate.events == []


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (Reply(status=500, reason="Server Error", body=b"oops"), FailureKind.HTTP_ERROR),
        (Reply(raw=b""), FailureKind.CONNECTION_ERROR),
    ],
)
async def test_the_slot_is_released_when_the_request_fails(reply: Reply, kind: FailureKind) -> None:
    gate = RecordingGate()
    async with LocalHttpServer({"/": reply}) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}), port=server.port, gate=gate
        )
        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))
    assert error.kind is kind
    assert [event.split()[0] for event in gate.events] == ["enter", "exit"]


async def test_the_deadline_covers_the_wait_for_a_slot() -> None:
    class SlowGate:
        @asynccontextmanager
        async def slot(self, url: NormalizedUrl) -> AsyncIterator[None]:
            await asyncio.sleep(5)
            yield

    async with LocalHttpServer({"/": html_reply(b"<p>x</p>")}) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}),
            port=server.port,
            gate=SlowGate(),
            deadline=0.2,
        )
        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))
    assert error.kind is FailureKind.DEADLINE_EXCEEDED
    assert server.connections == 0
