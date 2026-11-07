"""The safe fetcher against real local servers on 127.0.0.1 (no PostgreSQL, Redis or internet).

DNS answers come from ``ScriptedResolver``, a test double. The test host names end in ``.test``,
a reserved TLD that the operating system cannot resolve, so a successful fetch also shows that
nothing in the HTTP stack resolved the name a second time. ``RecordingBackend`` wraps the real
network backend and records the address of every TCP connection that was actually opened.
"""

import gzip
import ssl
import zlib

import pytest
import trustme

from app.crawler.errors import FailureKind, FetchError
from app.crawler.fetcher import create_tls_context
from app.crawler.html_parser import parse_html
from app.crawler.resolver import ResolutionError, SystemResolver
from tests.fetch_support import (
    LocalHttpServer,
    RecordingBackend,
    Reply,
    ScriptedResolver,
    make_fetcher,
)
from tests.integration.helpers import unused_port

pytestmark = pytest.mark.anyio

LOOPBACK = ["127.0.0.1"]
PAGE = b"<!doctype html><title>Fixture</title><p>Hello <a href='/next'>next</a></p>"


def html(body: bytes, **headers: str) -> Reply:
    extra = [(name.replace("_", "-"), value) for name, value in headers.items()]
    return Reply(headers=[("Content-Type", "text/html; charset=utf-8"), *extra], body=body)


def redirect(location: str, status: int = 302) -> Reply:
    return Reply(status=status, reason="Redirect", headers=[("Location", location)])


async def fetch_error(fetcher_call: object) -> FetchError:
    """Await a fetch that must fail, and return its FetchError."""
    with pytest.raises(FetchError) as caught:
        await fetcher_call  # type: ignore[misc]  # an awaitable built by the caller
    return caught.value


# --- Pinned connections, Host header, DNS rebinding ------------------------------------------


async def test_fetches_over_a_connection_pinned_to_the_validated_address() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    backend = RecordingBackend()
    async with LocalHttpServer({"/": html(PAGE)}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, backend=backend)

        result = await fetcher.fetch(f"http://fixture.test:{server.port}/")

    assert (result.status_code, result.body, result.content_type) == (200, PAGE, "text/html")
    assert result.charset == "utf-8"
    assert result.remote_address == "127.0.0.1"
    # The connection layer received the validated IP address, never the host name.
    assert backend.targets == [("127.0.0.1", server.port)]
    assert resolver.calls == ["fixture.test"]
    [request] = server.requests
    assert request.headers["host"] == f"fixture.test:{server.port}"  # the original authority
    assert request.headers["user-agent"].startswith("SiteScopeBot/")
    assert request.headers["accept-encoding"] == "gzip, deflate"
    assert request.peer == "127.0.0.1"


async def test_dns_is_resolved_once_per_hop_so_a_rebinding_answer_is_never_used() -> None:
    # The first answer is allowed; any later lookup would return the metadata address.
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK, ["169.254.169.254"]]})
    backend = RecordingBackend()
    async with LocalHttpServer({"/": html(PAGE)}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, backend=backend)

        result = await fetcher.fetch(f"http://fixture.test:{server.port}/")

        assert result.status_code == 200
        assert resolver.calls == ["fixture.test"]
        assert backend.targets == [("127.0.0.1", server.port)]
        # The next request resolves again, gets the rebinding answer, and is refused before
        # any connection is opened.
        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))
    assert error.kind is FailureKind.BLOCKED_DESTINATION
    assert backend.targets == [("127.0.0.1", server.port)]


async def test_each_redirect_hop_is_resolved_and_validated_again() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK, ["169.254.169.254"]]})
    backend = RecordingBackend()
    async with LocalHttpServer({"/start": redirect("/next"), "/next": html(PAGE)}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, backend=backend)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/start"))

    base = f"http://fixture.test:{server.port}"
    assert error.kind is FailureKind.BLOCKED_DESTINATION
    # The redirect was followed; its target was resolved again and refused before connecting.
    assert [(hop.url, hop.location) for hop in error.redirects] == [
        (f"{base}/start", f"{base}/next")
    ]
    assert error.url == f"{base}/next"
    assert resolver.calls == ["fixture.test", "fixture.test"]
    assert backend.targets == [("127.0.0.1", server.port)]  # the second hop never connected


