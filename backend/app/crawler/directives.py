"""Page-level robots directives: what a fetched page allows the crawler to do with it.

Two sources apply, and they are combined (ADR-0014):

* ``<meta name="robots">`` and ``<meta name="<crawler name>">``, which the HTML parser reports
  (``ParsedPage.robots``);
* ``X-Robots-Tag`` response headers, which the fetcher keeps one by one
  (``FetchResult.robots_tags``). Directives in a header apply to every crawler, except after a
  user-agent prefix: in ``otherbot: noindex, nofollow`` both directives apply only to
  ``otherbot``, up to the end of that header.

Two directives matter to SiteScope, and they are independent:

* ``noindex``: the page may be stored, but must never be indexed or returned by search;
* ``nofollow``: none of the page's links may be followed (admitted to the crawl). They are still
  recorded, for link analysis.

``none`` means both. Every other directive (``index``, ``follow``, ``all``, ``noarchive``,
``max-snippet: 20``, ...) is ignored. Conflicting directives resolve to the more restrictive one,
because ``index`` and ``follow`` never cancel ``noindex`` or ``nofollow``. Matching is
case-insensitive.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

# Directives whose names are followed by ":" and a value. In an X-Robots-Tag header, any other
# "<name>:" prefix whose name is a product token (RFC 9309: letters, "_" and "-") is a user
# agent. Anything else, such as the rest of a date with commas
# ("unavailable_after: Sunday, 06-Nov-94 08:49:37 GMT"), is a part of a value, not an agent.
_VALUED_DIRECTIVES: Final = frozenset(
    {"unavailable_after", "max-snippet", "max-image-preview", "max-video-preview"}
)
_PRODUCT_TOKEN: Final = re.compile(r"[a-z_-]+")


@dataclass(frozen=True, slots=True)
class PageDirectives:
    noindex: bool = False
    nofollow: bool = False


def page_directives(
    meta_robots: Iterable[str], robots_tags: Iterable[str], *, crawler_name: str
) -> PageDirectives:
    """Combine meta robots directives and X-Robots-Tag headers into what applies to us."""
    directives = {directive.strip().lower() for directive in meta_robots}
    for header in robots_tags:
        directives |= robots_tag_directives(header, crawler_name=crawler_name)
    return PageDirectives(
        noindex=bool(directives & {"noindex", "none"}),
        nofollow=bool(directives & {"nofollow", "none"}),
    )


def robots_tag_directives(header: str, *, crawler_name: str) -> set[str]:
    """The lowercase directives of one X-Robots-Tag header value that apply to ``crawler_name``."""
    name = crawler_name.lower()
    applies = True  # until a user-agent prefix appears, directives apply to every crawler
    directives: set[str] = set()
    for part in header.split(","):
        directive = part.strip()
        prefix, separator, rest = directive.partition(":")
        prefix = prefix.strip().lower()
        if separator and prefix not in _VALUED_DIRECTIVES and _PRODUCT_TOKEN.fullmatch(prefix):
            applies = prefix == name
            directive = rest.strip()
        if applies and directive:
            directives.add(directive.lower())
    return directives
