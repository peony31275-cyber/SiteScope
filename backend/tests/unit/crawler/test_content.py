"""Response content handling (app/crawler/content.py): media types, codings, bounded bodies."""

import gzip
import zlib

import pytest

from app.crawler.content import (
    DECODE_SLICE,
    BodyReader,
    BodyTooLargeError,
    ContentDecodingError,
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