async def test_environment_proxies_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{unused_port()}")
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": html(PAGE)}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        result = await fetcher.fetch(f"http://fixture.test:{server.port}/")

    assert result.status_code == 200
    assert server.requests[0].target == "/"  # a direct request, not a proxied absolute URI


# --- SSRF: destinations that are never contacted ---------------------------------------------


@pytest.mark.parametrize(
    ("answer", "address_class"),
    [
        (["10.0.0.7"], "private"),
        (["192.168.1.1"], "private"),
        (["127.0.0.1"], "loopback"),
        (["::1"], "loopback"),
        (["fd12:3456::1"], "private"),
        (["169.254.169.254"], "metadata"),
        (["fe80::1"], "link-local"),
        (["::ffff:127.0.0.1"], "loopback"),
        (["::ffff:169.254.169.254"], "metadata"),
        (["64:ff9b::a00:7"], "private"),  # NAT64 form of 10.0.0.7
        (["93.184.216.34", "10.0.0.7"], "private"),  # one unsafe answer refuses the host
    ],
)
async def test_unsafe_dns_answers_are_refused_before_connecting(
    answer: list[str], address_class: str
) -> None:
    resolver = ScriptedResolver({"public.test": [answer]})
    backend = RecordingBackend()
    fetcher = make_fetcher(
        resolver=resolver, port=8080, hosts=["public.test"], internal=[], backend=backend
    )

    error = await fetch_error(fetcher.fetch("http://public.test:8080/"))

    assert error.kind is FailureKind.BLOCKED_DESTINATION
    assert not error.retryable
    assert f"resolves to a {address_class} address" in error.message
    assert "10.0.0.7" not in error.message  # addresses are not disclosed in the message
    assert backend.targets == []


async def test_loopback_is_refused_for_internal_origins_when_not_allowed() -> None:
    """In production (allow_loopback=False), even an internal origin cannot reach loopback."""
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    backend = RecordingBackend()
    async with LocalHttpServer({"/": html(PAGE)}) as server:
        fetcher = make_fetcher(
            resolver=resolver, port=server.port, allow_loopback=False, backend=backend
        )

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert error.kind is FailureKind.BLOCKED_DESTINATION
    assert (backend.targets, server.connections) == ([], 0)


async def test_unapproved_hosts_are_refused_without_a_dns_lookup() -> None:
    resolver = ScriptedResolver({})
    fetcher = make_fetcher(resolver=resolver, port=8080)

    error = await fetch_error(fetcher.fetch("http://169.254.169.254:8080/latest/meta-data/"))

    assert error.kind is FailureKind.TARGET_NOT_ALLOWED
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("error", "kind", "retryable"),
    [
        (ResolutionError("gone", temporary=False), FailureKind.DNS_NOT_FOUND, False),
        (ResolutionError("servfail", temporary=True), FailureKind.DNS_ERROR, True),
    ],
)
async def test_dns_failures_are_classified(
    error: ResolutionError, kind: FailureKind, retryable: bool
) -> None:
    fetcher = make_fetcher(resolver=ScriptedResolver({"fixture.test": [error]}), port=8080)

    failure = await fetch_error(fetcher.fetch("http://fixture.test:8080/"))

    assert (failure.kind, failure.retryable) == (kind, retryable)


# --- Redirects -------------------------------------------------------------------------------


async def test_follows_validated_redirects_and_reports_the_chain() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    routes = {
        "/a": redirect("/b", 301),
        "/b": redirect("http://fixture.test:{port}/c#ignored", 307),
        "/c": redirect("d?x=1", 303),
        "/d": html(PAGE),
    }
    async with LocalHttpServer(routes) as server:
        routes["/b"].headers = [("Location", f"http://fixture.test:{server.port}/c#ignored")]
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        result = await fetcher.fetch(f"http://fixture.test:{server.port}/a")

    base = f"http://fixture.test:{server.port}"
    assert result.url == f"{base}/d?x=1"
    assert result.requested_url == f"{base}/a"
    assert [(hop.url, hop.status_code, hop.location) for hop in result.redirects] == [
        (f"{base}/a", 301, f"{base}/b"),
        (f"{base}/b", 307, f"{base}/c"),
        (f"{base}/c", 303, f"{base}/d?x=1"),
    ]
    assert [request.method for request in server.requests] == ["GET"] * 4


