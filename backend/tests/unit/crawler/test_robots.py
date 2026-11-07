"""robots.txt parsing and matching (RFC 9309), and the fetch-result mapping. No network."""

import pytest

from app.crawler.errors import FailureKind, FetchError
from app.crawler.robots import (
    OriginRobots,
    RobotsRule,
    RobotsRules,
    RobotsStatus,
    _robots_after_failure,
    drop_partial_line,
    normalize_pattern,
    parse_robots,
)
from app.crawler.urls import normalize_url

NAME = "SiteScopeBot"


def allowed(robots: str, path: str, name: str = NAME) -> bool:
    """Whether ``path`` (normalized as the crawler stores URLs) is allowed for ``name``."""
    url = normalize_url(f"https://example.com{path}")
    return parse_robots(robots, crawler_name=name).allows(url)


# RFC 9309, section 5.1 ("Simple Example").
RFC_EXAMPLE = """\
User-Agent: *
Disallow: *.gif$
Disallow: /example/
Allow: /publications/

User-Agent: foobot
Disallow:/
Allow:/example/page.html
Allow:/example/allowed.gif

User-Agent: barbot
User-Agent: bazbot
Disallow: /example/page.html

User-Agent: quxbot

EOF
"""


@pytest.mark.parametrize(
    ("name", "path", "expected"),
    [
        ("foobot", "/example/page.html", True),
        ("foobot", "/example/allowed.gif", True),
        ("foobot", "/example/other.html", False),
        ("foobot", "/", False),
        ("barbot", "/example/page.html", False),
        ("bazbot", "/example/page.html", False),
        ("barbot", "/example/other.html", True),
        ("quxbot", "/example/page.html", True),  # an empty group: no rules apply
        ("quxbot", "/x.gif", True),
        ("otherbot", "/publications/x.html", True),
        ("otherbot", "/example/page.html", False),
        ("otherbot", "/images/a.gif", False),
        ("otherbot", "/images/a.gif?x=1", True),  # "$" anchors the end of path and query
        ("otherbot", "/images/a.png", True),
    ],
)
def test_the_rfc_9309_example(name: str, path: str, expected: bool) -> None:
    assert allowed(RFC_EXAMPLE, path, name) is expected


def test_the_most_specific_rule_wins() -> None:
    # RFC 9309, section 5.2.
    robots = "User-Agent: foobot\nAllow: /example/page/\nDisallow: /example/page/disallowed.gif\n"
    assert allowed(robots, "/example/page/", "foobot")
    assert allowed(robots, "/example/page/other.html", "foobot")
    assert not allowed(robots, "/example/page/disallowed.gif", "foobot")


@pytest.mark.parametrize(
    ("rules", "path", "expected"),
    [
        ("Allow: /page\nDisallow: /page", "/page", True),  # same length: allow wins
        ("Disallow: /page\nAllow: /page", "/page", True),
        ("Allow: /$\nDisallow: /", "/", True),
        ("Allow: /$\nDisallow: /", "/x", False),
        ("Disallow: /a\nAllow: /a/b", "/a/b/c", True),
        ("Allow: /a\nDisallow: /a/b", "/a/b/c", False),
        ("Disallow: /*.php\nAllow: /public/*.php", "/public/x.php", True),
    ],
)
def test_rule_precedence(rules: str, path: str, expected: bool) -> None:
    assert allowed(f"User-agent: *\n{rules}\n", path) is expected


