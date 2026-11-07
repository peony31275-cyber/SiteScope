"""Destination policy: which IP addresses the safe fetcher may connect to (ADR-0006, ADR-0013).

This is the address-level half of the SSRF defense. The crawl scope (``scope.py``) decides which
host *names* are approved. This module decides whether the addresses those names resolve to may
be contacted. The fetcher applies it to every DNS answer, and then connects only to an address
that passed it (``fetcher.PinnedNetworkBackend``).

Every address is first classified. IPv4 addresses embedded in IPv6 forms that a connection
would actually reach (IPv4-mapped ``::ffff:a.b.c.d``, NAT64 ``64:ff9b::/96``) are unwrapped and
classified as the IPv4 address. Python's ``is_global`` alone is not enough: it counts multicast,
NAT64 forms of loopback, IPv4-compatible forms (``::127.0.0.1``) and deprecated site-local
addresses as global, so the classes are tested explicitly, in this order:

========================  =======================================================  ============
Class                     Addresses                                                Allowed
========================  =======================================================  ============
``metadata``              cloud metadata endpoints (169.254.169.254, fd00:ec2::254, never
                          100.100.100.200, 192.0.0.192)
``unspecified``           0.0.0.0, ::                                              never
``loopback``              127.0.0.0/8, ::1                                         internal
                                                                                   origins, not
                                                                                   in production
``link_local``            169.254.0.0/16, fe80::/10                                never
``multicast``             224.0.0.0/4, ff00::/8                                    never
``shared``                100.64.0.0/10 (carrier-grade NAT)                        never
``private``               10/8, 172.16/12, 192.168/16, fc00::/7                    internal
                                                                                   origins
``global``                publicly routable unicast                                yes
``reserved``              everything else (documentation, benchmarking, 240/4,     never
                          6to4, Teredo, site-local, ...)
========================  =======================================================  ============

*Internal origins* (``SITESCOPE_FETCH_INTERNAL_ORIGINS``) are exact origins, such as the
self-hosted demo site, that may resolve to private addresses. Outside production they may also
resolve to loopback addresses, for local fixture servers. A host whose DNS answer contains *any*
disallowed address is refused as a whole (fail closed), even if other answers are global.
"""

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from app.crawler.urls import InvalidUrlError, NormalizedUrl, normalize_url

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class AddressClass(StrEnum):
    GLOBAL = "global"
    PRIVATE = "private"
    LOOPBACK = "loopback"
    LINK_LOCAL = "link_local"
    METADATA = "metadata"
    SHARED = "shared"
    MULTICAST = "multicast"
    UNSPECIFIED = "unspecified"
    RESERVED = "reserved"


METADATA_ADDRESSES: frozenset[IPAddress] = frozenset(
    ipaddress.ip_address(address)
    for address in (
        "169.254.169.254",  # AWS, Azure, GCP, DigitalOcean, OpenStack, ...
        "fd00:ec2::254",  # AWS IPv6 instance metadata
        "100.100.100.200",  # Alibaba Cloud
        "192.0.0.192",  # Oracle Cloud
    )
)
_SHARED = ipaddress.IPv4Network("100.64.0.0/10")
_PRIVATE = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv6Network("fc00::/7"),
)
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


class BlockedDestinationError(Exception):
    """A host resolves to an address the fetcher must not contact. Safe to show users."""


def embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address that a connection to this IPv6 address actually reaches, if any."""
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def classify_address(address: IPAddress) -> AddressClass:
    """The destination class of an IP address (see the table in the module docstring)."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = embedded_ipv4(address)
        if embedded is not None:
            return classify_address(embedded)
    if address in METADATA_ADDRESSES:
        return AddressClass.METADATA
    if address.is_unspecified:
        return AddressClass.UNSPECIFIED
    if address.is_loopback:
        return AddressClass.LOOPBACK
    if address.is_link_local:
        return AddressClass.LINK_LOCAL
    if address.is_multicast:
        return AddressClass.MULTICAST
    if isinstance(address, ipaddress.IPv4Address) and address in _SHARED:
        return AddressClass.SHARED
    if any(address in network for network in _PRIVATE):
        return AddressClass.PRIVATE
    site_local = isinstance(address, ipaddress.IPv6Address) and address.is_site_local
    if address.is_global and not address.is_reserved and not site_local:
        return AddressClass.GLOBAL
    return AddressClass.RESERVED


@dataclass(frozen=True, slots=True)
class Origin:
    """A normalized origin: scheme, host and effective port."""

    scheme: str
    host: str
    port: int

    @classmethod
    def of(cls, url: NormalizedUrl) -> Self:
        return cls(url.scheme, url.host, url.port)

    def __str__(self) -> str:
        default = 443 if self.scheme == "https" else 80
        suffix = "" if self.port == default else f":{self.port}"
        return f"{self.scheme}://{self.host}{suffix}"


def parse_internal_origin(entry: str) -> Origin:
    """Parse one internal-origin entry (``http://demo-site:8080``), or raise ValueError."""
    try:
        url = normalize_url(entry)
    except InvalidUrlError as exc:
        raise ValueError(f"{entry!r}: {exc}") from None
    origin = Origin.of(url)
    if url.url != f"{origin}/":
        raise ValueError(f"{entry!r}: an internal origin has no path, query or fragment")
    return origin


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    """Which address classes the fetcher may connect to, per origin."""

    internal_origins: frozenset[Origin] = frozenset()
    # Loopback addresses for internal origins; never set in production (app/core/config.py).
    allow_loopback: bool = False

    @classmethod
    def from_config(cls, *, internal_origins: Iterable[str], allow_loopback: bool) -> Self:
        return cls(
            internal_origins=frozenset(parse_internal_origin(entry) for entry in internal_origins),
            allow_loopback=allow_loopback,
        )

    def check(self, url: NormalizedUrl, addresses: Sequence[IPAddress]) -> None:
        """Raise ``BlockedDestinationError`` unless *every* address may be contacted."""
        if not addresses:
            raise BlockedDestinationError(f"{url.host} did not resolve to any address")
        internal = Origin.of(url) in self.internal_origins
        for address in addresses:
            address_class = classify_address(address)
            if not self.allows(address_class, internal=internal):
                raise BlockedDestinationError(
                    f"{url.host} resolves to a {address_class.value.replace('_', '-')} address,"
                    " which crawl requests may not reach"
                )

    def allows(self, address_class: AddressClass, *, internal: bool) -> bool:
        if address_class is AddressClass.GLOBAL:
            return True
        if address_class is AddressClass.PRIVATE:
            return internal
        if address_class is AddressClass.LOOPBACK:
            return internal and self.allow_loopback
        return False