async def test_max_redirects_zero_returns_the_redirect_without_following_it() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/a": redirect("/elsewhere?q=1#f")}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        result = await fetcher.fetch(f"http://fixture.test:{server.port}/a", max_redirects=0)

    assert result.status_code == 302
    assert result.redirect_location == f"http://fixture.test:{server.port}/elsewhere?q=1"
    assert (result.body, result.redirects, len(server.requests)) == (b"", (), 1)


async def test_redirect_loops_are_detected() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/a": redirect("/b"), "/b": redirect("/a")}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/a"))

    assert error.kind is FailureKind.REDIRECT_LOOP
    assert len(server.requests) == 2


async def test_the_redirect_limit_is_enforced() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    routes = {f"/{n}": redirect(f"/{n + 1}") for n in range(5)}
    async with LocalHttpServer(routes) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, max_redirects=2)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/0"))

    assert error.kind is FailureKind.TOO_MANY_REDIRECTS
    assert len(error.redirects) == 2
    assert len(server.requests) == 3


async def test_a_redirect_outside_the_approved_targets_is_refused() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK], "outside.test": [LOOPBACK]})
    async with LocalHttpServer({"/": redirect("http://outside.test/")}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert error.kind is FailureKind.TARGET_NOT_ALLOWED
    assert resolver.calls == ["fixture.test"]  # the unapproved host was never resolved


async def test_a_redirect_to_an_approved_host_with_a_private_address_is_refused() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK], "inner.test": [["10.1.2.3"]]})
    backend = RecordingBackend()
    async with LocalHttpServer({"/": redirect("http://inner.test:{port}/")}) as server:
        server.routes["/"] = redirect(f"http://inner.test:{server.port}/admin")
        fetcher = make_fetcher(
            resolver=resolver,
            port=server.port,
            hosts=["fixture.test", "inner.test"],
            backend=backend,
        )

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert error.kind is FailureKind.BLOCKED_DESTINATION
    assert backend.targets == [("127.0.0.1", server.port)]  # only the first hop connected


@pytest.mark.parametrize(
    "reply",
    [
        Reply(status=302, reason="Found", headers=[]),
        redirect("javascript:alert(1)"),
        redirect("http://user:secret@fixture.test/"),
        redirect("http://exa mple.test/"),
    ],
)
async def test_unusable_redirect_targets_are_rejected(reply: Reply) -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": reply}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert (error.kind, error.http_status, error.retryable) == (
        FailureKind.INVALID_REDIRECT,
        302,
        False,
    )


# --- TLS -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def certificate_authority() -> trustme.CA:
    """A throwaway CA created for this test run (no private key is stored in the repository)."""
    return trustme.CA()


def server_tls(ca: trustme.CA, *names: str) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert(*names).configure_cert(context)
    return context


@pytest.fixture
def client_tls(certificate_authority: trustme.CA) -> ssl.SSLContext:
    context = create_tls_context()  # the production context, plus trust in the test CA
    certificate_authority.configure_trust(context)
    return context


async def test_https_sends_sni_and_verifies_the_certificate_of_the_host_name(
    certificate_authority: trustme.CA, client_tls: ssl.SSLContext
) -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    backend = RecordingBackend()
    tls = server_tls(certificate_authority, "fixture.test")
    async with LocalHttpServer({"/": html(PAGE)}, tls=tls) as server:
        fetcher = make_fetcher(
            resolver=resolver, port=server.port, backend=backend, tls_context=client_tls
        )

        result = await fetcher.fetch(f"https://fixture.test:{server.port}/")

    assert result.body == PAGE
    assert backend.targets == [("127.0.0.1", server.port)]  # connected by IP address ...
    [request] = server.requests
    assert request.sni == "fixture.test"  # ... while TLS used the host name
    assert request.headers["host"] == f"fixture.test:{server.port}"


