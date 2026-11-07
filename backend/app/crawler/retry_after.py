"""The Retry-After response header (RFC 9110, section 10.2.3), as a delay in seconds.

``Retry-After = HTTP-date / delay-seconds``. The fetcher reads it on HTTP 429 and 503 responses
and the retry policy waits at least that long (``app/workers/retry.py``), within a configured
cap.

* ``delay-seconds`` is a non-negative integer of ASCII digits. Values of more than 10 digits are
  rejected as unreasonable.
* An ``HTTP-date`` (IMF-fixdate, or the obsolete RFC 850 and asctime forms) counts from the
  response's own ``Date`` header when that is valid, so the server's clock is compared with the
  server's clock, and the crawler's clock skew does not matter. Without a valid ``Date``, it
  counts from ``now``. A date in the past means "now" (0 s).
* Anything else is ignored: the caller falls back to its own backoff.
"""

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

_DELAY_SECONDS = re.compile(r"[0-9]{1,10}")
_MAX_DATE_LENGTH = 64


def parse_retry_after(value: str | None, *, date: str | None, now: datetime) -> float | None:
    """Seconds to wait before retrying, or None if the header is absent or malformed."""
    if value is None:
        return None
    text = value.strip()
    if _DELAY_SECONDS.fullmatch(text):
        return float(int(text))
    retry_at = parse_http_date(text)
    if retry_at is None:
        return None
    reference = parse_http_date(date) if date is not None else None
    return max(0.0, (retry_at - (reference or now)).total_seconds())


def parse_http_date(value: str) -> datetime | None:
    """An HTTP-date as an aware UTC datetime, or None (HTTP dates are always GMT)."""
    text = value.strip()
    if not text or len(text) > _MAX_DATE_LENGTH:
        return None
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:  # the asctime form has no zone: it is GMT
            parsed = parsed.replace(tzinfo=UTC)
        # Converting can leave datetime's range: "Fri, 31 Dec 9999 23:59:59 -0100" is in year
        # 10000 in UTC (OverflowError; found by the Phase 6 independent review).
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
