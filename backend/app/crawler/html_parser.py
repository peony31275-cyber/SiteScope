"""HTML parsing: what the crawler keeps from a fetched document. No network, no database.

``parse_html`` turns the bytes of an HTML document into a ``ParsedPage``:

* metadata: the title, the meta description, the canonical URL, the document language, and the
  meta robots directives of ``<meta name="robots">`` and, if a ``crawler_name`` is given, of
  ``<meta name="<crawler_name>">`` (reported, not enforced: obeying them is the crawler's
  decision, ``app.crawler.directives``);
* the headings (h1-h6) and the visible text. Titles, scripts, styles, templates and elements
  with the ``hidden`` attribute are excluded, and whitespace is collapsed. The ``<head>`` element
  itself is not removed: its end tag is optional in HTML, and Python's parser then nests the
  whole body inside it. Elements hidden only by CSS cannot be detected without rendering;
* the links of ``<a href>`` and ``<area href>`` elements, resolved against the document's base
  URL (the first ``<base href>``, else the document URL) and normalized with ``urls.join_url``,
  so fragments are dropped and every link meets the database URL contract. Links are
  deduplicated (the first occurrence wins, which also fixes ``position``) and keep their anchor
  text and ``rel="nofollow"``. Links with other schemes (``mailto:``, ``javascript:``, ...) and
  links that cannot be normalized are counted and skipped. Links inside ``<template>`` are
  ignored, because template content is inert;
* statistics, including whether the input, the text or the links were cut at their limits.

Malformed HTML never raises: BeautifulSoup with Python's ``html.parser`` repairs what it can.

**Resource use.** At most ``ParseLimits.max_input_bytes`` of input are parsed, and every output
is capped (the defaults match the database column limits). BeautifulSoup builds the whole tree
in memory, so memory and CPU time grow with the size of the input, and a document near the body
limit costs far more than a typical page. Code running in an event loop should call
``parse_html`` in a worker thread (``asyncio.to_thread``), as ``app.crawler.cli`` does.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from bs4 import BeautifulSoup, Tag

from app.crawler.encoding import EncodingSource, decode_html
from app.crawler.urls import InvalidUrlError, join_url, normalize_url, scheme_of

if TYPE_CHECKING:
    from app.core.config import Settings

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# Removed before the visible text is extracted (templates are removed before anything else).
# Not "head": with an omitted </head>, Python's parser places the body inside the head element.
NON_VISIBLE_TAGS = ("title", "script", "style")


@dataclass(frozen=True, slots=True)
class ParseLimits:
    """Bounds on parser input and output. Defaults match the database column limits."""

    max_input_bytes: int = 2 * 1024 * 1024  # the fetcher's default body limit
    max_links: int = 1000
    max_text_chars: int = 1_000_000  # pages.extracted_text
    max_title_chars: int = 1000  # pages.title
    max_description_chars: int = 2000  # pages.meta_description
    max_headings_chars: int = 20_000  # pages.headings
    max_anchor_chars: int = 500  # page_links.anchor_text

    @classmethod
    def from_settings(cls, settings: "Settings") -> Self:
        return cls(
            max_input_bytes=settings.fetch_max_body_bytes,
            max_links=settings.parse_max_links,
            max_text_chars=settings.parse_max_text_chars,
        )


@dataclass(frozen=True, slots=True)
class ExtractedLink:
    url: str  # normalized absolute URL
    position: int  # order of first occurrence among the kept links, from 0
    anchor_text: str | None  # whitespace-collapsed link text (an <area>'s alt), if any
    nofollow: bool  # rel="nofollow"


@dataclass(frozen=True, slots=True)
class PageStats:
    input_bytes: int  # bytes received by the parser
    input_truncated: bool  # only the first max_input_bytes were parsed
    text_chars: int  # characters of visible text kept
    text_truncated: bool
    word_count: int  # whitespace-separated words in the kept text
    links_found: int  # <a>/<area> elements with an href
    links_kept: int  # distinct normalized links kept
    links_duplicate: int  # links to a target already kept (until max_links is reached)
    links_unsupported: int  # non-http(s) schemes: mailto:, javascript:, tel:, data:, ...
    links_invalid: int  # links that could not be resolved or normalized
    links_truncated: bool  # max_links was reached; later links were counted, not examined


@dataclass(frozen=True, slots=True)
class ParsedPage:
    url: str  # the normalized document URL
    base_url: str  # the URL links were resolved against
    title: str | None
    description: str | None
    headings: str  # one heading per line
    text: str
    links: tuple[ExtractedLink, ...]
    canonical_url: str | None
    language: str | None
    # Lowercase meta robots directives that apply to this crawler, e.g. {"noindex", "nofollow"}.
    robots: frozenset[str]
    encoding: str
    encoding_source: EncodingSource
    encoding_errors: bool  # some bytes were invalid in the encoding
    stats: PageStats


@dataclass(slots=True)
class _LinkCounts:
    found: int = 0
    duplicate: int = 0
    unsupported: int = 0
    invalid: int = 0
    truncated: bool = False


def parse_html(
    body: bytes,
    *,
    url: str,
    declared_charset: str | None = None,
    limits: ParseLimits | None = None,
    crawler_name: str | None = None,
) -> ParsedPage:
    """Parse an HTML document fetched from ``url`` (its final URL, after redirects).

    ``declared_charset`` is the charset parameter of the response's Content-Type, if any.
    ``crawler_name`` also reads ``<meta name="<crawler_name>">`` robots directives (matched
    case-insensitively). Raises ``InvalidUrlError`` only if ``url`` itself is not a valid
    http(s) URL.
    """
    limits = limits if limits is not None else ParseLimits()
    document_url = normalize_url(url).url
    decoded = decode_html(body[: limits.max_input_bytes], declared_charset)
    # The appended newline is whitespace in HTML. It also keeps BeautifulSoup from mistaking a
    # short body without markup for a file name or URL (MarkupResemblesLocatorWarning).
    soup = BeautifulSoup(decoded.text + "\n", "html.parser")

    _remove(soup.find_all("template"))
    base_url = _base_url(soup, document_url)
    links, counts = _links(soup, base_url, limits)
    title_tag = soup.find("title")
    title = (
        _collapse(title_tag.get_text(" "))[: limits.max_title_chars]
        if isinstance(title_tag, Tag)
        else ""
    )
    headings = "\n".join(
        text for tag in _tags(soup.find_all(HEADING_TAGS)) if (text := _collapse(tag.get_text(" ")))
    )[: limits.max_headings_chars]
    description = _meta_content(soup, "description", limits.max_description_chars)
    canonical_url = _canonical_url(soup, base_url)
    language = _language(soup)
    robot_names = {"robots"} if crawler_name is None else {"robots", crawler_name.lower()}
    robots = _robots(soup, robot_names)

    # Removing elements changes the tree, so the visible text is extracted last.
    _remove(soup.find_all(NON_VISIBLE_TAGS))
    _remove(soup.find_all(hidden=True))
    full_text = _collapse(soup.get_text(" "))
    text = full_text[: limits.max_text_chars]

    return ParsedPage(
        url=document_url,
        base_url=base_url,
        title=title or None,
        description=description,
        headings=headings,
        text=text,
        links=links,
        canonical_url=canonical_url,
        language=language,
        robots=robots,
        encoding=decoded.encoding,
        encoding_source=decoded.source,
        encoding_errors=decoded.errors,
        stats=PageStats(
            input_bytes=len(body),
            input_truncated=len(body) > limits.max_input_bytes,
            text_chars=len(text),
            text_truncated=len(full_text) > len(text),
            word_count=len(text.split()),
            links_found=counts.found,
            links_kept=len(links),
            links_duplicate=counts.duplicate,
            links_unsupported=counts.unsupported,
            links_invalid=counts.invalid,
            links_truncated=counts.truncated,
        ),
    )


def _links(
    soup: BeautifulSoup, base_url: str, limits: ParseLimits
) -> tuple[tuple[ExtractedLink, ...], _LinkCounts]:
    kept: dict[str, ExtractedLink] = {}
    counts = _LinkCounts()
    for element in _tags(soup.find_all(("a", "area"), href=True)):
        href = _attribute(element, "href")
        if href is None:
            continue
        counts.found += 1
        if len(kept) >= limits.max_links:
            # The limit bounds the work too: later links are counted, never resolved.
            counts.truncated = True
            continue
        scheme = scheme_of(href)
        if scheme is not None and scheme not in ("http", "https"):
            counts.unsupported += 1
            continue
        try:
            target = join_url(base_url, href).url
        except InvalidUrlError:
            counts.invalid += 1
            continue
        if target in kept:
            counts.duplicate += 1
            continue
        if element.name == "area":
            anchor = _collapse(_attribute(element, "alt") or "")
        else:
            anchor = _collapse(element.get_text(" "))
        kept[target] = ExtractedLink(
            url=target,
            position=len(kept),
            anchor_text=anchor[: limits.max_anchor_chars] or None,
            nofollow="nofollow" in _tokens(element, "rel"),
        )
    return tuple(kept.values()), counts


def _base_url(soup: BeautifulSoup, document_url: str) -> str:
    base = soup.find("base", href=True)
    href = _attribute(base, "href") if isinstance(base, Tag) else None
    if href is None:
        return document_url
    try:
        return join_url(document_url, href).url
    except InvalidUrlError:
        return document_url  # an unusable <base href> is ignored, as browsers do


def _canonical_url(soup: BeautifulSoup, base_url: str) -> str | None:
    for link in _tags(soup.find_all("link", href=True)):
        href = _attribute(link, "href")
        if href is not None and "canonical" in _tokens(link, "rel"):
            try:
                return join_url(base_url, href).url
            except InvalidUrlError:
                return None
    return None


def _language(soup: BeautifulSoup) -> str | None:
    html = soup.find("html")
    language = _attribute(html, "lang") if isinstance(html, Tag) else None
    return _collapse(language or "")[:35] or None  # BCP 47 tags are short


def _robots(soup: BeautifulSoup, names: set[str]) -> frozenset[str]:
    directives: set[str] = set()
    for meta in _tags(soup.find_all("meta")):
        if (_attribute(meta, "name") or "").strip().lower() in names:
            content = _attribute(meta, "content") or ""
            directives.update(part.strip().lower() for part in content.split(",") if part.strip())
    return frozenset(directives)


def _meta_content(soup: BeautifulSoup, name: str, limit: int) -> str | None:
    for meta in _tags(soup.find_all("meta")):
        if (_attribute(meta, "name") or "").strip().lower() == name:
            return _collapse(_attribute(meta, "content") or "")[:limit] or None
    return None


def _collapse(text: str) -> str:
    """Collapse every run of whitespace (Unicode included) into a single space."""
    return " ".join(text.split())


def _attribute(tag: Tag, name: str) -> str | None:
    """An attribute as one string; multi-valued attributes (rel, class) are joined by spaces."""
    value = tag.get(name)
    if value is None:
        return None
    return value if isinstance(value, str) else " ".join(value)


def _tokens(tag: Tag, name: str) -> set[str]:
    return {token.lower() for token in (_attribute(tag, name) or "").split()}


def _tags(elements: Iterable[object]) -> list[Tag]:
    return [element for element in elements if isinstance(element, Tag)]


def _remove(elements: Iterable[object]) -> None:
    """Remove elements and their content; skips those already removed with an ancestor."""
    for element in elements:
        if isinstance(element, Tag) and not element.decomposed:
            element.decompose()