async def test_a_certificate_for_another_host_name_is_rejected(
    certificate_authority: trustme.CA, client_tls: ssl.SSLContext
) -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    tls = server_tls(certificate_authority, "other.test")
    async with LocalHttpServer({"/": html(PAGE)}, tls=tls) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, tls_context=client_tls)

        error = await fetch_error(fetcher.fetch(f"https://fixture.test:{server.port}/"))

    assert (error.kind, error.retryable) == (FailureKind.TLS_ERROR, False)
    assert "certificate verification failed" in error.message
    assert server.requests == []


async def test_a_certificate_from_an_untrusted_authority_is_rejected(
    certificate_authority: trustme.CA,
) -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    tls = server_tls(certificate_authority, "fixture.test")
    async with LocalHttpServer({"/": html(PAGE)}, tls=tls) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)  # system trust store only

        error = await fetch_error(fetcher.fetch(f"https://fixture.test:{server.port}/"))

    assert error.kind is FailureKind.TLS_ERROR
    assert server.requests == []


async def test_a_stalled_tls_handshake_is_a_connect_timeout(client_tls: ssl.SSLContext) -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({}) as server:  # plain TCP: never answers the TLS handshake
        fetcher = make_fetcher(
            resolver=resolver, port=server.port, tls_context=client_tls, connect_timeout=0.5
        )

        error = await fetch_error(fetcher.fetch(f"https://fixture.test:{server.port}/"))

    assert (error.kind, error.retryable) == (FailureKind.CONNECT_TIMEOUT, True)


# --- Status codes and content ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(404, False), (410, False), (429, True), (500, True), (501, False), (503, True)],
)
async def test_error_statuses_are_classified(status: int, retryable: bool) -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": Reply(status=status, reason="X", body=b"x")}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert (error.kind, error.http_status, error.retryable) == (
        FailureKind.HTTP_ERROR,
        status,
        retryable,
    )


