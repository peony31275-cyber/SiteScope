# ADR-0017: Durable retries, dead letters, cancellation, and idempotent job creation

- Status: Accepted
- Date: 2026-11-07
- Extends: [ADR-0003](0003-at-least-once-leases-fencing.md),
  [ADR-0004](0004-level-synchronous-bfs.md),
  [ADR-0014](0014-crawl-state-transactions-and-redirects.md),
  [ADR-0016](0016-distributed-execution-redis-streams.md)
- Amends: [ADR-0015](0015-robots-txt-and-per-host-politeness.md) (politeness slots become
  renewable leases), [ADR-0016](0016-distributed-execution-redis-streams.md) (recovery defaults,
  the dead-letter record, poison messages)

## Context

Until Phase 5, every failure ended its URL: a timeout, a 503 or a refused politeness slot was
final. Messages that could never be settled were redelivered forever, a job could not be
stopped, and a client that retried `POST /api/v1/crawls` after a lost response created a second
job. Phase 6 must add, without weakening any earlier guarantee:

- retries of transient failures only, with bounded attempts, exponential backoff with jitter,
  `Retry-After`, and state that survives restarts and is safe with several schedulers;
- dead-lettering of malformed, unmatched and poison messages, idempotent and bounded;
- cancellation of queued and running jobs that cannot be undone by stale messages;
- an `Idempotency-Key` on job creation, with PostgreSQL as the only authority;
- faster recovery from crashed workers, including their politeness slots.

Every existing rule stays: PostgreSQL is the source of truth (ADR-0001), the state row is the
outbox (ADR-0002), effects are idempotent and fenced (ADR-0003, ADR-0010, ADR-0011), BFS is
level-synchronous (ADR-0004), and no message is acknowledged before its outcome is committed
(ADR-0016).

## Decision

### 1. Which failures are retried

`app/workers/retry.py` classifies a task's result:

- **Retryable:** `dns_error` (a temporary resolution failure), `connection_error`,
  `connect_timeout`, `read_timeout`, `deadline_exceeded` (the fetch deadline or the task
  deadline), `truncated`, `rate_limited` (no politeness slot in time; nothing was sent),
  `infrastructure_error` (Redis failed during the task, decision 3), and `http_error` with a
  status in `SITESCOPE_RETRY_HTTP_STATUSES` (default 429, 500, 502, 503, 504).
- **Permanent:** everything else: invalid URLs, scope and SSRF refusals
  (`target_not_allowed`, `blocked_destination`), `dns_not_found`, robots.txt decisions, TLS
  errors, redirect problems, unsupported content, oversized bodies, parse errors, and every
  other HTTP status. The settings refuse any client error in the retry list except 408, 425 and
  429, so 404 and 410 can never be retried.

### 2. A retry is a fenced transition back to `pending`

`record_result(..., retry_delay=d)` is the retry branch of T5. It is the same fenced update as a
result, with the worker's own token, but it sets `state = 'pending'`,
`next_attempt_at = clock_timestamp() + d`, releases the lease, keeps `outcome` and
`result_epoch` NULL, stores the HTTP status and the error, and writes a `url.retry_scheduled`
event in the same transaction. Zero rows means the lease was lost: the transaction rolls back
and nothing is written (`FENCED`), exactly as for a result.

- **No migration.** `next_attempt_at`, `attempt_count`, the dispatch index and every CHECK and
  trigger exist since migrations 0001 and 0002, and they allow this transition: the 0002 trigger
  only constrains rows that are, or become, `in_progress`.
- **Attempts.** Only a claim counts an attempt (T4, `attempt_count + 1`, a new token). The retry
  transition never changes `attempt_count`, so it never decreases, and a duplicate or replayed
  result cannot create an attempt without a claim. Crashed attempts count too.
- **Exhaustion.** A retryable failure of the last allowed attempt
  (`attempt == SITESCOPE_CRAWL_MAX_ATTEMPTS`) is recorded as the URL's final result, with its
  own outcome (for example `read_timeout`, or `http_error` with status 503). A last attempt that
  crashed still ends as `max_attempts_exceeded` (ADR-0014).