@pytest.mark.parametrize(
    ("pattern", "matching", "not_matching"),
    [
        (
            "/fish",
            ["/fish", "/fish.html", "/fish/salmon.html", "/fishheads", "/fish.php?id=anything"],
            ["/Fish.asp", "/catfish", "/?id=fish", "/desert/fish"],
        ),
        ("/fish*", ["/fish", "/fish.html", "/fishheads/yummy.html"], ["/Fish.html", "/catfish"]),
        ("/fish/", ["/fish/", "/fish/?id=anything", "/fish/salmon.htm"], ["/fish", "/fish.html"]),
        (
            "/*.php",
            ["/index.php", "/folder/filename.php?parameters", "/folder/any.php.file.html"],
            ["/", "/windows.PHP"],
        ),
        (
            "/*.php$",
            ["/filename.php", "/folder/filename.php"],
            ["/filename.php?parameters", "/filename.php/", "/filename.php5", "/windows.PHP"],
        ),
        ("/fish*.php", ["/fish.php", "/fishheads/catfish.php?parameters"], ["/Fish.PHP"]),
        ("*", ["/", "/anything?q=1"], []),
        ("/*?", ["/search?q=1", "/a/b?"], ["/search", "/a/b"]),
        ("/$", ["/"], ["/index.html", "/?q=1"]),
        ("/a*b*c$", ["/abc", "/a-b-c", "/aXbYc", "/abcbc"], ["/abcd", "/acb", "/ab"]),
    ],
)
def test_wildcards_and_anchors(pattern: str, matching: list[str], not_matching: list[str]) -> None:
    robots = f"User-agent: *\nDisallow: {pattern}\n"
    for path in matching:
        assert not allowed(robots, path), path
    for path in not_matching:
        assert allowed(robots, path), path


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        # RFC 9309, section 2.2.2: both sides compared with the same percent-encoding.
        ("/foo/bar/ツ", "/foo/bar/ツ"),
        ("/foo/bar/ツ", "/foo/bar/%E3%83%84"),
        ("/foo/bar/%E3%83%84", "/foo/bar/ツ"),
        ("/foo/bar/%e3%83%84", "/foo/bar/%E3%83%84"),  # hex digits are case-insensitive
        ("/foo/bar/%62%61%7A", "/foo/bar/baz"),  # encoded unreserved characters are decoded
        ("/a b", "/a%20b"),
        ("/q?x=a b", "/q?x=a%20b"),
        ("/a%2Ab", "/a*b"),  # a literal "*" must be encoded in the pattern
        ("/price%24", "/price$"),  # and so must a literal "$"
        ("/pr$ice", "/pr$ice"),  # a "$" before the end is a literal character
    ],
)
def test_patterns_match_after_percent_encoding_normalization(pattern: str, path: str) -> None:
    assert not allowed(f"User-agent: *\nDisallow: {pattern}\n", path)


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("/a%2Fb", "/a/b"),  # an encoded "/" is not a path separator
        ("/a/b", "/a%2Fb"),
        ("/Private", "/private"),  # paths are case-sensitive
        ("/price$", "/price$"),  # here "$" anchors the end: "/price$" is a longer path
    ],
)
def test_patterns_that_must_not_match(pattern: str, path: str) -> None:
    assert allowed(f"User-agent: *\nDisallow: {pattern}\n", path)


def test_robots_txt_itself_is_always_allowed() -> None:
    assert allowed("User-agent: *\nDisallow: /\n", "/robots.txt")
    assert not allowed("User-agent: *\nDisallow: /\n", "/robots.txt.bak")


def test_groups_are_selected_by_product_token() -> None:
    robots = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: sitescopebot/1.0\nDisallow: /a\n\n"  # case-insensitive, version ignored
        "User-agent: other\nDisallow: /b\n\n"
        "User-agent: SiteScopeBot\nDisallow: /c\n"  # matching groups are combined
    )
    rules = parse_robots(robots, crawler_name=NAME)
    assert {rule.pattern for rule in rules.rules} == {"/a", "/c"}
    assert allowed(robots, "/b")
    assert not allowed(robots, "/c/x")


@pytest.mark.parametrize("agent", ["SiteScope", "SiteScopeBotX", "Bot", "*SiteScopeBot"])
def test_other_product_tokens_do_not_match(agent: str) -> None:
    robots = f"User-agent: {agent}\nDisallow: /\n"
    assert allowed(robots, "/x")


def test_the_star_group_applies_only_without_a_matching_group() -> None:
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: SiteScopeBot\nAllow: /\n"
    assert allowed(robots, "/x")
    assert not allowed(robots, "/x", name="OtherBot")
    two_star_groups = "User-agent: *\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n"
    assert not allowed(two_star_groups, "/a")
    assert not allowed(two_star_groups, "/b")


def test_group_boundaries() -> None:
    robots = (
        "Disallow: /before-any-group\n"  # belongs to no group: ignored
        "User-agent: a\n"
        "Sitemap: https://example.com/sitemap.xml\n"  # other records never end a group
        "Crawl-delay: 3\n"
        "User-agent: SiteScopeBot\n"  # still the same group as "a"
        "Disallow: /shared\n"
        "Unknown-record: value\n"
        "Disallow: /also-shared\n"
        "User-agent: b\n"  # a user-agent line after a rule starts a new group
        "Disallow: /only-b\n"
    )
    rules = parse_robots(robots, crawler_name=NAME)
    assert [rule.pattern for rule in rules.rules] == ["/also-shared", "/shared"]
    assert rules.crawl_delay == 3.0
    assert allowed(robots, "/before-any-group")
    assert allowed(robots, "/only-b")


def test_no_groups_or_no_rules_mean_everything_is_allowed() -> None:
    assert parse_robots("", crawler_name=NAME) == RobotsRules()
    assert parse_robots("# only comments\n\nSitemap: /s.xml\n", crawler_name=NAME) == RobotsRules()
    assert allowed("User-agent: *\nDisallow:\n", "/anything")


