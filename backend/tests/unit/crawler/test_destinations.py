"""Destination policy (app/crawler/destinations.py): address classes and what may be contacted."""

import ipaddress

import pytest

from app.crawler.destinations import (
    AddressClass,
    BlockedDestinationError,
    DestinationPolicy,
    Origin,
    classify_address,
    embedded_ipv4,
    parse_internal_origin,
)
from app.crawler.urls import normalize_url

C = AddressClass


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        # Global unicast.
        ("93.184.216.34", C.GLOBAL),
        ("8.8.8.8", C.GLOBAL),
        ("2606:4700::1111", C.GLOBAL),
        ("::ffff:8.8.8.8", C.GLOBAL),  # IPv4-mapped: classified as the IPv4 address
        ("64:ff9b::808:808", C.GLOBAL),  # NAT64 of 8.8.8.8
        # Private networks.
        ("10.1.2.3", C.PRIVATE),
        ("172.16.0.1", C.PRIVATE),
        ("172.31.255.255", C.PRIVATE),
        ("192.168.1.1", C.PRIVATE),
        ("fc00::1", C.PRIVATE),
        ("fd12:3456::1", C.PRIVATE),
        ("::ffff:10.0.0.1", C.PRIVATE),
        ("64:ff9b::a00:1", C.PRIVATE),  # NAT64 of 10.0.0.1
        # Loopback.
        ("127.0.0.1", C.LOOPBACK),
        ("127.255.0.9", C.LOOPBACK),
        ("::1", C.LOOPBACK),  # Python also counts ::1 as reserved; loopback wins
        ("::ffff:127.0.0.1", C.LOOPBACK),
        ("64:ff9b::7f00:1", C.LOOPBACK),  # Python calls this global
        # Cloud metadata endpoints, before their generic classes.
        ("169.254.169.254", C.METADATA),
        ("::ffff:169.254.169.254", C.METADATA),
        ("fd00:ec2::254", C.METADATA),
        ("100.100.100.200", C.METADATA),
        ("192.0.0.192", C.METADATA),
        # Never contacted.
        ("169.254.1.1", C.LINK_LOCAL),
        ("fe80::1", C.LINK_LOCAL),
        ("100.64.0.1", C.SHARED),
        ("224.0.0.1", C.MULTICAST),  # Python calls this global
        ("ff02::1", C.MULTICAST),  # Python calls this global
        ("0.0.0.0", C.UNSPECIFIED),  # noqa: S104 - an address under test, not a bind address
        ("::", C.UNSPECIFIED),
        ("0.1.2.3", C.RESERVED),  # "this network"; Linux routes it to the local host
        ("192.0.2.1", C.RESERVED),  # documentation
        ("198.18.0.1", C.RESERVED),  # benchmarking
        ("240.0.0.1", C.RESERVED),
        ("255.255.255.255", C.RESERVED),
        ("2001:db8::1", C.RESERVED),
        ("::127.0.0.1", C.RESERVED),  # IPv4-compatible (deprecated); Python calls it global
        ("fec0::1", C.RESERVED),  # site-local (deprecated); Python calls it global
        ("2002:7f00:1::1", C.RESERVED),  # 6to4
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", C.RESERVED),  # Teredo
        ("64:ff9b:1::1", C.RESERVED),  # local-use NAT64 prefix
        ("100::1", C.RESERVED),  # discard-only
    ],
)
def test_classify_address(address: str, expected: AddressClass) -> None:
    assert classify_address(ipaddress.ip_address(address)) is expected


def test_embedded_ipv4_forms() -> None:
    assert str(embedded_ipv4(ipaddress.IPv6Address("::ffff:192.0.2.9"))) == "192.0.2.9"
    assert str(embedded_ipv4(ipaddress.IPv6Address("64:ff9b::c000:209"))) == "192.0.2.9"
    assert embedded_ipv4(ipaddress.IPv6Address("2606:4700::1111")) is None


@pytest.mark.parametrize(
    ("address_class", "internal", "allow_loopback", "allowed"),
    [
        (C.GLOBAL, False, False, True),
        (C.PRIVATE, False, True, False),
        (C.PRIVATE, True, False, True),
        (C.LOOPBACK, True, False, False),  # production: never
        (C.LOOPBACK, False, True, False),
        (C.LOOPBACK, True, True, True),  # development and test, internal origins only
        (C.METADATA, True, True, False),
        (C.LINK_LOCAL, True, True, False),
        (C.SHARED, True, True, False),
        (C.MULTICAST, True, True, False),
        (C.UNSPECIFIED, True, True, False),
        (C.RESERVED, True, True, False),
    ],
)
def test_allowed_classes(
    address_class: AddressClass, internal: bool, allow_loopback: bool, allowed: bool
) -> None:
    policy = DestinationPolicy(allow_loopback=allow_loopback)

    assert policy.allows(address_class, internal=internal) is allowed


def addresses(*values: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    return [ipaddress.ip_address(value) for value in values]


def test_every_address_must_be_allowed() -> None:
    policy = DestinationPolicy()
    url = normalize_url("https://docs.example.com/")

    policy.check(url, addresses("93.184.216.34", "2606:4700::1111"))
    with pytest.raises(BlockedDestinationError, match="resolves to a private address"):
        policy.check(url, addresses("93.184.216.34", "10.0.0.1"))
    with pytest.raises(BlockedDestinationError, match="did not resolve to any address"):
        policy.check(url, [])


def test_internal_origins_are_exact() -> None:
    policy = DestinationPolicy.from_config(
        internal_origins=["http://demo-site:8080"], allow_loopback=False
    )
    private = addresses("172.18.0.5")

    policy.check(normalize_url("http://demo-site:8080/page"), private)
    for other in (
        "https://demo-site:8080/",
        "http://demo-site/",
        "http://demo-site.evil.test:8080/",
    ):
        with pytest.raises(BlockedDestinationError):
            policy.check(normalize_url(other), private)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("http://demo-site:8080", "http://demo-site:8080"),
        ("HTTP://Demo-Site:80/", "http://demo-site"),
        ("https://localhost:443", "https://localhost"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://[::1]:8000/", "http://[::1]:8000"),
    ],
)
def test_parse_internal_origin(entry: str, expected: str) -> None:
    assert str(parse_internal_origin(entry)) == expected


@pytest.mark.parametrize(
    "entry", ["demo-site:8080", "http://demo-site/path", "http://demo-site/?q", "ftp://demo-site"]
)
def test_invalid_internal_origins(entry: str) -> None:
    with pytest.raises(ValueError, match=r"internal origin|not supported|must be absolute"):
        parse_internal_origin(entry)


def test_origin_of_a_url() -> None:
    assert Origin.of(normalize_url("https://Docs.Example.com:8443/x?y")) == Origin(
        "https", "docs.example.com", 8443
    )
