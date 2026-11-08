# ADR-0018: The asynchronous indexing pipeline: dispatch, counted attempts, versioned writes

- Status: Accepted
- Date: 2026-11-07
- Extends: [ADR-0007](0007-modular-monolith-process-roles.md) (the index dispatcher role),
  [ADR-0008](0008-search-documents-as-index-outbox.md),
  [ADR-0016](0016-distributed-execution-redis-streams.md)
- Amends: [ADR-0005](0005-postgresql-full-text-search.md) (field weights, `ts_rank` instead of
  `ts_rank_cd`)

## Context

ADR-0005 chose PostgreSQL full-text search, and ADR-0008 made `search_documents` both the index
and the outbox of index tasks: since Phase 4 the crawler's result transaction inserts a page's
row as `pending`. Nothing consumed those rows. Phase 7 must index them asynchronously, with the
guarantees of the crawl pipeline:

- only committed pages are indexed, and no indexing work is lost when a publisher crashes or
  Redis loses its data;
- duplicate, redelivered and outdated messages are harmless, and the effects are idempotent;
- an older index task can never overwrite a newer index, whatever order tasks finish in;
- retries are bounded by state in PostgreSQL, never by Redis delivery counts alone (the Phase 6
  review found that an outage inflates delivery counts);
- a job is never reported fully indexed while indexing work remains, and indexing never changes
  a job's crawl status;
- search returns only documents whose index matches their current content.

## Decision

### 1. Two process roles

| Role | Entrypoint | Does |
|---|---|---|
| Index dispatcher | `python -m app.scheduler.index_dispatcher` | publishes `pending` documents, republishes lost tasks, completes job indexing, maintains the index stream |
| Indexer | `python -m app.workers.indexer [--concurrency N]` | consumes index tasks, builds and stores vectors |

The index relay planned inside the scheduler (architecture, section 3) is a separate process
instead: it can run, scale and fail independently of crawling, and unlike the scheduler it
starts no job. Both reuse the existing machinery: the pass loop of the scheduler
(`app/scheduler/passes.py`), the generic task stream (`TaskStream`, `IndexStream`) and the
consumer loop of the crawler workers (`app/workers/consumer.py`). Several of each may run at
once, without a leader.

### 2. Messages and keys

An index task is `{v, job_id, page_id, content_version}` on the stream `<prefix>:index`, read by
the consumer group `indexers`; dead letters go to `<prefix>:index:dlq`. A message names the work;
it never carries page content. `content_version` is the page's version when the task was
published.

### 3. Dispatch publishes inside its transaction (I1)

As for crawl tasks (ADR-0016), the dispatcher locks `pending` rows (`FOR UPDATE SKIP LOCKED`,
oldest change first), publishes one message per row (`XADD`, one pipeline), marks the rows
`queued` with their entry IDs, and commits. A crash or a failed commit after the `XADD` leaves the
rows `pending`: they are published again, and the earlier message finds nothing to do.

The row locks are held across one Redis round trip. That is a deliberate exception to "no locks
across slow network calls", for the reason ADR-0016 gives: an indexer can receive a message before
the dispatch commits, and its attempt locks the row first, so it waits for the commit instead of
finding the row `pending` and dropping the message. The wait is bounded by the Redis socket
timeout, and a dispatcher that stalls inside the transaction loses its session after
`idle_in_transaction_session_timeout` (twice the Redis timeout plus 5 s). Backpressure: at most
`SITESCOPE_INDEX_MAX_OUTSTANDING` documents `queued`, at most `SITESCOPE_INDEX_DISPATCH_BATCH_SIZE`
per pass.

### 4. Attempts are counted in PostgreSQL, before any work (I2)

An indexer first *starts an attempt*: in its own transaction it locks the document row, then
increments `attempt_count` and takes a lease (`lease_owner`, `lease_expires_at`, migration 0004)
if the document is `queued`, the message's content version is not newer than the page's,
attempts are left (`SITESCOPE_INDEX_MAX_ATTEMPTS`), and no other attempt holds a live lease. A
message for an older version stands for the same work: the attempt indexes the current content
(decision 5). It is not dropped as outdated, because the current version need not have a message
of its own: a dispatch that races a content change can lock the document after the change and
still read the page's previous version.

