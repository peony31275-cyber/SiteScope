"""robots.txt (RFC 9309): parsing, matching, and the per-origin policy the crawler obeys.

**Parsing** (``parse_robots``, a pure function):

* The file is read as UTF-8: invalid bytes are replaced and a byte order mark is ignored. At most
  500 KiB is read, because the fetcher's ``ROBOTS_TXT`` content policy truncates larger files
  (section 2.5). The last line of a truncated file is dropped: the cut may have shortened it,
  and ``Allow: /public/docs`` cut to ``Allow: /`` would allow the whole site.
* Lines end with CR, LF or CR LF, ``#`` starts a comment, and keys are case-insensitive.
* A group is one or more ``user-agent`` lines followed by rules (``allow``, ``disallow``); a
  ``user-agent`` line after a rule starts a new group (section 2.1). Other records (``sitemap``,
  unknown keys) and the non-standard ``crawl-delay`` never end a group (section 2.2.4). Rules
  before the first ``user-agent`` line belong to no group and are ignored.
* The groups whose user-agent matches the crawler's name are combined. Matching is
  case-insensitive and uses the value's leading product token, so ``SiteScopeBot/1.0`` matches.
  Without such a group, the ``*`` groups are combined; without those, no rule applies
  (section 2.2.1).
* A rule's path pattern must start with ``/`` or ``*`` (section 2.2.2), and an empty pattern
  matches nothing. Other values are not valid patterns and are ignored.

**Matching** (``RobotsRules.allows``):

* Rules match the URL's path and query, case-sensitively, after both sides get the same
  percent-encoding (section 2.2.2; ``urls.normalize_target_text``).
* ``*`` matches any sequence of characters, and a final ``$`` anchors the end (section 2.2.3).
  A pattern matches these two characters literally only when it encodes them (``%2A``, ``%24``),
  so they are encoded in the URL too before comparison.
* The most specific matching rule wins: the one with the longest pattern. Between an allow rule
  and a disallow rule of the same length, allow wins. No match means allowed, and
  ``/robots.txt`` itself is always allowed.
* Matching uses no regular expressions. Literal segments are found in order with ``str.find``,
  which cannot backtrack exponentially. Runs of ``*`` are collapsed, and a pattern with more
  literal characters than the longest URL the crawler stores (``MAX_URL_BYTES``) can never match,
  so it is dropped.

**Fetch results** (section 2.3.1), mapped by ``RobotsPolicy``:

* 2xx: the rules are parsed;
* 4xx other than 429 ("unavailable"), and more than five redirects or a redirect loop: no
  restrictions;
* 5xx, and 429 by a deliberately conservative choice ("unreachable"), plus every network, TLS,
  protocol, size or policy failure, including a redirect to an unapproved target or a blocked
  address: complete disallow until a later retry;
* redirects are followed for up to five hops, and the safe fetcher validates each one;
* ``rate_limited`` (no politeness slot in time) is not a result for the origin: it is raised to
  the caller and not cached.

**Caching** (section 2.4). Results are cached in Redis per origin and crawler name, for at most
24 hours (``SITESCOPE_ROBOTS_CACHE_SECONDS``). A complete disallow caused by an unreachable file
is cached for ``SITESCOPE_ROBOTS_RETRY_SECONDS``; the next check after that fetches the file
again. Each process also keeps recent results in memory, for at most 60 s and never past the
Redis entry's own expiry. Concurrent checks of one origin in a process share a single fetch.
Separate processes may each fetch an expired file once; those requests still pass the per-host
politeness gate.
"""

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from redis.asyncio import Redis

from app.crawler.content import ROBOTS_TXT
from app.crawler.destinations import Origin
from app.crawler.errors import FailureKind, FetchError
from app.crawler.urls import (
    MAX_URL_BYTES,
    NormalizedUrl,
    normalize_target_text,
    request_target,
)

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.crawler.fetcher import SafeFetcher

logger = logging.getLogger(__name__)

# RFC 9309, section 2.3.1.2: follow at least five consecutive redirects.
ROBOTS_MAX_REDIRECTS: Final = 5
# Rule sets larger than this are matched in a worker thread. Matching is linear per rule, but a
# hostile 500 KiB file holds about 20,000 wildcard rules: about 20 ms per check, measured in
# Phase 4, which must not block the event loop.
INLINE_MATCH_RULES: Final = 1000
# Version of the cached JSON entries; an entry of another version is fetched again.
CACHE_FORMAT: Final = 1
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_PRODUCT_TOKEN = re.compile(r"[A-Za-z_-]+")
_STARS = re.compile(r"\*+")
_CRAWL_DELAY = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6})?")
# Fetch failures after which robots.txt counts as "unavailable": no restrictions apply.
_UNAVAILABLE_KINDS: Final = frozenset({FailureKind.TOO_MANY_REDIRECTS, FailureKind.REDIRECT_LOOP})