def test_lines_comments_keys_and_encodings() -> None:
    robots = (
        b"\xef\xbb\xbf"  # a UTF-8 byte order mark
        b"USER-AGENT : *\r\n"
        b"# a comment line\r"
        b"DisAllow:/a # a trailing comment\n"
        b"Disallow: /caf\xc3\xa9\n"
        b"Disallow: /bad\xff\n"  # invalid UTF-8 is replaced, the line is still parsed
        b"Allow /no-colon\n"
        b"Disallow: no-leading-slash\n"
    )
    rules = parse_robots(robots, crawler_name=NAME)
    assert sorted(rule.pattern for rule in rules.rules) == ["/a", "/bad%EF%BF%BD", "/caf%C3%A9"]
    assert all(not rule.allow for rule in rules.rules)


def test_rules_are_ordered_deduplicated_and_normalized() -> None:
    rules = parse_robots(
        "User-agent: *\nDisallow: /a\nDisallow: /a\nAllow: /ab\nDisallow: /ab\nDisallow: /a***b\n",
        crawler_name=NAME,
    ).rules
    # Longest pattern first (wildcards count, as in Google's reference parser); on equal
    # length, allow first.
    assert rules == (
        RobotsRule(allow=False, pattern="/a*b"),
        RobotsRule(allow=True, pattern="/ab"),
        RobotsRule(allow=False, pattern="/ab"),
        RobotsRule(allow=False, pattern="/a"),
    )


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("/a", "/a"),
        ("*", "*"),
        ("*.gif$", "*.gif$"),
        ("/a**b", "/a*b"),
        ("/%7euser", "/~user"),
        ("/a$b$", "/a%24b$"),
        ("/50%", "/50%25"),  # a stray "%" is a literal percent sign
        ("", None),
        ("a", None),
        ("?q", None),
        ("/" + "x" * 2048, None),  # longer than any stored URL: can never match
    ],
)
def test_normalize_pattern(value: str, pattern: str | None) -> None:
    assert normalize_pattern(value) == pattern


@pytest.mark.parametrize(
    ("body", "kept"),
    [
        (b"Disallow: /a\nAllow: /pub", b"Disallow: /a\n"),  # the cut line is dropped
        (b"Disallow: /a\r\nAllow: /pub", b"Disallow: /a\r\n"),
        (b"Disallow: /a\rAllow: /pub", b"Disallow: /a\r"),
        (b"Disallow: /a\r", b"Disallow: /a\r"),  # cut between CR and LF: the line is complete
        (b"Disallow: /a\n", b"Disallow: /a\n"),
        (b"Allow: /pub", b""),  # one cut line: nothing is left
        (b"", b""),
    ],
)
def test_a_truncated_file_loses_its_cut_line(body: bytes, kept: bytes) -> None:
    assert drop_partial_line(body) == kept


def test_many_wildcards_cannot_make_matching_slow() -> None:
    # A regex engine would backtrack exponentially here; segment search stays linear.
    robots = "User-agent: *\nDisallow: /" + "*a" * 1000 + "b\n"
    assert allowed(robots, "/" + "a" * 2000)


@pytest.mark.parametrize(
    ("lines", "delay"),
    [
        ("Crawl-delay: 5", 5.0),
        ("Crawl-delay: 0.5", 0.5),
        ("Crawl-delay: 2\nCrawl-delay: 7", 7.0),
        ("Crawl-delay: soon", None),
        ("Crawl-delay: -1", None),
        ("Crawl-delay: 1e9", None),
        ("", None),
    ],
)
def test_crawl_delay(lines: str, delay: float | None) -> None:
    rules = parse_robots(f"User-agent: SiteScopeBot\n{lines}\nDisallow: /x\n", crawler_name=NAME)
    assert rules.crawl_delay == delay


def test_a_crawl_delay_comes_only_from_the_selected_groups() -> None:
    robots = (
        "User-agent: *\nCrawl-delay: 9\nDisallow: /x\n\n"
        "User-agent: SiteScopeBot\nCrawl-delay: 2\nDisallow: /y\n"
    )
    assert parse_robots(robots, crawler_name=NAME).crawl_delay == 2.0
    assert parse_robots(robots, crawler_name="OtherBot").crawl_delay == 9.0


def test_blank_lines_and_other_records_do_not_separate_user_agent_lines() -> None:
    # RFC 9309 grammar: a group's user-agent lines run until its first rule, whatever blank
    # lines or other records (here the non-standard Crawl-delay) come between them.
    robots = "User-agent: *\nCrawl-delay: 9\n\nUser-agent: SiteScopeBot\n\nDisallow: /x\n"
    for name in (NAME, "OtherBot"):
        rules = parse_robots(robots, crawler_name=name)
        assert [rule.pattern for rule in rules.rules] == ["/x"]
        assert rules.crawl_delay == 9.0


