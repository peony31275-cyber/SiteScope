"""DNS resolution for the safe fetcher.

The fetcher resolves each host exactly once per request hop, validates every answer, and then
connects only to a validated address. No other component resolves the name again, so a DNS
answer that changes between the check and the connection (DNS rebinding) has no effect.

``Resolver`` is a protocol so that tests can script DNS answers, including answers that change
between lookups, with a test double instead of real DNS.
"""

import asyncio
import ipaddress
import socket
from typing import Protocol

from app.crawler.destinations import IPAddress

# getaddrinfo errors that mean "this name has no address" rather than "DNS is unavailable".
# Windows reports WSAHOST_NOT_FOUND (11001) and WSANO_DATA (11004).
_NOT_FOUND_ERRORS = frozenset(
    code
    for code in (
        getattr(socket, "EAI_NONAME", None),
        getattr(socket, "EAI_NODATA", None),
        11001,
        11004,
    )
    if code is not None
)
# An answer with more addresses than this is cut off; every address that is kept is validated.
MAX_ADDRESSES = 16


class ResolutionError(Exception):
    """The host could not be resolved. ``temporary`` failures may succeed if retried."""

    def __init__(self, message: str, *, temporary: bool) -> None:
        super().__init__(message)
        self.temporary = temporary


class Resolver(Protocol):
    async def resolve(self, host: str) -> list[IPAddress]:
        """Return the host's addresses, in preference order, or raise ``ResolutionError``."""
        ...


class SystemResolver:
    """The operating system's resolver (``getaddrinfo``), run in the event loop's thread pool."""

    async def resolve(self, host: str) -> list[IPAddress]:
        loop = asyncio.get_running_loop()
        try:
            answers = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            if exc.errno in _NOT_FOUND_ERRORS:
                raise ResolutionError(f"{host} does not exist", temporary=False) from exc
            raise ResolutionError(f"{host} could not be resolved", temporary=True) from exc
        except OSError as exc:
            raise ResolutionError(f"{host} could not be resolved", temporary=True) from exc
        addresses: list[IPAddress] = []
        for *_, sockaddr in answers:
            address = ipaddress.ip_address(sockaddr[0])
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise ResolutionError(f"{host} has no addresses", temporary=False)
        return addresses[:MAX_ADDRESSES]
