# ADR-0015: robots.txt (RFC 9309) and per-host politeness through Redis

- Status: Accepted. Amended by [ADR-0017](0017-retries-dead-letters-cancellation-idempotency.md) (a politeness slot is a lease that its holder renews;
  decision 7's fixed expiry becomes the longest hold).
- Date: 2026-11-06
- Extends: [ADR-0006](0006-target-allowlist-ssrf-defenses.md),
  [ADR-0013](0013-safe-fetcher-pinned-connections.md)

## Context

Architecture section 12 requires the crawler to obey robots.txt (RFC 9309) and to limit its
request rate per host, across every crawler process. Requirements from the owner:

- robots.txt is fetched through the safe fetcher (scope check, DNS validation, pinning), with
  no other HTTP client, and without weakening the HTML-only rules for pages;
- an unreachable robots.txt fails safe; results are cached, without uncontrolled refetching;
- politeness is a shared, atomic reservation that works with many workers, and is testable with
  a controlled clock and no real waiting.

Python's `urllib.robotparser` applies the first matching rule and has no wildcards, so it is
not RFC 9309-compliant (architecture section 12.5).

## Decision

**robots.txt** (`app/crawler/robots.py`):

1. **Our own parser and matcher**, a small module covered by unit tests that include the RFC's
   own examples. Groups follow the RFC grammar (consecutive user-agent lines, other records
   never end a group); the crawler's name is matched case-insensitively by product token, with
   the `*` group as fallback; rules use longest match with allow winning ties; `*` and a final
   `$` are wildcards; both sides are compared with the same percent-encoding. Matching uses
   ordered `str.find` over literal segments, never regular expressions, so a hostile pattern
   cannot cause exponential backtracking. Measured on the development machine, a hostile
   500 KiB file of about 20,000 wildcard rules costs about 20 ms per check, so rule sets above
   1,000 rules are matched in a worker thread.
2. **Fetched through the safe fetcher** with a new content policy, `ROBOTS_TXT`: any media type,
   first 500 KiB kept (RFC 9309 section 2.5), the result flagged as `truncated`. The last line
   of a truncated file is dropped, because the cut may have changed its meaning (`Allow:
   /public/docs` cut to `Allow: /`; found in the Phase 4 review). Pages keep the default
   `HTML_PAGE` policy, exactly as in Phase 3. Redirects are followed for five hops, each
   validated by the fetcher.
3. **Status mapping** (section 2.3.1): 2xx parsed; 4xx other than 429, and more than five
   redirects or a loop, mean no restrictions; 5xx, 429, and every network, TLS, protocol, size or
   policy failure (including a redirect to an unapproved target or a blocked address) mean
   complete disallow. `rate_limited` (no politeness slot) is not a result for the origin and is
   never cached.
4. **Caching.** One Redis entry per origin and crawler name: 24 hours at most
   (`SITESCOPE_ROBOTS_CACHE_SECONDS`), or `SITESCOPE_ROBOTS_RETRY_SECONDS` (300 s) for a complete
   disallow caused by an unreachable file. Each process also keeps results in memory for at most
   60 s, never past the Redis entry's expiry, and concurrent checks of one origin share one
   fetch (single flight).
5. **Crawl-delay** (non-standard) raises the host's politeness interval, capped by
   `SITESCOPE_ROBOTS_MAX_CRAWL_DELAY_SECONDS`.

**Per-host politeness** (`app/crawler/politeness.py`):

6. **A request gate in the fetcher.** `SafeFetcher` takes an optional `RequestGate`. Every
   request hop, redirect hops and robots.txt included, takes a slot of its host after the scope
   check and before DNS resolution, and holds it until the response is read. No slot within
   `SITESCOPE_POLITENESS_MAX_WAIT_SECONDS` fails the fetch as `rate_limited` (retryable) before
   anything is sent.
7. **One Redis Lua script** grants slots atomically, by Redis time (`TIME`), with three rules
   per host:
   - **first come, first served**: a request joins the host's queue on its first attempt, and
     only the oldest waiters can get a slot;
   - **spacing**: a slot is granted only once `SITESCOPE_POLITENESS_INTERVAL_SECONDS` (default
     1 s) has passed since the host's previous start. The waiter that is next in line is told
     the exact remaining time and sleeps that long; the others poll;
   - **concurrency**: at most `SITESCOPE_POLITENESS_MAX_CONCURRENT_PER_HOST` (default 1) slots
     are held at once.

   A slot expires on its own after the fetch deadline plus 10 s, and a waiter that stops asking
   (its process crashed) leaves the queue after its longest sleep plus 5 s, so a crashed process
   cannot block a host for long. Keys share a hash tag per host and all expire.

   `TIME` is the Redis host's wall clock, and the WSL 2 VM stepped it back about one second
   during a Phase 4 test run. A last start in the future is therefore replaced by the current
   time (the real gap stays at least one interval, and the wait is one interval, not the step),
   and a new waiter always queues after the current tail. A forward step can shorten one gap.
8. **Crawl-delay** is kept per host as the largest delay found in the robots.txt of any of its
   origins (a second small Lua script); a file without Crawl-delay changes nothing. The delay is
   applied before the robots.txt result is cached.
9. **Testability.** Only the slot store talks to Redis. The gate takes an injected clock and
   sleep, so unit tests run it against an in-memory store without waiting: a fake clock for
   simple cases, and a small virtual-time simulation for concurrent waiters. The Lua scripts are
   tested against real Redis.

**Revision during the Phase 4 review.** The first version of the script reserved slots for
future starts (GCRA with a burst of one), held while the requester slept. The independent review
measured on Redis that a task that had just released its slot re-reserved before the waiters
woke up: four tasks on one host got 71, 1, 1 and 1 grants in 70 s, and the other three failed as
`rate_limited`, which loses URLs and makes a 1-worker crawl differ from an N-worker crawl. The
queue replaces the reservations; a regression test replays the measured scenario in virtual time
and on Redis.

## Consequences

- No new dependency. The parser is small, and its behavior is pinned by tests rather than by a
  third-party library's interpretation.
- robots.txt can never be fetched around the SSRF defenses: it uses the same fetcher, and a test
  shows that a robots.txt host resolving to a blocked address is never contacted.
- Requests to one host are serialized by default (one in flight, one per second), across
  processes, and served in arrival order. Throughput per host is bounded by design
  (architecture section 19).
- Waiters that are not next in line poll Redis every 0.1 s, a few calls per second per waiting
  request. Each request waits at most the maximum wait, which counts against the fetch deadline
  and the task deadline.
- Politeness is per host name. Two names served by one machine are limited separately.
- Different processes may each fetch an expired robots.txt once; those fetches still pass the
  gate. A cross-process fetch lock was not added.
- 429 responses to robots.txt disallow the origin, a deliberately conservative reading. An
  origin whose robots.txt stays unreachable stays disallowed; the RFC's option to treat a file
  unreachable for 30 days as unavailable is not used.

## Alternatives considered

- **A third-party RFC 9309 parser** (for example a binding to Google's C++ parser): a new native
  dependency for logic that fits in a small, fully tested module.
- **A per-process rate limiter:** cannot hold a shared limit with several workers.
- **A Redis token bucket or a simple lock per host:** a lock with expiry serializes requests but
  does not space them; a token bucket allows bursts.
- **GCRA reservations for future starts** (the first version, see above): exact spacing with one
  key, but unfair when slots are also capped, and waiters starved.
- **An in-process FIFO in front of Redis:** fair within one process only; Phase 5 runs several.
- **Waiting for the rate slot before claiming the URL:** the lease would not cover the wait, but
  workers would reserve slots for URLs they might never get. The architecture already puts the
  wait inside the task deadline (section 10.2).
