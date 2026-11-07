"""Results of the safe fetcher: what a worker receives for one fetch (the success contract).

Failures are not results: the fetcher raises ``errors.FetchError`` for them.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Redirect:
    """One redirect that the fetcher validated and followed."""

    url: str  # the URL that answered with the redirect
    status_code: int  # 301, 302, 303, 307 or 308
    location: str  # the normalized redirect target


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A complete HTTP response from an approved, validated destination.

    Two shapes exist:

    * a 2xx HTML document: ``body`` holds the complete body, with any content coding (gzip,
      deflate) removed, and never more than the configured maximum;
    * a redirect, returned only when the caller asked not to follow it (``max_redirects=0``):
      ``redirect_location`` holds the normalized target and ``body`` is empty. The target has
      not been checked against the crawl scope or resolved, because nothing connected to it.
    """

    requested_url: str  # the normalized URL the caller asked for
    url: str  # the normalized URL that produced this response (after followed redirects)
    status_code: int
    content_type: str | None  # media type, lowercase, without parameters
    charset: str | None  # the charset parameter of Content-Type, exactly as sent
    headers: Mapping[str, str] = field(repr=False)  # selected response headers, lowercase names
    body: bytes = field(repr=False)
    redirects: tuple[Redirect, ...]  # redirects followed, in order
    redirect_location: str | None  # set only for a redirect that was not followed
    remote_address: str  # the IP address the final response actually came from
    wire_bytes: int  # body bytes received, before content decoding
    elapsed_seconds: float  # the whole fetch: resolution, redirects and body
