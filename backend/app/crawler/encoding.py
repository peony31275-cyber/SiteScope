"""Character encoding of HTML documents: detection and decoding that never fails.

The precedence is a simplified form of the WHATWG HTML encoding sniffing algorithm:

1. a byte order mark (UTF-8, UTF-16LE, UTF-16BE);
2. the ``charset`` parameter of the HTTP ``Content-Type`` header;
3. a ``<meta charset>`` or ``<meta http-equiv="Content-Type">`` declaration in the first 1024
   bytes (a declaration of UTF-16 there means UTF-8, as in browsers);
4. UTF-8 if the bytes are valid UTF-8, otherwise windows-1252.

Labels are looked up in the label table of the WHATWG Encoding Standard, and unknown labels are
ignored, as browsers do. Only the encodings in that table can ever be selected, so a document
cannot make the parser use one of Python's other codecs (``utf-7``, ``rot13``, ``punycode``,
``zlib_codec``, ...). Bytes that are invalid in the chosen encoding become U+FFFD, and
``DecodedDocument.errors`` says so.
"""

import re
from dataclasses import dataclass
from typing import Final, Literal

type EncodingSource = Literal["bom", "http", "meta", "default"]

PRESCAN_BYTES: Final = 1024

# WHATWG encoding name -> (Python codec, labels). From https://encoding.spec.whatwg.org/#names-and-labels.
_ENCODINGS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "utf-8": (
        "utf-8",
        ("unicode-1-1-utf-8", "unicode11utf8", "unicode20utf8", "utf-8", "utf8", "x-unicode20utf8"),
    ),
    "ibm866": ("cp866", ("866", "cp866", "csibm866", "ibm866")),
    "iso-8859-2": (
        "iso8859_2",
        (
            "csisolatin2",
            "iso-8859-2",
            "iso-ir-101",
            "iso8859-2",
            "iso88592",
            "iso_8859-2",
            "iso_8859-2:1987",
            "l2",
            "latin2",
        ),
    ),
    "iso-8859-3": (
        "iso8859_3",
        (
            "csisolatin3",
            "iso-8859-3",
            "iso-ir-109",
            "iso8859-3",
            "iso88593",
            "iso_8859-3",
            "iso_8859-3:1988",
            "l3",
            "latin3",
        ),
    ),
    "iso-8859-4": (
        "iso8859_4",
        (
            "csisolatin4",
            "iso-8859-4",
            "iso-ir-110",
            "iso8859-4",
            "iso88594",
            "iso_8859-4",
            "iso_8859-4:1988",
            "l4",
            "latin4",
        ),
    ),
    "iso-8859-5": (
        "iso8859_5",
        (
            "csisolatincyrillic",
            "cyrillic",
            "iso-8859-5",
            "iso-ir-144",
            "iso8859-5",
            "iso88595",
            "iso_8859-5",
            "iso_8859-5:1988",
        ),
    ),
    "iso-8859-6": (
        "iso8859_6",
        (
            "arabic",
            "asmo-708",
            "csiso88596e",
            "csiso88596i",
            "csisolatinarabic",
            "ecma-114",
            "iso-8859-6",
            "iso-8859-6-e",
            "iso-8859-6-i",
            "iso-ir-127",
            "iso8859-6",
            "iso88596",
            "iso_8859-6",
            "iso_8859-6:1987",
        ),
    ),
    "iso-8859-7": (
        "iso8859_7",
        (
            "csisolatingreek",
            "ecma-118",
            "elot_928",
            "greek",
            "greek8",
            "iso-8859-7",
            "iso-ir-126",
            "iso8859-7",
            "iso88597",
            "iso_8859-7",
            "iso_8859-7:1987",
            "sun_eu_greek",
        ),
    ),
    "iso-8859-8": (
        "iso8859_8",
        (
            "csiso88598e",
            "csisolatinhebrew",
            "hebrew",
            "iso-8859-8",
            "iso-8859-8-e",
            "iso-ir-138",
            "iso8859-8",
            "iso88598",
            "iso_8859-8",
            "iso_8859-8:1988",
            "visual",
        ),
    ),
    "iso-8859-8-i": ("iso8859_8", ("csiso88598i", "iso-8859-8-i", "logical")),
    "iso-8859-10": (
        "iso8859_10",
        ("csisolatin6", "iso-8859-10", "iso-ir-157", "iso8859-10", "iso885910", "l6", "latin6"),
    ),
    "iso-8859-13": ("iso8859_13", ("iso-8859-13", "iso8859-13", "iso885913")),
    "iso-8859-14": ("iso8859_14", ("iso-8859-14", "iso8859-14", "iso885914")),
    "iso-8859-15": (
        "iso8859_15",
        ("csisolatin9", "iso-8859-15", "iso8859-15", "iso885915", "iso_8859-15", "l9"),
    ),
    "iso-8859-16": ("iso8859_16", ("iso-8859-16",)),
    "koi8-r": ("koi8_r", ("cskoi8r", "koi", "koi8", "koi8-r", "koi8_r")),
    "koi8-u": ("koi8_u", ("koi8-ru", "koi8-u")),
    "macintosh": ("mac_roman", ("csmacintosh", "mac", "macintosh", "x-mac-roman")),
    "windows-874": (
        "cp874",
        ("dos-874", "iso-8859-11", "iso8859-11", "iso885911", "tis-620", "windows-874"),
    ),
    "windows-1250": ("cp1250", ("cp1250", "windows-1250", "x-cp1250")),
    "windows-1251": ("cp1251", ("cp1251", "windows-1251", "x-cp1251")),
    "windows-1252": (
        "cp1252",
        (
            "ansi_x3.4-1968",
            "ascii",
            "cp1252",
            "cp819",
            "csisolatin1",
            "ibm819",
            "iso-8859-1",
            "iso-ir-100",
            "iso8859-1",
            "iso88591",
            "iso_8859-1",
            "iso_8859-1:1987",
            "l1",
            "latin1",
            "us-ascii",
            "windows-1252",
            "x-cp1252",
        ),
    ),
    "windows-1253": ("cp1253", ("cp1253", "windows-1253", "x-cp1253")),
    "windows-1254": (
        "cp1254",
        (
            "cp1254",
            "csisolatin5",
            "iso-8859-9",
            "iso-ir-148",
            "iso8859-9",
            "iso88599",
            "iso_8859-9",
            "iso_8859-9:1989",
            "l5",
            "latin5",
            "windows-1254",
            "x-cp1254",
        ),
    ),
    "windows-1255": ("cp1255", ("cp1255", "windows-1255", "x-cp1255")),
    "windows-1256": ("cp1256", ("cp1256", "windows-1256", "x-cp1256")),
    "windows-1257": ("cp1257", ("cp1257", "windows-1257", "x-cp1257")),
    "windows-1258": ("cp1258", ("cp1258", "windows-1258", "x-cp1258")),
    "x-mac-cyrillic": ("mac_cyrillic", ("x-mac-cyrillic", "x-mac-ukrainian")),
    # WHATWG decodes GBK with the GB18030 decoder, a superset.
    "gbk": (
        "gb18030",
        (
            "chinese",
            "csgb2312",
            "csiso58gb231280",
            "gb2312",
            "gb_2312",
            "gb_2312-80",
            "gbk",
            "iso-ir-58",
            "x-gbk",
        ),
    ),
    "gb18030": ("gb18030", ("gb18030",)),
    "big5": ("big5hkscs", ("big5", "big5-hkscs", "cn-big5", "csbig5", "x-x-big5")),
    "euc-jp": ("euc_jp", ("cseucpkdfmtjapanese", "euc-jp", "x-euc-jp")),
    "iso-2022-jp": ("iso2022_jp", ("csiso2022jp", "iso-2022-jp")),
    "shift_jis": (
        "cp932",
        (
            "csshiftjis",
            "ms932",
            "ms_kanji",
            "shift-jis",
            "shift_jis",
            "sjis",
            "windows-31j",
            "x-sjis",
        ),
    ),
    "euc-kr": (
        "cp949",
        (
            "cseuckr",
            "csksc56011987",
            "euc-kr",
            "iso-ir-149",
            "korean",
            "ks_c_5601-1987",
            "ks_c_5601-1989",
            "ksc5601",
            "ksc_5601",
            "windows-949",
        ),
    ),
    "utf-16be": ("utf-16-be", ("unicodefffe", "utf-16be")),
    "utf-16le": (
        "utf-16-le",
        ("csunicode", "iso-10646-ucs-2", "ucs-2", "unicode", "unicodefeff", "utf-16", "utf-16le"),
    ),
}
_NAME_BY_LABEL: Final = {
    label: name for name, (_, labels) in _ENCODINGS.items() for label in labels
}
_BOMS: Final = ((b"\xef\xbb\xbf", "utf-8"), (b"\xfe\xff", "utf-16be"), (b"\xff\xfe", "utf-16le"))
# Matches both <meta charset="x"> and the charset parameter of <meta http-equiv content="...">.
_META_CHARSET = re.compile(rb"<meta\b[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:()+-]{1,40})", re.I)


