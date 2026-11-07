"""URL normalization: the canonical form of every URL that SiteScope stores or compares.

Pure functions: no network, no DNS, no database. The API normalizes seed URLs with them. The HTML
parser and the safe fetcher resolve links and redirect targets against their document with
``join_url`` (RFC 3986, section 5), which normalizes the result with the same rules.

**Policy** (also in docs/architecture.md, section 8.1). Two inputs normalize to the same URL only
when the URI standards define them as equivalent (RFC 3986 sections 6.2.2 and 6.2.3; RFC 9110
section 4.2.3 for http and https), or when an invalid character is replaced by its only valid
percent-encoding. Everything that could be meaningful is kept.

Changed, because the forms are equivalent:

* the scheme and the host are lowercased;
* an internationalized host is converted to IDNA A-labels (IDNA 2008 with UTS #46 mapping, the
  behavior of current browsers), e.g. ``bücher.example`` -> ``xn--bcher-kva.example``;
* an IPv6 host is written in its compressed form, e.g. ``[2001:db8::1]``;
* an empty or default port (80 for http, 443 for https) is removed;
* an empty path becomes ``/``, and ``.`` and ``..`` path segments are resolved;
* percent-encodings get uppercase hex digits, and encoded unreserved characters
  (``A-Z a-z 0-9 - . _ ~``) are decoded;
* characters that a URL cannot contain literally (non-ASCII, space, and ``" < > [ ] ^ ` { | }``
  outside the host) are percent-encoded as UTF-8;
* the fragment is removed; it is never sent to the server.

Kept, because it can be meaningful:

* the case of the path, trailing slashes and repeated slashes;
* the query string: parameter order, duplicates, empty values, ``+``, and an empty query (``?``);
* encoded reserved characters, such as ``%2F`` in a path segment;
* a trailing dot in the host: ``example.com.`` and ``example.com`` are different authorities for
  HTTP (Host header, TLS name, cookies).

Rejected with ``InvalidUrlError``:

* relative URLs, and schemes other than http and https;
* credentials (``user:password@``);
* control characters inside the URL (including tabs and line breaks), and backslashes.
  Leading and trailing spaces and control characters are ignored, as browsers do;
* empty or malformed hosts, and percent-encoded hosts;
* numeric hosts that are not a dotted-decimal IPv4 address, such as ``127.1``, ``0x7f.0.0.1``
  or ``2130706433``: parsers disagree about them;
* IPv6 zone identifiers, invalid ports, and malformed percent-encodings;
* URLs longer than ``MAX_URL_BYTES`` after normalization.

The output is printable ASCII without spaces, starts with ``http://`` or ``https://`` and is at
most ``MAX_URL_BYTES`` bytes long: exactly the contract that the database's CHECK constraints
enforce on every URL column (ADR-0009).
"""

import ipaddress
import re
import string
from dataclasses import dataclass
from typing import Final

import idna

# The database contract (app/db/models.py, MAX_URL_BYTES). This package must not import the
# database layer; a unit test keeps the two values equal.
MAX_URL_BYTES: Final = 2048
# Inputs longer than this are rejected before any parsing, which bounds the work per URL.
MAX_INPUT_CHARS: Final = 4 * MAX_URL_BYTES
MAX_HOST_LENGTH: Final = 253
DEFAULT_PORTS: Final = {"http": 80, "https": 443}

# RFC 3986, appendix B: splits any URI reference into scheme, authority, path, query, fragment.
# Unlike urllib.parse.urlsplit, it never silently removes characters (urlsplit drops tabs and
# line breaks anywhere in the URL).
_URI_REFERENCE = re.compile(r"(?:([^:/?#]+):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")

