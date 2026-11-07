"""URL normalization (app/crawler/urls.py): the policy in its module docstring, case by case."""

import random
import re

import pytest

from app.crawler.urls import (
    MAX_URL_BYTES,
    InvalidUrlError,
    NormalizedUrl,
    ip_address_of,
    join_url,
    normalize_host,
    normalize_target_text,
    normalize_url,
    request_target,
    resolve_reference,
    scheme_of,
)
from app.db import models

# The database CHECK on every URL column (app/db/models.py, normalized_url_check).
DATABASE_URL_FORMAT = re.compile(r"https?://[\x21-\x7e]+")

ACCEPTED = [
    # Scheme and host are case-insensitive; the path is not.
    ("HTTPS://Docs.Example.COM", "https://docs.example.com/"),
    ("http://Example.com/Docs/Index.HTML", "http://example.com/Docs/Index.HTML"),
    # Default and empty ports are removed; other ports are kept.
    ("http://example.com:80/", "http://example.com/"),
    ("https://example.com:443/a", "https://example.com/a"),
    ("http://example.com:443/", "http://example.com:443/"),
    ("https://example.com:8443/", "https://example.com:8443/"),
    ("http://example.com:/", "http://example.com/"),
    ("http://example.com:0080/", "http://example.com/"),
    # Empty path, fragments, dot segments.
    ("https://example.com", "https://example.com/"),
    ("https://example.com?q=1", "https://example.com/?q=1"),
    ("https://example.com/page#section-2", "https://example.com/page"),
    ("https://example.com/page#", "https://example.com/page"),
    ("http://example.com/a/./b/../c", "http://example.com/a/c"),
    ("http://example.com/a/b/c/./../../g", "http://example.com/a/g"),
    ("http://example.com/a/b/..", "http://example.com/a/"),
    ("http://example.com/a/.", "http://example.com/a/"),
    ("http://example.com/../../a", "http://example.com/a"),
    ("http://example.com/%2e%2E/x", "http://example.com/x"),
    # Trailing and repeated slashes are kept.
    ("http://example.com/a/", "http://example.com/a/"),
    ("http://example.com/a//b///", "http://example.com/a//b///"),
    # Percent-encoding: unreserved decoded, the rest uppercased, reserved kept encoded.
    ("http://example.com/%7euser/%41%2d%5F", "http://example.com/~user/A-_"),
    ("http://example.com/a%2fb%3a", "http://example.com/a%2Fb%3A"),
    ("http://example.com/caf%c3%a9", "http://example.com/caf%C3%A9"),
    # Characters a URL cannot contain literally are encoded as UTF-8.
    ("http://example.com/café menu", "http://example.com/caf%C3%A9%20menu"),
    ('http://example.com/[x]|^`{}"<>', "http://example.com/%5Bx%5D%7C%5E%60%7B%7D%22%3C%3E"),
    ("http://example.com/?q=ü ö", "http://example.com/?q=%C3%BC%20%C3%B6"),
    # Sub-delimiters, ":" and "@" are allowed in the path and the query.
    ("http://example.com/a:b@c!$&'()*+,;=", "http://example.com/a:b@c!$&'()*+,;="),
    ("http://example.com/s?u=http://x/y?z", "http://example.com/s?u=http://x/y?z"),
    # The query is kept as it is: order, duplicates, empty values, "+", encoded reserved.
    ("http://example.com/p?b=2&a=1&a=0&e=&c+d=%2B", "http://example.com/p?b=2&a=1&a=0&e=&c+d=%2B"),
    ("http://example.com/p?utm_source=x&id=7", "http://example.com/p?utm_source=x&id=7"),
    ("http://example.com/?", "http://example.com/?"),
    # Hosts: IDNA 2008 / UTS #46, A-labels, IPv4, IPv6, underscores, trailing dot.
    ("http://Bücher.Example/", "http://xn--bcher-kva.example/"),
    ("http://xn--bcher-kva.example/", "http://xn--bcher-kva.example/"),
    ("http://faß.de/", "http://xn--fa-hia.de/"),
    # Fullwidth digits and dots (UTS #46 maps them to ASCII, as browsers do).
    (
        "http://\uff11\uff12\uff17\uff0e\uff10\uff0e\uff10\uff0e\uff11/",
        "http://127.0.0.1/",
    ),
    ("http://192.0.2.1:8080/", "http://192.0.2.1:8080/"),
    ("http://[2001:DB8:0:0:0:0:0:1]/", "http://[2001:db8::1]/"),
    ("http://[::1]:8080/", "http://[::1]:8080/"),
    ("http://Ex_Ample.com/", "http://ex_ample.com/"),
    ("http://example.com./", "http://example.com./"),
    # Leading and trailing spaces and control characters are ignored, as browsers do.
    ("  https://example.com/a \n", "https://example.com/a"),
]

