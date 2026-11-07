"""Response content handling (app/crawler/content.py): media types, codings, bounded bodies."""

import gzip
import zlib

import pytest

from app.crawler.content import (
    DECODE_SLICE,
    HTML_PAGE,
    ROBOTS_TXT,
    ROBOTS_TXT_MAX_BYTES,
    BodyReader,
    BodyTooLargeError,
    ContentDecodingError,
    max_wire_bytes,
    parse_content_coding,
    parse_media_type,
)

PAGE = b"<p>" + b"hello world " * 500 + b"</p>"


@pytest.mark.parametrize(
    ("value", "media_type", "parameters"),
    [
        ("text/html", "text/html", {}),
        ("TEXT/HTML; Charset=UTF-8", "text/html", {"charset": "UTF-8"}),
        (
            'text/html; charset="windows-1252"; boundary=x',
            "text/html",
            {"charset": "windows-1252", "boundary": "x"},
        ),
        ("text/html;charset=utf-8;charset=latin1", "text/html", {"charset": "utf-8"}),  # first wins
        ("text/html; junk; =x", "text/html", {}),
        ("", None, {}),
        (None, None, {}),
        (" ; charset=utf-8", None, {"charset": "utf-8"}),
    ],
)
def test_parse_media_type(
    value: str | None, media_type: str | None, parameters: dict[str, str]
) -> None:
    assert parse_media_type(value) == (media_type, parameters)


@pytest.mark.parametrize(
    ("value", "coding"),
    [
        (None, None),
        ("", None),
        ("identity", None),
        ("gzip", "gzip"),
        ("X-GZIP", "x-gzip"),
        ("deflate", "deflate"),
        ("identity, gzip", "gzip"),
    ],
)
def test_supported_content_codings(value: str | None, coding: str | None) -> None:
    assert parse_content_coding(value) == coding


@pytest.mark.parametrize("value", ["br", "zstd", "compress", "gzip, gzip", "gzip, deflate"])
def test_unsupported_content_codings(value: str) -> None:
    with pytest.raises(ContentDecodingError, match="unsupported content coding"):
        parse_content_coding(value)


def read(coding: str | None, data: bytes, *, max_bytes: int = 1 << 20, chunk: int = 1000) -> bytes:
    reader = BodyReader(coding, max_bytes=max_bytes, max_wire_bytes=max_bytes * 2)
    for start in range(0, len(data), chunk):
        reader.feed(data[start : start + chunk])
    return reader.finish()


@pytest.mark.parametrize(
    ("coding", "encoded"),
    [
        (None, PAGE),
        ("gzip", gzip.compress(PAGE)),
        ("x-gzip", gzip.compress(PAGE)),
        ("deflate", zlib.compress(PAGE)),
        ("deflate", zlib.compress(PAGE)[2:-4]),  # raw DEFLATE without the zlib wrapper
    ],
)
@pytest.mark.parametrize("chunk", [1, 7, 4096])
def test_bodies_are_decoded_in_any_chunking(coding: str | None, encoded: bytes, chunk: int) -> None:
    assert read(coding, encoded, chunk=chunk) == PAGE


def test_the_decoded_limit_stops_a_decompression_bomb_early() -> None:
    bomb = gzip.compress(b"\0" * 20_000_000)  # about 20 kB on the wire
    reader = BodyReader("gzip", max_bytes=100_000, max_wire_bytes=10_000_000)

    with pytest.raises(BodyTooLargeError, match="decoded body exceeds 100000 bytes"):
        reader.feed(bomb)
    # It stopped after at most one decompression slice past the limit, not after 20 MB.
    assert reader.wire_bytes == len(bomb)


def test_decompression_never_produces_more_than_one_slice_beyond_the_limit() -> None:
    reader = BodyReader("gzip", max_bytes=DECODE_SLICE * 3, max_wire_bytes=1 << 30)
    produced: list[int] = []
    original_append = reader._append

    def record(data: bytes) -> None:
        produced.append(len(data))
        original_append(data)

    reader._append = record  # type: ignore[method-assign]  # test-only instrumentation
    with pytest.raises(BodyTooLargeError):
        reader.feed(gzip.compress(b"\0" * 10_000_000))
    assert max(produced) <= DECODE_SLICE


def test_the_wire_limit_applies_before_decoding() -> None:
    reader = BodyReader(None, max_bytes=1 << 20, max_wire_bytes=10)

    reader.feed(b"0123456789")
    with pytest.raises(BodyTooLargeError, match="on the wire"):
        reader.feed(b"x")


