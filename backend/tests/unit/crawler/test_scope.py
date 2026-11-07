"""Crawl scope (app/crawler/scope.py): approved hosts, the subdomain policy, schemes and ports."""

import re

import pytest

from app.crawler.scope import (
    CrawlTargetPolicy,
    HostPattern,
    HostPatternError,
    TargetNotAllowedError,
    parse_host_pattern,
)
from app.crawler.urls import normalize_url


def policy(
    *hosts: str, schemes: tuple[str, ...] = ("http", "https"), ports: tuple[int, ...] = (80, 443)
) -> CrawlTargetPolicy:
    return CrawlTargetPolicy.from_config(hosts=hosts, schemes=schemes, ports=ports)


def allowed(target_policy: CrawlTargetPolicy, url: str) -> bool:
    try:
        target_policy.check(normalize_url(url))
    except TargetNotAllowedError:
        return False
    return True


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://docs.example.com/guide", True),
        ("HTTPS://DOCS.EXAMPLE.COM/", True),  # compared after normalization
        ("https://example.com/", False),  # the parent domain
        ("https://www.docs.example.com/", False),  # a subdomain
        ("https://evil-docs.example.com/", False),  # a suffix match would accept this
        ("https://docs.example.com.evil.test/", False),  # a prefix match would accept this
        ("https://xdocs.example.com/", False),
        ("https://docs.example.co/", False),
        ("https://docs.example.com./", False),  # trailing dot: a different authority
    ],
)
def test_exact_entries_match_whole_host_names_only(url: str, expected: bool) -> None:
    assert allowed(policy("docs.example.com"), url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://docs.example.org/", True),
        ("https://a.b.example.org/", True),  # any depth
        ("https://example.org/", False),  # not the base domain itself
        ("https://evilexample.org/", False),
        ("https://example.org.evil.test/", False),
        ("https://docs.example.org./", False),
    ],
)
def test_wildcard_entries_match_subdomains_but_not_the_base(url: str, expected: bool) -> None:
    assert allowed(policy("*.example.org"), url) is expected


def test_base_domain_and_subdomains_are_listed_separately() -> None:
    both = policy("example.org", "*.example.org")

    assert allowed(both, "https://example.org/")
    assert allowed(both, "https://docs.example.org/")


def test_internationalized_entries_match_their_a_label_form() -> None:
    target_policy = policy("Bücher.Example")

    assert allowed(target_policy, "https://xn--bcher-kva.example/")
    assert allowed(target_policy, "https://BÜCHER.example/")


def test_ip_entries_match_exactly() -> None:
    target_policy = policy("192.0.2.10", "[2001:db8::1]")

    assert allowed(target_policy, "http://192.0.2.10/")
    assert allowed(target_policy, "http://[2001:DB8:0::1]/")
    assert not allowed(target_policy, "http://192.0.2.11/")


def test_scheme_and_port_restrictions() -> None:
    https_only = policy("docs.example.com", schemes=("https",), ports=(443, 8443))

    assert allowed(https_only, "https://docs.example.com:8443/")
    with pytest.raises(TargetNotAllowedError, match=r"scheme 'http' is not allowed.*https"):
        https_only.check(normalize_url("http://docs.example.com/"))
    with pytest.raises(TargetNotAllowedError, match=r"port 8080 is not allowed.*443, 8443"):
        https_only.check(normalize_url("https://docs.example.com:8080/"))
    # The effective port of https://host/ is 443, which must be allowed too.
    with pytest.raises(TargetNotAllowedError, match="port 443"):
        policy("docs.example.com", ports=(8443,)).check(normalize_url("https://docs.example.com/"))


def test_error_messages_explain_the_rejection() -> None:
    with pytest.raises(TargetNotAllowedError, match="no crawl targets are approved"):
        policy().check(normalize_url("https://docs.example.com/"))
    with pytest.raises(
        TargetNotAllowedError, match=re.escape("host 'other.example' is not an approved")
    ):
        policy("docs.example.com").check(normalize_url("https://other.example/"))
    with pytest.raises(TargetNotAllowedError, match="remove the trailing dot"):
        policy("docs.example.com").check(normalize_url("https://docs.example.com./"))


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("docs.example.com", HostPattern("docs.example.com")),
        ("  Docs.Example.COM ", HostPattern("docs.example.com")),
        ("*.Example.org", HostPattern("example.org", include_subdomains=True)),
        ("bücher.example", HostPattern("xn--bcher-kva.example")),
        ("localhost", HostPattern("localhost")),
        ("127.0.0.1", HostPattern("127.0.0.1")),
        ("[::1]", HostPattern("[::1]")),
        ("demo-site", HostPattern("demo-site")),  # a single-label name, e.g. a container
    ],
)
def test_entries_are_parsed_and_normalized(entry: str, expected: HostPattern) -> None:
    assert parse_host_pattern(entry) == expected


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("", "empty"),
        ("*", "first label"),
        ("*.com", "at least two labels"),
        ("*.localhost", "at least two labels"),
        ("docs.*.example.com", "first label"),
        ("*example.com", "first label"),
        ("**.example.com", "first label"),
        ("https://docs.example.com", "without a scheme"),
        ("docs.example.com/path", "without a scheme"),
        ("user@docs.example.com", "credentials"),
        ("docs.example.com:8080", "without a port"),
        ("[::1]:8080", "without a port"),
        ("docs.example.com.", "trailing dot"),
        ("exa mple.com", "may contain only"),
        ("127.1", "dotted-decimal"),
        ("*.192.0.2.1", "no subdomains"),
        ("169.254.169.254", "never be crawl targets"),  # cloud metadata endpoint
        ("[fe80::1]", "never be crawl targets"),
        ("[::ffff:169.254.169.254]", "never be crawl targets"),
        ("224.0.0.1", "never be crawl targets"),
        ("0.0.0.0", "never be crawl targets"),  # noqa: S104 - a rejected entry, not a bind address
        ("240.0.0.1", "never be crawl targets"),
    ],
)
def test_invalid_entries_are_rejected(entry: str, message: str) -> None:
    with pytest.raises(HostPatternError, match=re.escape(message)):
        parse_host_pattern(entry)


@pytest.mark.parametrize(
    ("entry", "development_only"),
    [
        ("localhost", True),
        ("app.localhost", True),
        ("*.dev.localhost", True),
        ("127.0.0.1", True),
        ("[::1]", True),
        ("192.0.2.10", True),  # every IP literal
        ("docs.example.com", False),
        ("*.example.org", False),
        ("demo-site", False),
        ("localhost.example.com", False),
    ],
)
def test_development_only_entries(entry: str, development_only: bool) -> None:
    assert parse_host_pattern(entry).development_only is development_only


def test_patterns_render_as_their_configuration_form() -> None:
    assert str(parse_host_pattern("*.Example.org")) == "*.example.org"
    assert str(parse_host_pattern("Docs.Example.com")) == "docs.example.com"