# Leading and trailing characters ignored, as browsers do: C0 controls and space.
_STRIPPED: Final = "".join(chr(code) for code in range(0x21))
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_UNRESERVED: Final = frozenset(string.ascii_letters + string.digits + "-._~")
_SUB_DELIMS: Final = "!$&'()*+,;="
_PATH_CHARS: Final = _UNRESERVED | frozenset(_SUB_DELIMS + ":@/")
_QUERY_CHARS: Final = _PATH_CHARS | frozenset("?")
_HEX_DIGITS: Final = frozenset(string.hexdigits)

# A host name label after lowercasing. Underscores are not valid in DNS host names, but they
# appear in real ones and browsers accept them.
_LABEL = re.compile(r"[a-z0-9_-]{1,63}")
# A last label that makes the host an IPv4 address (WHATWG URL Standard, "ends in a number").
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")


class InvalidUrlError(ValueError):
    """The input is not an acceptable absolute http(s) URL. The message is safe to show users."""


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    """A normalized absolute http(s) URL and the components that scope checks need."""

    url: str
    scheme: str
    # Lowercase ASCII host name (IDNA A-labels), dotted-decimal IPv4, or bracketed IPv6.
    host: str
    # The effective port: the explicit port, or the scheme's default.
    port: int

    def __str__(self) -> str:
        return self.url