@dataclass(frozen=True, slots=True)
class DecodedDocument:
    text: str
    encoding: str  # WHATWG encoding name, e.g. "utf-8" or "windows-1252"
    source: EncodingSource  # where the encoding came from
    errors: bool  # some bytes were invalid in that encoding and became U+FFFD


def lookup_encoding(label: str | None) -> str | None:
    """The WHATWG encoding name for a label (case-insensitive), or None if it is unknown."""
    if label is None:
        return None
    return _NAME_BY_LABEL.get(label.strip().lower())


def python_codec(encoding: str) -> str:
    """The Python codec that decodes a WHATWG encoding name returned by this module."""
    return _ENCODINGS[encoding][0]


def decode_html(body: bytes, declared_charset: str | None = None) -> DecodedDocument:
    """Decode an HTML document. Never raises: invalid bytes become U+FFFD."""
    for bom, bom_encoding in _BOMS:
        if body.startswith(bom):
            return _decode(body[len(bom) :], bom_encoding, "bom")
    declared = lookup_encoding(declared_charset)
    if declared is not None:
        return _decode(body, declared, "http")
    match = _META_CHARSET.search(body[:PRESCAN_BYTES])
    meta = lookup_encoding(match.group(1).decode("ascii")) if match is not None else None
    if meta is not None:
        if meta in ("utf-16le", "utf-16be"):
            meta = "utf-8"  # a document that can be read as ASCII is not UTF-16
        return _decode(body, meta, "meta")
    try:
        return DecodedDocument(body.decode("utf-8"), "utf-8", "default", errors=False)
    except UnicodeDecodeError:
        return _decode(body, "windows-1252", "default")


def _decode(data: bytes, encoding: str, source: EncodingSource) -> DecodedDocument:
    codec = python_codec(encoding)
    try:
        return DecodedDocument(data.decode(codec), encoding, source, errors=False)
    except UnicodeDecodeError:
        return DecodedDocument(data.decode(codec, errors="replace"), encoding, source, errors=True)
