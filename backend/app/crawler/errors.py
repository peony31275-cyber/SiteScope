"""Fetch failures, classified for the crawler (docs/architecture.md, section 10.5).

Every failure has a ``FailureKind``. The values are snake_case, so a worker can store them
directly in ``crawl_urls.outcome``. ``FetchError.retryable`` tells the worker whether trying the
same URL again later may succeed:

* retryable: DNS failures that may be temporary, connection failures, timeouts, truncated
  responses, no politeness slot within the maximum wait (``rate_limited``; nothing was sent),
  and HTTP 429, 500, 502, 503 and 504;
* permanent: everything else (policy refusals, TLS errors, redirect problems, other HTTP errors,
  unsupported or oversized content, malformed responses).

Retry scheduling itself (backoff, attempt limits, the retryable statuses actually used) belongs to
the worker's retry policy (``app/workers/retry.py``, Phase 6). An HTTP 429 or 503 response's
``Retry-After`` header is reported as ``FetchError.retry_after`` (seconds).
"""

from enum import StrEnum

from app.crawler.results import Redirect


class FailureKind(StrEnum):
    INVALID_URL = "invalid_url"
    TARGET_NOT_ALLOWED = "target_not_allowed"  # outside the approved crawl targets
    BLOCKED_DESTINATION = "blocked_destination"  # resolves to a forbidden address class
    DNS_NOT_FOUND = "dns_not_found"
    DNS_ERROR = "dns_error"
    CONNECTION_ERROR = "connection_error"  # refused, reset, or closed without a response
    CONNECT_TIMEOUT = "connect_timeout"  # includes the TLS handshake
    READ_TIMEOUT = "read_timeout"
    DEADLINE_EXCEEDED = "deadline_exceeded"  # the whole fetch, all redirects included
    TLS_ERROR = "tls_error"  # certificate verification or handshake failure
    TOO_MANY_REDIRECTS = "too_many_redirects"
    REDIRECT_LOOP = "redirect_loop"
    INVALID_REDIRECT = "invalid_redirect"  # missing or unusable Location header
    HTTP_ERROR = "http_error"  # a response whose status is not 2xx or a followed redirect
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    UNSUPPORTED_CONTENT_ENCODING = "unsupported_content_encoding"
    TOO_LARGE = "too_large"
    TRUNCATED = "truncated"  # the connection closed before the declared body was complete
    MALFORMED_RESPONSE = "malformed_response"
    # The per-host politeness gate gave no request slot within its maximum wait (Phase 4).
    RATE_LIMITED = "rate_limited"


RETRYABLE_KINDS = frozenset(
    {
        FailureKind.DNS_ERROR,
        FailureKind.CONNECTION_ERROR,
        FailureKind.CONNECT_TIMEOUT,
        FailureKind.READ_TIMEOUT,
        FailureKind.DEADLINE_EXCEEDED,
        FailureKind.TRUNCATED,
        FailureKind.RATE_LIMITED,
    }
)
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class RateLimitedError(Exception):
    """A request gate gave no slot within its maximum wait; nothing was sent.

    Raised by ``politeness.PolitenessGate``; the fetcher reports it as
    ``FailureKind.RATE_LIMITED``. The message names the host and the wait, never a URL.
    """


class FetchError(Exception):
    """A fetch that produced no usable response.

    ``message`` is safe to store and show: it names hosts and causes, never full URLs (a query
    string may hold a token) and never resolved addresses.
    """

    def __init__(
        self,
        kind: FailureKind,
        message: str,
        *,
        url: str,
        http_status: int | None = None,
        redirects: tuple[Redirect, ...] = (),
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.url = url  # the URL whose request failed (the last hop of a redirect chain)
        self.http_status = http_status
        self.redirects = redirects  # redirects followed before the failure
        # Seconds a 429 or 503 response's Retry-After asked to wait (app/crawler/retry_after.py).
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        if self.kind is FailureKind.HTTP_ERROR:
            return self.http_status in RETRYABLE_HTTP_STATUSES
        return self.kind in RETRYABLE_KINDS

    def __repr__(self) -> str:
        return f"FetchError({self.kind.value!r}, http_status={self.http_status!r})"
