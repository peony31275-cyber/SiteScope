# SiteScope Architecture

> **Status: design baseline from Phase 0 (2026-11-06). Nothing in this document is implemented yet.**
> [`development-roadmap.md`](development-roadmap.md) tracks what has been built. Details will be refined
> during implementation. Changing an invariant requires a new ADR in [`adr/`](adr/README.md).

## Contents

1. Goals and non-goals
2. System overview
3. Components and responsibilities
4. End-to-end flow
5. Data model (planned)
6. Redis layout
7. Lifecycle state machines
8. Crawl semantics
9. Transactions and concurrency control
10. Reliability model
11. Correctness invariants
12. Security model
13. Search, indexing, and link analysis
14. Observability and configuration
15. Deployment topology
16. Repository structure (proposed)
17. Technology choices
18. Testing strategy
19. Assumptions and known limitations
20. Decision records

## 1. Goals and non-goals

**Goals**

- Crawl explicitly approved websites breadth-first, safely and politely, with configurable depth and page limits.
- Spread crawl work across several asynchronous worker processes.
- Recover without manual intervention from crashes, stalls, and duplicate message delivery, as long as
  PostgreSQL survives.
- Store jobs, URLs, pages, and link relationships durably, with idempotent writes.
- Index crawled pages asynchronously and serve ranked full-text search.
- Report broken links and internal link structure.
- Be reproducible: a one-command local deployment, automated unit, integration, and failure-recovery
  tests, and benchmarks with a published methodology.

**Non-goals (v1)**

- Internet-scale crawling, and crawling arbitrary user-supplied sites in the public demo.
- JavaScript rendering, form submission, and authenticated crawling.
- Exactly-once processing. SiteScope provides at-least-once processing with idempotent effects.
- High availability for PostgreSQL or Redis. Each runs as a single instance, and the design targets
  recovery rather than failover.
- User accounts and multi-tenant authorization.
- Incremental re-crawls and URL deduplication across jobs. Each job is an independent snapshot.
- Text analysis in languages other than English. There is one text-search configuration, `english`.

## 2. System overview

```
  React dashboard
        |  REST / JSON
        v
  api (FastAPI) --- create job + seed URL (one transaction) ------------->+
        ^                                                                 |
        + <-- reads: job status, pages, links, search results ------------+
                                                                          |
  scheduler <-- eligible frontier rows, unpublished outbox rows ----------+
    | dispatcher: XADD {job_id, url_id}, then mark rows `queued`          |
    v                                                                     |
  Redis stream sitescope:crawl    (consumer group: crawlers)              |   PostgreSQL
    | XREADGROUP / XACK / XAUTOCLAIM                                      |   (durable source
    v                                                                     |    of truth)
  crawler worker x N <-- claim URL lease (compare-and-set) -------------->+
    | safe fetch (HTTPX) -> parse (BeautifulSoup)                         |
    + --- fenced commit: page + links + index_outbox + URL state -------->+
                                                                          |
  scheduler (outbox relay) <-- unpublished index_outbox rows -------------+
    | XADD {page_id, content_hash}                                        |
    v                                                                     |
  Redis stream sitescope:index    (consumer group: indexers)              |
    |                                                                     |
    v                                                                     |
  indexer worker --- upsert search_documents (tsvector + GIN) ----------->+
```

The system has two planes:

- **State plane: PostgreSQL.** Holds everything that must survive a crash: jobs, the URL frontier and
  each URL's lifecycle, pages, links, outbox rows, and the search index.
- **Delivery plane: Redis Streams.** Carries messages that announce "this row needs work". A message is
  a pointer (`job_id` + `url_id`, or `page_id`), never the state itself, and the consumer re-reads
  PostgreSQL.

Every message published to Redis comes from a PostgreSQL row, and every effect of a message lands in
PostgreSQL through a guarded transaction. That is why Redis can be treated as disposable
([ADR-0001](adr/0001-postgresql-source-of-truth.md)).

## 3. Components and responsibilities

| Component | Process / entrypoint | Responsibilities | Scaling |
|---|---|---|---|
| Dashboard | static SPA behind nginx | create and cancel jobs; show progress, pages, link reports, search | n/a |
| API | `api`: `uvicorn app.main:app` | input validation, job commands, read models, search queries | stateless, N replicas |
| Scheduler | `scheduler`: `python -m app.scheduler` | job activation, crawl dispatch, BFS level advancement, job completion, index-outbox relay, reconciliation, stream maintenance | one active leader; standbys are safe |
| Crawler worker | `crawler-worker`: `python -m app.workers.crawler` | consume the crawl stream, claim URL leases, fetch safely, parse, commit results (fenced), ack, adopt abandoned messages | N processes, each with bounded concurrency |
| Indexer worker | `indexer-worker`: `python -m app.workers.indexer` | consume the index stream, build a weighted `tsvector`, upsert idempotently, ack | 1..N |
| PostgreSQL | service | durable state, constraints, row locks, full-text index | single instance |
| Redis | service | streams and consumer groups, per-host rate-limit state, robots.txt cache | single instance, AOF on |
| Demo site | static web server | approved crawl target for the public demo and the end-to-end tests | n/a |

**API**

- Never contacts crawl targets and never publishes tasks.
- Creating a job is a single PostgreSQL transaction (job row + seed URL), so it works while Redis is
  down.
- Uses Redis only for queue statistics and request rate limiting.
- Enforces the target allowlist and the server-side caps on depth, pages, and running jobs.
- Accepts an `Idempotency-Key` header on job creation.

**Scheduler.** One asyncio process running independent periodic loops:

- *Activator*: moves jobs from `queued` to `running` while the running-jobs cap allows it.
- *Dispatcher*: publishes eligible frontier rows to the crawl stream and marks them `queued`. A row is
  eligible when its state is `pending`, its depth equals the job's `current_level`, and
  `next_attempt_at <= now()`. The dispatcher respects a per-job in-flight window and rotates fairly
  across jobs.
- *Level advancer / finalizer*: when a running job has no unfinished URLs, admits the next BFS level from
  the recorded links, or completes the job. Moves `cancelling` jobs to `cancelled` once no URL is in
  progress.
- *Outbox relay*: publishes unpublished `index_outbox` rows to the index stream.
- *Reconciler*: repairs divergence between PostgreSQL and Redis: lost or trimmed messages, abandoned
  leases, and exhausted attempts.
- *Maintenance*: trims streams and removes idle consumers.