@dataclass(frozen=True, slots=True)
class RobotsRule:
    """One allow or disallow rule, with its pattern in comparable form (``normalize_pattern``)."""

    allow: bool
    pattern: str
    _segments: tuple[str, ...] = field(init=False, repr=False, compare=False)
    _anchored: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        anchored = self.pattern.endswith("$")
        body = self.pattern[:-1] if anchored else self.pattern
        object.__setattr__(self, "_anchored", anchored)
        object.__setattr__(self, "_segments", tuple(body.split("*")))

    def matches(self, subject: str) -> bool:
        """Whether the pattern matches ``subject``, a request target in comparable form."""
        segments = self._segments
        first = segments[0]
        if not subject.startswith(first):
            return False
        if len(segments) == 1:
            return len(subject) == len(first) if self._anchored else True
        position = len(first)
        for segment in segments[1:-1]:
            found = subject.find(segment, position)
            if found < 0:
                return False
            position = found + len(segment)
        last = segments[-1]
        if self._anchored:
            return len(subject) - len(last) >= position and subject.endswith(last)
        return subject.find(last, position) >= 0


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """The rules of one robots.txt file that apply to one crawler."""

    rules: tuple[RobotsRule, ...] = ()
    crawl_delay: float | None = None  # seconds; the non-standard Crawl-delay record

    def __post_init__(self) -> None:
        # Most specific first: longest pattern, then allow before disallow (section 2.2.2).
        ordered = sorted(dict.fromkeys(self.rules), key=lambda r: (-len(r.pattern), not r.allow))
        object.__setattr__(self, "rules", tuple(ordered))

    def allows(self, url: NormalizedUrl) -> bool:
        return self.allows_target(request_target(url))

    def allows_target(self, target: str) -> bool:
        """Whether a request target (``/path?query``, normalized as in stored URLs) is allowed."""
        if target.split("?", 1)[0] == "/robots.txt":
            return True
        subject = target.replace("*", "%2A").replace("$", "%24")
        for rule in self.rules:
            if rule.matches(subject):
                return rule.allow
        return True


def drop_partial_line(body: bytes) -> bytes:
    """A truncated file without its last line, which the cut may have shortened.

    A cut line can change meaning: ``Allow: /public/docs`` cut to ``Allow: /`` would allow the
    whole site. Only lines that end with CR or LF before the parsing limit are parsed.
    """
    return body[: max(body.rfind(b"\n"), body.rfind(b"\r")) + 1]


def normalize_pattern(value: str) -> str | None:
    """The comparable form of a rule's path pattern, or None if the rule is to be ignored.

    Literal parts get the percent-encoding of stored URLs, ``*`` stays a wildcard, a final
    ``$`` stays an anchor, and any other ``$`` becomes the literal ``%24``. Returns None for a
    value that is empty or does not start with ``/`` or ``*``, and for a pattern that cannot
    match any URL of at most ``MAX_URL_BYTES`` bytes.
    """
    if not value or value[0] not in "/*":
        return None
    anchored = value.endswith("$")
    body = _STARS.sub("*", value[:-1] if anchored else value)
    parts = [normalize_target_text(part).replace("$", "%24") for part in body.split("*")]
    if sum(len(part) for part in parts) > MAX_URL_BYTES:
        return None
    return "*".join(parts) + ("$" if anchored else "")


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[RobotsRule] = field(default_factory=list)
    crawl_delays: list[float] = field(default_factory=list)


def parse_robots(body: bytes | str, *, crawler_name: str) -> RobotsRules:
    """The rules of a robots.txt file that apply to ``crawler_name``. Never raises."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    text = text.removeprefix("﻿")
    groups: list[_Group] = []
    current: _Group | None = None
    reading_agents = False
    for line in _LINE_BREAK.split(text):
        key, separator, value = line.split("#", 1)[0].partition(":")
        if not separator:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if current is None or not reading_agents:
                current = _Group()
                groups.append(current)
                reading_agents = True
            current.agents.append(value)
        elif key in ("allow", "disallow"):
            reading_agents = False
            pattern = normalize_pattern(value)
            if current is not None and pattern is not None:
                current.rules.append(RobotsRule(allow=key == "allow", pattern=pattern))
        elif key == "crawl-delay":
            delay = _crawl_delay(value)
            if current is not None and delay is not None:
                current.crawl_delays.append(delay)
        # Other records (sitemap, unknown keys) are ignored and never end a group.

    name = crawler_name.lower()
    matched = [group for group in groups if name in {_agent(a) for a in group.agents}]
    if not matched:
        matched = [group for group in groups if "*" in group.agents]
    delays = [delay for group in matched for delay in group.crawl_delays]
    return RobotsRules(
        rules=tuple(rule for group in matched for rule in group.rules),
        crawl_delay=max(delays) if delays else None,
    )


def _agent(value: str) -> str | None:
    """The product token at the start of a user-agent value, lowercase (``*`` gives None)."""
    match = _PRODUCT_TOKEN.match(value)
    return match.group().lower() if match is not None else None


def _crawl_delay(value: str) -> float | None:
    return float(value) if _CRAWL_DELAY.fullmatch(value) else None


class RobotsStatus(StrEnum):
    """What the crawler learned about an origin's robots.txt."""

    PARSED = "parsed"  # 2xx: its rules apply
    UNAVAILABLE = "unavailable"  # 4xx (not 429), too many redirects: no restrictions
    UNREACHABLE = "unreachable"  # 5xx, 429, network or policy failure: complete disallow


