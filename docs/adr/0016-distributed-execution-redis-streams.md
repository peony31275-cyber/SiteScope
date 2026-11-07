# ADR-0016: Distributed execution over Redis Streams: publication, acknowledgment, recovery

- Status: Accepted
- Date: 2026-11-06
- Extends: [ADR-0001](0001-postgresql-source-of-truth.md),
  [ADR-0002](0002-transactional-outbox.md),
  [ADR-0003](0003-at-least-once-leases-fencing.md),
  [ADR-0004](0004-level-synchronous-bfs.md)

## Context

Phase 5 replaces the single-process driver with separate processes: a scheduler
(`python -m app.scheduler`) and any number of crawler workers (`python -m app.workers.crawler`),
connected by the Redis stream `<prefix>:crawl` and its consumer group `crawlers`. PostgreSQL
stays the source of truth; the stream only delivers work (ADR-0001). The owner's requirements:
at-least-once delivery with idempotent, fenced effects; an uncertain outcome is never
acknowledged; recovery from worker crashes, stalls, scheduler crashes, and Redis or PostgreSQL
outages; several schedulers must be safe; bounded stream retention; backpressure.

The decisions below answer: when `XADD` happens relative to the dispatch commit, what a worker
does with a message it cannot claim, how lost messages come back without unbounded duplicates,
whether leases need renewal, whether schedulers need a leader, and how trimming stays safe.

## Decision

1. **Message contract.** Version 1 is `{v: "1", job_id, url_id}`, identifiers only; the worker
   acts on PostgreSQL state, never on the message. A malformed message is copied to the
   dead-letter stream `<prefix>:crawl:dlq` (capped near 10,000 entries) and acknowledged.
   Replies are decoded leniently (bytes that are not UTF-8 become U+FFFD): decoded strictly,
   one such entry failed every `XREADGROUP` and `XAUTOCLAIM` batch it was in, and the valid
   tasks of the batch stayed pending forever (found by the independent review). Now it is
   malformed like any other, and dead-lettered.

2. **Publication inside the dispatch transaction** (the outbox of ADR-0002). The dispatcher
   locks due `pending` rows of the job's current level (`FOR UPDATE SKIP LOCKED`), publishes
   them (`XADD`, pipelined: one round trip), marks them `queued` with their entry IDs
   (compare-and-set on `pending`), and commits. A crash or a failed commit after `XADD` leaves
   the rows `pending`: they are published again, a duplicate, never a lost task. This `XADD` is
   the one network call made inside a transaction, bounded by the Redis socket timeout. The
   scheduler's sessions set `idle_in_transaction_session_timeout` (twice the Redis timeout plus
   5 s), so a stalled scheduler cannot hold row locks for long. The workers' sessions set it to
   the lease margin: their transactions never wait on the network, and a worker frozen between
   its claim's row lock and its commit would otherwise block every takeover of that URL (found
   by the independent review).

3. **A claim waits for an in-flight dispatch.** Because `XADD` precedes the commit, a worker
   can read a message while its row is still `pending` in committed state. `claim_url` first
   locks the row (`SELECT ... FOR NO KEY UPDATE`), which waits for the dispatch transaction;
   its compare-and-set then sees the committed outcome. After that wait, `pending` means the
   dispatch rolled back, and the orphaned message can be acknowledged.

4. **Acknowledgment rules.** A worker acknowledges a message only when no work is left that the
   message stands for:

   | Situation | Disposition |
   |---|---|
   | result committed (`RECORDED`) | `XACK`, after the commit |
   | result fenced out (`FENCED`): the lease was taken over | keep pending, never `XACK` (it acts on the whole group) |
   | any error (PostgreSQL, Redis, a bug) or cancellation | keep pending: the outcome may be uncertain |
   | claim refused, URL terminal | `XACK` (`already_finished`) |
   | claim refused, job not running | `XACK` (`job_not_running`) |
   | claim refused, URL `pending` (the dispatch rolled back) | `XACK` (`not_dispatched`) |
   | claim refused, URL `queued` (a race) | claim again (bounded) |
   | claim refused, another worker holds a live lease | keep pending (`leased`): if that worker dies, this message recovers the URL |
   | lease expired, attempts left | takeover claim with a new token |
   | lease expired after the last attempt | fenced recovery (`max_attempts_exceeded`), then `XACK` |
   | unknown URL, or URL of another job | dead-letter, then `XACK` |

   A failed `XACK` is logged; the message stays pending and its redelivery is acknowledged as
   `already_finished`.

5. **Takeover of abandoned messages.** Every worker runs `XAUTOCLAIM` every
   `SITESCOPE_STREAM_RECLAIM_INTERVAL_SECONDS` for messages idle at least
   `SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS` (at least the lease), following the cursor
   through the pending list, never taking more than its free task slots. A reclaimed message
   goes through the same rules.

6. **Reconciliation only for lost messages.** The scheduler examines tasks without progress for
   `SITESCOPE_SCHEDULER_RECONCILE_AFTER_SECONDS` (`queued`, or `in_progress` with a lease
   expired that long), page by page (keyset on `dispatched_at, id`), and publishes again only
   those whose stream entry is no longer *delivering*: gone, or acknowledged, rather than
   undelivered or pending (`CrawlStream.delivering`). A period without workers therefore
   publishes nothing, and the stream does not grow.