- An attempt that crashes its indexer has already been counted, so a poison document fails after
  `max_attempts` attempts, and an outage that makes messages bounce counts nothing: a start that
  cannot reach PostgreSQL never happened.
- A duplicate message that arrives while an attempt runs finds a live lease and stays pending
  (`leased`); it does not use up an attempt.
- **Leases are advisory, not fencing tokens.** The index transaction (decision 5) reads the
  content it indexes under its own row locks and writes only while the document is `queued`, so
  an indexer whose lease expired writes the same result as its successor, or nothing. Fencing
  protects results computed *outside* a transaction (a crawl fetch); here there are none.
- An attempt ends by its deadline, `SITESCOPE_INDEX_TASK_DEADLINE_SECONDS` (decision 5), and
  its lease lasts halfway between that deadline and the reclaim idle time
  (`(SITESCOPE_INDEX_TASK_DEADLINE_SECONDS + SITESCOPE_INDEX_RECLAIM_MIN_IDLE_SECONDS) / 2`): an
  attempt keeps its lease while it may still run, and a message reclaimed from a crashed indexer
  finds the lease expired. Timing order, checked at startup: task deadline + 2 s <= reclaim idle
  time <= reconcile after <= stream retention (so a lease outlasts its attempt by at least 1 s),
  and task deadline < `SITESCOPE_DB_COMMAND_TIMEOUT_SECONDS`.
- An index task holds one database connection at a time, and an indexer runs at most as many
  tasks as its connection pool holds (`SITESCOPE_DB_POOL_SIZE + SITESCOPE_DB_MAX_OVERFLOW`,
  checked at startup). A task therefore never waits for a connection after its attempt was
  counted, which would use up an attempt without indexing.

### 5. The vector is built inside PostgreSQL, with its version (I3)

One statement builds and stores the vector:

```sql
WITH locked AS MATERIALIZED (SELECT ... FROM pages WHERE id = :page AND job_id = :job FOR SHARE),
     page AS (... the body cut to the limit, without a split last word ...)
UPDATE search_documents SET search_vector = <weighted vector of page>,
       indexed_content_version = page.content_version, status = 'indexed', ...
FROM page
WHERE ... AND status = 'queued'
  AND (indexed_content_version IS NULL OR indexed_content_version <= page.content_version)
```

- The page row is locked first, in its latest committed version, so the vector and the version
  stored with it always come from the same content. A content change (which, per ADR-0008, sets
  the document back to `pending` in its own transaction) either waits for this statement or
  makes it match no row.
- The guard writes nothing over a newer indexed version, and triggers (migration 0004) reject any
  update that lowers `indexed_content_version` or `pages.content_version`, whatever code path
  attempts it.
- The page text never leaves PostgreSQL, so an indexer's memory does not grow with page size.
- The transaction's statements share one budget, the attempt's deadline: before each one,
  `statement_timeout` is set to what remains of it, so PostgreSQL stops the work once it is spent
  (SQLSTATE 57014), whether the time went to the vector, to the quarter-body retry or to a lock
  wait. The indexer also abandons the transaction when the deadline passes (it rolls back), in
  case the server does not answer. Such an attempt failed (`deadline_exceeded`): its error is
  recorded, its lease released, and its message retried after the reclaim idle time.

### 6. What becomes of a message

| Situation | Disposition | Reason |
|---|---|---|
| Malformed, unknown page, page of another job, a version the page never had | dead-letter | `malformed`, `unknown_document`, `job_mismatch`, `version_ahead` |
| Indexed (duplicate, or redelivery after a commit whose `XACK` was lost) | ack | `already_indexed` |
| Failed already | ack | `already_failed` |
| `pending` (its dispatch rolled back, or it was reset) | ack | `not_dispatched` |
| Another attempt holds a live lease | keep | `leased` |
| Attempts used up, no attempt running | fail the document, ack | `max_attempts_exceeded` |
| Indexed now | ack after the commit | `indexed` |
| No longer `queued` when the index statement ran (another attempt finished, or it was reset) | ack | `not_queued` |
| Vector over PostgreSQL's limit even for a quarter of the body | fail the document, ack | `index_failed` (`tsvector_too_large`) |
| The attempt's deadline passed | record the error, release the lease, keep | `deadline_exceeded` |
| PostgreSQL or Redis error | keep (and pause reading) | `error` |

