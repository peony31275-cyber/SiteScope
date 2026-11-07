"""Page-level robots directives: meta robots and X-Robots-Tag (app/crawler/directives.py)."""

import pytest

from app.crawler.directives import PageDirectives, page_directives, robots_tag_directives

NAME = "SiteScopeBot"


@pytest.mark.parametrize(
    ("meta", "tags", "expected"),
    [
        ((), (), PageDirectives()),
        (("noindex",), (), PageDirectives(noindex=True)),
        (("nofollow",), (), PageDirectives(nofollow=True)),
        (("none",), (), PageDirectives(noindex=True, nofollow=True)),
        (("NoIndex", " NOFOLLOW "), (), PageDirectives(noindex=True, nofollow=True)),
        (("index", "follow", "all", "noarchive"), (), PageDirectives()),
        (("index", "noindex"), (), PageDirectives(noindex=True)),  # the restrictive one wins
        ((), ("noindex",), PageDirectives(noindex=True)),
        ((), ("noindex, nofollow",), PageDirectives(noindex=True, nofollow=True)),
        ((), ("none",), PageDirectives(noindex=True, nofollow=True)),
        ((), ("noindex", "nofollow"), PageDirectives(noindex=True, nofollow=True)),
        (("noindex",), ("nofollow",), PageDirectives(noindex=True, nofollow=True)),
        ((), ("otherbot: noindex, nofollow",), PageDirectives()),
        ((), ("sitescopebot: noindex",), PageDirectives(noindex=True)),
        ((), ("SiteScopeBot: nofollow",), PageDirectives(nofollow=True)),
        # A user-agent prefix applies to the rest of its own header only.
        ((), ("otherbot: noindex", "nofollow"), PageDirectives(nofollow=True)),
        ((), ("noindex, otherbot: nofollow",), PageDirectives(noindex=True)),
        ((), ("otherbot: noindex, SiteScopeBot: nofollow",), PageDirectives(nofollow=True)),
        # Directives with values are not user agents.
        ((), ("max-snippet: 20, noindex",), PageDirectives(noindex=True)),
        ((), ("unavailable_after: 25 Jun 2030 15:00:00 PST",), PageDirectives()),
        # Regression (review): a date with commas and colons is not a user-agent prefix.
        (
            (),
            ("unavailable_after: Sunday, 06-Nov-94 08:49:37 GMT, noindex",),
            PageDirectives(noindex=True),
        ),
        (
            (),
            ("unavailable_after: Sunday, 06-Nov-94 08:49:37 GMT, otherbot: nofollow, noindex",),
            PageDirectives(),  # after "otherbot:", the rest of the header is otherbot's
        ),
        ((), ("SiteScope Bot: noindex",), PageDirectives()),  # not a product token: ignored
        ((), ("",), PageDirectives()),
    ],
)
def test_page_directives(
    meta: tuple[str, ...], tags: tuple[str, ...], expected: PageDirectives
) -> None:
    assert page_directives(meta, tags, crawler_name=NAME) == expected


def test_robots_tag_directives_are_lowercase_and_scoped() -> None:
    assert robots_tag_directives("NoIndex, Max-Snippet: 5", crawler_name=NAME) == {
        "noindex",
        "max-snippet: 5",
    }
    assert robots_tag_directives("googlebot: noindex", crawler_name=NAME) == set()
    assert robots_tag_directives("googlebot: noindex", crawler_name="googlebot") == {"noindex"}
