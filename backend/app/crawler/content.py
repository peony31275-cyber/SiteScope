"""Response content handling for the safe fetcher: media types, content codings, size limits.

``BodyReader`` turns raw body chunks, as they arrive from the network, into the decoded body
while enforcing two limits:

* the *wire* limit, on bytes received, which bounds work for any content coding;
* the *decoded* limit, on bytes after removing gzip or deflate, which stops decompression bombs.

Decompression runs in slices of at most ``DECODE_SLICE`` bytes (``zlib`` ``max_length``), so a
small compressed chunk can never expand into more memory than the decoded limit plus one slice.

A ``ContentPolicy`` says which bodies a fetch accepts. Under the default ``HTML_PAGE`` policy,
exceeding a limit raises ``BodyTooLargeError``: a page is either complete or rejected, never
silently cut short. Only ``ROBOTS_TXT`` truncates instead: RFC 9309 (section 2.5) lets a crawler
parse just the first 500 KiB of a robots.txt file, and the result is flagged as truncated.
"""

import zlib
from dataclasses import dataclass
from typing import Final

HTML_MEDIA_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
# Content codings the fetcher advertises (Accept-Encoding) and decodes.
SUPPORTED_CODINGS: Final = frozenset({"gzip", "x-gzip", "deflate"})
ACCEPT_ENCODING: Final = "gzip, deflate"
DECODE_SLICE: Final = 64 * 1024


# RFC 9309, section 2.5: the parsing limit must be at least 500 kibibytes.
ROBOTS_TXT_MAX_BYTES: Final = 500 * 1024


@dataclass(frozen=True, slots=True)
class ContentPolicy:
    """Which 2xx response bodies a fetch accepts, and how much of them it keeps.

    * ``media_types``: the accepted media types; ``None`` accepts any. A response with another
      type fails with ``unsupported_content_type``.
    * ``max_bytes``: the limit on the decoded body; ``None`` means the fetcher's
      ``max_body_bytes``.
    * ``truncate``: what happens at the limit. ``False`` rejects the body (``too_large``).
      ``True`` keeps the first ``max_bytes`` bytes, stops reading, and flags the result as
      ``truncated``. The limit on bytes received still applies, and the content coding must
      still be supported.
    """

    name: str
    accept: str  # the Accept request header
    media_types: frozenset[str] | None
    max_bytes: int | None = None
    truncate: bool = False


# Crawled pages: HTML only, complete or rejected (the Phase 3 rules, unchanged).
HTML_PAGE: Final = ContentPolicy(
    name="html",
    accept="text/html,application/xhtml+xml;q=0.9",
    media_types=HTML_MEDIA_TYPES,
)
# robots.txt files. RFC 9309 defines them as text/plain, but servers often send a valid file
# with another type or none. The body is only ever read as lines of text, never rendered or
# parsed as HTML, so robots.txt accepts any media type, as other RFC 9309 parsers do.
ROBOTS_TXT: Final = ContentPolicy(
    name="robots.txt",
    accept="text/plain,*/*;q=0.5",
    media_types=None,
    max_bytes=ROBOTS_TXT_MAX_BYTES,
    truncate=True,
)


def max_wire_bytes(max_body_bytes: int) -> int:
    """Limit on received body bytes: coded data is never much larger than its content."""
    return max_body_bytes + max_body_bytes // 64 + 4096


class BodyTooLargeError(Exception):
    """The body exceeds the configured limit (on the wire or after decoding)."""


class ContentDecodingError(Exception):
    """The content coding is unsupported, or the coded data is corrupt or incomplete."""


def parse_media_type(value: str | None) -> tuple[str | None, dict[str, str]]:
    """Split a Content-Type value into its lowercase media type and its parameters.

    Parameter names are lowercased; values keep their case, without surrounding quotes. A
    missing or empty value gives ``(None, {})``.
    """
    if not value:
        return None, {}
    media_type, *raw_parameters = value.split(";")
    parameters: dict[str, str] = {}
    for raw in raw_parameters:
        name, separator, parameter_value = raw.partition("=")
        name = name.strip().lower()
        if separator and name and name not in parameters:
            parameters[name] = parameter_value.strip().strip('"').strip()
    media_type = media_type.strip().lower()
    return media_type or None, parameters


