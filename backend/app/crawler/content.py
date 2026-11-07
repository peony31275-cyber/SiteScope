"""Response content handling for the safe fetcher: media types, content codings, size limits.

``BodyReader`` turns raw body chunks, as they arrive from the network, into the decoded body
while enforcing two limits:

* the *wire* limit, on bytes received, which bounds work for any content coding;
* the *decoded* limit, on bytes after removing gzip or deflate, which stops decompression bombs.

Decompression runs in slices of at most ``DECODE_SLICE`` bytes (``zlib`` ``max_length``), so a
small compressed chunk can never expand into more memory than the decoded limit plus one slice.
Exceeding a limit raises ``BodyTooLargeError``: a body is either complete or rejected, never
silently cut short.
"""

import zlib
from typing import Final

HTML_MEDIA_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
# Content codings the fetcher advertises (Accept-Encoding) and decodes.
SUPPORTED_CODINGS: Final = frozenset({"gzip", "x-gzip", "deflate"})
ACCEPT_ENCODING: Final = "gzip, deflate"
DECODE_SLICE: Final = 64 * 1024


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
    """Accumulates a response body chunk by chunk, decoding it within fixed limits."""

    def __init__(self, coding: str | None, *, max_bytes: int, max_wire_bytes: int) -> None:
        self._coding = coding
        self._max_bytes = max_bytes
        self._max_wire_bytes = max_wire_bytes
        self._body = bytearray()
        self.wire_bytes = 0
        # "deflate" waits for the first two bytes to tell the zlib format from raw DEFLATE.
        self._header = bytearray()
        self._decompressor: zlib._Decompress | None = None
        if coding in ("gzip", "x-gzip"):
            self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)

    def feed(self, chunk: bytes) -> None:
        self.wire_bytes += len(chunk)
        if self.wire_bytes > self._max_wire_bytes:
            raise BodyTooLargeError(f"the body exceeds {self._max_wire_bytes} bytes on the wire")
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
        """The complete decoded body. Raises if the coded data stopped part-way."""
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
        while output:
            self._append(output)
            output = decompressor.decompress(decompressor.unconsumed_tail, DECODE_SLICE)

    def _append(self, data: bytes) -> None:
        if len(self._body) + len(data) > self._max_bytes:
            raise BodyTooLargeError(f"the decoded body exceeds {self._max_bytes} bytes")
        self._body += data


def _deflate_window_bits(start: bytes) -> int:
    """Window bits for "deflate" data: the zlib format (RFC 1950) if its header is valid, else
    raw DEFLATE (RFC 1951), which some servers send instead."""
    method_and_flags = (start[0] << 8) | start[1]
    is_zlib = start[0] & 0x0F == 8 and start[0] >> 4 <= 7 and method_and_flags % 31 == 0
    return zlib.MAX_WBITS if is_zlib else -zlib.MAX_WBITS