@pytest.mark.parametrize(
    ("status", "headers", "retry_after"),
    [
        (503, [("Retry-After", "120")], 120.0),
        (429, [("Retry-After", " 7 ")], 7.0),
        # An HTTP-date counts from the response's own Date header (the server's clock).
        (
            503,
            [
                ("Date", "Sat, 07 Nov 2026 12:00:00 GMT"),
                ("Retry-After", "Sat, 07 Nov 2026 12:00:30 GMT"),
            ],
            30.0,
        ),
        (
            429,
            [
                ("Date", "Sat, 07 Nov 2026 12:00:00 GMT"),
                ("Retry-After", "Sat, 07 Nov 2026 11:00:00 GMT"),
            ],
            0.0,
        ),
        (503, [("Retry-After", "soon")], None),  # malformed: ignored
        # Regression (Phase 6 independent review): dates outside datetime's range in UTC
        # raised OverflowError out of the fetcher; now they are ignored like any malformed one.
        (503, [("Retry-After", "Fri, 31 Dec 9999 23:59:59 -0100")], None),
        (
            429,
            [
                ("Date", "Fri, 31 Dec 9999 23:59:59 -0100"),
                ("Retry-After", "Mon, 01 Jan 2001 00:00:00 GMT"),
            ],
            0.0,  # the Date is unusable: counted from now, a date in the past means 0 s
        ),
        (503, [("Retry-After", "-5")], None),
        (503, [], None),
        (500, [("Retry-After", "120")], None),  # only 429 and 503 carry a Retry-After here
    ],
)
async def test_retry_after_is_read_from_429_and_503_responses(
    status: int, headers: list[tuple[str, str]], retry_after: float | None
) -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    reply = Reply(status=status, reason="X", headers=headers, body=b"busy")
    async with LocalHttpServer({"/": reply}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert (error.kind, error.http_status, error.retry_after) == (
        FailureKind.HTTP_ERROR,
        status,
        retry_after,
    )


@pytest.mark.parametrize(
    "headers",
    [[("Content-Type", "application/pdf")], [("Content-Type", "text/plain")], []],
)
async def test_non_html_content_is_refused(headers: list[tuple[str, str]]) -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": Reply(headers=headers, body=b"%PDF-1.7")}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert (error.kind, error.http_status) == (FailureKind.UNSUPPORTED_CONTENT_TYPE, 200)


async def test_xhtml_and_uppercase_media_types_are_html() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    routes = {
        "/x": Reply(headers=[("Content-Type", "application/xhtml+xml")], body=PAGE),
        "/u": Reply(headers=[("Content-Type", 'TEXT/HTML; Charset="ISO-8859-1"')], body=PAGE),
    }
    async with LocalHttpServer(routes) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        xhtml = await fetcher.fetch(f"http://fixture.test:{server.port}/x")
        upper = await fetcher.fetch(f"http://fixture.test:{server.port}/u")

    assert xhtml.content_type == "application/xhtml+xml"
    assert (upper.content_type, upper.charset) == ("text/html", "ISO-8859-1")


@pytest.mark.parametrize(
    ("coding", "encoded"),
    [
        ("gzip", gzip.compress(PAGE)),
        ("deflate", zlib.compress(PAGE)),
        ("deflate", zlib.compress(PAGE)[2:-4]),  # raw DEFLATE, as some servers send it
    ],
)
async def test_content_codings_are_decoded(coding: str, encoded: bytes) -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": html(encoded, Content_Encoding=coding)}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        result = await fetcher.fetch(f"http://fixture.test:{server.port}/")

    assert result.body == PAGE
    assert result.wire_bytes == len(encoded)


async def test_chunked_bodies_are_read() -> None:
    body = b"5\r\n<p>Hi\r\n6\r\n there\r\n0\r\n\r\n"
    reply = Reply(
        headers=[("Content-Type", "text/html"), ("Transfer-Encoding", "chunked")],
        body=body,
        content_length=False,
    )
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": reply}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        result = await fetcher.fetch(f"http://fixture.test:{server.port}/")

    assert result.body == b"<p>Hi there"


async def test_unadvertised_content_codings_are_refused() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": html(b"\x1b\x00", Content_Encoding="br")}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert error.kind is FailureKind.UNSUPPORTED_CONTENT_ENCODING


# --- Size limits -----------------------------------------------------------------------------


async def test_a_declared_length_over_the_limit_is_refused_without_reading() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    reply = Reply(
        headers=[("Content-Type", "text/html"), ("Content-Length", "50000000")],
        body=b"<p>tiny</p>",
        content_length=False,
    )
    async with LocalHttpServer({"/": reply}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, max_body_bytes=4096)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert (error.kind, error.retryable) == (FailureKind.TOO_LARGE, False)


async def test_a_body_over_the_limit_without_a_declared_length_is_refused() -> None:
    body = b"x" * 20_000
    chunked = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    reply = Reply(
        headers=[("Content-Type", "text/html"), ("Transfer-Encoding", "chunked")],
        body=chunked,
        content_length=False,
    )
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": reply}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, max_body_bytes=4096)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert error.kind is FailureKind.TOO_LARGE


async def test_a_decompression_bomb_is_stopped_at_the_decoded_limit() -> None:
    bomb = gzip.compress(b"\0" * 50_000_000)  # 50 MB of zeros in about 50 kB
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": html(bomb, Content_Encoding="gzip")}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, max_body_bytes=1024 * 1024)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert error.kind is FailureKind.TOO_LARGE
    assert "decoded body exceeds" in error.message


# --- Broken, slow and hostile servers ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 100\r\n\r\nshort",
            FailureKind.TRUNCATED,
        ),
        (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"10\r\nabc",
            FailureKind.TRUNCATED,
        ),
        (b"", FailureKind.CONNECTION_ERROR),  # closed without any response
        (b"HTTP/1.1 ABC Nope\r\n\r\n", FailureKind.MALFORMED_RESPONSE),
        (b"not http at all\r\n\r\n", FailureKind.MALFORMED_RESPONSE),
        (b"HTTP/1.1 200 OK\r\nBroken Header\r\n\r\n", FailureKind.MALFORMED_RESPONSE),
        (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 1\r\n"
            b"Content-Length: 2\r\n\r\nab",
            FailureKind.MALFORMED_RESPONSE,
        ),
        (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: gzip\r\n"
            b"Content-Length: 8\r\n\r\nnot gzip",
            FailureKind.MALFORMED_RESPONSE,
        ),
        (
            b"HTTP/1.1 200 OK\r\nX-Huge: " + b"a" * 200_000 + b"\r\n\r\n",
            FailureKind.MALFORMED_RESPONSE,
        ),
    ],
)
async def test_broken_responses_are_classified(raw: bytes, kind: FailureKind) -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": Reply(raw=raw)}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert error.kind is kind


