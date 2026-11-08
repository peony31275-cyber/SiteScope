"""Search query normalization, paging bounds and snippet decoding (app/indexing/queries.py)."""

import pytest

from app.indexing.queries import (
    HEADLINE_OPTIONS,
    HIGHLIGHT_START,
    HIGHLIGHT_STOP,
    MAX_LIMIT,
    MAX_OFFSET,
    MAX_QUERY_CHARS,
    QueryError,
    Snippet,
    check_paging,
    decode_snippet,
    normalize_query,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("asyncio", "asyncio"),
        ("  redis   streams \t\n", "redis streams"),
        ("a\u00a0b", "a b"),  # a no-break space is whitespace
        ("zero\u200bwidth", "zero width"),  # format characters become spaces
        ("bidi\u202eoverride", "bidi override"),
        ("bell\x07char", "bell char"),
        ("nul\x00char", "nul char"),
        ("marker\x02text\x03", "marker text"),  # the snippet markers can never be searched
        ("lone\ud800surrogate", "lone surrogate"),
        ("private\ue000use", "private use"),
        ('"exact phrase" -excluded or other', '"exact phrase" -excluded or other'),
        ("c++ & (foo | !bar) <-> :*", "c++ & (foo | !bar) <-> :*"),
        ("'; DROP TABLE pages; --", "'; DROP TABLE pages; --"),
        ("中文 搜索", "中文 搜索"),
        ("Café naïve", "Café naïve"),
    ],
)
def test_queries_keep_visible_text_and_collapse_whitespace(raw: str, expected: str) -> None:
    assert normalize_query(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", "\u200b\u200c", "\x00\x02\x03"])
def test_an_empty_query_is_refused(raw: str) -> None:
    with pytest.raises(QueryError) as refused:
        normalize_query(raw)

    assert refused.value.code == "empty_query"


def test_a_query_at_the_limit_is_kept_and_a_longer_one_is_refused_not_cut() -> None:
    assert normalize_query("a" * MAX_QUERY_CHARS) == "a" * MAX_QUERY_CHARS
    # Whitespace collapses before the length is checked.
    assert len(normalize_query(" ".join(["ab"] * 85) + "    ")) == 254

    with pytest.raises(QueryError) as refused:
        normalize_query("a" * (MAX_QUERY_CHARS + 1))

    assert refused.value.code == "query_too_long"


@pytest.mark.parametrize(
    ("limit", "offset"), [(1, 0), (MAX_LIMIT, 0), (10, MAX_OFFSET), (MAX_LIMIT, MAX_OFFSET)]
)
def test_paging_within_bounds_is_accepted(limit: int, offset: int) -> None:
    check_paging(limit, offset)


@pytest.mark.parametrize(
    ("limit", "offset", "code"),
    [
        (0, 0, "invalid_limit"),
        (-1, 0, "invalid_limit"),
        (MAX_LIMIT + 1, 0, "invalid_limit"),
        (10, -1, "invalid_offset"),
        (10, MAX_OFFSET + 1, "invalid_offset"),
    ],
)
def test_paging_out_of_bounds_is_refused(limit: int, offset: int, code: str) -> None:
    with pytest.raises(QueryError) as refused:
        check_paging(limit, offset)

    assert refused.value.code == code


def test_a_snippet_without_markers_is_plain_text() -> None:
    assert decode_snippet("The quick brown fox") == Snippet("The quick brown fox")
    assert decode_snippet("") == Snippet("")
    assert decode_snippet(None) == Snippet("")


def test_markers_become_highlight_ranges_of_the_plain_text() -> None:
    snippet = decode_snippet(
        f"{HIGHLIGHT_START}fox{HIGHLIGHT_STOP} jumps over the lazy "
        f"{HIGHLIGHT_START}dog{HIGHLIGHT_STOP}"
    )

    assert snippet.text == "fox jumps over the lazy dog"
    assert [snippet.text[start:end] for start, end in snippet.highlights] == ["fox", "dog"]
    assert snippet.highlights == ((0, 3), (24, 27))


def test_unbalanced_markers_highlight_nothing_and_never_leak() -> None:
    start, stop = HIGHLIGHT_START, HIGHLIGHT_STOP

    unclosed = decode_snippet(f"a {start}b c")
    stray = decode_snippet(f"a{stop} b")
    empty = decode_snippet(f"x {start}{stop}y")
    nested = decode_snippet(f"{start}a{start}b{stop}c{stop}")

    assert unclosed == Snippet("a b c")
    assert stray == Snippet("a b")
    assert empty == Snippet("x y")
    assert nested == Snippet("abc", ((1, 2),))  # the inner start wins; the extra stop is dropped
    for snippet in (unclosed, stray, empty, nested):
        assert start not in snippet.text
        assert stop not in snippet.text


def test_headline_options_use_the_markers_and_bounded_fragments() -> None:
    assert f"StartSel={HIGHLIGHT_START}" in HEADLINE_OPTIONS
    assert f"StopSel={HIGHLIGHT_STOP}" in HEADLINE_OPTIONS
    assert "MaxFragments=2" in HEADLINE_OPTIONS
    assert "<" not in HEADLINE_OPTIONS  # never HTML
