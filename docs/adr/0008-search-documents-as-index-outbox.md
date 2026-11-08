# ADR-0008: `search_documents` is both the index store and the indexing outbox

- Status: Accepted; extended by [ADR-0018](0018-asynchronous-indexing-pipeline.md)
- Date: 2026-11-06
- Supersedes: the index-event part of [ADR-0002](0002-transactional-outbox.md)

## Context

ADR-0002 planned two tables for indexing:

- an append-only `index_outbox`, with one event row per page write;
- `search_documents`, holding the index itself.

The Phase 2B requirements asked for indexing state to be modeled explicitly: an indexing status per
page, page content versions, indexed content versions, and reindexing after content changes.

With both tables, indexing state would live in two places: the outbox's `published_at`, and the
search document's version. That causes four problems:

- Reindexing would require inserting new outbox rows.
- The outbox would grow without bound.
- Detecting lost Redis messages would still require comparing versions, which is the real source of
  truth anyway.
- The two tables could disagree.

## Decision

There is no `index_outbox` table. `search_documents` serves as both the index store and the outbox
for index tasks. This mirrors how `crawl_urls` serves as the outbox for crawl tasks.

- **Enqueue.** The worker's result transaction inserts the page and its `search_documents` row
  (`status = 'pending'`) atomically, so only committed work is ever published.
- **Publish.** The relay selects pending rows with `FOR UPDATE SKIP LOCKED` and `XADD`s each one.
  It sets `status = 'queued'`, `dispatched_at` and `stream_entry_id`, then commits. A crash
  between `XADD` and commit yields a duplicate message, never a lost one.
- **Index.** The indexer writes `search_vector` and `indexed_content_version`, and sets
  `status = 'indexed'`. A CHECK requires all three for the `indexed` status. Indexing is
  idempotent: rebuilding the vector from the same content gives the same result.
- **Reindex.** When a page's content changes, the same transaction increments
  `pages.content_version` and sets `status = 'pending'`. Reindexing everything is a single
  `UPDATE ... SET status = 'pending'`.
- **Recover.** The reconciler re-publishes stale `queued` rows whose stream entries are gone,
  exactly as for crawl URLs.

## Consequences

- There is one publication pattern for both pipelines: the state row is the outbox.
- The table is bounded (one row per page), and indexing progress is a count by status.
- Rows exist before they are indexed, with a `NULL` vector. Search queries therefore filter on the
  vector (`@@` never matches `NULL`), and progress queries filter on status.
- The relay polls a partial index (`ix_search_documents_pending`) rather than an event log.

## Alternatives considered

- **Keep `index_outbox` (ADR-0002 as written):** produces redundant, possibly diverging state, and
  an ever-growing table.
- **`LISTEN`/`NOTIFY` triggers:** not durable. A notification is lost if no listener is connected.
- **A generated `tsvector` column on `pages`:** synchronous, which puts GIN maintenance on the
  crawl write path (rejected in ADR-0005).