def test_an_identity_body_over_the_limit_is_rejected_not_truncated() -> None:
    with pytest.raises(BodyTooLargeError):
        read(None, b"x" * 101, max_bytes=100)
    assert read(None, b"x" * 100, max_bytes=100) == b"x" * 100


@pytest.mark.parametrize(
    ("coding", "data", "message"),
    [
        ("gzip", b"definitely not gzip", "corrupt gzip data"),
        ("deflate", b"\xff\xff\xff\xff not deflate", "corrupt deflate data"),
        ("gzip", gzip.compress(PAGE)[:-10], "incomplete"),  # cut before the gzip trailer
        ("deflate", zlib.compress(PAGE)[:50], "incomplete"),
    ],
)
def test_corrupt_or_incomplete_coded_data_is_an_error(
    coding: str, data: bytes, message: str
) -> None:
    with pytest.raises(ContentDecodingError, match=message):
        read(coding, data)


# --- Content policies (Phase 4) -----------------------------------------------------------------


def test_the_html_policy_is_the_phase_3_rule_set() -> None:
    assert HTML_PAGE.media_types == frozenset({"text/html", "application/xhtml+xml"})
    assert (HTML_PAGE.max_bytes, HTML_PAGE.truncate) == (None, False)


def test_the_robots_policy_truncates_at_500_kib() -> None:
    assert ROBOTS_TXT_MAX_BYTES == 500 * 1024  # RFC 9309, section 2.5: at least 500 KiB
    assert (ROBOTS_TXT.media_types, ROBOTS_TXT.max_bytes) == (None, ROBOTS_TXT_MAX_BYTES)
    assert ROBOTS_TXT.truncate is True


@pytest.mark.parametrize("chunk", [1, 7, 1000, 10_000])
def test_a_truncating_reader_keeps_exactly_the_limit(chunk: int) -> None:
    body = bytes(range(256)) * 40  # 10 240 bytes
    reader = BodyReader(None, max_bytes=5000, max_wire_bytes=6000, truncate=True)
    for start in range(0, len(body), chunk):
        reader.feed(body[start : start + chunk])
        if reader.truncated:
            break
    assert reader.truncated
    assert reader.finish() == body[:5000]
    reader.feed(b"more")  # ignored once truncated
    assert reader.finish() == body[:5000]


def test_a_truncating_reader_accepts_compressed_data_that_stops_early() -> None:
    body = b"Disallow: /x\n" * 100_000
    coded = gzip.compress(body)
    reader = BodyReader("gzip", max_bytes=1000, max_wire_bytes=10_000_000, truncate=True)
    reader.feed(coded[: len(coded) // 2])
    assert reader.truncated
    assert reader.finish() == body[:1000]  # no "incomplete gzip data" error


def test_a_body_within_the_limit_is_not_truncated() -> None:
    reader = BodyReader(None, max_bytes=100, max_wire_bytes=200, truncate=True)
    reader.feed(b"x" * 100)
    assert not reader.truncated
    assert reader.finish() == b"x" * 100


@pytest.mark.parametrize("coding", [None, "gzip"])
def test_the_chunk_that_reaches_the_limit_is_truncated_not_rejected(coding: str | None) -> None:
    # Regression: a 64 KiB network chunk can cross the decoded limit and the wire limit at
    # once (identity coding, or incompressible gzip). It must be truncated, not rejected.
    body = bytes(range(256)) * 400  # 102 400 bytes, incompressible enough
    coded = gzip.compress(body) if coding else body
    reader = BodyReader(
        coding, max_bytes=50_000, max_wire_bytes=max_wire_bytes(50_000), truncate=True
    )
    reader.feed(coded[:49_000])
    reader.feed(coded[49_000:113_000])  # crosses both limits
    assert reader.truncated
    assert reader.finish() == body[:50_000]


def test_the_wire_limit_still_applies_when_truncating() -> None:
    # Received bytes are limited separately from decoded bytes, truncation or not.
    reader = BodyReader(None, max_bytes=10_000, max_wire_bytes=10, truncate=True)
    with pytest.raises(BodyTooLargeError):
        reader.feed(b"x" * 11)


def test_max_wire_bytes() -> None:
    assert max_wire_bytes(64_000) == 64_000 + 1000 + 4096