A failed document is `failed` with `last_error`, and a `page.index_failed` event is written. The
message of a failed document is acknowledged, not dead-lettered: PostgreSQL holds the record.

### 7. Recovery

- **Lost messages** (Redis lost data, an entry was trimmed or acknowledged without the work):
  every `reconcile_after / 4` seconds the dispatcher examines `queued` documents dispatched at
  least `SITESCOPE_INDEX_RECONCILE_AFTER_SECONDS` ago and without a live lease, and publishes
  again, with the page's current version, those whose entry no longer delivers
  (`TaskStream.delivering`). A missing consumer group is recreated first.
- **Abandoned messages**: indexers reclaim them with `XAUTOCLAIM` after the reclaim idle time.
- **A commit whose `XACK` was lost**: the redelivered message finds the document indexed.

### 8. Job lifecycle

The crawl state machine is unchanged; indexing has its own, derived view.

- **Completion (I5).** On every pass the dispatcher looks at finished jobs (`completed`, `failed`
  or `cancelled`) whose `indexing_completed_at` is NULL and none of whose documents is `pending`
  or `queued` (the partial indexes `ix_crawl_jobs_indexing_open` and `ix_search_documents_open`),
  so jobs still being indexed never hold up the others. Under the job row lock
  (`FOR NO KEY UPDATE`), in a fresh statement, it checks again that no document of the job is
  `pending` or `queued`, then sets `indexing_completed_at` and writes `job.indexing_completed`. A
  finished job gets no new pages (its URLs are terminal, and only a fenced result stores a page),
  so the check cannot be overtaken.
- **Derived indexing status** (job detail, search results): `pending` (nothing published or
  indexed), `running`, `indexed` (complete, no failure) and `failed` (complete, at least one page
  could not be indexed). A job is fully complete when its crawl status is `completed` and its
  indexing status is `indexed`.
- **Indexing never changes a job's status.** Stale index messages touch only `search_documents`.
- **Cancelled jobs.** Cancelling stops the crawl, not the indexing: pages stored before the
  cancellation took effect are indexed and stay searchable, and the job's indexing completes.
  Nothing is deleted.
- **Reindexing (I6, `request_reindex`).** Under the job row lock, every `indexed` and `failed`
  document of a job goes back to `pending` (attempts reset), and `indexing_completed_at` is
  cleared. Until a document is indexed again, search keeps serving its previous vector, which
  still matches its unchanged content.

### 9. Documents, ranking and search (amends ADR-0005)

- **Weights:** title A, description B, headings C, body D (ADR-0005 had title A, headings B,
  body C; the description is now indexed, between the title and the headings). Configuration
  `english`, named in every statement.
- **Ranking:** `ts_rank` with its default weights (1.0, 0.4, 0.2, 0.1) and no length
  normalization, instead of `ts_rank_cd`: a title match counts ten times a body match, and
  repeating a word in the body cannot outrank one title match. Ties are broken by URL (byte
  order), then page ID, so the order is total and pages are stable.
- **Queries:** `websearch_to_tsquery`, which never raises a syntax error on user input. A query
  is normalized first (invisible characters removed, whitespace collapsed); an empty query, one
  longer than 256 characters, or paging out of range (limit 1 to 50, offset 0 to 1000) is
  refused. A query with no term an index can look up (only stop words, or only excluded words)
  searches nothing and says so: it would match every page.
- **Visibility:** a document is returned only if `indexed_content_version =
  pages.content_version`. A page never indexed, failed, or changed since it was indexed is not.
- **Snippets:** `ts_headline` runs only for the returned page of results, on at most 20,000
  characters of each body, with control-character markers that are removed from the text
  beforehand; the service returns plain text and highlight ranges, never HTML.