@dataclass(frozen=True, slots=True)
class OriginRobots:
    """The robots.txt state of one origin, as cached."""

    status: RobotsStatus
    rules: RobotsRules = RobotsRules()
    detail: str | None = None  # why the file is unreachable; names no URL, safe to store

    def allows(self, url: NormalizedUrl) -> bool:
        if self.status is RobotsStatus.UNREACHABLE:
            return False
        return self.rules.allows(url)

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": CACHE_FORMAT,
                "status": self.status.value,
                "rules": [[int(rule.allow), rule.pattern] for rule in self.rules.rules],
                "crawl_delay": self.rules.crawl_delay,
                "detail": self.detail,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "OriginRobots | None":
        """Decode a cache entry; None for another format version or a damaged entry."""
        try:
            data = json.loads(raw)
            if data.get("v") != CACHE_FORMAT:
                return None
            delay = data["crawl_delay"]
            return cls(
                status=RobotsStatus(data["status"]),
                rules=RobotsRules(
                    rules=tuple(
                        RobotsRule(allow=bool(allow), pattern=str(pattern))
                        for allow, pattern in data["rules"]
                    ),
                    crawl_delay=float(delay) if delay is not None else None,
                ),
                detail=data["detail"],
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            return None


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    """Whether robots.txt lets the crawler fetch one URL."""

    allowed: bool
    status: RobotsStatus
    crawl_delay: float | None
    detail: str | None


class CrawlDelaySink(Protocol):
    """Receives each origin's Crawl-delay (or None) when its robots.txt is fetched: the
    politeness gate, which keeps the largest delay of a host's origins."""

    async def set_crawl_delay(self, host: str, delay: float | None, *, ttl: float) -> None: ...


@dataclass(frozen=True, slots=True)
class _Remembered:
    robots: OriginRobots
    expires: float  # local monotonic clock


class RobotsPolicy:
    """Decides, per URL, what the origin's robots.txt allows. See the module docstring."""

    def __init__(
        self,
        *,
        fetcher: "SafeFetcher",
        redis: Redis,
        crawler_name: str,
        key_prefix: str,
        cache_seconds: int = 86_400,
        retry_seconds: int = 300,
        delay_sink: CrawlDelaySink | None = None,
        memory_seconds: float = 60.0,
        memory_size: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._redis = redis
        self._name = crawler_name
        self._prefix = key_prefix
        self._cache_seconds = cache_seconds
        self._retry_seconds = retry_seconds
        self._delay_sink = delay_sink
        self._memory_seconds = memory_seconds
        self._memory_size = memory_size
        self._clock = clock
        self._memory: OrderedDict[str, _Remembered] = OrderedDict()
        self._loading: dict[str, asyncio.Task[OriginRobots]] = {}

    def cache_key(self, origin: Origin) -> str:
        return f"{self._prefix}:robots:{self._name.lower()}:{origin}"

    async def check(self, url: NormalizedUrl) -> RobotsDecision:
        """What robots.txt says about ``url``.

        Raises ``FetchError`` with ``rate_limited`` if robots.txt had to be fetched and got no
        politeness slot in time, and Redis errors if the cache is unavailable.
        """
        robots = await self.for_origin(Origin.of(url))
        if len(robots.rules.rules) > INLINE_MATCH_RULES:
            allowed = await asyncio.to_thread(robots.allows, url)
        else:
            allowed = robots.allows(url)
        return RobotsDecision(
            allowed=allowed,
            status=robots.status,
            crawl_delay=robots.rules.crawl_delay,
            detail=robots.detail,
        )

    async def for_origin(self, origin: Origin) -> OriginRobots:
        key = str(origin)
        remembered = self._memory.get(key)
        if remembered is not None and remembered.expires > self._clock():
            self._memory.move_to_end(key)
            return remembered.robots
        task = self._loading.get(key)
        if task is None:
            # Concurrent checks of one origin share this load (single flight). It runs as its
            # own task, so a caller that is cancelled does not cancel the others' load.
            task = asyncio.create_task(self._load(origin), name=f"robots.txt {key}")
            self._loading[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        return await asyncio.shield(task)

    def _finished(self, key: str, task: "asyncio.Task[OriginRobots]") -> None:
        if self._loading.get(key) is task:
            del self._loading[key]
        if not task.cancelled():
            task.exception()  # retrieved: every waiter re-raises it; nothing is left unhandled

    async def _load(self, origin: Origin) -> OriginRobots:
        key = self.cache_key(origin)
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.get(key)
            pipe.pttl(key)
            raw, ttl_ms = await pipe.execute()
        if raw is not None and (cached := OriginRobots.from_json(raw)) is not None:
            self._remember(origin, cached, ttl=max(int(ttl_ms), 0) / 1000)
            return cached
        robots = await self._fetch(origin)
        unreachable = robots.status is RobotsStatus.UNREACHABLE
        ttl = self._retry_seconds if unreachable else self._cache_seconds
        # The crawl delay first: if caching then fails, the next check fetches robots.txt again
        # and applies its delay again. The other order could leave a cached file whose delay
        # was never applied, for as long as the cache entry lives.
        if self._delay_sink is not None:
            await self._delay_sink.set_crawl_delay(origin.host, robots.rules.crawl_delay, ttl=ttl)
        await self._redis.set(key, robots.to_json(), ex=ttl)
        self._remember(origin, robots, ttl=ttl)
        return robots

    async def _fetch(self, origin: Origin) -> OriginRobots:
        try:
            result = await self._fetcher.fetch(
                f"{origin}/robots.txt", max_redirects=ROBOTS_MAX_REDIRECTS, content=ROBOTS_TXT
            )
        except FetchError as exc:
            robots = _robots_after_failure(exc)
            logger.info(
                "robots.txt could not be read",
                extra={
                    "event": "robots.fetch_failed",
                    "host": origin.host,
                    "status": robots.status.value,
                    "failure": exc.kind.value,
                    "http_status": exc.http_status,
                },
            )
            return robots
        body = drop_partial_line(result.body) if result.truncated else result.body
        # Parsing up to 500 KiB is CPU work: keep it off the event loop.
        rules = await asyncio.to_thread(parse_robots, body, crawler_name=self._name)
        logger.info(
            "robots.txt fetched",
            extra={
                "event": "robots.fetched",
                "host": origin.host,
                "rules": len(rules.rules),
                "crawl_delay": rules.crawl_delay,
                "truncated": result.truncated,
                "redirects": len(result.redirects),
            },
        )
        return OriginRobots(status=RobotsStatus.PARSED, rules=rules)

    def _remember(self, origin: Origin, robots: OriginRobots, *, ttl: float) -> None:
        key = str(origin)
        lifetime = min(self._memory_seconds, ttl)
        if lifetime <= 0 or self._memory_size <= 0:
            return
        self._memory[key] = _Remembered(robots, self._clock() + lifetime)
        self._memory.move_to_end(key)
        while len(self._memory) > self._memory_size:
            self._memory.popitem(last=False)


def _robots_after_failure(error: FetchError) -> OriginRobots:
    """The robots.txt state after a failed fetch (section 2.3.1). Re-raises ``rate_limited``."""
    if error.kind is FailureKind.RATE_LIMITED:
        raise error
    status = error.http_status
    client_error = status is not None and 400 <= status < 500 and status != 429
    if error.kind is FailureKind.HTTP_ERROR and client_error:
        return OriginRobots(status=RobotsStatus.UNAVAILABLE)
    if error.kind in _UNAVAILABLE_KINDS:
        return OriginRobots(status=RobotsStatus.UNAVAILABLE)
    reason = f"HTTP {status}" if error.kind is FailureKind.HTTP_ERROR else error.kind.value
    return OriginRobots(
        status=RobotsStatus.UNREACHABLE,
        detail=f"robots.txt unreachable ({reason}): {error.message}"[:500],
    )


def build_robots_policy(
    settings: "Settings",
    *,
    fetcher: "SafeFetcher",
    redis: Redis,
    delay_sink: CrawlDelaySink | None = None,
) -> RobotsPolicy:
    """The robots.txt policy configured from the application settings."""
    return RobotsPolicy(
        fetcher=fetcher,
        redis=redis,
        crawler_name=settings.crawler_name,
        key_prefix=settings.redis_key_prefix,
        cache_seconds=settings.robots_cache_seconds,
        retry_seconds=settings.robots_retry_seconds,
        delay_sink=delay_sink,
    )