A leader is elected with `pg_try_advisory_lock` on a dedicated connection. This is an optimization, not a
correctness requirement: every scheduler action is either a conditional update or uses `SKIP LOCKED`, so
two overlapping schedulers produce at most duplicate messages.

**Crawler worker**

- Reads from the crawl stream with `XREADGROUP`, never requesting more messages than it has free
  concurrency slots.
- Handles each message as: claim (compare-and-set) → policy checks, robots.txt, per-host rate limit →
  fetch (outside any transaction) → parse → fenced result commit → `XACK`.
- Runs `XAUTOCLAIM` periodically to adopt messages abandoned by crashed or stalled consumers.
- On shutdown, stops reading, finishes in-flight tasks within a grace period, and exits. Other workers
  recover anything left unfinished.

**Indexer worker**

- Consumes `{page_id, content_hash}` events.
- Builds a weighted `tsvector`: title A, headings B, body C.
- Upserts `search_documents` keyed by `page_id`. Pages are write-once, so replayed events are no-ops.

## 4. End-to-end flow

1. **Create.** `POST /api/v1/jobs` normalizes and validates the seed against the allowlist and the caps.
   One transaction inserts the job (`queued`, `admitted_count = 1`) and the seed URL (`pending`, depth 0).
2. **Activate.** The scheduler sets the job to `running` with `current_level = 0` once the running-jobs
   cap allows it.
3. **Dispatch.** The dispatcher locks eligible rows (`FOR UPDATE SKIP LOCKED`) and `XADD`s one
   `{job_id, url_id}` message per row. It then marks the rows `queued` and commits.
4. **Claim.** A worker receives the message and claims the URL with a compare-and-set:
   - the state becomes `in_progress`;
   - `lease_epoch` and `attempt_count` are incremented;
   - `lease_expires_at` is set from the database clock.

   The claim commits immediately.
5. **Fetch.** Outside any transaction: check robots.txt, wait for a per-host rate-limit slot, resolve DNS
   and validate the IP, then send the HTTP request pinned to that IP. The body is streamed under size and
   time limits.
6. **Commit.** One transaction does all of this:
   - verifies the lease (fencing);
   - stores the page;
   - records every discovered link with its position and a scope flag;
   - inserts an `index_outbox` row;
   - marks the URL terminal, or, for a retryable failure, returns it to `pending` with a backoff.

   Then the worker sends `XACK`.
7. **Advance.** When none of the job's URLs is unfinished, the scheduler takes the job row lock. It
   admits the next level's URLs from links recorded by the current level, in deterministic order, up to
   the remaining page budget. If nothing can be admitted, it marks the job `completed`, or `failed` if
   the seed itself was never fetched.
8. **Index.** The relay publishes `index_outbox` rows, and the indexer upserts `search_documents`.
9. **Query.** The API serves progress, pages, link reports, and search results from PostgreSQL.

## 5. Data model (planned)

This is the intended shape. Phase 2 turns it into Alembic migrations (Phase 6 for the search table).
Types are abbreviated.

**`crawl_jobs`**: one row per crawl request.

| Column | Notes |
|---|---|
| `id uuid` PK | |
| `seed_url text` | normalized |
| `allowed_hosts text[]` | crawl scope; defaults to the seed host; must lie within the deployment allowlist |
| `max_depth smallint`, `max_pages int` | validated against server-side caps |
| `status text` | `queued`, `running`, `cancelling`, `completed`, `failed`, `cancelled` (CHECK) |
| `current_level smallint` | BFS level currently being crawled |
| `admitted_count int` | URLs admitted so far; `CHECK (admitted_count <= max_pages)` |
| `idempotency_key text` UNIQUE NULL, `request_fingerprint bytea` | job-creation idempotency |
| `failure_reason text` NULL | |
| `created_at`, `started_at`, `finished_at`, `cancel_requested_at` | `timestamptz` |

**`crawl_urls`**: the frontier and the lifecycle of each URL. This table also serves as the outbox for
crawl tasks.

| Column | Notes |
|---|---|
| `id bigint` identity PK | |
| `job_id uuid` FK | |
| `url text`, `url_hash bytea` | normalized URL and its SHA-256; `UNIQUE (job_id, url_hash)` |
| `depth smallint` | BFS distance from the seed |
| `discovered_from_url_id bigint` NULL | BFS parent (first discoverer) |
| `redirect_hops smallint` | length of the redirect chain that led here |
| `state text` | `pending`, `queued`, `in_progress`, `fetched`, `failed`, `skipped`, `cancelled` (CHECK) |
| `attempt_count smallint`, `next_attempt_at timestamptz` | retry control |
| `lease_epoch bigint`, `lease_owner text`, `lease_expires_at timestamptz` | lease; `lease_epoch` is the fencing token |
| `stream_entry_id text`, `dispatched_at timestamptz` | last publication, used by the reconciler |
| `http_status smallint`, `outcome text`, `last_error text` | e.g. `html_stored`, `non_html`, `redirect`, `robots_disallowed`, `timeout` |
| `redirect_target text` NULL | normalized `Location` of a 3xx response |
| `created_at`, `updated_at`, `finished_at` | |

Partial indexes:

- dispatch: `(job_id, depth, id) WHERE state = 'pending'`
- liveness: `(job_id) WHERE state IN ('pending', 'queued', 'in_progress')`
- leases: `(lease_expires_at) WHERE state = 'in_progress'`

**`pages`**: content of fetched HTML documents. Rows are write-once. Columns: `id`, `job_id`,
`crawl_url_id` (UNIQUE FK), `http_status`, `content_type`, `title`, `headings`, `body_text` (truncated),
`content_hash`, `byte_size`, `fetched_at`, `fetch_ms`.

**`page_links`**: every http(s) link found on a page, which together form the crawl graph.

