# ADR-0003: At-least-once processing with leases, fencing tokens, and idempotent effects

- Status: Accepted. Extended by [ADR-0010](0010-database-enforced-fencing.md) (the database
  enforces fencing) and [ADR-0014](0014-crawl-state-transactions-and-redirects.md) (the
  result transaction and the recovery of exhausted leases), and by
  [ADR-0016](0016-distributed-execution-redis-streams.md) (acknowledgment rules, no lease renewal).
- Date: 2026-11-06

## Context

Workers fail in three ways:

- they crash;
- they stall (GC pauses, VM suspension, network partitions);
- they receive duplicate messages (outbox re-publication, redelivery).

Redis's pending entries list and `XAUTOCLAIM` re-deliver unacknowledged messages. On their own, however,
they cannot stop a stalled worker from writing a stale result after another worker has taken over its
task.

Exactly-once processing across Redis, PostgreSQL, and remote HTTP servers is not achievable here.

## Decision

1. **Claim with a compare-and-set in PostgreSQL.** A claim moves a URL from `queued` to `in_progress`,
   or takes over an `in_progress` row whose lease has expired according to the database clock. Every
   claim increments `lease_epoch` (the fencing token) and `attempt_count`.
2. **Fence the result commit.** The result transaction locks the URL row and continues only if
   `state = 'in_progress' AND lease_epoch = :token`. Otherwise it rolls back, discards the result, and
   does **not** `XACK`. `XACK` applies to the whole consumer group, so acking here would hide the
   message from its new owner.
3. **Acknowledge after the commit,** or as soon as the URL is known to be terminal.
4. **Recover with `XAUTOCLAIM`,** using a min-idle time at least as long as the lease. Enforce
   `task_deadline < lease_duration <= reclaim_min_idle`.
5. **Make every write idempotent:**
   - unique constraints with `ON CONFLICT DO NOTHING`;
   - write-once pages and links;
   - upserts keyed by natural IDs.
6. **Bound retries in PostgreSQL** through `attempt_count`, with durable backoff via `next_attempt_at`.
   Dead-letter messages that cannot be parsed or claimed.

## Consequences

- Duplicate deliveries and stalled workers cause no duplicate effects. Each failure window has a short
  argument and its own test (`docs/architecture.md`, section 10.4).
- Pages may be fetched more than once, because delivery is at-least-once. The project says so and never
  claims exactly-once processing.
- Timeouts must be configured consistently. Startup validation rejects inconsistent settings.
- Each task needs one extra short transaction: the claim.

## Alternatives considered

- **Redis pending list + `XACK` only:** a stalled worker can overwrite a newer result.
- **Redis locks (e.g. Redlock):** a lock alone provides no fencing; the database would still have to
  reject stale writers.
- **Lease heartbeats:** unnecessary while every task has a hard deadline. Revisit if long-running tasks
  appear.
- **Distributed transactions across Redis and PostgreSQL:** not available.