- **Delay.** Full jitter: `uniform(0, min(SITESCOPE_RETRY_MAX_DELAY_SECONDS,
  SITESCOPE_RETRY_BASE_DELAY_SECONDS * 2^(attempt - 1)))`, and at least a valid `Retry-After`.
  A `Retry-After` longer than `SITESCOPE_RETRY_AFTER_MAX_SECONDS` is not waited for: the failure
  is final. A shorter retry would ignore the server's request, and a much longer one would hold
  the whole BFS level.
- **`Retry-After`** (RFC 9110, section 10.2.3) is read on 429 and 503 responses only:
  `delay-seconds` (1 to 10 ASCII digits), or an HTTP-date in any of its three forms, counted
  from the response's own `Date` header when it is valid, so that the server's clock is compared
  with the server's clock. A date in the past means 0 s; anything malformed is ignored, and so
  is a date that leaves the representable range in UTC (the independent review found that
  `Fri, 31 Dec 9999 23:59:59 -0100` raised `OverflowError` out of the fetcher).
- **Dispatch.** The dispatcher already selects `pending` rows whose `next_attempt_at` has passed
  (`FOR UPDATE SKIP LOCKED`). A retry therefore needs no timer anywhere: it survives restarts of
  every process, any scheduler publishes it when it is due, and concurrent schedulers publish it
  once per due time (the row lock and the compare-and-set on `pending`).
- **Nothing is bypassed.** The next attempt is an ordinary task: a new claim, the robots.txt
  check, the politeness gate, the safe fetcher with its scope, SSRF and DNS-rebinding defenses,
  and the worker's concurrency limit.
- **Strict BFS.** A URL waiting for a retry is unfinished, so its level cannot advance until the
  retry succeeds or the last attempt fails (ADR-0004).
- **No retry storms.** Jitter spreads retries that failed together; per-host politeness and the
  per-job in-flight window bound the request rate whatever the retry schedule.

### 3. Infrastructure errors

A Redis error inside a task (the politeness gate, the robots.txt cache) is recorded as a
retryable failed attempt, `infrastructure_error`, instead of leaving the lease to expire. An
error before the claim (PostgreSQL or Redis unreachable) is handled again in the same process a
few times with backoff, within half the reclaim idle time (after that `XAUTOCLAIM` could hand
the message out again), and the message then stays pending. An uncertain outcome is still never
acknowledged.

### 4. Dead letters and poison messages

- **Kinds:** `malformed` (it breaks the message contract), `unknown_url`, `job_mismatch`, and
  `max_deliveries_exceeded`.
- **Poison.** A worker reads the delivery count (`XPENDING`, `times_delivered`) of every message
  it reclaims; if it cannot, it handles the messages as first deliveries. A delivery count alone
  does not prove poison: every `XAUTOCLAIM` counts one, also while PostgreSQL is down and every
  handling fails before its claim. The count therefore acts only where the attempt limit cannot.
  A message delivered more than `SITESCOPE_STREAM_MAX_DELIVERIES` times (default 10) whose URL
  is `queued` with its attempts used up (possible only after `SITESCOPE_CRAWL_MAX_ATTEMPTS` was
  lowered) can never be claimed, and would stay pending forever. A quarantine claim takes such a
  URL with a new token (no attempt counted), and that token fails it as
  `max_deliveries_exceeded` through the normal fenced update, with a `url.dead_lettered` event.
  Every other URL follows the usual rules:
  - a URL with attempts left is claimed (claims count attempts, which bound the work);
  - an expired last attempt is recovered as `max_attempts_exceeded` (ADR-0014);
  - a live lease is left alone;
  - a URL that needs nothing (finished, pending, job not running) is settled.

  The first version also quarantined claimable URLs and expired leases. The independent review
  found that a PostgreSQL outage longer than ten reclaim intervals then failed URLs that were
  never tried, seeds included.
- **Order.** The database transition commits first; only then is the message dead-lettered and
  acknowledged. If that fails, the message stays pending and its redelivery finds the URL
  finished.