REJECTED = [
    ("", "empty"),
    ("   ", "empty"),
    ("example.com", "must be absolute"),
    ("//example.com/a", "must be absolute"),
    ("/a/b", "must be absolute"),
    ("ftp://example.com/", "scheme 'ftp' is not supported"),
    ("javascript:alert(1)", "scheme 'javascript' is not supported"),
    ("file:///etc/passwd", "scheme 'file' is not supported"),
    ("1http://example.com/", "is not supported"),
    ("http:example.com", "must include a host"),
    ("http:/example.com", "must include a host"),
    ("http://", "no host"),
    ("http:///path", "no host"),
    ("http://:80/", "no host"),
    ("http://user:secret@example.com/", "credentials"),
    ("http://user@example.com/", "credentials"),
    ("http://@example.com/", "credentials"),
    ("http://exa\tmple.com/", "control characters"),
    ("http://example.com/a\nb", "control characters"),
    ("http://example.com/a\x00b", "control characters"),
    ("http://example.com/a\x7fb", "control characters"),
    (r"http://example.com\a", "backslash"),
    (r"http:\\example.com\a", "backslash"),
    ("http://exa mple.com/", "host name may contain only"),
    ("http://exa!mple.com/", "host name may contain only"),
    ("http://" + "a" * 64 + ".example/", "host name may contain only"),
    ("http://" + ("a" * 60 + ".") * 5 + "example/", "longer than 253"),
    ("http://a..b/", "empty label"),
    ("http://.example.com/", "empty label"),
    ("http://ex%41mple.com/", "percent-encoded host"),
    ("http://127.1/", "dotted-decimal"),
    ("http://0x7f.0.0.1/", "dotted-decimal"),
    ("http://2130706433/", "dotted-decimal"),
    ("http://017.0.0.1/", "dotted-decimal"),
    ("http://1.2.3.4./", "dotted-decimal"),
    ("http://256.1.1.1/", "dotted-decimal"),
    ("http://example.123/", "dotted-decimal"),
    ("http://[::1/", "closing ']'"),
    ("http://[::1]x/", "after the IPv6 host"),
    ("http://[fe80::1%25eth0]/", "zone identifiers"),
    ("http://[v1.fe]/", "IPvFuture"),
    ("http://[not-ipv6]/", "not a valid IPv6"),
    ("http://2001:db8::1/", "enclosed in brackets"),
    ("http://example.com:0/", "between 1 and 65535"),
    ("http://example.com:65536/", "between 1 and 65535"),
    ("http://example.com:" + "9" * 5000 + "/", "between 1 and 65535"),
    ("http://example.com:8o/", "must be a number"),
    ("http://example.com:٨٠/", "must be a number"),  # Arabic-Indic digits
    ("http://example.com/100%", "percent-encoding"),
    ("http://example.com/%zz", "percent-encoding"),
    ("http://example.com/?q=%4", "percent-encoding"),
    ("http://my_host.bücher.de/", "internationalized domain name"),
    ("http://\ud800.example/", "not valid Unicode"),
    ("http://example.com/" + "a" * MAX_URL_BYTES, "after normalization"),
    ("http://example.com/" + "a" * (4 * MAX_URL_BYTES), "longer than"),
]


