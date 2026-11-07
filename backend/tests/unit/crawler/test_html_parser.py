"""HTML parsing (app/crawler/html_parser.py), on static fixtures and inline documents."""

from pathlib import Path

import pytest

from app.crawler.html_parser import ExtractedLink, ParsedPage, ParseLimits, parse_html
from app.crawler.urls import InvalidUrlError

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "html"
DOCUMENT = "https://docs.example.com/guide/intro.html"


def parse(
    html: str | bytes, *, declared_charset: str | None = None, limits: ParseLimits | None = None
) -> ParsedPage:
    body = html.encode() if isinstance(html, str) else html
    return parse_html(body, url=DOCUMENT, declared_charset=declared_charset, limits=limits)


def test_a_realistic_document() -> None:
    page = parse_html((FIXTURES / "article.html").read_bytes(), url=DOCUMENT)

    assert page.title == "SiteScope Guide"
    assert page.description == "How the crawler works."
    assert page.robots == frozenset({"noindex", "nofollow"})
    assert page.canonical_url == "https://docs.example.com/guide/"
    assert page.language == "en-US"
    assert page.headings == "The SiteScope Guide\nFetching"
    assert page.links == (
        ExtractedLink("https://docs.example.com/", 0, "Home", nofollow=False),
        ExtractedLink("https://docs.example.com/guide/intro.html", 1, "Intro", nofollow=False),
        ExtractedLink("https://external.example.org/page?ref=1", 2, "External", nofollow=True),
        ExtractedLink("https://docs.example.com/area-target", 3, "Area link", nofollow=False),
        ExtractedLink("https://docs.example.com/hidden-link", 4, "hidden", nofollow=False),
    )
    assert page.text == (
        "Home Intro Intro again External Mail us Menu Call Broken No href "
        "The SiteScope Guide SiteScope crawls approved sites. Fetching "
        "Every request is pinned to a validated address. Enable JavaScript."
    )
    stats = page.stats
    assert (stats.links_found, stats.links_kept, stats.links_duplicate) == (10, 5, 1)
    assert (stats.links_unsupported, stats.links_invalid, stats.links_truncated) == (3, 1, False)
    assert (stats.word_count, stats.text_truncated, stats.input_truncated) == (30, False, False)
    assert (page.encoding, page.encoding_source, page.encoding_errors) == ("utf-8", "meta", False)


def test_malformed_html_is_parsed_without_errors() -> None:
    page = parse_html((FIXTURES / "malformed.html").read_bytes(), url=DOCUMENT)

    urls = [link.url for link in page.links]
    assert urls[:3] == [
        "https://docs.example.com/one",
        "https://docs.example.com/two",
        "https://docs.example.com/three",
    ]
    assert "https://docs.example.com/inside-comment" not in urls
    assert page.title is not None
    assert page.title.startswith("Broken")
    assert "Unclosed paragraph" in page.text


def test_a_document_without_a_head_end_tag_keeps_its_body_text() -> None:
    """Regression: </head> is optional in HTML, and html.parser then nests <body> in <head>."""
    page = parse("<html><head><title>T</title><meta charset=utf-8><body><p>Body text</p>")

    assert (page.title, page.text) == ("T", "Body text")


@pytest.mark.parametrize(
    "html", [b"", b"   ", b"<html></html>", b"<!doctype html><title></title>", b"\x00\xff"]
)
def test_empty_documents(html: bytes) -> None:
    page = parse_html(html, url=DOCUMENT)

    assert (page.title, page.description, page.links, page.headings) == (None, None, (), "")
    assert page.text in ("", "\x00�", "\x00ÿ")


def test_short_bodies_that_look_like_urls_or_file_names_are_markup() -> None:
    """BeautifulSoup would warn about these (and pytest turns warnings into errors)."""
    for body in (b"https://example.com/", b"index.html", b"notes.txt"):
        assert parse_html(body, url=DOCUMENT).text == body.decode()


def test_missing_title_and_metadata() -> None:
    page = parse("<p>Only text</p>")

    assert (page.title, page.description, page.canonical_url, page.language) == (None,) * 4
    assert page.robots == frozenset()


def test_relative_and_absolute_links_resolve_against_the_document() -> None:
    page = parse(
        '<a href="next.html">a</a><a href="../up/">b</a><a href="/root">c</a>'
        '<a href="//cdn.example.org/x">d</a><a href="https://Other.Example.com:443/Y">e</a>'
        '<a href="?page=2">f</a><a href="">g</a>'
    )

    assert [link.url for link in page.links] == [
        "https://docs.example.com/guide/next.html",
        "https://docs.example.com/up/",
        "https://docs.example.com/root",
        "https://cdn.example.org/x",
        "https://other.example.com/Y",
        "https://docs.example.com/guide/intro.html?page=2",
        "https://docs.example.com/guide/intro.html",  # an empty href is the document itself
    ]


def test_the_base_element_changes_the_base_url() -> None:
    page = parse(
        '<base href="https://cdn.example.org/assets/"><base href="/ignored/">'
        '<a href="img/a.html">a</a><link rel="canonical" href="canonical.html">'
    )

    assert page.base_url == "https://cdn.example.org/assets/"
    assert [link.url for link in page.links] == ["https://cdn.example.org/assets/img/a.html"]
    assert page.canonical_url == "https://cdn.example.org/assets/canonical.html"


@pytest.mark.parametrize("href", ["javascript:alert(1)", "ftp://files.example/", "http://[::1"])
def test_an_unusable_base_element_is_ignored(href: str) -> None:
    page = parse(f'<base href="{href}"><a href="next.html">a</a>')

    assert page.base_url == DOCUMENT
    assert page.links[0].url == "https://docs.example.com/guide/next.html"


