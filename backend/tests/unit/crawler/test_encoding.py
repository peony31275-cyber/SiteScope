"""Character encoding of HTML documents (app/crawler/encoding.py)."""

import codecs

import pytest

from app.crawler import encoding as encoding_module
from app.crawler.encoding import decode_html, lookup_encoding, python_codec

TEXT = "Grüße, 東京, ½ €"


def test_every_supported_encoding_has_a_python_codec() -> None:
    for name in encoding_module._ENCODINGS:
        codecs.lookup(python_codec(name))


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("UTF-8", "utf-8"),
        (" utf8 ", "utf-8"),
        ("latin1", "windows-1252"),  # the WHATWG mapping, not ISO-8859-1
        ("ISO-8859-1", "windows-1252"),
        ("us-ascii", "windows-1252"),
        ("Shift_JIS", "shift_jis"),
        ("gb2312", "gbk"),
        ("utf-16", "utf-16le"),
        ("utf-7", None),  # not a web encoding: ignored
        ("rot13", None),
        ("zlib_codec", None),
        ("punycode", None),
        ("x-no-such-encoding", None),
        (None, None),
    ],
)
def test_lookup_encoding(label: str | None, name: str | None) -> None:
    assert lookup_encoding(label) == name


def test_a_byte_order_mark_wins_over_every_declaration() -> None:
    utf16 = codecs.BOM_UTF16_LE + TEXT.encode("utf-16-le")
    decoded = decode_html(utf16, "windows-1252")

    assert (decoded.text, decoded.encoding, decoded.source) == (TEXT, "utf-16le", "bom")
    assert decode_html(codecs.BOM_UTF8 + TEXT.encode()).text == TEXT  # the BOM is removed
    assert decode_html(codecs.BOM_UTF16_BE + TEXT.encode("utf-16-be")).encoding == "utf-16be"


def test_the_http_charset_wins_over_a_meta_declaration() -> None:
    body = '<meta charset="utf-8"><p>caf\xe9'.encode("cp1252")

    decoded = decode_html(body, "windows-1252")

    assert (decoded.encoding, decoded.source, decoded.errors) == ("windows-1252", "http", False)
    assert decoded.text.endswith("café")


@pytest.mark.parametrize(
    "meta",
    [
        '<meta charset="windows-1252">',
        "<META CHARSET=windows-1252>",
        '<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">',
    ],
)
def test_a_meta_declaration_in_the_first_kilobyte_is_used(meta: str) -> None:
    body = f"<html><head>{meta}</head><body>caf\xe9</body></html>".encode("cp1252")

    decoded = decode_html(body)

    assert (decoded.encoding, decoded.source) == ("windows-1252", "meta")
    assert "café" in decoded.text


def test_a_meta_declaration_after_the_first_kilobyte_is_ignored() -> None:
    body = (" " * 1100 + '<meta charset="windows-1252"><p>café').encode("utf-8")

    decoded = decode_html(body)

    assert (decoded.encoding, decoded.source) == ("utf-8", "default")


def test_a_meta_declaration_of_utf16_means_utf8() -> None:
    decoded = decode_html('<meta charset="utf-16"><p>café'.encode())

    assert (decoded.encoding, decoded.source, decoded.text[-4:]) == ("utf-8", "meta", "café")


def test_unknown_declarations_are_ignored() -> None:
    decoded = decode_html('<meta charset="utf-7"><p>café'.encode(), "x-unknown")

    assert (decoded.encoding, decoded.source) == ("utf-8", "default")


def test_undeclared_documents_are_utf8_if_valid_else_windows_1252() -> None:
    assert decode_html(TEXT.encode()).encoding == "utf-8"
    legacy = decode_html("café €".encode("cp1252"))  # é is 0xE9, € is 0x80

    assert (legacy.encoding, legacy.text, legacy.errors) == ("windows-1252", "café €", False)


def test_invalid_bytes_become_replacement_characters_and_are_reported() -> None:
    decoded = decode_html(b"<p>ok \xff\xfe broken \xc3", "utf-8")

    assert decoded.errors
    assert decoded.text.startswith("<p>ok ")
    assert "�" in decoded.text


def test_an_odd_length_utf16_body_does_not_fail() -> None:
    decoded = decode_html(codecs.BOM_UTF16_LE + b"a\x00b")

    assert decoded.text.startswith("a")
    assert decoded.errors
