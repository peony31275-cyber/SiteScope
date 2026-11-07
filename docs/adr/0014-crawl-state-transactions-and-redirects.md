# ADR-0014: Crawl state transactions, same-level redirects, and page directives

- Status: Accepted
- Date: 2026-11-06
- Extends: [ADR-0003](0003-at-least-once-leases-fencing.md),
  [ADR-0004](0004-level-synchronous-bfs.md), [ADR-0011](0011-fencing-tokens-issued-only-by-claims.md)

## Context

Phase 4 turns the schema of Phase 2B and the fetcher and parser of Phase 3 into a crawl. Several
choices were left open:

- **The shape of the result transaction.** The architecture sketch (section 9) locked the URL
  row with `SELECT ... FOR UPDATE`, wrote the page, and updated the URL last. ADR-0011 lists the
  residual risk of that shape: a worker that ignores a fenced update matching no row would
  commit a page without an outcome.
- **Redirects** (architecture section 8.5): follow them inside the fetch, or record the 3xx
  response and admit its target as a new URL.
- **Page-level robots directives.** The parser reports `<meta name="robots">` and the fetcher
  keeps `X-Robots-Tag`, but the schema had no place for the decisions they imply.
- **Exhausted attempts.** A URL whose holders keep crashing must end, as `failed`. The database
  requires `result_epoch = lease_epoch` for that outcome, and the owner forbids copying the
  column (`SET result_epoch = lease_epoch`).
- **Retries.** Durable retries with backoff are Phase 6 work.

## Decision

1. **The fenced update comes first.** The result transaction (`services.crawl_state.
   record_result`) starts with the conditional update of the URL row:
   `WHERE id = :url AND state = 'in_progress' AND lease_epoch = :token`, setting
   `result_epoch = :token`. The token is always the one the claim returned to this worker. If
   the update matches no row, the transaction raises, rolls back, and reports `FENCED`; nothing
   else has been written. Otherwise the same transaction, holding the URL row lock, writes the
   page, its links, its `search_documents` row, and any redirect admission. Any failure rolls
   all of it back.
2. **Redirects use the fetcher's "return" mode** (`max_redirects=0`). A 3xx response is recorded
   as `fetched` with outcome `redirect` and its `redirect_target`. If the target is in the
   job's scope (the seed's host and an approved target), the redirect hop limit
   (`SITESCOPE_FETCH_MAX_REDIRECTS`) allows it, and budget remains, the same transaction locks
   the job row (after the URL row) and admits the target at the **same depth**, with
   `redirect_hops + 1`. The target then goes through the whole pipeline itself: scope,
   robots.txt of its own origin, DNS validation, politeness.
3. **Directives are stored on the page.** Migration `0003` adds `pages.robots_noindex`,
   `pages.robots_nofollow` and `page_links.nofollow`. `noindex` (or `none`) means the page is
   stored without a `search_documents` row, so it never enters the index outbox. `nofollow` (or
   `none`) on the page, or `rel="nofollow"` on a link, means level advancement never admits
   that link. Links are recorded in every case, for link analysis. Directives come from
   `<meta name="robots">`, `<meta name="<crawler name>">` and `X-Robots-Tag` headers; a header's
   user-agent prefix scopes its directives to that crawler.
4. **Recovery claims.** A URL whose lease expired after its last allowed attempt is failed as
   `max_attempts_exceeded` in one transaction: a *recovery claim* first issues a new token
   (`lease_epoch + 1`, no attempt counted, only while the lease is expired by the database
   clock), then that token records the failure through the same fenced update as any result.
   Every outcome therefore names the grant that recorded it, and a stalled holder is fenced out.
5. **Outcomes.** A received response is `fetched` (`html_stored`, `redirect`, `http_error`,
   `non_html`, `too_large`, ...). A policy refusal is `skipped` (`robots_disallowed`,
   `robots_unreachable`, `target_not_allowed`, `blocked_destination`). Everything else is
   `failed`, with the `FailureKind` value as its outcome. Until Phase 6, retryable failures are
   terminal too.
6. **A single-process driver** (`app.workers.local_crawler`) runs these use cases for development
   and tests: dispatch (T3) without a stream, claims, fenced results, level advancement. URLs
   left `queued` or with expired leases by a stopped run are picked up by the next run, through
   normal claims. Nothing resets a live lease.

## Consequences

- **The zero-row case is the normal fencing path**, not a corner case: every stale-token test
  exercises it, and the tests show that nothing (page, links, search document, redirect
  admission) survives a rejected result. A test also captures the SQL sent and checks that both
  the fencing condition and `result_epoch` are bound to the worker's token.
- **Redirect targets are deduplicated and budgeted like links**, under the same job row lock.
  Concurrent redirects to one target admit it once; concurrent redirects never exceed the
  budget (both tested with eight concurrent transactions).
- **Lock order is preserved:** the URL row (fenced update) before the job row (redirect
  admission). Level advancement locks the job row first but then only reads URL rows and
  inserts new ones, so there is no cycle.
- **Unique checks can wait too.** `INSERT ... ON CONFLICT` waits for any transaction that is
  updating a row with the same key. Admitting a redirect target that another task is recording
  at that moment (A redirects to B while B's result transaction is open) would therefore wait
  while holding the job row lock, and deadlock if B's transaction then needs that lock for a
  redirect of its own. Redirect admission therefore skips a known target with a plain `SELECT`
  first, which never waits. No other transaction can insert the target in between, because
  every admission holds the job row lock, and `ON CONFLICT DO NOTHING` stays as the backstop.
  This was found by analysis during the Phase 4 review, and a regression test reproduces the
  wait.
- **Job timestamps tolerate clock steps.** A test run caught the database host's clock (the
  WSL 2 VM) stepping back about one second inside a transaction, and a finished job violated
  `ck_crawl_jobs_timestamps_ordered`. `started_at` and `finished_at` are now
  `GREATEST(clock_timestamp(), created_at)`; the constraint is unchanged.
- **Level advancement reads plain columns under the lock.** Locking the job through the ORM
  entity returned a stale copy whenever the session already held the object (the identity map
  is not refreshed by `FOR UPDATE`), and the job could complete at the wrong level. The
  independent review found it; a test reuses one session across two levels.
- **`noindex` pages are stored.** Their text stays in `pages` (for link analysis and future
  views) but never reaches search. Correctness invariant 6 becomes "every page without
  `noindex` has exactly one `search_documents` row".
- **Retryable failures end the URL in Phase 4.** A transient error, or no politeness slot
  within the maximum wait, fails that URL; Phase 6 turns them into durable retries.
- **A job whose seed redirected is `completed`**, even if the redirect target then failed: the
  seed was fetched. Job status describes whether the crawl ran, not how much it found.

## Alternatives considered

- **Lock with `SELECT ... FOR UPDATE`, update last (the earlier sketch):** equivalent when
  written correctly, but the fenced update is then the last step, and forgetting to check its
  row count commits a page without an outcome (ADR-0011). Leading with the fenced update makes
  the check structural.
- **Follow redirects inside the fetch:** simpler, but the final URL would be stored under the
  requested URL, the target would bypass deduplication and the budget, and robots.txt of a
  redirect target on another origin would not be consulted.
- **Store directives only as a page flag for noindex:** `nofollow` per link (`rel`) and per page
  are separate facts, and level advancement needs both in SQL.
- **Fail exhausted URLs by binding the epoch read under a row lock:** correct, but it records an
  outcome under a token that no claim issued to the recovering process. The recovery claim keeps
  "every outcome names the grant that recorded it" (ADR-0011) without exceptions.