7. **No lease renewal.** Leases are never extended (no heartbeats). Every task runs under a
   deadline that ends `SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS` before its lease (`asyncio.timeout`
   around the robots.txt check, the politeness wait, the fetch and the parse), and startup
   validation guarantees `lease >= fetch deadline + margin`. No task can need a longer lease.
   Renewal would add a write per task and interval, and a new failure mode: a worker that keeps
   renewing while stuck would hold its URL forever. A task that cannot finish in time fails as
   `deadline_exceeded`; a stalled worker is fenced out.

8. **No scheduler leader.** Several schedulers may run at once. Activation takes a
   transaction-level advisory lock, so the running-jobs cap is exact; level advancement takes
   the job row lock (ADR-0004); dispatch and reconciliation skip rows another scheduler holds
   (`SKIP LOCKED`); trimming and consumer cleanup are idempotent. The backpressure limits are
   approximate with several schedulers, since each may fill the room it saw. A pass looks at
   every running job (the cap limits activation only), and a job whose step fails for a reason
   of its own is skipped for that pass, so it cannot stop the others; a transient error
   (PostgreSQL or Redis unreachable) fails the whole pass.

9. **Retention.** The scheduler trims with `XTRIM MINID ~` at the smallest of: now minus
   `SITESCOPE_STREAM_RETENTION_SECONDS`, the oldest pending entry, and the ID just after each
   group's last-delivered ID. Undelivered and pending entries are never trimmed. Consumers with
   nothing pending are deleted after `SITESCOPE_STREAM_CONSUMER_MAX_IDLE_SECONDS`, by one Lua
   script that checks and deletes atomically (`XGROUP DELCONSUMER` drops a consumer's pending
   entries, so a consumer resuming between a separate check and the deletion would lose them).

10. **Timing order**, validated at startup: task deadline < lease <= reclaim min idle <=
    reconcile after <= retention, and the `XREADGROUP` block time below the Redis socket
    timeout.

11. **Identity.** Every process start gets a unique name, `<role>-<host>-<pid>-<random>`. A
    worker's name is its Redis consumer name and its lease owner, and appears in every log line
    of its tasks.

12. **Batch updates select first.** A statement that picks rows with `LIMIT ... FOR UPDATE
    SKIP LOCKED` and updates them uses a `MATERIALIZED` CTE. The form `UPDATE ... WHERE id IN
    (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)` updated more than `n` rows: PostgreSQL may
    rescan the subquery per outer row, and a rescan skips rows the statement already updated.
    Found in Phase 5 (batch activation exceeded the cap); the Phase 4 dispatch statement had
    the same defect.

## Consequences

- The guarantee is at-least-once task delivery with idempotent, fencing-protected state
  transitions and result persistence. It is not exactly-once: an HTTP fetch can repeat after a
  crash or a takeover, and a message can be delivered more than once.
- Duplicate messages come only from failures: a dispatch that did not commit, a reclaim, or a
  republished lost entry. Each costs one refused claim.
- Recovery takes time by design: a crashed worker's tasks resume after the reclaim idle time
  (120 s by default); a lost message after the reconciliation time (10 minutes by default). A
  transient PostgreSQL error during a claim leaves the message pending too, so that task waits
  for the reclaim idle time as well.
- A worker that dies, or loses Redis, while it holds a politeness slot leaves that slot held
  until its TTL (fetch deadline + 10 s). With one slot per host (the default), requests to that
  host wait, and those waiting longer than `SITESCOPE_POLITENESS_MAX_WAIT_SECONDS` fail as
  `rate_limited`, which stays terminal until Phase 6 retries retryable failures. The outage
  test observed this.
- Graceful stop works on Windows (Ctrl+C, or `CTRL_BREAK_EVENT`, reported as `SIGBREAK`) and on
  POSIX (SIGINT, SIGTERM). Killing a process is safe too: its leases expire and its messages are
  reclaimed.
- Left for Phase 6: durable retries with backoff (including `rate_limited` and
  `deadline_exceeded`), dead-lettering by delivery count, cancellation, `Idempotency-Key`, and
  an optional leader lock to save duplicate work.

## Alternatives considered

- **Commit first, then publish.** A crash between the commit and `XADD` would leave a `queued`
  row without a message until reconciliation (minutes). Publishing first turns the same crash
  into a harmless duplicate.
- **A separate outbox table.** Rejected by ADR-0002 and ADR-0008: the state row is the outbox.
- **Lease renewal (heartbeats).** Rejected; see decision 7.
- **Leader election** (`pg_try_advisory_lock` on a dedicated connection). Not needed for
  correctness, and it adds failover delay; possible later as an optimization.
- **Acknowledging a message whose URL another worker holds.** If that worker then died, no
  message would remain to recover the URL until the reconciler ran.
- **Republishing every stale task.** Without workers, the stream would grow by up to the
  outstanding-task limit at every reconciliation.
- **`XTRIM MAXLEN`.** It can delete entries that were never delivered.