- Columns: `id`, `job_id`, `source_page_id` (FK), `target_url`, `target_url_hash`, `anchor_text`
  (truncated), `position` (order within the document), and `is_internal` (whether the target is in the
  job's scope).
- `UNIQUE (source_page_id, target_url_hash)`.
- A target's status is found by joining `crawl_urls` on `(job_id, url_hash)`.

**`index_outbox`**: `id`, `page_id`, `content_hash`, `created_at`, `published_at` NULL,
`publish_attempts`. Partial index `WHERE published_at IS NULL`.

**`search_documents`** (Phase 6): `page_id` (PK, FK), `job_id`, `url`, `title`, `search_vector tsvector`,
`indexed_content_hash`, `indexed_at`, with a GIN index on `search_vector`.

Design notes:

- States are `text` with a `CHECK` constraint rather than PostgreSQL enums, because that is easier to
  evolve in migrations.
- Uniqueness is enforced on a URL hash: long URLs are common, and B-tree index entries have a size limit.
- All data is per job (snapshot semantics). The same URL stored in several jobs is accepted duplication.

## 6. Redis layout

| Key | Type | Purpose |
|---|---|---|
| `sitescope:crawl` | stream; group `crawlers` | crawl tasks `{v, job_id, url_id}` |
| `sitescope:index` | stream; group `indexers` | index events `{v, page_id, content_hash}` |
| `sitescope:crawl:dlq`, `sitescope:index:dlq` | streams | dead letters (malformed or unclaimable messages), kept for inspection |
| `sitescope:rate:{host}` | string | per-host GCRA rate-limit state, shared by all workers |
| `sitescope:robots:{origin}` | string with TTL | cached robots.txt (not authoritative) |

- Messages are small and versioned (`v`). Consumers re-read state from PostgreSQL.
- Consumer groups are created idempotently at startup with `XGROUP CREATE <stream> <group> 0 MKSTREAM`,
  ignoring `BUSYGROUP` errors.
- Each worker process uses a unique consumer name. Maintenance removes idle consumers that have no pending
  entries.
- Streams are trimmed by age with `XTRIM ... MINID ~ ...`. Trimming can delete entries that were never
  delivered. That is acceptable only because PostgreSQL still records the task as outstanding, and the
  reconciler re-publishes it.
- Redis 7.0 or later is required. `XAUTOCLAIM` arrived in 6.2, and 7.0 added reporting of entries that
  were deleted from the stream.
- The key prefix is configurable so test runs can be isolated.

## 7. Lifecycle state machines

### 7.1 Job

| From | To | Trigger (actor) | Guard / effect |
|---|---|---|---|
| (none) | `queued` | create (API) | valid, allowlisted seed within caps; seed URL inserted in the same transaction |
| `queued` | `running` | activation (scheduler) | running jobs below the cap |
| `queued` | `cancelled` | cancel (API) | seed URL is cancelled |
| `running` | `cancelling` | cancel (API) | the same transaction cancels every `pending` and `queued` URL |
| `running` | `completed` | level advancer | no unfinished URLs, no further level to admit, seed was fetched |
| `running` | `failed` | level advancer | no unfinished URLs and the seed was never fetched (e.g. disallowed or unreachable) |
| `cancelling` | `cancelled` | finalizer | no URL is `in_progress`; the reconciler cancels expired leases |

Terminal states (`completed`, `failed`, `cancelled`) are absorbing. After a job becomes terminal, its URLs,
pages, and links never change. Indexing may still be catching up; its progress is reported separately.

### 7.2 Crawl URL

| From | To | Trigger (actor) | Guard |
|---|---|---|---|
| (none) | `pending` | admission: seed (API), next level (scheduler), or redirect target (worker) | unique per job, depth ≤ `max_depth`, budget remaining, job active |
| `pending` | `queued` | dispatch (scheduler) | job `running`, depth = `current_level`, `next_attempt_at <= now()`, within the in-flight window |
| `queued` | `in_progress` | claim (worker) | job `running`, `attempt_count < max_attempts` |
| `in_progress` | `in_progress` | takeover claim (worker) | lease expired by the database clock; `lease_epoch` is incremented |
| `in_progress` | `fetched` / `skipped` / `failed` | result commit (worker) | fencing check passes |
| `in_progress` | `pending` | retryable failure (worker) | fencing check passes; `next_attempt_at = now() + backoff` |
| `in_progress` | `failed` | attempts exhausted after crashes (worker or reconciler) | lease expired |
| `pending`, `queued` | `cancelled` | job cancel (API) | none |
| `in_progress` | `cancelled` | reconciler | job is `cancelling` and the lease has expired |

Terminal outcomes:

- `fetched`: an HTTP response was received and recorded, whatever its status code. The `outcome` column
  says more: `html_stored`, `non_html`, `too_large`, `redirect`, and so on.
- `skipped`: no request was sent because of a policy decision (`robots_disallowed`,
  `blocked_destination`, ...).
- `failed`: no usable response after bounded retries (`timeout`, `connection_error`, `dns_error`,
  `max_attempts_exceeded`, ...).
- `cancelled`: the job was cancelled before this URL was processed.

## 8. Crawl semantics

### 8.1 URL normalization

Normalization is a pure function, covered by table-driven tests. It runs before any scope check,
deduplication, or storage:

1. Resolve relative references against the document URL, honoring `<base href>`.
2. Accept only `http` and `https`. Reject userinfo (`user:pass@`), empty hosts, and URLs over the length
   limit.
3. Lowercase the scheme and host, convert internationalized hosts to IDNA, and drop any trailing dot from
   the host.
4. Remove default ports (`:80` for http, `:443` for https).
5. Drop the fragment.
6. Normalize the path:
   - remove dot segments;
   - turn an empty path into `/`;
   - uppercase the hex digits in percent-encodings;
   - decode percent-encoded unreserved characters.
7. Keep the query string as it is, apart from percent-encoding normalization. Sorting parameters or
   stripping tracking parameters changes the meaning of some sites' URLs, so both are opt-in.

The deduplication key is the SHA-256 of the normalized URL.

### 8.2 Scope

A URL is in scope for a job when all of the following hold:

- the scheme is http or https;
- the host is in the job's `allowed_hosts` (exact match by default);
- the port is allowed;
- the origin is in the deployment allowlist.

Out-of-scope links are recorded (`is_internal = false`) but never fetched.

### 8.3 BFS depth correctness

The seed has depth 0, and each link hop adds 1. Pages at `max_depth` are fetched, and their links are
recorded but not admitted.

The crawl runs **level-synchronously** ([ADR-0004](adr/0004-level-synchronous-bfs.md)):

- The dispatcher releases only URLs whose depth equals the job's `current_level`.
- Level `d + 1` is admitted only after every URL at level `d` is terminal.
- Workers never admit links. They only record them.

*Why this gives correct depths.* A URL is admitted at depth `d + 1` only from links recorded by fetched
level-`d` pages, and only after levels `0..d` are all terminal. Suppose the URL were reachable in
`k <= d` hops through fetched pages. Then some page at level `k - 1` links to it, and level `k - 1` was
expanded earlier, so the URL already exists with depth `k`. The unique constraint keeps that first,
smallest depth. Every recorded depth is therefore the shortest-path distance from the seed through
fetched pages.

*Consequence.* With identical site content and no transient failures, the set of crawled URLs and their
depths does not depend on the number of workers or on timing. The tests check this by comparing a
1-worker crawl with an N-worker crawl.

### 8.4 Page budget (`max_pages`)

- `max_pages` bounds the number of URLs admitted to a job, seed included. Each admitted URL produces at
  most one result, so a job stores at most `max_pages` pages.
- URLs are admitted in only three places: job creation, level advancement, and same-level redirect
  admission. Each of these runs while holding the job row lock and increments `admitted_count` in the
  same transaction. `CHECK (admitted_count <= max_pages)` is the backstop.
- Level advancement admits candidates in a deterministic order: by source URL, then by link position. If
  the budget runs out partway through a level, the admitted set is a deterministic prefix of BFS order.
- URLs that robots.txt disallows still consume budget: they are admitted first and then `skipped`.

### 8.5 Redirects

- HTTPX never follows redirects automatically. A 3xx response is recorded on the URL as `fetched`, with
  `outcome = redirect` and the `redirect_target`.
- The worker's result transaction admits the target, at the **same depth**, while holding the job row
  lock, provided that:
  - the normalized target is in scope;
  - the redirect chain is within the hop limit;
  - budget remains.

  A redirect is not a link hop, and the target finishes within the current level.
- The target then goes through the whole pipeline (scope, robots.txt, DNS and IP validation, rate limit),
  so a redirect cannot bypass any check.
- The robots.txt fetcher is the one place that follows redirects inline. It follows up to five hops and
  validates each one.

### 8.6 Cancellation

`POST /api/v1/jobs/{id}/cancel` runs one transaction that takes the job row lock, moves the job from
`running` to `cancelling` (or from `queued` to `cancelled`), and cancels every `pending` and `queued`
URL. After that:

- The dispatcher stops, because the job is no longer `running`. Messages already in the stream fail their
  claim and are acked.
- In-flight tasks may finish. Their commit still passes the fencing check and stores the page, but admits
  nothing new.
- The finalizer moves the job to `cancelled` once no URL is `in_progress`. The reconciler cancels leases
  left behind by crashed workers once they expire. Cancellation therefore completes within about one
  lease duration.
- Cancel is idempotent. Cancelling a terminal job returns its current state.

### 8.7 Terminal completion

A running job completes when no URL is `pending`, `queued`, or `in_progress` and no further level can be
admitted, because the depth limit was reached, the budget ran out, or there are no new in-scope links.

The check and the state change happen in one transaction that holds the job row lock. A worker commits
its URL's terminal state together with any redirect admission, in a single transaction. So no snapshot
can show "nothing in progress" while new work is about to appear.

## 9. Transactions and concurrency control

| | Transaction | Steps | Notes |
|---|---|---|---|
| T1 | Create job | insert job + seed URL | repeating an `Idempotency-Key` returns the existing job; a different request body returns `409` |
| T2 | Activate | compare-and-set `queued -> running` | |
| T3 | Dispatch | lock eligible rows `FOR UPDATE SKIP LOCKED` → `XADD` each → set `queued` and `stream_entry_id` → commit | a crash between `XADD` and commit causes a duplicate message, never a lost task |
| T4 | Claim | compare-and-set update (sketch below) | committed before any network I/O |
| T5 | Result | fencing check → page → links → `index_outbox` → URL terminal or retry → (for a redirect: lock job, admit target) → commit → `XACK` | all or nothing |
| T6 | Advance | lock job → verify no unfinished URLs → admit next level or set terminal | a single writer per job |
| T7 | Cancel | lock job → set status → cancel `pending` and `queued` URLs | |
| T8 | Relay | lock unpublished rows `FOR UPDATE SKIP LOCKED` → `XADD` → set `published_at` | |
| T9 | Index | upsert `search_documents`, guarded by content hash → commit → `XACK` | |
| T10 | Reconcile | per-row compare-and-set: re-publish, fail, or cancel | |

Claim (sketch):

```sql
UPDATE crawl_urls AS u
SET    state            = 'in_progress',
       lease_epoch      = u.lease_epoch + 1,
       attempt_count    = u.attempt_count + 1,
       lease_owner      = :consumer,
       lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds)
FROM   crawl_jobs AS j
WHERE  u.id = :url_id
  AND  j.id = u.job_id AND j.status = 'running'
  AND  u.attempt_count < :max_attempts
  AND  (u.state = 'queued'
        OR (u.state = 'in_progress' AND u.lease_expires_at < clock_timestamp()))
RETURNING u.lease_epoch, u.url, u.depth, j.allowed_hosts, j.max_depth;
```

Fenced result commit (sketch):

```sql
BEGIN;
SELECT 1 FROM crawl_urls
WHERE  id = :url_id AND state = 'in_progress' AND lease_epoch = :epoch
FOR UPDATE;   -- no row: fenced out -> ROLLBACK, discard the result, do NOT XACK
INSERT INTO pages ...       ON CONFLICT (crawl_url_id) DO NOTHING;
INSERT INTO page_links ...  ON CONFLICT (source_page_id, target_url_hash) DO NOTHING;
INSERT INTO index_outbox ...;
UPDATE crawl_urls
SET    state = 'fetched', http_status = :status, outcome = :outcome,
       finished_at = clock_timestamp(), lease_owner = NULL, lease_expires_at = NULL
WHERE  id = :url_id;
COMMIT;       -- then XACK
```

Rules:

- **Isolation.** Transactions run at READ COMMITTED, the PostgreSQL default, combined with explicit row
  locks, unique constraints, and compare-and-set updates. SERIALIZABLE is not used. Every invariant is
  already protected by a constraint or a lock, and avoiding serialization-failure retries keeps the
  worker code simple.
- **Lock order.** A worker locks its own URL row first, then the job row, and the latter only when it
  admits a redirect target. Cancellation and level advancement lock the job row first, but afterwards
  touch only rows that no worker holds: `pending` and `queued` URLs, and newly inserted rows. There is no
  cycle, so there is no deadlock. Any new transaction must also lock a URL row before its job row, or
  show that it touches disjoint rows.
- **Contention.** The dispatcher and the relay select rows with `SKIP LOCKED`, so concurrent instances
  never block each other and never pick the same rows.
- **Uniqueness.** Unique constraints keep racing duplicate inserts safe even when application-level
  checks race.
- **No network I/O inside a transaction.** The only exception is the short `XADD` in T3 and T8, which is
  bounded by a Redis timeout.

## 10. Reliability model

### 10.1 Delivery semantics

Processing is at-least-once, and every effect is idempotent
([ADR-0003](adr/0003-at-least-once-leases-fencing.md)):

| Effect | Why repeating it is safe |
|---|---|
| Job creation | unique `Idempotency-Key` |
| URL admission | `UNIQUE (job_id, url_hash)` + `ON CONFLICT DO NOTHING`; the budget changes in the same transaction |
| Crawl result | fencing check, and the URL leaves `in_progress` in the same transaction, so each URL gets at most one result |
| Page and link rows | write-once keys: `crawl_url_id` and `(source_page_id, target_url_hash)` |
| Search document | upsert keyed by `page_id`, guarded by the content hash |
| HTTP fetch | **can repeat** after a crash or a lease takeover; acceptable because these are rate-limited GET requests to approved targets |

### 10.2 Leases, deadlines, and recovery timing

```
task_deadline  <  lease_duration  <=  reclaim_min_idle  <<  reconcile_after
```

- `task_deadline`: a hard asyncio timeout covering the rate-limit wait, the fetch, and parsing.
- `lease_duration`: counted from the claim, by the PostgreSQL clock.
- `reclaim_min_idle`: the idle threshold for `XAUTOCLAIM`, measured by Redis.
- `reconcile_after`: how long PostgreSQL and Redis may disagree before the reconciler steps in.

A worker gives up before its lease ends. A message therefore becomes reclaimable only after its first
worker has finished, failed, or stalled abnormally. If a stalled worker later resumes, the fencing check
rejects its commit. The process refuses to start with settings that violate this ordering.

Initial defaults are starting points to validate in tests, not measured values:

| Setting | Default |
|---|---|
| task deadline | 60 s |
| lease duration | 90 s |
| reclaim min-idle | 120 s |
| reconcile after | 10 min |
| attempts | 3 |
| backoff | base 2 s, capped at 60 s |

### 10.3 How the crawler worker handles a message

1. **Malformed message.** Dead-letter it and ack.
2. **Claim succeeds.** Process the URL, run the fenced commit, then ack.
   - If the commit is fenced out, discard the result and **do not ack**. The message now belongs to the
     new lease holder, and `XACK` acts on the whole group, so acking would hide it.
3. **Claim fails.** Read the URL row and act on what it shows:
   - the URL is terminal, or the job is no longer running: ack;
   - another live worker holds the lease: leave the message pending, to be re-examined after
     `reclaim_min_idle`;
   - attempts are exhausted: mark the URL `failed` (compare-and-set) and ack.

### 10.4 Failure windows

| # | Failure | State left behind | Recovery | Outcome |
|---|---|---|---|---|
| F1 | API crashes after commit, before responding | job exists | client retries with the same `Idempotency-Key` | same job, no duplicate |
| F2 | Dispatcher crashes after `XADD`, before commit | URL still `pending`; message in the stream | URL is dispatched again | duplicate message; the second claim is a no-op |
| F3 | Worker crashes before claiming | message pending under a dead consumer | another worker runs `XAUTOCLAIM` | delay only |
| F4 | Worker crashes after claiming, before commit | URL `in_progress` with a running lease | `XAUTOCLAIM` after min-idle, then a takeover claim (`lease_epoch + 1`) | page fetched again; the attempt is counted |
| F5 | Worker stalls past its lease, then resumes | another worker holds the lease | the stale commit fails fencing and rolls back | no duplicate effects |
| F6 | Worker crashes after commit, before `XACK` | URL terminal; message pending | redelivered → claim fails → ack | no duplicate effects |
| F7 | Redis loses or trims messages | URL `queued` or `in_progress` with no live stream entry | reconciler re-publishes | delay only |
| F8 | Redis is unavailable | jobs are still accepted; nothing is dispatched | dispatcher, relay, and workers retry with backoff | delay only |
| F9 | PostgreSQL is unavailable | no claims or commits; messages stay pending | resumes through `XAUTOCLAIM` and the reconciler | delay; PostgreSQL is the hard dependency |
| F10 | Relay crashes after `XADD`, before marking the row published | outbox row still unpublished | published again | duplicate index event; the upsert is a no-op |
| F11 | Indexer crashes before or after commit | message pending | redelivery | idempotent upsert |
| F12 | Scheduler crashes, or two schedulers overlap | none | restart; advisory-lock leader; `SKIP LOCKED` + compare-and-set | at most duplicate messages |
| F13 | Poison message repeatedly crashes workers | delivery count grows | `attempt_count` bounds claims, ending in `failed`; unclaimable messages are dead-lettered after N deliveries | bounded work |

### 10.5 Retries and backoff

- **Retryable:** timeouts, connection errors, temporary DNS failures, and HTTP 429, 500, 502, 503, and
  504. Every other error is terminal.
- **Delay:** `uniform(0, min(cap, base * 2^(attempt - 1)))`, i.e. full jitter. `Retry-After` is honored up
  to the cap.
- **Durability:** a retry sets the URL back to `pending` with a future `next_attempt_at`. A crash never
  loses a retry, and a waiting retry never occupies a worker slot.
- **Infrastructure calls:** PostgreSQL and Redis calls get a few short in-process retries. After that, the
  message stays pending and is recovered as in F3 and F4.

### 10.6 Redis and PostgreSQL consistency limitations

- No commit spans both systems. The outbox gives at-least-once publication, so duplicates are expected
  and tolerated.
- With AOF `everysec`, a Redis crash can lose about a second of writes, and more with weaker persistence
  settings. Correctness does not depend on Redis persistence, because the reconciler restores liveness
  within `reconcile_after`.
- A message re-published as a new entry starts with a fresh delivery count. PostgreSQL's `attempt_count`
  is therefore the authoritative retry limit.
- Queue statistics read from Redis (stream lengths, pending counts) are approximate. Job progress always
  comes from PostgreSQL.
- PostgreSQL is a single point of failure for progress: while it is down nothing advances, but nothing is
  lost.

### 10.7 Not guaranteed

- Exactly-once fetching or processing.
- Any particular order of tasks within a level, or of index events.
- A bound on total crawl time against slow or rate-limited targets. Only the work per URL is bounded.
- Durability of anything that exists only in Redis.

## 11. Correctness invariants

The integration, concurrency, and fault tests (Phases 3 to 5) check these after every scenario:

1. Each `(job_id, normalized URL)` appears at most once in `crawl_urls`.
2. `admitted_count` equals the job's number of `crawl_urls` rows and is at most `max_pages`.
3. Each URL's depth equals its shortest-path distance from the seed through fetched pages, and crawling a
   fixture site with 1 worker gives the same result as crawling it with N workers.
4. Each URL has at most one committed result, and each `html_stored` URL has exactly one `pages` row.
5. A terminal job has no unfinished URLs, and its data never changes afterward.
6. Every `index_outbox` row is eventually published, and every stored page eventually has exactly one
   `search_documents` row.
7. No request is ever sent to a blocked destination. Tests assert this with a recording resolver and a
   recording transport.

## 12. Security model

### 12.1 Threats

| Source | Examples |
|---|---|
| Users of the public demo | submitting internal URLs (SSRF), oversized crawls, request floods |
| Content on crawled sites | redirects to internal addresses, non-HTTP link schemes, huge or slow responses, decompression bombs, hostile HTML shown in the dashboard |
| Whoever controls DNS for an allowed domain | DNS rebinding to private addresses between the check and the connection |

Out of scope: a compromised host or leaked infrastructure credentials, and supply-chain attacks beyond
what dependency pinning and auditing catch.

### 12.2 Target allowlist and scope

- Every deployment has an explicit allowlist of crawl origins. There is no "crawl anything" mode;
  developers add hosts to their local allowlist.
- The public demo allows only the controlled demo site and any public site explicitly approved for it.
- The API validates the seed, and the worker re-checks the policy before every fetch. Changes to the
  allowlist therefore apply to existing jobs too.

### 12.3 SSRF and DNS rebinding

1. **URL policy.** Only `http` and `https`; no userinfo; only allowed ports (80 and 443 by default); a
   length limit; hosts normalized with IDNA.
2. **Resolve and validate.** The worker resolves the host itself and rejects the request if **any**
   resolved address is in a blocked class (see the table below). IPv4-mapped, IPv4-compatible, 6to4, and
   NAT64 IPv6 forms are unwrapped, and the embedded IPv4 address is checked as well.
3. **Pin the connection.**
   - The TCP connection goes to the validated IP.
   - The hostname is used only for the `Host` header, TLS SNI, and certificate verification.
   - There is no second DNS lookup for an attacker to answer differently, so the rebinding gap does not
     exist.
   - Every connection, retry, redirect target, and robots.txt fetch is resolved and validated again.
4. **HTTPX settings.** `trust_env=False`, so proxy and netrc settings from the environment are ignored,
   and `follow_redirects=False`.
5. **Network isolation.** In the public deployment, crawler workers sit on a network that can reach only
   approved targets.

| Destination class | production | development / test |
|---|---|---|
| Global unicast | allowed if the origin is allowlisted | same |
| Private (RFC 1918, IPv6 ULA) | only for origins configured as internal targets (e.g. the demo-site container) | same |
| Loopback | never | only for origins configured as internal targets (local fixture servers) |
| Link-local (including the `169.254.169.254` cloud metadata endpoint), shared/CGNAT, multicast, reserved, unspecified | never | never |

Internal targets are exact origins (scheme, host, and port) and are logged at startup. In production,
configuration cannot relax the loopback and link-local rules.

### 12.4 Resource bounds

All values are configurable. The defaults are starting points, not measurements.

| Bound | Mechanism | Initial default |
|---|---|---|
| Time | per-operation connect/read/write/pool timeouts, plus the overall task deadline (stops slow-drip responses) | 10 s per operation, 60 s per task |
| Size | streamed read, aborted once **decoded** bytes exceed the limit (stops decompression bombs) | 2 MiB |
| Content type | parse only `text/html` and `application/xhtml+xml` | n/a |
| Redirects | never followed inline; hop limit per chain | 5 |
| URLs and links | maximum URL length; maximum links recorded per page | 2048 characters; 1000 links |
| Crawl size | per-job `max_depth` and `max_pages`, under server-side caps | set per deployment |
| Concurrency | per-worker semaphore, per-job in-flight window, cap on running jobs | set per deployment |
| Politeness | per-host rate limit shared by all workers (GCRA in a Redis Lua script, using Redis server time); robots.txt `Crawl-delay` as a lower bound, capped | 1 request/s per host |

### 12.5 robots.txt (RFC 9309)

- robots.txt is fetched once per origin through the same safe fetcher, cached in Redis for at most
  24 hours, and parsed up to 500 KiB.
- The rule group is selected by user-agent token. `Allow` and `Disallow` use longest-match precedence and
  support the `*` and `$` wildcards.
- Status handling:
  - 4xx (other than 429): no restrictions.
  - 5xx, 429, or a network error: complete disallow for now, with a later retry. Treating 429 like a 5xx
    is a deliberately conservative choice.
  - Redirects: followed for up to five hops.
- Python's `urllib.robotparser` applies the first matching rule and does not support path wildcards, so
  it is not RFC 9309-compliant. Phase 5 will choose a compliant parser: either a small, well-tested
  library or our own implementation with tests.
- Page-level `<meta name="robots">` and `X-Robots-Tag` are honored. `noindex` keeps the page out of
  search, and `nofollow` stops its links from being admitted.

### 12.6 Untrusted content in the dashboard

- Crawled titles, snippets, anchor text, and URLs are rendered as text through React's escaping.
  `dangerouslySetInnerHTML` is never used for crawled content.
- Search highlighting: `ts_headline` uses sentinel delimiters instead of HTML tags, and the frontend
  splits on them to render `<mark>` elements. Sentinel characters are removed from page text before
  indexing.
- A crawled URL is rendered as a link only if it is http(s), and always with
  `rel="noopener noreferrer nofollow"`.

### 12.7 API surface and secrets

- Every input is validated with Pydantic, request sizes are limited, and server-side caps apply.
- In public deployments, mutating endpoints can require a bearer token. Read endpoints are public.
- Job creation is rate-limited per client, and CORS is restricted to the dashboard origin.
- Errors are returned as `application/problem+json` without internal details. SQL is issued only with
  bound parameters.
- Configuration comes from environment variables. `.env` is git-ignored, and secrets never appear in
  code, tests, docs, or logs.
- PostgreSQL and Redis are never reachable from outside the Compose network.

## 13. Search, indexing, and link analysis

**Pipeline.** Result commit → `index_outbox` row (in the same transaction) → relay → `sitescope:index` →
indexer → upsert into `search_documents` ([ADR-0005](adr/0005-postgresql-full-text-search.md)). The index
lives in its own table so that GIN maintenance stays off the crawl write path, and so that re-indexing
(for example, with new weights) needs no re-crawl.

**Document.**

```sql
setweight(to_tsvector('english', title), 'A')
  || setweight(to_tsvector('english', headings), 'B')
  || setweight(to_tsvector('english', body), 'C')
```

Body text is truncated before `to_tsvector`, because a `tsvector` is limited to 1 MB.

**Query**

- `websearch_to_tsquery('english', :q)` builds the query. It never raises a syntax error on user input.
- `search_vector @@ query` matches documents through the GIN index.
- Results are ordered by `ts_rank_cd(...) DESC, page_id`.
- `ts_headline` runs only for the rows on the returned page.
- Search is scoped to one job, with `limit`/`offset` pagination capped at a maximum offset.

**Ranking limits.** `ts_rank_cd` uses term frequency and proximity, not corpus statistics, so there is no
IDF or BM25. There is no fuzzy matching, and only the `english` configuration is used. Any ranking
change, such as boosting pages by in-link count, must be justified by an evaluation rather than assumed.

**Link analysis.** Computed in SQL over `page_links` joined to `crawl_urls` on `(job_id, url_hash)`:

- **Broken links:** internal links whose target `failed`, or was fetched with a status of 400 or higher.
  Each is reported with its source page, anchor text, and status or error.
- **Redirecting links:** targets that answered 3xx, together with their redirect target.
- **Unchecked links:** internal targets that were never fetched, because they were beyond the depth or
  budget, or robots.txt skipped them. They are reported separately and never counted as "OK".
- **External links:** grouped by host. They are never fetched, since that would violate the allowlist, so
  they are never classified as broken.
- **Structure:** in-link and out-link counts per page, the depth distribution, the most-linked pages, and
  pages reached through only one in-link.

## 14. Observability and configuration

**Logs.** JSON lines on stdout with `timestamp`, `level`, `logger`, `event`, and `service`, plus context
fields: `request_id`, `job_id`, `url_id`, `worker_id`, `lease_epoch`, `attempt`. Key events:

| Area | Events |
|---|---|
| Jobs | `job.created`, `job.level_advanced`, `job.completed` |
| Tasks | `task.dispatched`, `task.claimed`, `task.committed`, `task.fenced`, `task.retry_scheduled`, `task.dead_lettered` |
| Recovery | `lease.taken_over`, `reconcile.republished` |
| Indexing | `outbox.published`, `index.upserted` |

**Health**

- `GET /health` is liveness: the process is up. It checks no dependencies.
- `GET /ready` reports whether PostgreSQL and Redis are reachable.
- The scheduler and the workers log periodic heartbeats, which the Compose health checks use.

**Progress views**

- Job detail shows, from PostgreSQL: counts per URL state, the current level, admitted vs. maximum pages,
  and indexing progress.
- A system endpoint shows, from Redis and therefore approximately: stream length, pending count, and
  consumers per group.

**Configuration**

- One typed `Settings` object built with pydantic-settings from `SITESCOPE_`-prefixed environment
  variables.
- Validated at startup. The process exits on invalid or inconsistent values, such as a violation of the
  timing order in §10.2.
- Settings groups:
  - environment
  - connections
  - target policy
  - crawl caps
  - concurrency
  - timing
  - retries
  - politeness: rate limit, and the user agent `SiteScopeBot/<version>`
  - API: token, CORS
- `.env.example` lists every variable with a safe placeholder.

## 15. Deployment topology

Deployment uses Docker Compose. Phase 2 starts PostgreSQL and Redis for development, and Phase 9 runs the
full stack.

| Service | Networks | Port published | Notes |
|---|---|---|---|
| `frontend` (nginx) | edge, internal | yes | serves the SPA and reverse-proxies `/api` |
| `api` | internal | no | |
| `scheduler` | internal | no | |
| `crawler-worker` (N replicas) | internal, crawl | no | `docker compose up --scale crawler-worker=N` |
| `indexer-worker` | internal | no | |
| `migrate` | internal | no | one-shot `alembic upgrade head`; other services wait until it succeeds |
| `postgres` | internal | no | named volume |
| `redis` | internal | no | AOF on, named volume |
| `demo-site` (nginx, static) | crawl | optional | the approved demo target |

- One backend image runs every Python role (with different commands) as a non-root user.
- In the public profile, the `crawl` network has no general internet egress.
- Before Phase 9, the Python processes run on Windows from `backend/.venv` against the PostgreSQL and
  Redis containers started by Compose.

## 16. Repository structure (proposed)

```
SITESCOPE/
|-- CLAUDE.md                    # stable context and rules for contributors and Claude
|-- README.md                    # Phase 11: overview, quickstart, design highlights
|-- .gitignore                   # Phase 1
|-- .env.example                 # Phase 1: every setting, with safe placeholders
|-- docker-compose.yml           # Phase 2: postgres + redis; Phase 9: full stack
|-- .github/workflows/ci.yml     # Phase 1; extended in later phases
|-- backend/
|   |-- .venv/                   # existing; never committed, never recreated
|   |-- pyproject.toml           # dependencies + ruff / mypy / pytest config
|   |-- alembic.ini
|   |-- alembic/                 # migrations
|   |-- Dockerfile               # Phase 9
|   |-- app/
|   |   |-- main.py              # FastAPI app factory, lifespan, routers, error handlers
|   |   |-- core/                # settings, logging, domain errors
|   |   |-- api/                 # routers (jobs, search, links, system), schemas, dependencies
|   |   |-- services/            # use cases that own transactions: jobs, frontier, indexing, search, links
|   |   |-- db/                  # SQLAlchemy models, engine and session factory
|   |   |-- queue/               # Redis Streams adapter, versioned message schemas
|   |   |-- scheduler/           # activator, dispatcher, level advancer, relay, reconciler, leader lock
|   |   |-- crawler/             # URL normalization, scope, destination policy, resolver, safe fetcher,
|   |   |                        #   robots.txt, rate limiter, HTML parser (no database imports)
|   |   |-- indexing/            # search document building
|   |   `-- workers/             # crawler and indexer entrypoints, shared consumer runtime
|   `-- tests/
|       |-- unit/
|       |-- integration/
|       `-- fault/
|-- frontend/                    # Phase 8 (exists on disk as the misspelled `fronted/`; rename pending approval)
|-- demo-site/                   # Phase 5/9: static site + robots.txt, the approved demo target
|-- benchmarks/                  # Phase 10: scripts, raw results, environment notes
`-- docs/
    |-- architecture.md
    |-- development-roadmap.md
    `-- adr/
```

- The Python package is named `app`, following FastAPI convention and the planned `backend/app/main.py`.
  Every process role is an entrypoint inside it ([ADR-0007](adr/0007-modular-monolith-process-roles.md)).
- There is no generic repository layer. Query functions live with the service that owns the transaction,
  and `db/` holds the models and session management.
- Dependency direction:
  - `api` depends on `services`, which depends on `db`.
  - `scheduler` and `workers` depend on `services`, `queue`, `crawler`, and `indexing`.
  - `crawler` and `queue` have no domain or database dependencies, which keeps them unit-testable.

## 17. Technology choices

| Concern | Choice | Reason |
|---|---|---|
| API | FastAPI + Uvicorn (already installed) | async, Pydantic validation, generated OpenAPI docs |
| Settings | pydantic-settings | typed environment configuration, using the same validation model as the API |
| Database | PostgreSQL, a current release (16 or later), with the version pinned in Phase 2 | transactions, row locks, `SKIP LOCKED`, constraints, and full-text search with GIN, all in one system |
| ORM / migrations | SQLAlchemy 2.x (async) + Alembic | typed models; SQLAlchemy Core for set-based statements |
| PostgreSQL driver | asyncpg | mature async driver; psycopg's async mode would require switching Windows to the selector event loop (to be verified on this machine in Phase 2) |
| Queue | Redis Streams, Redis 7.0 or later | consumer groups, pending entries list, `XAUTOCLAIM` |
| Redis client | redis-py (`redis.asyncio`) | the official client |
| HTTP | HTTPX (HTTP/1.1) | async streaming, granular timeouts, pluggable transport |
| HTML parsing | BeautifulSoup 4 with the stdlib `html.parser` | tolerant and has no native dependency; lxml only if benchmarks justify it |
| robots.txt | chosen in Phase 5 | must be RFC 9309-compliant (§12.5) |
| Logging | stdlib `logging` + a JSON formatter | no extra dependency |
| Tests | pytest + an async plugin (chosen in Phase 1), `httpx.MockTransport` | standard tooling |
| Quality | ruff (lint and format), mypy | |
| Frontend | React + TypeScript + Vite; Vitest + Testing Library | |
| Delivery | Docker Compose; GitHub Actions with PostgreSQL and Redis service containers | |

## 18. Testing strategy

| Layer | What it covers | Needs |
|---|---|---|
| Unit | pure logic: normalization, scope, destination policy, robots.txt matching, backoff, parsing, message schemas, settings validation | nothing |
| Integration | migrations up and down; claim, commit, and fencing SQL; level advancement; cancellation; stream-adapter semantics (groups, ack, autoclaim, deleted entries); the API against a real database | PostgreSQL, Redis |
| Concurrency | 1 to 8 workers crawl synthetic site graphs through an in-memory fetcher test double; checks the §11 invariants and that 1-worker and N-worker results match | PostgreSQL, Redis |
| Fault injection | deterministic crash points (before claim, after claim, before commit, after commit but before ack); a worker stalled past its lease; `FLUSHALL` mid-crawl; a dispatcher crash between `XADD` and commit; poison messages; killing a real worker process | PostgreSQL, Redis |
| Security | SSRF table tests (IPv4 and IPv6 address classes, mapped forms, alternative host encodings, redirects to private addresses, rebinding through a scripted resolver); size and time limits; robots.txt semantics | nothing, or a loopback fixture |
| End-to-end | crawl a local fixture site over real HTTP, then check search results and link reports | full stack |
| Frontend | components and the API client; type check; production build | Node.js |

Tests that need unavailable services skip with an explicit reason. Benchmarks are kept separate from
tests (Phase 10): fixed parameters, a recorded environment, and raw output committed to the repository.

## 19. Assumptions and known limitations

**Reliability assumptions**

- PostgreSQL is durable (fsync on), runs as a single primary without automatic failover, and is the only
  component whose loss loses data.
- Redis is a single node with AOF. It may lose recent writes or all of its data; the reconciler recovers
  from either.
- Any process can crash at any instruction, or pause (GC, VM suspension, network partition) and resume
  later.
- Messages can be duplicated, delayed, reordered, or lost in Redis.
- Server clocks are NTP-synchronized. Even so, correctness relies only on the PostgreSQL clock (leases)
  and the Redis clock (idle times and rate limits).
- The scale is small: up to about 10^5 URLs per deployment and between a few and a few tens of worker
  processes.

**Security assumptions**

- Crawl targets are explicitly approved, but their content is untrusted.
- The deployment host, the Docker networks, and the secrets are trusted. The public demo has no user
  accounts.
- Complying with robots.txt is a matter of politeness, not a security boundary.

**Known limitations (accepted for v1)**

- Level-synchronous BFS means one slow URL holds back the next level.
- For a single host, throughput is bounded by that host's rate limit, not by the number of workers.
  Horizontal scaling helps across hosts and jobs.
- URLs disallowed by robots.txt consume page budget.
- HTML only, no JavaScript rendering, and English-only text search.
- Basic relevance ranking (no BM25).
- No incremental re-crawl; storage is duplicated across jobs.

## 20. Decision records

| ADR | Decision |
|---|---|
| [0001](adr/0001-postgresql-source-of-truth.md) | PostgreSQL is the source of truth; Redis Streams only delivers work |
| [0002](adr/0002-transactional-outbox.md) | Transactional outbox for publishing to Redis |
| [0003](adr/0003-at-least-once-leases-fencing.md) | At-least-once processing with leases, fencing tokens, and idempotent effects |
| [0004](adr/0004-level-synchronous-bfs.md) | Level-synchronous BFS with scheduler-side admission |
| [0005](adr/0005-postgresql-full-text-search.md) | PostgreSQL full-text search instead of a dedicated search engine |
| [0006](adr/0006-target-allowlist-ssrf-defenses.md) | Target allowlist and layered SSRF defenses |
| [0007](adr/0007-modular-monolith-process-roles.md) | Modular monolith with separate process roles |
