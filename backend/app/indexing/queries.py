"""User queries and search results without SQL: normalization, paging bounds, snippets.

**Queries.** A query is the user's text, parsed by PostgreSQL's ``websearch_to_tsquery``, which
accepts any input without a syntax error: quoted phrases (``"exact phrase"``), ``or``, and a
leading ``-`` to exclude a word. Before that, ``normalize_query`` removes control, format and
other invisible characters, collapses whitespace, and rejects an empty or overlong query. It
never truncates: a query is searched as given, or refused. A query made only of stop words
("the", "and") has no searchable terms; the search service reports that rather than searching.

**Paging.** At most ``MAX_LIMIT`` results per page, and no offset beyond ``MAX_OFFSET``: every
match is ranked before a page is cut, so deep pages cost as much as the whole result set.

**Snippets.** ``ts_headline`` marks matches with two control characters that cannot appear in
the text it is given (``HIGHLIGHT_START``, ``HIGHLIGHT_STOP``; the service removes them from the
page text first). ``decode_snippet`` turns the marked text into plain text and character ranges,
so no caller ever has to trust or escape HTML.
"""

import unicodedata
from dataclasses import dataclass
from typing import Final

MAX_QUERY_CHARS: Final = 256
DEFAULT_LIMIT: Final = 10
MAX_LIMIT: Final = 50
MAX_OFFSET: Final = 1000

HIGHLIGHT_START: Final = "\x02"
HIGHLIGHT_STOP: Final = "\x03"
FRAGMENT_DELIMITER: Final = " … "
# ts_headline options: up to two fragments of 15 to 35 words around the matches.
HEADLINE_OPTIONS: Final = (
    f"StartSel={HIGHLIGHT_START}, StopSel={HIGHLIGHT_STOP}, MaxWords=35, MinWords=15,"
    f' ShortWord=3, MaxFragments=2, FragmentDelimiter="{FRAGMENT_DELIMITER}"'
)
# Characters of the page body that ts_headline reads to build a snippet.
SNIPPET_SOURCE_CHARS: Final = 20_000

# Unicode categories removed from queries: control (Cc), format (Cf: zero-width and
# bidirectional marks), surrogate (Cs), private use (Co) and unassigned (Cn) code points.
_INVISIBLE_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


class QueryError(ValueError):
    """A query or paging value that cannot be searched; ``code`` is a stable token."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_query(raw: str) -> str:
    """The query as it is searched: visible characters, single spaces, no outer whitespace.

    Raises ``QueryError`` (``empty_query``, ``query_too_long``) instead of searching an empty
    query or silently cutting a long one.
    """
    visible = "".join(
        " " if char.isspace() or unicodedata.category(char) in _INVISIBLE_CATEGORIES else char
        for char in raw
    )
    query = " ".join(visible.split())
    if not query:
        raise QueryError("empty_query", "The query is empty.")
    if len(query) > MAX_QUERY_CHARS:
        raise QueryError(
            "query_too_long", f"The query is longer than {MAX_QUERY_CHARS} characters."
        )
    return query


def check_paging(limit: int, offset: int) -> None:
    """Raise ``QueryError`` unless ``1 <= limit <= MAX_LIMIT`` and ``0 <= offset <= MAX_OFFSET``."""
    if not 1 <= limit <= MAX_LIMIT:
        raise QueryError("invalid_limit", f"limit must be from 1 to {MAX_LIMIT}.")
    if not 0 <= offset <= MAX_OFFSET:
        raise QueryError("invalid_offset", f"offset must be from 0 to {MAX_OFFSET}.")


@dataclass(frozen=True, slots=True)
class Snippet:
    """Plain text around the matches, and the matched words as ``[start, end)`` ranges."""

    text: str
    highlights: tuple[tuple[int, int], ...] = ()


def decode_snippet(marked: str | None) -> Snippet:
    """Turn ``ts_headline`` output into plain text and highlight ranges.

    An unbalanced marker (which the service's input never produces) is dropped rather than
    trusted: a start without a stop highlights nothing, a stop without a start is ignored.
    """
    if not marked:
        return Snippet("")
    parts: list[str] = []
    highlights: list[tuple[int, int]] = []
    length = 0
    start: int | None = None
    for char in marked:
        if char == HIGHLIGHT_START:
            start = length
        elif char == HIGHLIGHT_STOP:
            if start is not None and length > start:
                highlights.append((start, length))
            start = None
        else:
            parts.append(char)
            length += 1
    return Snippet("".join(parts), tuple(highlights))