def parse_content_coding(value: str | None) -> str | None:
    """The single content coding to remove, or None for identity.

    Raises ``ContentDecodingError`` for a coding the fetcher did not advertise (``br``, ``zstd``,
    ...) or for more than one coding, which no legitimate server sends for these requests.
    """
    if not value:
        return None
    codings = [token.strip().lower() for token in value.split(",")]
    codings = [coding for coding in codings if coding and coding != "identity"]
    if not codings:
        return None
    if len(codings) > 1 or codings[0] not in SUPPORTED_CODINGS:
        raise ContentDecodingError(f"unsupported content coding: {value.strip()[:64]}")
    return codings[0]


class BodyReader:
    """Accumulates a response body chunk by chunk, decoding it within fixed limits.

    With ``truncate=True``, the decoded body stops at ``max_bytes`` instead of raising:
    ``truncated`` becomes true, later chunks are ignored (the caller should stop reading), and
    ``finish`` accepts coded data that stopped part-way.
    """

    def __init__(
        self, coding: str | None, *, max_bytes: int, max_wire_bytes: int, truncate: bool = False
    ) -> None:
        self._coding = coding
        self._max_bytes = max_bytes
        self._max_wire_bytes = max_wire_bytes
        self._truncate = truncate
        self._body = bytearray()
        self.wire_bytes = 0
        self.truncated = False
        # "deflate" waits for the first two bytes to tell the zlib format from raw DEFLATE.
        self._header = bytearray()
        self._decompressor: zlib._Decompress | None = None
        if coding in ("gzip", "x-gzip"):
            self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)

    def feed(self, chunk: bytes) -> None:
        if self.truncated:
            return
        self.wire_bytes += len(chunk)
        over_wire_limit = self.wire_bytes > self._max_wire_bytes
        if over_wire_limit and not self._truncate:
            raise BodyTooLargeError(f"the body exceeds {self._max_wire_bytes} bytes on the wire")
        self._decode(chunk)
        # A truncating reader decodes first: the chunk that reaches the decoded limit may also
        # cross the wire limit, and is then truncated rather than rejected.
        if over_wire_limit and not self.truncated:
            raise BodyTooLargeError(f"the body exceeds {self._max_wire_bytes} bytes on the wire")

    def _decode(self, chunk: bytes) -> None:
        if self._coding is None:
            self._append(chunk)
            return
        decompressor = self._decompressor
        if decompressor is None:  # deflate, before its first two bytes arrived
            self._header += chunk
            if len(self._header) < 2:
                return
            chunk = bytes(self._header)
            self._decompressor = decompressor = zlib.decompressobj(_deflate_window_bits(chunk))
        try:
            self._inflate(decompressor, chunk)
        except zlib.error as exc:
            raise ContentDecodingError(f"corrupt {self._coding} data") from exc

    def finish(self) -> bytes:
        """The decoded body. Raises if the coded data stopped part-way, unless truncated."""
        if self.truncated:
            return bytes(self._body)
        if self._coding is not None:
            decompressor = self._decompressor
            if decompressor is None:  # deflate with fewer than two bytes
                if self._header:
                    raise ContentDecodingError("the deflate data is incomplete")
                return b""
            if not decompressor.eof:
                raise ContentDecodingError(f"the {self._coding} data is incomplete")
            self._append(decompressor.flush())
        return bytes(self._body)

    def _inflate(self, decompressor: "zlib._Decompress", data: bytes) -> None:
        output = decompressor.decompress(data, DECODE_SLICE)
        while output and not self.truncated:
            self._append(output)
            output = decompressor.decompress(decompressor.unconsumed_tail, DECODE_SLICE)

    def _append(self, data: bytes) -> None:
        if len(self._body) + len(data) > self._max_bytes:
            if not self._truncate:
                raise BodyTooLargeError(f"the decoded body exceeds {self._max_bytes} bytes")
            self._body += data[: self._max_bytes - len(self._body)]
            self.truncated = True
            return
        self._body += data


def _deflate_window_bits(start: bytes) -> int:
    """Window bits for "deflate" data: the zlib format (RFC 1950) if its header is valid, else
    raw DEFLATE (RFC 1951), which some servers send instead."""
    method_and_flags = (start[0] << 8) | start[1]
    is_zlib = start[0] & 0x0F == 8 and start[0] >> 4 <= 7 and method_and_flags % 31 == 0
    return zlib.MAX_WBITS if is_zlib else -zlib.MAX_WBITS