- **Idempotent.** One Lua script acknowledges the entry and copies it to `<prefix>:crawl:dlq`
  only if that `XACK` removed it from the pending entries list. Two consumers, or a retry after
  a lost reply, copy an entry at most once.
- **Contents:** the original entry ID, `reason` (as in Phase 5), `kind`, the delivery count, the
  consumer, the task's `job_id` and `url_id`, and the values of the contract fields
  (shortened). Any other field is listed by name only, so an arbitrary payload, a URL for
  example, is never copied.
- **Retention and visibility.** The dead-letter stream is capped near 10,000 entries (on every
  write and at maintenance). `python -m app.scheduler.status` lists the newest ones.

### 5. Cancellation

`POST /api/v1/crawls/{job_id}/cancel`:

- **T7.** Under the job row lock: `queued` becomes `cancelled`; `running` becomes `cancelling`,
  and the same transaction cancels the job's `pending` and `queued` URLs. If none of its URLs is
  in progress, the job becomes `cancelled` in the same transaction. Repeating the request, or
  cancelling a finished job, changes nothing. The response is `202` while the job is
  `cancelling`, `200` otherwise.
- **T7's locks.** T7 never locks an `in_progress` URL (lock order, architecture section 9).
  - It locks the job row `FOR NO KEY UPDATE`. That serializes it with every other writer of
    the job row, but not with foreign-key checks (`FOR KEY SHARE`).
  - With `FOR UPDATE` it deadlocked with a quarantine, which holds its queued URL row while its
    event insert checks the job row's key. The Phase 6 self-review and the independent review
    both found this; a regression test reproduces the interleaving.
- **In-flight policy.** Each worker checks every `SITESCOPE_CRAWL_CANCEL_CHECK_SECONDS` whether
  the jobs of its running tasks are being cancelled. It stops those tasks and records their URLs
  `cancelled` (outcome `job_cancelled`) with its own token, a fenced update that also requires
  the job to be `cancelling`. A task that is already committing its result finishes, and the
  result is kept. A retry scheduled meanwhile is cancelled by the finalizer.
- **Finalizer.** Every scheduler pass, for each `cancelling` job: cancel `pending` and `queued`
  URLs again; cancel `in_progress` URLs whose lease expired (their worker is gone, and if it was
  only frozen, its late result is fenced out); then, under the job row lock, mark the job
  `cancelled` once no URL is unfinished. The single-process driver settles a job that was
  cancelled during its run the same way.
- **`cancelled` means done.** The job is marked `cancelled` only when none of its URLs is in
  progress and none can start again. Nothing revives it: dispatch, claims, reconciliation,
  redirect admission and level advancement all require a `running` job, so a stale or duplicate
  message only meets a refused claim, and is acknowledged.
- **Stream entries** of a cancelled job are not deleted. Workers acknowledge them as no-ops
  when they arrive, and retention trims them. Request handlers never touch Redis (ADR-0002).

### 6. Idempotent job creation

- **Storage.** `POST /api/v1/crawls` accepts an optional `Idempotency-Key` header: 1 to 255
  visible ASCII characters, otherwise `422`. The key and a fingerprint of the normalized request
  (SHA-256 of the normalized seed URL, `max_depth` and `max_pages`) are stored on the job row in
  its own transaction, with `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`. The columns
  and the unique constraint exist since migration 0001.
- **Same key, equivalent request:** the job the first request created, `200` with
  `Idempotent-Replayed: true`, and nothing new is written.
- **Same key, different request:** `409`, with code `idempotency_key_reused`.
- **No key:** unchanged (`201`, a new job each time).
- **Concurrency.** The unique index serializes concurrent requests: one inserts, the others wait
  for its commit and replay its job (or insert, if it rolled back). A failed creation rolls the
  key back with the job.
- **Retention.** A key is remembered for `SITESCOPE_IDEMPOTENCY_KEY_TTL_SECONDS` (default 24 h).
  An expired key is cleared from its job when it is used again (in the same transaction), and
  by the scheduler's maintenance in batches; the job itself stays. Redis is never involved.

### 7. Politeness slots are renewable leases

This amends ADR-0015 decision 7: a slot no longer expires a fixed time after its grant.

