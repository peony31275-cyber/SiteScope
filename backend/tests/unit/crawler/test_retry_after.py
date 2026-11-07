"""The Retry-After header (RFC 9110, section 10.2.3): delay-seconds and the HTTP-date forms."""

from datetime import UTC, datetime, timedelta

import pytest

from app.crawler.retry_after import parse_http_date, parse_retry_after

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
# The response's own Date header, 2 minutes "behind" the crawler: only the server's clock counts.
SERVER_DATE = "Sat, 07 Nov 2026 11:58:00 GMT"


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("0", 0.0),
        ("1", 1.0),
        ("120", 120.0),
        (" 30 ", 30.0),
        ("0120", 120.0),
        ("9999999999", 9999999999.0),  # parsed; the retry policy's cap decides what it means
    ],
)
def test_delay_seconds(value: str, seconds: float) -> None:
    assert parse_retry_after(value, date=None, now=NOW) == seconds


@pytest.mark.parametrize(
    "value",
    [
        "Sat, 07 Nov 2026 12:00:30 GMT",  # IMF-fixdate
        "Saturday, 07-Nov-26 12:00:30 GMT",  # obsolete RFC 850 form
        "Sat Nov  7 12:00:30 2026",  # obsolete asctime form (no zone: GMT)
    ],
)
def test_http_dates_in_every_form_count_from_now_without_a_date_header(value: str) -> None:
    assert parse_retry_after(value, date=None, now=NOW) == 30.0


def test_an_http_date_counts_from_the_responses_date_header() -> None:
    """The server says "retry at 12:00" and its Date says 11:58: 120 s, whatever our clock says."""
    retry_at = "Sat, 07 Nov 2026 12:00:00 GMT"
    assert parse_retry_after(retry_at, date=SERVER_DATE, now=NOW) == 120.0
    assert parse_retry_after(retry_at, date=SERVER_DATE, now=NOW + timedelta(days=3)) == 120.0


def test_an_invalid_date_header_falls_back_to_now() -> None:
    retry_at = "Sat, 07 Nov 2026 12:00:10 GMT"
    assert parse_retry_after(retry_at, date="yesterday", now=NOW) == 10.0


def test_a_date_in_the_past_means_now() -> None:
    assert parse_retry_after("Sat, 07 Nov 2026 11:00:00 GMT", date=None, now=NOW) == 0.0


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "-5",  # delay-seconds has no sign
        "+5",
        "1.5",  # nor a fraction
        "5s",
        "12345678901",  # more than 10 digits
        "soon",
        "Sat, 99 Nov 2026 12:00:00 GMT",  # no such day
        "Sat, 07 Nov 2026 12:00:00 GMT" + " (please wait)" * 3,  # longer than any HTTP-date
        "\x00",
    ],
)
def test_malformed_values_are_ignored(value: str) -> None:
    assert parse_retry_after(value, date=None, now=NOW) is None


def test_no_header_means_no_delay() -> None:
    assert parse_retry_after(None, date=SERVER_DATE, now=NOW) is None


# The last representable instant, in a zone west of UTC: converting it to UTC leaves the range
# of datetime (year 10000).
BEYOND_THE_RANGE = "Fri, 31 Dec 9999 23:59:59 -0100"


def test_a_date_beyond_the_datetime_range_is_ignored() -> None:
    """Regression (Phase 6 independent review): the conversion to UTC raised OverflowError,
    which escaped the safe fetcher as an unexpected error instead of a FetchError."""
    assert parse_http_date(BEYOND_THE_RANGE) is None
    assert parse_retry_after(BEYOND_THE_RANGE, date=None, now=NOW) is None


def test_a_date_header_beyond_the_range_falls_back_to_now() -> None:
    retry_at = "Sat, 07 Nov 2026 12:00:30 GMT"
    assert parse_retry_after(retry_at, date=BEYOND_THE_RANGE, now=NOW) == 30.0


def test_a_far_but_valid_date_is_a_long_delay() -> None:
    """Parsed, so that the retry policy's cap can refuse it (the failure is then final)."""
    delay = parse_retry_after("Fri, 31 Dec 9999 23:59:59 GMT", date=None, now=NOW)
    assert delay is not None
    assert delay > 250_000_000_000


def test_http_dates_are_utc() -> None:
    parsed = parse_http_date("Sat, 07 Nov 2026 12:00:00 GMT")
    assert parsed == NOW
    assert parsed is not None
    assert parsed.tzinfo is UTC
