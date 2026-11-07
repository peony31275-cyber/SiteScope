"""Crawl scope: which URLs this deployment approves as crawl targets.

The checks are purely syntactic, with no DNS lookups and no network access, so they are safe to
run on every API request.

**This is not a complete SSRF defense.** A check here decides only whether a normalized URL names
an approved host, scheme and port. An approved host name can still resolve to any address,
including a private or loopback one, and its DNS answers can change between a check and a
connection (DNS rebinding). Before every request, the Phase 3 safe fetcher must resolve the host
itself, reject blocked address classes, and pin the connection to the validated address
(ADR-0006; docs/architecture.md, section 12.3).

Approved hosts (``SITESCOPE_CRAWL_ALLOWED_HOSTS``) are compared as whole normalized host names,
never as substrings:

* ``docs.example.com`` approves exactly that host. It does not approve ``www.docs.example.com``,
  ``docs.example.com.evil.test``, ``evil-docs.example.com`` or ``docs.example.com.``.
* ``*.example.com`` approves every subdomain of example.com, at any depth (``a.example.com``,
  ``a.b.example.com``), but not ``example.com`` itself: list that separately if wanted. The base
  domain needs at least two labels, so ``*.com`` is refused.
* An IP literal (``192.0.2.10``, ``[2001:db8::1]``) approves exactly that address.

Some entries are development-only: ``localhost``, names under ``.localhost``, and every IP
literal. They are refused when ``SITESCOPE_ENVIRONMENT`` is ``production`` (see
``app/core/config.py``), so a public deployment can approve only DNS names. Link-local
(including the cloud metadata address 169.254.169.254), multicast, unspecified and reserved
addresses are refused in every environment.

The scheme and the effective port (explicit, or the scheme's default) must also be in the
configured ``SITESCOPE_CRAWL_ALLOWED_SCHEMES`` and ``SITESCOPE_CRAWL_ALLOWED_PORTS``.
"""

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

from app.crawler.urls import InvalidUrlError, NormalizedUrl, ip_address_of, normalize_host


class TargetNotAllowedError(ValueError):
    """A URL is outside the approved crawl targets. The message is safe to show users."""


class HostPatternError(ValueError):
    """An approved-host entry in the configuration is invalid."""


@dataclass(frozen=True, slots=True)
class HostPattern:
    """One approved-host entry: an exact host, or the subdomains of a base domain."""

    host: str
    include_subdomains: bool = False

    def matches(self, host: str) -> bool:
        """Whether a *normalized* host is approved by this entry."""
        if self.include_subdomains:
            return host.endswith("." + self.host)
        return host == self.host

    @property
    def development_only(self) -> bool:
        """Local targets that only development and test deployments may approve."""
        return (
            self.host == "localhost"
            or self.host.endswith(".localhost")
            or ip_address_of(self.host) is not None
        )

    def __str__(self) -> str:
        return f"*.{self.host}" if self.include_subdomains else self.host


def parse_host_pattern(entry: str) -> HostPattern:
    """Parse and normalize one approved-host entry, or raise ``HostPatternError``."""
    text = entry.strip()
    if not text:
        raise HostPatternError("an approved-host entry is empty")
    include_subdomains = text.startswith("*.")
    base = text.removeprefix("*.")
    if "*" in base:
        raise HostPatternError(
            f"{entry!r}: '*' is allowed only as the whole first label, as in '*.example.com'"
        )
    if any(char in base for char in "/?#@"):
        raise HostPatternError(
            f"{entry!r}: list host names only, without a scheme, path or credentials"
        )
    if base.startswith("[") != base.endswith("]") or (not base.startswith("[") and ":" in base):
        raise HostPatternError(
            f"{entry!r}: list host names without a port; ports are configured separately"
        )
    if base.endswith("."):
        raise HostPatternError(f"{entry!r}: remove the trailing dot")
    try:
        host = normalize_host(base)
    except InvalidUrlError as exc:
        raise HostPatternError(f"{entry!r}: {exc}") from None

    address = ip_address_of(host)
    if address is not None:
        if include_subdomains:
            raise HostPatternError(f"{entry!r}: an IP address has no subdomains")
        if _never_a_target(address):
            raise HostPatternError(
                f"{entry!r}: link-local, multicast, unspecified and reserved addresses can"
                " never be crawl targets"
            )
    elif include_subdomains and "." not in host:
        raise HostPatternError(
            f"{entry!r}: a wildcard needs a base domain with at least two labels,"
            " as in '*.example.com'"
        )
    return HostPattern(host, include_subdomains)


@dataclass(frozen=True, slots=True)
class CrawlTargetPolicy:
    """The deployment's approved crawl targets: host patterns, schemes and ports."""

    hosts: tuple[HostPattern, ...]
    schemes: frozenset[str]
    ports: frozenset[int]

    @classmethod
    def from_config(
        cls, *, hosts: Iterable[str], schemes: Iterable[str], ports: Iterable[int]
    ) -> Self:
        return cls(
            hosts=tuple(parse_host_pattern(entry) for entry in hosts),
            schemes=frozenset(schemes),
            ports=frozenset(ports),
        )

    def allows_host(self, host: str) -> bool:
        return any(pattern.matches(host) for pattern in self.hosts)

    def check(self, url: NormalizedUrl) -> None:
        """Raise ``TargetNotAllowedError`` unless the URL is an approved crawl target."""
        if not self.hosts:
            raise TargetNotAllowedError("no crawl targets are approved on this server")
        if not self.allows_host(url.host):
            message = f"host '{url.host}' is not an approved crawl target"
            if url.host.endswith(".") and self.allows_host(url.host[:-1]):
                message += " (remove the trailing dot)"
            raise TargetNotAllowedError(message)
        if url.scheme not in self.schemes:
            allowed = ", ".join(sorted(self.schemes))
            raise TargetNotAllowedError(
                f"scheme '{url.scheme}' is not allowed for crawl targets (allowed: {allowed})"
            )
        if url.port not in self.ports:
            allowed = ", ".join(str(port) for port in sorted(self.ports))
            raise TargetNotAllowedError(
                f"port {url.port} is not allowed for crawl targets (allowed: {allowed})"
            )


def _never_a_target(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        # Development-only, not "never". Checked first because Python counts ::1 as reserved
        # (it lies in the IETF-reserved block ::/8).
        return False
    return (
        address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )
