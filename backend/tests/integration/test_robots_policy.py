"""The robots.txt policy against local servers on 127.0.0.1 and real Redis (database 15).

robots.txt files are served by ``LocalHttpServer`` and fetched through the real safe fetcher;
DNS answers come from ``ScriptedResolver`` (a test double). Results are cached in Redis under
a key prefix unique to each test.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TypeVar

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.crawler.content import ROBOTS_TXT_MAX_BYTES
from app.crawler.destinations import Origin
from app.crawler.errors import FailureKind, FetchError, RateLimitedError
from app.crawler.fetcher import SafeFetcher
from app.crawler.robots import INLINE_MATCH_RULES, RobotsPolicy, RobotsStatus
from app.crawler.urls import NormalizedUrl, normalize_url
from tests.fetch_support import (
    LocalHttpServer,
    RecordingBackend,
    Reply,
    ScriptedResolver,
    make_fetcher,
)

pytestmark = pytest.mark.anyio

LOOPBACK = ["127.0.0.1"]
CACHE_SECONDS = 3600
RETRY_SECONDS = 120
T = TypeVar("T")


@dataclass
class RecordingSink:
    """Test double of the politeness gate's crawl-delay input."""

    calls: list[tuple[str, float | None, float]] = field(default_factory=list)

    async def set_crawl_delay(self, host: str, delay: float | None, *, ttl: float) -> None:
        self.calls.append((host, delay, ttl))


def robots_reply(body: bytes, *, status: int = 200, delay: float = 0.0) -> Reply:
    return Reply(status=status, headers=[("Content-Type", "text/plain")], body=body, delay=delay)


def moved(location: str) -> Reply:
    return Reply(status=301, reason="Moved", headers=[("Location", location)])


class Site:
    """One local server and the robots.txt policy that crawls it."""

    def __init__(self, server: LocalHttpServer, fetcher: SafeFetcher, policy: RobotsPolicy) -> None:
        self.server = server
        self.fetcher = fetcher
        self.policy = policy

    def url(self, path: str) -> NormalizedUrl:
        return normalize_url(f"http://fixture.test:{self.server.port}{path}")

    @property
    def origin(self) -> Origin:
        return Origin.of(self.url("/"))

    def robots_requests(self) -> int:
        return sum(1 for request in self.server.requests if request.target == "/robots.txt")


@asynccontextmanager
async def site(
    routes: dict[str, Reply],
    redis_client: Redis,
    prefix: str,
    *,
    sink: RecordingSink | None = None,
    hosts: tuple[str, ...] = ("fixture.test",),
    resolver: ScriptedResolver | None = None,
    backend: RecordingBackend | None = None,
    memory_seconds: float = 60.0,
) -> AsyncIterator[Site]:
    async with LocalHttpServer(routes) as server:
        fetcher = make_fetcher(
            resolver=resolver or ScriptedResolver({host: [LOOPBACK] for host in hosts}),
            port=server.port,
            hosts=hosts,
            backend=backend,
        )
        yield Site(server, fetcher, new_policy(fetcher, redis_client, prefix, sink, memory_seconds))


def new_policy(
    fetcher: SafeFetcher,
    redis_client: Redis,
    prefix: str,
    sink: RecordingSink | None = None,
    memory_seconds: float = 60.0,
) -> RobotsPolicy:
    return RobotsPolicy(
        fetcher=fetcher,
        redis=redis_client,
        crawler_name="SiteScopeBot",
        key_prefix=prefix,
        cache_seconds=CACHE_SECONDS,
        retry_seconds=RETRY_SECONDS,
        delay_sink=sink,
        memory_seconds=memory_seconds,
    )


async def test_parsed_rules_are_obeyed_and_cached_for_other_processes(
    redis_client: Redis, redis_prefix: str
) -> None:
    body = b"User-agent: *\nDisallow: /private/\nCrawl-delay: 2\n"
    sink = RecordingSink()
    async with site(
        {"/robots.txt": robots_reply(body)}, redis_client, redis_prefix, sink=sink
    ) as s:
        assert (await s.policy.check(s.url("/public"))).allowed
        denied = await s.policy.check(s.url("/private/x"))
        assert (denied.allowed, denied.status, denied.crawl_delay) == (
            False,
            RobotsStatus.PARSED,
            2.0,
        )
        key = s.policy.cache_key(s.origin)
        assert key == f"{redis_prefix}:robots:sitescopebot:http://fixture.test:{s.server.port}"
        assert CACHE_SECONDS - 5 < await redis_client.ttl(key) <= CACHE_SECONDS
        cached = json.loads(await redis_client.get(key) or "{}")
        assert cached["status"] == "parsed"
        # Another process (a new policy) reads the shared cache instead of fetching again.
        other = new_policy(s.fetcher, redis_client, redis_prefix)
        assert not (await other.check(s.url("/private/y"))).allowed
        assert s.robots_requests() == 1
        [request] = s.server.requests
        assert request.headers["user-agent"].startswith("SiteScopeBot/")
    assert sink.calls == [("fixture.test", 2.0, CACHE_SECONDS)]


async def test_concurrent_checks_share_one_fetch(redis_client: Redis, redis_prefix: str) -> None:
    reply = robots_reply(b"User-agent: *\nDisallow: /x\n", delay=0.3)
    async with site({"/robots.txt": reply}, redis_client, redis_prefix) as s:
        decisions = await asyncio.gather(*(s.policy.check(s.url(f"/x{n}")) for n in range(10)))
        assert not any(decision.allowed for decision in decisions)
        assert s.robots_requests() == 1


@pytest.mark.parametrize(
    ("reply", "status", "allowed", "ttl"),
    [
        (Reply(status=404, reason="Not Found"), RobotsStatus.UNAVAILABLE, True, CACHE_SECONDS),
        (Reply(status=403, reason="Forbidden"), RobotsStatus.UNAVAILABLE, True, CACHE_SECONDS),
        (Reply(status=503, reason="Unavailable"), RobotsStatus.UNREACHABLE, False, RETRY_SECONDS),
        (Reply(status=500, reason="Error"), RobotsStatus.UNREACHABLE, False, RETRY_SECONDS),
        (Reply(status=429, reason="Too Many"), RobotsStatus.UNREACHABLE, False, RETRY_SECONDS),
        (Reply(raw=b""), RobotsStatus.UNREACHABLE, False, RETRY_SECONDS),  # no response at all
        (Reply(raw=b"NOT HTTP\r\n\r\n"), RobotsStatus.UNREACHABLE, False, RETRY_SECONDS),
    ],
)
async def test_status_codes_and_failures(
    redis_client: Redis,
    redis_prefix: str,
    reply: Reply,
    status: RobotsStatus,
    allowed: bool,
    ttl: int,
) -> None:
    sink = RecordingSink()
    async with site({"/robots.txt": reply}, redis_client, redis_prefix, sink=sink) as s:
        decision = await s.policy.check(s.url("/page"))
        assert (decision.status, decision.allowed) == (status, allowed)
        if status is RobotsStatus.UNREACHABLE:
            assert decision.detail is not None
            assert decision.detail.startswith("robots.txt unreachable (")
        assert ttl - 5 < await redis_client.ttl(s.policy.cache_key(s.origin)) <= ttl
    assert sink.calls == [("fixture.test", None, ttl)]


async def test_an_unreachable_file_is_fetched_again_after_its_retry_time(
    redis_client: Redis, redis_prefix: str
) -> None:
    routes = {"/robots.txt": Reply(status=503, reason="Unavailable")}
    async with site(routes, redis_client, redis_prefix, memory_seconds=0) as s:
        assert not (await s.policy.check(s.url("/a"))).allowed
        assert not (await s.policy.check(s.url("/a"))).allowed  # cached: no new request
        assert s.robots_requests() == 1
        await redis_client.delete(s.policy.cache_key(s.origin))  # the retry time has passed
        s.server.routes["/robots.txt"] = robots_reply(b"User-agent: *\nDisallow:\n")
        assert (await s.policy.check(s.url("/a"))).allowed
        assert s.robots_requests() == 2


async def test_redirects_are_followed_up_to_five_hops(
    redis_client: Redis, redis_prefix: str
) -> None:
    routes = {
        "/robots.txt": moved("/r1"),
        "/r1": moved("/r2"),
        "/r2": robots_reply(b"User-agent: *\nDisallow: /blocked\n"),
    }
    async with site(routes, redis_client, redis_prefix) as s:
        assert not (await s.policy.check(s.url("/blocked"))).allowed
        assert (await s.policy.check(s.url("/open"))).allowed


async def test_more_than_five_redirects_mean_no_restrictions(
    redis_client: Redis, redis_prefix: str
) -> None:
    routes = {"/robots.txt": moved("/r1")}
    routes.update({f"/r{n}": moved(f"/r{n + 1}") for n in range(1, 7)})
    async with site(routes, redis_client, redis_prefix) as s:
        decision = await s.policy.check(s.url("/anything"))
        assert (decision.status, decision.allowed) == (RobotsStatus.UNAVAILABLE, True)


async def test_a_redirect_to_an_unapproved_host_disallows_everything(
    redis_client: Redis, redis_prefix: str
) -> None:
    routes = {"/robots.txt": moved("http://elsewhere.test/robots.txt")}
    async with site(routes, redis_client, redis_prefix) as s:
        decision = await s.policy.check(s.url("/page"))
        assert (decision.status, decision.allowed) == (RobotsStatus.UNREACHABLE, False)
        assert decision.detail is not None
        assert "target_not_allowed" in decision.detail


async def test_a_blocked_destination_is_never_contacted(
    redis_client: Redis, redis_prefix: str
) -> None:
    # fixture.test is approved but its DNS answer is the cloud metadata address.
    resolver = ScriptedResolver({"fixture.test": [["169.254.169.254"]]})
    backend = RecordingBackend()
    async with site(
        {"/robots.txt": robots_reply(b"")},
        redis_client,
        redis_prefix,
        resolver=resolver,
        backend=backend,
    ) as s:
        decision = await s.policy.check(s.url("/page"))
        assert (decision.status, decision.allowed) == (RobotsStatus.UNREACHABLE, False)
        assert decision.detail is not None
        assert "blocked_destination" in decision.detail
        assert backend.targets == []
        assert s.server.connections == 0


async def test_only_the_first_500_kib_are_parsed(redis_client: Redis, redis_prefix: str) -> None:
    head = b"User-agent: *\nDisallow: /early\n"
    filler = b"# " + b"x" * 1000 + b"\n"
    body = head + filler * (ROBOTS_TXT_MAX_BYTES // len(filler) + 1) + b"Disallow: /late\n"
    assert len(body) > ROBOTS_TXT_MAX_BYTES
    async with site({"/robots.txt": robots_reply(body)}, redis_client, redis_prefix) as s:
        assert not (await s.policy.check(s.url("/early"))).allowed
        assert (await s.policy.check(s.url("/late"))).allowed  # beyond the parsing limit


async def test_a_line_cut_by_the_parsing_limit_is_ignored(
    redis_client: Redis, redis_prefix: str
) -> None:
    """Regression: the 500 KiB limit cuts "Allow: /public/docs" to "Allow: /", which would
    tie with "Disallow: /" and, since allow wins ties, allow the whole site."""
    head = b"User-agent: *\nDisallow: /\n"
    kept = b"Allow: /"  # what the limit leaves of the next line
    room = ROBOTS_TXT_MAX_BYTES - len(kept) - len(head)
    filler = b"#" + b"x" * (room - 2) + b"\n"  # one long comment line
    body = head + filler + b"Allow: /public/docs\n"
    assert body[:ROBOTS_TXT_MAX_BYTES].endswith(b"\n" + kept)
    async with site({"/robots.txt": robots_reply(body)}, redis_client, redis_prefix) as s:
        assert not (await s.policy.check(s.url("/anything"))).allowed
        assert not (await s.policy.check(s.url("/public/docs"))).allowed


async def test_large_rule_sets_are_matched_off_the_event_loop(
    redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = asyncio.to_thread

    async def spy(function: Callable[..., T], /, *args: object, **kwargs: object) -> T:
        calls.append(getattr(function, "__name__", "?"))
        return await original(function, *args, **kwargs)

    rules = b"".join(b"Disallow: /p%05d\n" % n for n in range(INLINE_MATCH_RULES + 500))
    body = b"User-agent: *\n" + rules + b"Allow: /p00007/open\n"
    async with site({"/robots.txt": robots_reply(body)}, redis_client, redis_prefix) as s:
        await s.policy.check(s.url("/warm-up"))  # fetch and parse first
        monkeypatch.setattr(asyncio, "to_thread", spy)
        assert not (await s.policy.check(s.url("/p01234/x"))).allowed
        assert (await s.policy.check(s.url("/p00007/open/page"))).allowed
        assert (await s.policy.check(s.url("/q"))).allowed
    assert calls == ["allows", "allows", "allows"]


async def test_robots_txt_is_cached_only_after_its_crawl_delay_is_applied(
    redis_client: Redis, redis_prefix: str
) -> None:
    """Review concern: if applying the crawl delay fails, robots.txt must not stay cached
    without it (for up to 24 hours); the next check fetches it and applies it again."""

    class FailingOnceSink(RecordingSink):
        async def set_crawl_delay(self, host: str, delay: float | None, *, ttl: float) -> None:
            if not self.calls:
                self.calls.append((host, delay, ttl))
                raise RedisConnectionError("Redis is unavailable")
            await super().set_crawl_delay(host, delay, ttl=ttl)

    sink = FailingOnceSink()
    body = b"User-agent: *\nCrawl-delay: 4\nDisallow: /x\n"
    routes = {"/robots.txt": robots_reply(body)}
    async with site(routes, redis_client, redis_prefix, sink=sink, memory_seconds=0) as s:
        with pytest.raises(RedisConnectionError):
            await s.policy.check(s.url("/x"))
        assert await redis_client.exists(s.policy.cache_key(s.origin)) == 0
        assert not (await s.policy.check(s.url("/x"))).allowed
        assert s.robots_requests() == 2
        assert await redis_client.exists(s.policy.cache_key(s.origin)) == 1
    assert [delay for _, delay, _ in sink.calls] == [4.0, 4.0]


async def test_a_damaged_cache_entry_is_replaced(redis_client: Redis, redis_prefix: str) -> None:
    body = b"User-agent: *\nDisallow: /x\n"
    async with site({"/robots.txt": robots_reply(body)}, redis_client, redis_prefix) as s:
        await redis_client.set(s.policy.cache_key(s.origin), "{not json", ex=60)
        assert not (await s.policy.check(s.url("/x"))).allowed
        assert s.robots_requests() == 1
        assert json.loads(await redis_client.get(s.policy.cache_key(s.origin)) or "{}")["v"] == 1


async def test_no_politeness_slot_is_not_cached(redis_client: Redis, redis_prefix: str) -> None:
    class RefusingGate:
        @asynccontextmanager
        async def slot(self, url: NormalizedUrl) -> AsyncIterator[None]:
            raise RateLimitedError("no request slot for fixture.test within 1 s")
            yield  # pragma: no cover - makes this an async generator

    async with LocalHttpServer({"/robots.txt": robots_reply(b"")}) as server:
        fetcher = make_fetcher(
            resolver=ScriptedResolver({"fixture.test": [LOOPBACK]}),
            port=server.port,
            gate=RefusingGate(),
        )
        policy = new_policy(fetcher, redis_client, redis_prefix)
        url = normalize_url(f"http://fixture.test:{server.port}/page")
        with pytest.raises(FetchError) as caught:
            await policy.check(url)
        assert caught.value.kind is FailureKind.RATE_LIMITED
        assert await redis_client.exists(policy.cache_key(Origin.of(url))) == 0
        assert server.connections == 0