- **The service** (`app/services/search.py`) runs one read-only REPEATABLE READ transaction, so
  the results and the indexing progress reported with them come from one snapshot. A search has
  `SITESCOPE_SEARCH_TIMEOUT_SECONDS` in all (shorter than the database command timeout):
  PostgreSQL stops each statement after that long, and the service abandons the transaction
  then too. Phase 7 exposes it through a read-only command (`python -m app.scheduler.search`);
  the HTTP search API is Phase 8.

### 10. Size limits

PostgreSQL limits the lexemes and positions of one `tsvector` to 1 MB. Title, description and
headings (at most 23,000 characters together, by the `pages` constraints) are indexed whole; the
body is cut to `SITESCOPE_INDEX_MAX_BODY_CHARS` (default 100,000, at most 150,000) before
`to_tsvector`, at the last whitespace before the limit (looking back at most 2,048 characters;
the parser ignores longer words anyway). Measured on PostgreSQL 18: distinct hyphenated CJK compounds,
the densest text found, need about 6.7 bytes per character, so the default stays below the limit
for every text measured while the maximum can exceed it. A vector over the limit is built again
once from a quarter of the body; a document that still does not fit fails (`tsvector_too_large`).

## Consequences

- One publication pattern for both pipelines, and no new outbox table: `search_documents` stays
  the outbox (ADR-0008). Migration 0004 adds two version triggers, two lease columns with their
  CHECK constraints, and one partial index (`ix_crawl_jobs_indexing_open`).
- Every effect is idempotent, and the guarantees hold under duplicates, reordering, crashes and
  Redis data loss. Processing is at least once, never exactly once: a page can be indexed twice
  (a late attempt and its successor), with the same result.
- An index attempt costs two transactions (start, then index). That buys bounded retries that
  survive crashes without trusting Redis delivery counts.
- An indexer's concurrency is capped by its connection pool, and a lease outlasts its attempt by
  at least 1 s: these keep counted attempts honest (an independent review found both gaps in
  the first version).
- Search cost grows with the number of matching documents, because each is ranked; deep paging is
  bounded, and a search is cut off by its timeout. Latency at scale is to be measured (Phase 10).
- Ranking stays basic (ADR-0005): no IDF or BM25, English stemming only, no segmentation of
  languages written without spaces, no fuzzy matching. Positions are capped at 16,383 by
  PostgreSQL, so proximity ranking is less precise near the end of long documents.
- Cancelled jobs keep consuming indexing work for the pages they stored.
- `backend/compose.yaml` still runs only PostgreSQL and Redis; containers for the application's
  roles arrive with the backend image in Phase 11.

## Alternatives considered

- **The index relay inside the scheduler process** (the original plan): fewer processes, but
  crawl scheduling and indexing could then not be scaled, stopped or restarted separately, and
  indexing an existing database would require a process that also starts jobs.
- **Counting attempts with Redis delivery counts:** rejected; delivery counts grow during
  outages without any attempt, and are lost with Redis data (the Phase 6 lesson).
- **Counting the attempt inside the index transaction:** an attempt that crashes the indexer
  rolls back with it, so a poison document would be retried forever.
- **Fencing tokens for index attempts:** they protect results computed outside a transaction;
  the vector is computed inside one, under row locks, so a token would add a column and checks
  without adding safety. The advisory lease is enough to keep duplicates from using attempts.
- **An `indexing` status instead of a lease on `queued` rows:** it would need a new value in the
  status CHECK and every partial index, and a recovery path for rows stuck in it; a lease
  expires on its own.
- **Building the vector in Python and sending it to PostgreSQL:** moves page text into the
  indexer's memory and duplicates PostgreSQL's parser; nothing is gained.
- **`ts_rank_cd`** (ADR-0005's choice): it ranks by cover density, how close the matching
  terms are, which suits phrase-like queries; the requirement here is field weight first (title
  above body), which `ts_rank` with weights expresses directly.
- **Dropping the documents of cancelled jobs:** loses work that was done and is still valid; a
  user who cancels a crawl usually still wants to search what was crawled.
- **A `completed_and_indexed` job status:** it would put indexing into the crawl state machine
  and its CHECK constraints; a separate `indexing_completed_at` and a derived indexing status
  keep the two lifecycles apart.