async def test_a_server_that_does_not_answer_is_a_read_timeout() -> None:
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": Reply(hold=True)}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, read_timeout=0.3)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert (error.kind, error.retryable) == (FailureKind.READ_TIMEOUT, True)


async def test_a_slow_drip_response_is_stopped_by_the_deadline() -> None:
    """Each byte arrives well within the read timeout, but the whole fetch takes too long."""
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    reply = Reply(headers=[("Content-Type", "text/html")], body=b"x" * 200, drip=0.05)
    async with LocalHttpServer({"/": reply}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port, read_timeout=1.0, deadline=0.6)

        error = await fetch_error(fetcher.fetch(f"http://fixture.test:{server.port}/"))

    assert (error.kind, error.retryable) == (FailureKind.DEADLINE_EXCEEDED, True)


async def test_a_refused_connection_is_a_retryable_connection_error() -> None:
    port = unused_port()
    # Windows reports a refused localhost connection only after about 2 s of SYN retries.
    fetcher = make_fetcher(
        resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}), port=port, connect_timeout=5.0
    )

    error = await fetch_error(fetcher.fetch(f"http://fixture.test:{port}/"))

    assert (error.kind, error.retryable) == (FailureKind.CONNECTION_ERROR, True)


# --- The real resolver and the parser end to end --------------------------------------------


async def test_the_system_resolver_resolves_localhost_and_rejects_reserved_names() -> None:
    resolver = SystemResolver()

    addresses = await resolver.resolve("localhost")

    assert addresses
    assert all(address.is_loopback for address in addresses)
    with pytest.raises(ResolutionError):
        await resolver.resolve("does-not-exist.invalid")  # RFC 6761: never resolves


async def test_a_fetched_page_parses_with_links_resolved_against_its_final_url() -> None:
    page = (
        b"<html lang=en><head><title>Docs home</title>"
        b"<meta name=description content='The docs'></head><body>"
        b"<h1>Welcome</h1><p>Read the <a href='guide/'>guide</a> or "
        b"<a href='/api#top'>API</a>.</p><a href='mailto:docs@fixture.test'>mail</a></body></html>"
    )
    resolver = ScriptedResolver({"fixture.test": [LOOPBACK]})
    async with LocalHttpServer({"/": redirect("/docs/"), "/docs/": html(page)}) as server:
        fetcher = make_fetcher(resolver=resolver, port=server.port)

        result = await fetcher.fetch(f"http://fixture.test:{server.port}/")
        parsed = parse_html(result.body, url=result.url, declared_charset=result.charset)

    base = f"http://fixture.test:{server.port}"
    assert (parsed.title, parsed.description, parsed.language) == ("Docs home", "The docs", "en")
    assert [(link.url, link.anchor_text) for link in parsed.links] == [
        (f"{base}/docs/guide/", "guide"),
        (f"{base}/api", "API"),
    ]
    assert parsed.stats.links_unsupported == 1
    assert parsed.text == "Welcome Read the guide or API . mail"


# --- The manual-check command ----------------------------------------------------------------


async def test_the_cli_reports_a_fetched_and_parsed_page() -> None:
    from app.core.config import Settings
    from app.crawler import cli

    async with LocalHttpServer({"/": redirect("/docs/"), "/docs/": html(PAGE)}) as server:
        settings = Settings(
            _env_file=None,
            environment="test",
            crawl_allowed_hosts=("127.0.0.1",),
            crawl_allowed_ports=(server.port,),
            fetch_internal_origins=(f"http://127.0.0.1:{server.port}",),
        )

        report, ok = await cli.run(settings, f"http://127.0.0.1:{server.port}/")

    assert ok
    assert report["url"] == f"http://127.0.0.1:{server.port}/docs/"
    assert report["remote_address"] == "127.0.0.1"
    assert report["page"]["title"] == "Fixture"
    assert report["page"]["links"] == [f"http://127.0.0.1:{server.port}/next"]
    assert len(report["redirects"]) == 1