def test_cache_entries_round_trip() -> None:
    robots = OriginRobots(
        status=RobotsStatus.PARSED,
        rules=parse_robots(
            "User-agent: *\nAllow: /a$\nDisallow: /\nCrawl-delay: 4\n", crawler_name=NAME
        ),
    )
    assert OriginRobots.from_json(robots.to_json()) == robots
    unreachable = OriginRobots(RobotsStatus.UNREACHABLE, detail="robots.txt unreachable (HTTP 503)")
    assert OriginRobots.from_json(unreachable.to_json()) == unreachable


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"v": 2, "status": "parsed", "rules": [], "crawl_delay": null, "detail": null}',
        '{"v": 1, "status": "unknown", "rules": [], "crawl_delay": null, "detail": null}',
        '{"v": 1, "status": "parsed", "rules": [[1]], "crawl_delay": null, "detail": null}',
        '{"v": 1, "status": "parsed", "rules": []}',
    ],
)
def test_damaged_or_foreign_cache_entries_are_ignored(raw: str) -> None:
    assert OriginRobots.from_json(raw) is None


def test_an_unreachable_origin_disallows_everything() -> None:
    robots = OriginRobots(RobotsStatus.UNREACHABLE, detail="x")
    assert not robots.allows(normalize_url("https://example.com/"))
    assert OriginRobots(RobotsStatus.UNAVAILABLE).allows(normalize_url("https://example.com/"))


UNAVAILABLE = RobotsStatus.UNAVAILABLE
UNREACHABLE = RobotsStatus.UNREACHABLE


@pytest.mark.parametrize(
    ("kind", "status", "expected"),
    [
        (FailureKind.HTTP_ERROR, 400, UNAVAILABLE),
        (FailureKind.HTTP_ERROR, 401, UNAVAILABLE),
        (FailureKind.HTTP_ERROR, 403, UNAVAILABLE),
        (FailureKind.HTTP_ERROR, 404, UNAVAILABLE),
        (FailureKind.HTTP_ERROR, 410, UNAVAILABLE),
        (FailureKind.HTTP_ERROR, 429, UNREACHABLE),  # deliberately conservative
        (FailureKind.HTTP_ERROR, 500, UNREACHABLE),
        (FailureKind.HTTP_ERROR, 503, UNREACHABLE),
        (FailureKind.HTTP_ERROR, 304, UNREACHABLE),
        (FailureKind.TOO_MANY_REDIRECTS, 301, UNAVAILABLE),
        (FailureKind.REDIRECT_LOOP, 302, UNAVAILABLE),
        (FailureKind.INVALID_REDIRECT, 301, UNREACHABLE),
        (FailureKind.TARGET_NOT_ALLOWED, None, UNREACHABLE),
        (FailureKind.BLOCKED_DESTINATION, None, UNREACHABLE),
        (FailureKind.DNS_NOT_FOUND, None, UNREACHABLE),
        (FailureKind.DNS_ERROR, None, UNREACHABLE),
        (FailureKind.CONNECTION_ERROR, None, UNREACHABLE),
        (FailureKind.CONNECT_TIMEOUT, None, UNREACHABLE),
        (FailureKind.READ_TIMEOUT, None, UNREACHABLE),
        (FailureKind.DEADLINE_EXCEEDED, None, UNREACHABLE),
        (FailureKind.TLS_ERROR, None, UNREACHABLE),
        (FailureKind.TRUNCATED, 200, UNREACHABLE),
        (FailureKind.MALFORMED_RESPONSE, None, UNREACHABLE),
        (FailureKind.TOO_LARGE, 200, UNREACHABLE),
        (FailureKind.UNSUPPORTED_CONTENT_ENCODING, 200, UNREACHABLE),
        (FailureKind.UNSUPPORTED_CONTENT_TYPE, 200, UNREACHABLE),
        (FailureKind.INVALID_URL, None, UNREACHABLE),
    ],
)
def test_fetch_failures_map_to_rfc_9309_states(
    kind: FailureKind, status: int | None, expected: RobotsStatus
) -> None:
    robots = _robots_after_failure(FetchError(kind, "message", url="u", http_status=status))
    assert robots.status is expected
    assert robots.rules == RobotsRules()
    if expected is UNREACHABLE:
        assert robots.detail is not None
        assert robots.detail.startswith("robots.txt unreachable (")


def test_every_failure_kind_is_mapped() -> None:
    for kind in FailureKind:
        error = FetchError(kind, "m", url="u", http_status=500)
        if kind is FailureKind.RATE_LIMITED:
            with pytest.raises(FetchError):  # not a result for the origin: never cached
                _robots_after_failure(error)
        else:
            assert _robots_after_failure(error).status in (UNAVAILABLE, UNREACHABLE)