def test_fragments_are_removed_and_duplicates_keep_the_first_occurrence() -> None:
    page = parse(
        '<a href="#top">Top</a><a href="#bottom">Bottom</a>'
        '<a href="page#a">First</a><a href="PAGE">Case matters</a><a href="page#b">Again</a>'
    )

    assert [(link.url, link.anchor_text, link.position) for link in page.links] == [
        (DOCUMENT, "Top", 0),
        ("https://docs.example.com/guide/page", "First", 1),
        ("https://docs.example.com/guide/PAGE", "Case matters", 2),
    ]
    assert page.stats.links_duplicate == 2


def test_unsupported_schemes_and_invalid_links_are_counted_and_skipped() -> None:
    page = parse(
        '<a href="mailto:a@example.com">m</a><a href=" JavaScript:void(0)">j</a>'
        '<a href="data:text/html,hi">d</a><a href="tel:+1">t</a><a href="ftp://f.example/">f</a>'
        '<a href="http://exa mple.com/">space</a><a href="http://user:pw@example.com/">cred</a>'
        '<a href="/fine">ok</a>'
    )

    assert [link.url for link in page.links] == ["https://docs.example.com/fine"]
    assert (page.stats.links_unsupported, page.stats.links_invalid) == (5, 2)


def test_unicode_content_and_internationalized_links() -> None:
    page = parse(
        "<title>東京 🗼 guide</title><h1>Überblick</h1><p>日本語のテキスト。</p>"
        '<a href="http://bücher.example/パス">本</a>'
    )

    assert page.title == "東京 🗼 guide"
    assert page.text == "Überblick 日本語のテキスト。 本"
    assert page.links[0].url == "http://xn--bcher-kva.example/%E3%83%91%E3%82%B9"


def test_declared_and_meta_charsets_are_honored() -> None:
    legacy = '<meta charset="windows-1252"><p>Café — €5</p>'.encode("cp1252")

    assert parse(legacy).text == "Café — €5"
    assert parse("<p>Café</p>".encode("latin-1"), declared_charset="ISO-8859-1").text == "Café"
    broken = parse(b"<p>ok \xff</p>", declared_charset="utf-8")
    assert (broken.text, broken.encoding_errors) == ("ok �", True)


def test_scripts_styles_templates_and_hidden_elements_are_not_visible_text() -> None:
    page = parse(
        "<style>.a{}</style><script>alert('x')</script><p>Visible</p>"
        "<template><p>Inert</p><a href='/t'>t</a></template><div hidden>Hidden</div>"
        "<svg><style>.s{}</style><text>Label</text></svg><noscript>Fallback</noscript>"
    )

    assert page.text == "Visible Label Fallback"
    assert page.links == ()


def test_area_alt_text_and_nofollow() -> None:
    page = parse(
        '<map><area href="/a" alt=" Area  one "><area href="/b"></map>'
        '<a href="/c" rel="NoFollow">c</a><a href="/d" rel="external">d</a>'
    )

    assert [(link.anchor_text, link.nofollow) for link in page.links] == [
        ("Area one", False),
        (None, False),
        ("c", True),
        ("d", False),
    ]


def test_outputs_are_capped_and_the_caps_are_reported() -> None:
    links = "".join(f'<a href="/p/{n}">{"word " * 200}</a>' for n in range(10))
    html = f"<title>{'T' * 2000}</title><h1>{'H' * 30_000}</h1>{links}"

    page = parse(html, limits=ParseLimits(max_links=3, max_text_chars=500))

    assert len(page.links) == 3
    assert page.stats.links_truncated
    assert all(len(link.anchor_text or "") <= 500 for link in page.links)
    assert page.title == "T" * 1000
    assert len(page.headings) == 20_000
    assert (len(page.text), page.stats.text_truncated) == (500, True)


def test_input_beyond_the_limit_is_not_parsed() -> None:
    html = "<p>kept</p>" + "<p>" + "x" * 5000 + "</p><a href='/late'>late</a>"

    page = parse(html, limits=ParseLimits(max_input_bytes=100))

    assert page.stats.input_truncated
    assert page.stats.input_bytes == len(html)
    assert page.links == ()
    assert page.text.startswith("kept")


def test_a_large_document_stays_within_the_default_limits() -> None:
    html = "".join(f'<p>Paragraph {n} <a href="/p/{n}">link {n}</a></p>' for n in range(5000))

    page = parse(html)

    assert len(page.links) == 1000
    assert page.stats.links_truncated
    assert page.stats.links_found == 5000


def test_deeply_nested_markup_does_not_crash() -> None:
    html = "<div>" * 20_000 + "<a href='/deep'>deep</a>" + "</div>" * 20_000

    page = parse(html)

    assert [link.url for link in page.links] == ["https://docs.example.com/deep"]


def test_the_document_url_must_be_valid() -> None:
    with pytest.raises(InvalidUrlError):
        parse_html(b"<p>x</p>", url="not a url")


def test_parse_limits_from_settings() -> None:
    from app.core.config import Settings

    settings = Settings(
        _env_file=None, fetch_max_body_bytes=4096, parse_max_links=7, parse_max_text_chars=2000
    )

    limits = ParseLimits.from_settings(settings)

    assert (limits.max_input_bytes, limits.max_links, limits.max_text_chars) == (4096, 7, 2000)


def test_meta_robots_for_the_crawler_by_name() -> None:
    html = (
        '<meta name="robots" content="noarchive">'
        '<meta name="SiteScopeBot" content="NoIndex">'
        '<meta name="otherbot" content="nofollow">'
    )
    assert parse(html).robots == frozenset({"noarchive"})
    named = parse_html(html.encode(), url=DOCUMENT, crawler_name="sitescopebot")
    assert named.robots == frozenset({"noarchive", "noindex"})
