# ADR-0002: Transactional outbox for publishing to Redis

- Status: Accepted. The index-event part (`index_outbox`) is superseded by
  [ADR-0008](0008-search-documents-as-index-outbox.md).
- Date: 2026-11-06

## Context

A state change in PostgreSQL and the matching `XADD` to Redis cannot be committed atomically. Either
order can fail:

- Publish first: the message announces work whose state may never commit.
- Commit first: the message is lost if the process dies before it publishes.

## Decision

- No request handler or worker publishes to Redis inside its business transaction.
- Rows that need publishing are written in the same transaction as the state change. A relay in the
  scheduler publishes them later. There are two such outboxes:
  - **Crawl tasks: the frontier is the outbox.** The `pending` rows of `crawl_urls` act as the outbox.
    1. The dispatcher locks eligible rows (`FOR UPDATE SKIP LOCKED`).
    2. It `XADD`s one message per row.
    3. It marks the rows `queued` with their stream entry IDs, then commits.

    The dispatcher also applies the scheduling policy (BFS level, per-job window, fairness), which a
    generic relay should not know about.
  - **Index events: the `index_outbox` table.** A worker inserts an outbox row in the same transaction
    that stores the page. The relay publishes it and sets `published_at`.
- The relay publishes first and marks second. If it crashes between `XADD` and commit, the row stays
  unpublished and is published again. The result is a duplicate message, never a lost one.
- Consumers are idempotent ([ADR-0003](0003-at-least-once-leases-fencing.md)).
- The reconciler re-publishes rows whose messages were lost in Redis.

## Consequences

- No task is lost as long as its PostgreSQL transaction commits.
- Job creation works even while Redis is down.
- Duplicate messages are normal and must stay harmless.
- Publication latency is bounded by the relay's polling interval. `LISTEN`/`NOTIFY` can shorten it later.
- Row locks are held during the `XADD` round trip. A short Redis timeout bounds how long.

## Alternatives considered

- **Dual write in the request path:** loses or orphans messages when either write fails.
- **Commit, then publish, with no outbox:** loses messages on a crash.
- **Change data capture (logical decoding, Debezium):** robust, but heavy for this project.
- **A generic outbox table for crawl tasks too:** would duplicate frontier data and push BFS gating into
  the relay.
