"""How a page becomes a search document (ADR-0005, ADR-0018).

A page's search vector is built inside PostgreSQL from the stored page, by one statement
(``services/search_index.py``), so the page text never travels to the indexer process::

    setweight(to_tsvector('english', title), 'A')
      || setweight(to_tsvector('english', description), 'B')
      || setweight(to_tsvector('english', headings), 'C')
      || setweight(to_tsvector('english', left(body, max_body_chars)), 'D')

**Weights.** ``ts_rank`` gives the four weights 1.0 (A), 0.4 (B), 0.2 (C) and 0.1 (D), so a
match in the title counts ten times a match in the body.

**Configuration.** ``english`` (stemming and English stop words), named explicitly in every
statement rather than taken from ``default_text_search_config``. Indexing and querying must use
the same configuration; changing it requires reindexing every document. Text in other languages
is still split into words by PostgreSQL's parser, but is not stemmed. Chinese, Japanese and
other languages written without spaces are not segmented into words: a run of such characters
becomes one token, so a query matches it only as a whole (or as a prefix with ``:*``). There is
no multilingual search.

**Size limits.** PostgreSQL limits the lexemes and positions of one ``tsvector`` to 1 MB
(1,048,575 bytes). The title (at most 1,000 characters), the description (2,000) and the
headings (20,000) are indexed whole; the body is cut to its first ``max_body_chars`` characters
(``SITESCOPE_INDEX_MAX_BODY_CHARS``: 100,000 by default, at most 150,000) *before*
``to_tsvector`` sees it, at the last whitespace before the limit (looking back at most
``WORD_LOOKBACK`` characters), so no word the cut splits is indexed. Measured on PostgreSQL 18,
the densest text, distinct hyphenated CJK compounds (the parser indexes each compound and both
of its parts), needs about 6.7 bytes of that per character, and distinct two-character CJK words
about 3.7: the default stays below the limit for every text measured (about 820 KB at most,
fields included), while the maximum can exceed it for such text. A vector over the limit is
built again once from a quarter of the body (``FALLBACK_BODY_DIVISOR``); a document that still
does not fit fails as ``tsvector_too_large``.

Positions inside a vector are capped at 16,383 by PostgreSQL, so in very long documents the
words near the end share one position; ranking by proximity is then less precise there.
"""

from typing import Final

# The text-search configuration of every search vector and every query.
TEXT_SEARCH_CONFIG: Final = "english"

# Field weights (setweight labels).
TITLE_WEIGHT: Final = "A"
DESCRIPTION_WEIGHT: Final = "B"
HEADINGS_WEIGHT: Final = "C"
BODY_WEIGHT: Final = "D"

# PostgreSQL's limit on a tsvector's size, in bytes.
MAX_TSVECTOR_BYTES: Final = 1_048_575
# If a vector is still too large, the body is cut to max_body_chars // FALLBACK_BODY_DIVISOR.
FALLBACK_BODY_DIVISOR: Final = 4

# How far back from the limit the cut looks for whitespace. PostgreSQL's parser ignores words
# longer than 2,047 characters, so a run of non-space characters this long that the cut splits
# is not indexed either way, and the cut is left where it is.
WORD_LOOKBACK: Final = 2048

# Reasons stored in search_documents.last_error (stable tokens, never page content).
TSVECTOR_TOO_LARGE: Final = "tsvector_too_large"
MAX_ATTEMPTS_EXCEEDED: Final = "max_attempts_exceeded"
DEADLINE_EXCEEDED: Final = "deadline_exceeded"  # an attempt ran out of its deadline


def fallback_body_chars(max_body_chars: int) -> int:
    """The body length of the second try, after a vector turned out too large."""
    return max(1, max_body_chars // FALLBACK_BODY_DIVISOR)