def normalize_url(raw: str) -> NormalizedUrl:
    """Normalize an absolute http(s) URL, or raise ``InvalidUrlError`` explaining why not."""
    text = raw.strip(_STRIPPED)
    if not text:
        raise InvalidUrlError("URL is empty")
    if len(text) > MAX_INPUT_CHARS:
        raise InvalidUrlError(f"URL is longer than {MAX_INPUT_CHARS} characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates, e.g. from a JSON "\ud800" escape
        raise InvalidUrlError("URL contains characters that are not valid Unicode") from None
    if _CONTROL.search(text):
        raise InvalidUrlError("URL contains control characters, such as tabs or line breaks")
    if "\\" in text:
        # Browsers read "\" as "/" in http(s) URLs, other parsers do not: refuse the ambiguity.
        raise InvalidUrlError("URL contains a backslash; use '/' to separate path segments")

    match = _URI_REFERENCE.fullmatch(text)
    if match is None:  # pragma: no cover - the pattern matches every string without newlines
        raise InvalidUrlError("URL is malformed")
    scheme, authority, path, query = match.group(1, 2, 3, 4)

    if scheme is None:
        raise InvalidUrlError("URL must be absolute and start with http:// or https://")
    if not _SCHEME.fullmatch(scheme) or scheme.lower() not in DEFAULT_PORTS:
        raise InvalidUrlError(
            f"URL scheme '{_shorten(scheme)}' is not supported; use http:// or https://"
        )
    scheme = scheme.lower()
    if authority is None:
        raise InvalidUrlError("URL must include a host, as in https://example.com/")

    raw_host, raw_port = _split_authority(authority)
    host = normalize_host(raw_host)
    default_port = DEFAULT_PORTS[scheme]
    port = _parse_port(raw_port) or default_port

    url = f"{scheme}://{host}" if port == default_port else f"{scheme}://{host}:{port}"
    # With an authority, the path is empty or starts with "/".
    url += _remove_dot_segments(_normalize_percent_encoding(path, _PATH_CHARS)) or "/"
    if query is not None:
        url += "?" + _normalize_percent_encoding(query, _QUERY_CHARS)
    if len(url) > MAX_URL_BYTES:  # the URL is ASCII, so characters are bytes
        raise InvalidUrlError(f"URL is longer than {MAX_URL_BYTES} bytes after normalization")
    return NormalizedUrl(url=url, scheme=scheme, host=host, port=port)


def join_url(base: str, reference: str) -> NormalizedUrl:
    """Resolve ``reference`` (an href or a Location header) against ``base``, then normalize it.

    ``base`` must be an absolute http(s) URL. Raises ``InvalidUrlError`` if the reference cannot
    be resolved, or if the result is not an acceptable URL (``normalize_url``).
    """
    return normalize_url(resolve_reference(base, reference))


def resolve_reference(base: str, reference: str) -> str:
    """Resolve a URI reference against an absolute base URL (RFC 3986, section 5.2.2).

    Strict parsing: a reference with a scheme is absolute even if the scheme equals the base's
    (``http:page`` is not relative). Leading and trailing spaces and control characters are
    ignored, as browsers do, but control characters inside the reference and backslashes are
    rejected, exactly as ``normalize_url`` rejects them. Dot segments are left for
    ``normalize_url`` to resolve, which gives the same result as resolving them here.
    """
    text = reference.strip(_STRIPPED)
    if _CONTROL.search(text):
        raise InvalidUrlError("URL contains control characters, such as tabs or line breaks")
    if "\\" in text:
        raise InvalidUrlError("URL contains a backslash; use '/' to separate path segments")
    base_match = _URI_REFERENCE.fullmatch(base)
    reference_match = _URI_REFERENCE.fullmatch(text)
    if base_match is None or reference_match is None:  # pragma: no cover - matches any string
        raise InvalidUrlError("URL is malformed")
    base_scheme, base_authority, base_path, base_query = base_match.group(1, 2, 3, 4)
    if base_scheme is None or base_authority is None:
        raise InvalidUrlError("the base URL must be absolute, with a host")
    scheme, authority, path, query = reference_match.group(1, 2, 3, 4)

    if scheme is not None:
        return text
    if authority is None:
        authority = base_authority
        if not path:
            path = base_path
            query = query if query is not None else base_query
        elif not path.startswith("/"):
            # Merge with the base path (RFC 3986, section 5.2.3).
            path = (base_path[: base_path.rfind("/") + 1] or "/") + path
    resolved = f"{base_scheme}://{authority}{path}"
    return resolved if query is None else f"{resolved}?{query}"


def scheme_of(reference: str) -> str | None:
    """The lowercase scheme of a URI reference, or None if the reference is relative."""
    match = _URI_REFERENCE.fullmatch(reference.strip(_STRIPPED))
    scheme = match.group(1) if match is not None else None
    return scheme.lower() if scheme is not None and _SCHEME.fullmatch(scheme) else None


def normalize_host(host: str) -> str:
    """Normalize a URL host, or raise ``InvalidUrlError``.

    Also used for approved-host entries in the configuration, so that both sides of a scope
    comparison have the same canonical form.
    """
    if not host:
        raise InvalidUrlError("URL has no host")
    if host.startswith("["):
        return _normalize_ipv6_literal(host)
    if "%" in host:
        raise InvalidUrlError("percent-encoded host names are not supported")
    if host.isascii():
        host = host.lower()
    else:
        try:
            # IDNA 2008 + UTS #46, non-transitional: "faß.de" stays distinct from "fass.de".
            # (Python's built-in "idna" codec implements IDNA 2003 and would merge them.)
            host = idna.encode(host, uts46=True).decode("ascii")
        except UnicodeError as exc:  # idna.IDNAError is a UnicodeError
            raise InvalidUrlError(
                f"host is not a valid internationalized domain name: {exc}"
            ) from None
    _check_host_name(host)
    return host


def ip_address_of(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP address that a *normalized* host denotes, or None for a host name."""
    if host.startswith("["):
        return ipaddress.IPv6Address(host[1:-1])
    try:
        return ipaddress.IPv4Address(host)
    except ValueError:
        return None


def _split_authority(authority: str) -> tuple[str, str | None]:
    """Split ``host[:port]``; the host keeps its brackets if it is an IPv6 literal."""
    if "@" in authority:
        raise InvalidUrlError("URLs with credentials (user:password@) are not allowed")
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            raise InvalidUrlError("IPv6 host is missing its closing ']'")
        host, rest = authority[: end + 1], authority[end + 1 :]
        if rest and not rest.startswith(":"):
            raise InvalidUrlError("unexpected characters after the IPv6 host")
        return host, rest[1:] if rest else None
    if authority.count(":") > 1:
        raise InvalidUrlError(
            "IPv6 hosts must be enclosed in brackets, as in http://[2001:db8::1]/"
        )
    host, colon, port = authority.partition(":")
    return host, port if colon else None


def _parse_port(text: str | None) -> int | None:
    """The explicit port, or None when it is absent or empty (``http://example.com:/``)."""
    if not text:
        return None
    if not (text.isascii() and text.isdigit()):
        raise InvalidUrlError("port must be a number")
    digits = text.lstrip("0") or "0"
    if len(digits) > 5 or not 1 <= int(digits) <= 65535:
        raise InvalidUrlError("port must be between 1 and 65535")
    return int(digits)


def _normalize_ipv6_literal(host: str) -> str:
    if not host.endswith("]"):
        raise InvalidUrlError("IPv6 host is missing its closing ']'")
    literal = host[1:-1]
    if literal[:1] in ("v", "V"):
        raise InvalidUrlError("IPvFuture hosts are not supported")
    if "%" in literal:
        raise InvalidUrlError("IPv6 zone identifiers are not allowed in URLs")
    try:
        address = ipaddress.IPv6Address(literal)
    except ValueError:
        raise InvalidUrlError("host is not a valid IPv6 address") from None
    return f"[{address.compressed}]"


def _check_host_name(host: str) -> None:
    """Validate a lowercase ASCII host name or IPv4 address (with an optional trailing dot)."""
    name = host.removesuffix(".")
    if not name:
        raise InvalidUrlError("URL has no host")
    if len(name) > MAX_HOST_LENGTH:
        raise InvalidUrlError(f"host name is longer than {MAX_HOST_LENGTH} characters")
    labels = name.split(".")
    for label in labels:
        if not label:
            raise InvalidUrlError("host name contains an empty label")
        if not _LABEL.fullmatch(label):
            raise InvalidUrlError(
                "host name may contain only letters, digits, '-', '_' and '.', in labels of"
                " at most 63 characters"
            )
    if _NUMERIC_LABEL.fullmatch(labels[-1]) and (name != host or not _is_ipv4(name)):
        raise InvalidUrlError(
            "a numeric host must be an IPv4 address in dotted-decimal form, such as 192.0.2.1"
        )


def _is_ipv4(name: str) -> bool:
    try:
        ipaddress.IPv4Address(name)  # strict: four decimal octets, no leading zeros
    except ValueError:
        return False
    return True


def _normalize_percent_encoding(component: str, allowed: frozenset[str]) -> str:
    """Decode encoded unreserved characters, uppercase the others, encode disallowed ones."""
    parts: list[str] = []
    index, length = 0, len(component)
    while index < length:
        char = component[index]
        if char == "%":
            digits = component[index + 1 : index + 3]
            if len(digits) != 2 or not _HEX_DIGITS.issuperset(digits):
                raise InvalidUrlError(
                    "URL contains a '%' that does not start a percent-encoding such as %20"
                )
            decoded = chr(int(digits, 16))
            parts.append(decoded if decoded in _UNRESERVED else "%" + digits.upper())
            index += 3
            continue
        if char in allowed:
            parts.append(char)
        else:
            parts.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        index += 1
    return "".join(parts)


def _remove_dot_segments(path: str) -> str:
    """Resolve "." and ".." segments of an absolute path (RFC 3986, section 5.2.4)."""
    if not path:
        return path
    segments = path.split("/")[1:]  # the path starts with "/"
    output: list[str] = []
    last = len(segments) - 1
    for position, segment in enumerate(segments):
        if segment == "..":
            if output:
                output.pop()
            if position == last:
                output.append("")  # "/a/b/.." -> "/a/"
        elif segment == ".":
            if position == last:
                output.append("")  # "/a/." -> "/a/"
        else:
            output.append(segment)
    return "/" + "/".join(output)


def _shorten(text: str, limit: int = 32) -> str:
    return text if len(text) <= limit else text[:limit] + "..."