@pytest.mark.parametrize(("raw", "expected"), ACCEPTED)
def test_accepted_urls_normalize_to_their_canonical_form(raw: str, expected: str) -> None:
    assert normalize_url(raw).url == expected


@pytest.mark.parametrize(("raw", "message"), REJECTED)
def test_invalid_urls_are_rejected_with_a_reason(raw: str, message: str) -> None:
    with pytest.raises(InvalidUrlError, match=re.escape(message)):
        normalize_url(raw)


@pytest.mark.parametrize(("raw", "expected"), ACCEPTED)
def test_normalization_is_idempotent(raw: str, expected: str) -> None:
    assert normalize_url(normalize_url(raw).url).url == expected


@pytest.mark.parametrize(("raw", "expected"), ACCEPTED)
def test_output_satisfies_the_database_url_contract(raw: str, expected: str) -> None:
    url = normalize_url(raw).url

    assert DATABASE_URL_FORMAT.fullmatch(url)
    assert len(url.encode()) <= MAX_URL_BYTES


def test_length_limit_matches_the_database_contract() -> None:
    assert MAX_URL_BYTES == models.MAX_URL_BYTES
    longest = "http://example.com/" + "a" * (MAX_URL_BYTES - len("http://example.com/"))

    assert normalize_url(longest).url == longest
    with pytest.raises(InvalidUrlError, match="after normalization"):
        normalize_url(longest + "a")


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("http://example.com/Page", "http://example.com/page"),  # paths are case-sensitive
        ("http://example.com/a", "http://example.com/a/"),
        ("http://example.com/a/b", "http://example.com/a%2Fb"),  # encoded "/" is not "/"
        ("http://example.com/?a=1&b=2", "http://example.com/?b=2&a=1"),  # parameter order
        ("http://example.com/?a=1", "http://example.com/?a=1&a=1"),
        ("http://example.com/?q=a+b", "http://example.com/?q=a%20b"),
        ("http://example.com/?q=%2B", "http://example.com/?q=+"),
        ("http://example.com/", "http://example.com/?"),  # empty query
        ("http://example.com/p?utm_source=x", "http://example.com/p"),  # no parameter stripping
        ("http://faß.de/", "http://fass.de/"),  # IDNA 2008, not IDNA 2003
        ("http://example.com/", "http://example.com./"),
        ("http://example.com/", "https://example.com/"),
        ("http://example.com/", "http://example.com:8080/"),
        ("http://www.example.com/", "http://example.com/"),
        ("http://example.com/caf%C3%A9", "http://example.com/cafe%CC%81"),  # no Unicode folding
    ],
)
def test_distinct_urls_stay_distinct(first: str, second: str) -> None:
    assert normalize_url(first).url != normalize_url(second).url


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("HTTP://EXAMPLE.COM/a", "http://example.com/a"),
        ("http://example.com", "http://example.com/"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("http://example.com/%7Ea", "http://example.com/~a"),
        ("http://example.com/%3a", "http://example.com/%3A"),
        ("http://example.com/a#x", "http://example.com/a#y"),
        ("http://bücher.example/", "http://xn--bcher-kva.example/"),
        ("http://[::1]/", "http://[0:0:0:0:0:0:0:1]/"),
    ],
)
def test_equivalent_urls_normalize_to_the_same_url(first: str, second: str) -> None:
    assert normalize_url(first).url == normalize_url(second).url


def test_components_are_reported_for_scope_checks() -> None:
    assert normalize_url("HTTPS://Docs.Example.com:8443/x") == NormalizedUrl(
        url="https://docs.example.com:8443/x", scheme="https", host="docs.example.com", port=8443
    )
    assert normalize_url("http://example.com/").port == 80  # the effective default port
    assert str(normalize_url("https://example.com")) == "https://example.com/"


def test_ipv4_mapped_ipv6_hosts_keep_their_dotted_form() -> None:
    # Pinned: the compressed form comes from Python's ipaddress module (3.13).
    assert normalize_url("http://[::FFFF:192.0.2.1]/").host == "[::ffff:192.0.2.1]"


