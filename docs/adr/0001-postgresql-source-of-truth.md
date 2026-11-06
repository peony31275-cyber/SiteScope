# ADR-0001: PostgreSQL is the source of truth; Redis Streams only delivers work

- Status: Accepted
- Date: 2026-11-06

## Context

SiteScope needs durable job and URL state, plus a way to hand tasks to many worker processes.

Redis Streams consumer groups deliver messages quickly, keep a pending list per consumer, and redeliver
unacknowledged messages. However, Redis durability is configurable and weaker than a database's:

- With AOF `everysec`, a crash can lose about a second of writes.
- Replication is asynchronous.
- Streams can be trimmed.

PostgreSQL offers transactions, constraints, and row locks.

## Decision

- **PostgreSQL holds all persistent state:** jobs, frontier URLs and their lifecycle, pages, links,
  outbox rows, and search documents.
- **Redis holds only non-authoritative data:**
  - stream messages that point at PostgreSQL rows (`{job_id, url_id}`, `{page_id, content_hash}`)
  - per-host rate-limit state
  - caches (robots.txt)
- **Messages carry no trusted state.** A consumer re-reads PostgreSQL before acting.
- **Losing Redis data never loses work.** A reconciler re-publishes every task that PostgreSQL still
  records as outstanding.

## Consequences

- **Correctness does not depend on Redis persistence.** Tests can flush Redis in the middle of a crawl
  and still require the crawl to finish with the same result.
- **PostgreSQL sets the throughput ceiling.** Each task costs at least two short PostgreSQL
  transactions (claim and result). That is acceptable at this project's scale.
- **Two systems to operate,** and queue statistics derived from Redis are approximate.

## Alternatives considered

- **PostgreSQL-only queue** (workers poll with `FOR UPDATE SKIP LOCKED`):
  - Simpler and fully transactional, and a valid design at this scale.
  - Not chosen, for two reasons:
    - the project deliberately demonstrates consumer-group delivery semantics;
    - push-style delivery keeps polling load off PostgreSQL as the number of workers grows.
  - Because PostgreSQL stays authoritative, adding Redis does not weaken correctness.
- **Redis as the frontier** (sets for deduplication, lists or streams for queues): fast, but
  deduplication, the page budget, and task state would not be transactional with page writes. Losing
  Redis data would also lose work.
- **Kafka or RabbitMQ:** more operational weight than this project needs.