- **Lease.** A slot is granted with a lease of `SITESCOPE_POLITENESS_SLOT_LEASE_SECONDS`
  (default 15 s). A background task renews it every third of the lease while the request runs.
- **Renewal script.** A Lua script extends only a slot that is still held and unexpired: an
  expired slot may already have been given to someone else, and is never revived. The
  concurrency limit therefore holds in every case.
- **Longest hold.** Renewal stops `slot_ttl` (the fetch deadline + 10 s) after the grant, so a
  live request holds its slot no longer than before. A holder that stalls between computing its
  last renewal and the script running can keep the slot up to one more lease (the cap is
  computed with the holder's clock).
- **Recovery.** The slot of a crashed or frozen process frees within one lease (15 s instead of
  70 s by default), and a live request keeps its slot.
- **Trade-off.** A holder frozen for longer than a lease loses its slot (`politeness.slot_lost`)
  and, when it resumes, may overlap with one other request to its host.

This is not a renewal of URL leases: ADR-0016 decision 7 still holds.

### 8. Shorter recovery defaults

This amends the defaults in ADR-0016's consequences:

- `SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS` goes from 120 to 100: still at least the 90 s
  lease.
- `SITESCOPE_SCHEDULER_RECONCILE_AFTER_SECONDS` goes from 600 to 300.

The scan intervals are unchanged (`XAUTOCLAIM` every 15 s, reconciliation at most every 30 s,
in keyset pages), so the shorter thresholds add no queries; they only act on stale work sooner.

## Consequences

- **Guarantees.** The guarantee stays at-least-once delivery with idempotent, fencing-protected
  effects, never exactly-once. A retry is a new attempt through a new claim, and an HTTP request
  can repeat after a crash.
- **Bounded work.** A URL is claimed at most `SITESCOPE_CRAWL_MAX_ATTEMPTS` times, plus one
  recovery or quarantine claim that sends nothing.
- **Delivery counts** are read only for reclaimed messages. A message republished as a new entry
  starts at zero; `attempt_count` remains the authoritative bound.
- **Errors before the claim.** A message whose handling fails before its claim on every
  delivery (a bug, not an outage) is not dead-lettered: the delivery count cannot tell it from
  an outage. It keeps failing visibly, with an ERROR log and a traceback each time.
- **Cancellation latency.** Normally one cancellation check plus one scheduler pass. A URL whose
  worker crashed is cancelled when its lease expires, so a job can stay `cancelling` for up to
  one lease.
- **Not retried.** A `Retry-After` beyond the cap is final. `robots_unreachable` stays permanent
  for its URL: the origin's result is cached for `SITESCOPE_ROBOTS_RETRY_SECONDS`, so a quick
  retry would mostly meet the same cached answer.
- **The worker checks cancellation in PostgreSQL** with one small query per check interval
  while it has running tasks.

## Alternatives considered

- **Retry timers in memory, or a delayed queue in Redis.** A crash would lose the timers, and
  Redis is not authoritative (ADR-0001). PostgreSQL already stores `next_attempt_at`.
- **Counting attempts when a retry is scheduled.** Crashed attempts would not count, and a
  replayed result could add attempts. Claims count them instead.
- **Clamping a long `Retry-After` to the cap.** That retries earlier than the server asked, which
  is impolite and likely to fail again.
- **Marking the job `cancelled` at once.** In-flight tasks could still commit results afterwards,
  and the status would be false.
- **Deleting a cancelled job's stream entries from the API.** Request handlers never touch Redis,
  and the entries are harmless no-ops.
- **A separate idempotency table, or keys in Redis.** The job row records the key atomically with
  the job it created; Redis is not authoritative.
- **A shorter fixed slot TTL.** A slow live request would lose its slot mid-request and break the
  concurrency limit.
- **Dead-lettering by attempt count only.** Attempts count claims, and a message whose URL can
  no longer be claimed never reaches one: it would stay pending forever.
- **Dead-lettering by delivery count only.** Deliveries also grow while PostgreSQL is down, so
  an outage would fail URLs that were never tried (decision 4).