def test_normalize_host_and_ip_address_of() -> None:
    assert normalize_host("Docs.Example.COM") == "docs.example.com"
    assert normalize_host("[2001:DB8::1]") == "[2001:db8::1]"
    assert str(ip_address_of("[2001:db8::1]")) == "2001:db8::1"
    assert str(ip_address_of("192.0.2.1")) == "192.0.2.1"
    assert ip_address_of("example.com") is None


def test_random_inputs_are_normalized_or_rejected_never_crash() -> None:
    """A fixed-seed fuzz run: every input is either rejected or satisfies the contract."""
    generator = random.Random(20261106)  # noqa: S311 - reproducible test data, not security
    alphabet = "aZ09-._~:/?#[]@!$&'()*+,;=% \t\\\"<>^`{|}ü€\x00\x7f"
    prefixes = ["http://", "https://", "HTTP://", "ftp://", "//", "", "http://[", "http://a:"]
    for _ in range(3000):
        length = generator.randint(0, 40)
        tail = "".join(generator.choice(alphabet) for _ in range(length))
        raw = generator.choice(prefixes) + tail
        try:
            url = normalize_url(raw).url
        except InvalidUrlError:
            continue
        assert DATABASE_URL_FORMAT.fullmatch(url), raw
        assert len(url) <= MAX_URL_BYTES, raw
        assert normalize_url(url).url == url, raw


# --- Reference resolution (RFC 3986, section 5) ---------------------------------------------

RFC_BASE = "http://a/b/c/d;p?q"

# RFC 3986, section 5.4: the normal and abnormal examples, with the results normalized by our
# rules (fragments dropped, "/" for an empty path).
RFC_EXAMPLES = [
    ("g", "http://a/b/c/g"),
    ("./g", "http://a/b/c/g"),
    ("g/", "http://a/b/c/g/"),
    ("/g", "http://a/g"),
    ("//g", "http://g/"),
    ("?y", "http://a/b/c/d;p?y"),
    ("g?y", "http://a/b/c/g?y"),
    ("#s", "http://a/b/c/d;p?q"),
    ("g#s", "http://a/b/c/g"),
    ("g?y#s", "http://a/b/c/g?y"),
    (";x", "http://a/b/c/;x"),
    ("g;x", "http://a/b/c/g;x"),
    ("g;x?y#s", "http://a/b/c/g;x?y"),
    ("", "http://a/b/c/d;p?q"),
    (".", "http://a/b/c/"),
    ("./", "http://a/b/c/"),
    ("..", "http://a/b/"),
    ("../", "http://a/b/"),
    ("../g", "http://a/b/g"),
    ("../..", "http://a/"),
    ("../../", "http://a/"),
    ("../../g", "http://a/g"),
    ("../../../g", "http://a/g"),
    ("../../../../g", "http://a/g"),
    ("/./g", "http://a/g"),
    ("/../g", "http://a/g"),
    ("g.", "http://a/b/c/g."),
    (".g", "http://a/b/c/.g"),
    ("g..", "http://a/b/c/g.."),
    ("..g", "http://a/b/c/..g"),
    ("./../g", "http://a/b/g"),
    ("./g/.", "http://a/b/c/g/"),
    ("g/./h", "http://a/b/c/g/h"),
    ("g/../h", "http://a/b/c/h"),
    ("g;x=1/./y", "http://a/b/c/g;x=1/y"),
    ("g;x=1/../y", "http://a/b/c/y"),
    ("g?y/./x", "http://a/b/c/g?y/./x"),
    ("g?y/../x", "http://a/b/c/g?y/../x"),
    ("g#s/./x", "http://a/b/c/g"),
    ("g#s/../x", "http://a/b/c/g"),
]


@pytest.mark.parametrize(("reference", "expected"), RFC_EXAMPLES)
def test_references_resolve_as_rfc_3986_specifies(reference: str, expected: str) -> None:
    assert join_url(RFC_BASE, reference).url == expected


def test_a_reference_with_its_own_scheme_is_absolute() -> None:
    # Strict parsing (RFC 3986, section 5.2.2): "http:g" is not relative to an http base.
    assert resolve_reference(RFC_BASE, "g:h") == "g:h"
    assert resolve_reference(RFC_BASE, "http:g") == "http:g"
    with pytest.raises(InvalidUrlError, match="must include a host"):
        join_url(RFC_BASE, "http:g")
    with pytest.raises(InvalidUrlError, match="scheme 'g' is not supported"):
        join_url(RFC_BASE, "g:h")


@pytest.mark.parametrize(
    ("base", "reference", "expected"),
    [
        (
            "https://docs.example.com/guide/",
            "  intro.html \n",
            "https://docs.example.com/guide/intro.html",
        ),
        ("https://docs.example.com", "page", "https://docs.example.com/page"),  # empty base path
        ("https://docs.example.com/a/b", "//cdn.example.org/x", "https://cdn.example.org/x"),
        ("https://docs.example.com/a/", "café", "https://docs.example.com/a/caf%C3%A9"),
        (
            "https://docs.example.com/a/",
            "HTTP://Docs.Example.com:443/B",
            "http://docs.example.com:443/B",
        ),
    ],
)
def test_join_url_resolves_then_normalizes(base: str, reference: str, expected: str) -> None:
    assert join_url(base, reference).url == expected


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("/a\tb", "control characters"),
        ("/a\nb", "control characters"),
        (r"\\evil.example\x", "backslash"),
        ("javascript:alert(1)", "scheme 'javascript' is not supported"),
        ("mailto:docs@example.com", "scheme 'mailto' is not supported"),
        ("//user:pw@example.com/", "credentials"),
        ("http://[::1", "closing ']'"),
    ],
)
def test_join_url_rejects_unusable_references(reference: str, message: str) -> None:
    with pytest.raises(InvalidUrlError, match=re.escape(message)):
        join_url("https://docs.example.com/a/", reference)


def test_join_url_needs_an_absolute_base() -> None:
    with pytest.raises(InvalidUrlError, match="base URL must be absolute"):
        join_url("/relative/base", "page")


@pytest.mark.parametrize(
    ("reference", "scheme"),
    [
        ("https://example.com/", "https"),
        ("  JavaScript:void(0)", "javascript"),
        ("mailto:a@example.com", "mailto"),
        ("tel:+1-555-0100", "tel"),
        ("data:text/html,hi", "data"),
        ("/relative", None),
        ("//example.com/", None),
        ("./a:b", None),
        ("#top", None),
        ("", None),
    ],
)
def test_scheme_of(reference: str, scheme: str | None) -> None:
    assert scheme_of(reference) == scheme


# --- Request targets and robots.txt pattern text (Phase 4) -------------------------------------


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("https://example.com", "/"),
        ("https://example.com/a/b?q=1", "/a/b?q=1"),
        ("http://example.com:8080/x?", "/x?"),
        ("https://[2001:db8::1]:8443/p", "/p"),
        ("https://example.com:443/default-port", "/default-port"),
    ],
)
def test_request_target(url: str, target: str) -> None:
    assert request_target(normalize_url(url)) == target


@pytest.mark.parametrize(
    ("text", "normalized"),
    [
        ("/a/b?q=1&r=2", "/a/b?q=1&r=2"),
        ("/café", "/caf%C3%A9"),
        ("/%7e%41%2f", "/~A%2F"),  # unreserved decoded, reserved kept with uppercase digits
        ("/a b", "/a%20b"),
        ("/50%", "/50%25"),  # lenient: a stray "%" is encoded
        ("/%zz", "/%25zz"),
        ("/*$", "/*$"),  # sub-delimiters stay literal
    ],
)
def test_normalize_target_text(text: str, normalized: str) -> None:
    assert normalize_target_text(text) == normalized


def test_target_text_matches_the_path_and_query_of_normalized_urls() -> None:
    for raw in ("https://example.com/café?q=a b", "https://example.com/%7euser/%2F"):
        url = normalize_url(raw)
        assert normalize_target_text(request_target(url)) == request_target(url)
