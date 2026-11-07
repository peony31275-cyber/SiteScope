# ADR-0004: Level-synchronous BFS with scheduler-side admission

- Status: Accepted. The deduplication key is amended by
  [ADR-0009](0009-dedup-on-normalized-url.md) (`normalized_url` instead of `url_hash`).
  Extended by [ADR-0014](0014-crawl-state-transactions-and-redirects.md) (same-level
  redirect admission, nofollow), by [ADR-0016](0016-distributed-execution-redis-streams.md) (several schedulers without a leader),
  and by [ADR-0017](0017-retries-dead-letters-cancellation-idempotency.md) (a URL waiting for a retry keeps its level open).
- Date: 2026-11-06

## Context

Each job has a `max_depth` and a `max_pages` limit. Concurrent workers make both hard to enforce:

- **Depth.** If workers run freely, a URL can be discovered first through a longer path. It then records
  a depth greater than its true BFS distance, and under `max_depth` the set of crawled pages depends on
  timing.
- **Budget.** Enforcing `max_pages` while many workers discover links at once needs a serialized
  counter. That means contention on the job row for every page.

## Decision

- **Levels.** The dispatcher publishes only URLs whose depth equals the job's `current_level`.
- **Workers record, the scheduler admits.** Workers store every discovered link in `page_links`, with
  its position and an in-scope flag. They never create frontier rows for links.
- **Level advancement.** When a job has no unfinished URLs, the scheduler takes the job row lock and, in
  one transaction:
  1. Admits level `d + 1` from the links recorded by level-`d` pages:
     - deduplicated by `UNIQUE (job_id, url_hash)`;
     - in a deterministic order (source URL, then link position);
     - limited to the remaining budget.
  2. Updates `admitted_count` and `current_level`.

  If nothing can be admitted, the job completes instead.
- **Redirects.** The worker's result transaction admits redirect targets at the *same* depth, under the
  job row lock, so they finish within the current level.
- **Backstop.** `CHECK (admitted_count <= max_pages)`.

## Consequences

- Recorded depth equals the shortest-path distance through fetched pages (argument in
  [architecture section 8.3](../architecture.md#83-bfs-depth-correctness)).
- `max_pages` is exact under any concurrency. If the budget runs out mid-level, the admitted set is a
  deterministic prefix of BFS order.
- For identical site content, crawl results do not depend on worker count or timing. That enables strong
  end-to-end tests: a 1-worker crawl and an N-worker crawl must match.
- No per-page contention on the job row.
- **Cost:** a slow or retrying URL holds back the next level (per-level tail latency), and the scheduler
  adds up to one polling interval between levels.
- URLs that robots.txt disallows still consume budget.

## Alternatives considered

- **Free-running BFS with depth relaxation** (lower a URL's depth when a shorter path appears, then
  re-expand it): no level barrier, but more complex, and non-deterministic when the budget runs out.
- **Worker-side admission with a per-job lock:** exact budget, but a hot row and timing-dependent results.
- **Redis set + counter for deduplication and budget:** fast, but not transactional with PostgreSQL
  state, and retries double-count.
