# SiteScope Architecture

> **Status: design baseline from Phase 0 (2026-11-06).** Implemented so far:
>
> - Phase 1: the API foundation (configuration, structured logging, error handling, liveness).
> - Phase 2B: the PostgreSQL schema (section 5), migrations, the PostgreSQL and Redis clients,
>   and the readiness probe.
> - Phase 2C: the crawl job API (`/api/v1/crawls`), URL normalization (section 8.1), the crawl
>   scope (section 8.2) and transactional job creation. Migration `0002` tightens the
>   database-enforced fencing (ADR-0011).
> - Phase 3: the safe fetcher (DNS validation, pinned connections, validated redirects, bounded
>   bodies; sections 12.3 and 12.4) and the HTML parser (`backend/app/crawler/`, ADR-0013).
>
> BFS crawling, robots.txt, rate limiting, scheduling, workers and search are not implemented yet.
> [`development-roadmap.md`](development-roadmap.md) tracks what has been built. Details will be
> refined during implementation. Changing an invariant requires a new ADR in
> [`adr/`](adr/README.md).

## Contents

1. Goals and non-goals
2. System overview
3. Components and responsibilities
4. End-to-end flow
5. Data model (implemented in Phase 2B)
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
    + --- fenced commit: page + links + search doc + URL outcome -------->+
                                                                          |
  scheduler (outbox relay) <-- pending search_documents rows -------------+
    | XADD {page_id}, then mark rows `queued`                             |
    v                                                                     |
  Redis stream sitescope:index    (consumer group: indexers)              |
    |                                                                     |
    v                                                                     |
  indexer worker --- write search_vector (GIN), mark row indexed -------->+
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
| Scheduler | `scheduler`: `python -m app.scheduler` | job activation, crawl dispatch, BFS level advancement, job completion, index relay, reconciliation, stream maintenance | one active leader; standbys are safe |
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
- Will accept an `Idempotency-Key` header on job creation (planned for Phase 6; the columns exist
  since Phase 2B).
- Follows [`api-conventions.md`](api-conventions.md): versioned routes under `/api/v1`, an
  `X-Request-ID` header on every response, and RFC 9457 problem+json errors.

**Scheduler.** One asyncio process running independent periodic loops:

- *Activator*: moves jobs from `queued` to `running` while the running-jobs cap allows it.
- *Dispatcher*: publishes eligible frontier rows to the crawl stream and marks them `queued`. A row is
  eligible when its state is `pending`, its depth equals the job's `current_level`, and
  `next_attempt_at <= now()`. The dispatcher respects a per-job in-flight window and rotates fairly
  across jobs.
- *Level advancer / finalizer*: when a running job has no unfinished URLs, admits the next BFS level from
  the recorded links, or completes the job. Moves `cancelling` jobs to `cancelled` once no URL is in
  progress.
- *Index relay*: publishes `pending` `search_documents` rows to the index stream and marks
  them `queued` (the indexing outbox, ADR-0008).
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

- Consumes `{page_id}` events.
- Builds a weighted `tsvector`: title A, headings B, body C.
- Writes the page's `search_vector` and `indexed_content_version`, and marks it `indexed`.
  Rebuilding a vector from the same content gives the same result, so replayed events are
  harmless.

## 4. End-to-end flow

1. **Create** (implemented in Phase 2C). `POST /api/v1/crawls` normalizes the seed URL and checks it
   against the approved targets and the server's caps, without any network access. One transaction
   inserts the job (`queued`, `current_level = 0`, `admitted_count = 1`) and the seed URL
   (`pending`, depth 0). Nothing is published to Redis.
2. **Activate.** The scheduler finds `queued` jobs in PostgreSQL (index `ix_crawl_jobs_active`) and
   sets each to `running` with `current_level = 0` once the running-jobs cap allows it.
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
   - inserts the page's `search_documents` row as `pending` (the indexing outbox);
   - marks the URL terminal, or, for a retryable failure, returns it to `pending` with a backoff.

   Then the worker sends `XACK`.
7. **Advance.** When none of the job's URLs is unfinished, the scheduler takes the job row lock. It
   admits the next level's URLs from links recorded by the current level, in deterministic order, up to
   the remaining page budget. If nothing can be admitted, it marks the job `completed`, or `failed` if
   the seed itself was never fetched.
8. **Index.** The relay publishes `pending` `search_documents` rows; the indexer writes each
   vector and marks the row `indexed`.
9. **Query.** The API serves progress, pages, link reports, and search results from PostgreSQL.

## 5. Data model (implemented in Phase 2B)

The schema is created by migration `0001` (`backend/alembic/versions/0001_initial_schema.py`).
Migration `0002` adds a fencing trigger (Phase 2C, ADR-0011). The ORM models in `backend/app/db/models.py` mirror it, and an integration test compares the two
(`alembic.autogenerate.compare_metadata` must report no differences). Six tables, nothing more.

```
crawl_jobs --1:N--> crawl_urls --1:0..1--> pages --1:N--> page_links ..> target URL (text only)
    |                  ^    |                  |
    |                  +----+                  +--1:0..1--> search_documents
    |            discovered_from_url_id
    |               (BFS parent)
    +--1:N--> crawl_events --N:0..1--> crawl_urls
```

| Table | Role | Primary key |
|---|---|---|
| `crawl_jobs` | one crawl request: configuration, lifecycle, BFS level, page budget | `id` UUIDv7 |
| `crawl_urls` | the URL frontier and each URL's lifecycle; **the crawl-task outbox** (ADR-0002) | `id` UUIDv7 |
| `pages` | content extracted from a fetched HTML document; at most one per crawl URL | `id` UUIDv7 |
| `page_links` | the crawl graph: every http(s) link on a page; targets need not be crawled | `(source_page_id, target_normalized_url)` |
| `search_documents` | per-page full-text index state; **the indexing outbox** (ADR-0008) | `page_id` |
| `crawl_events` | append-only audit log of lifecycle events | `id` bigint identity |

UUIDv7 keys are generated by PostgreSQL 18's `uuidv7()`. They are time-ordered, so new rows land at
the end of the primary-key index instead of at random positions.

### 5.1 Columns

**`crawl_jobs`**

| Column | Notes |
|---|---|
| `seed_url` | normalized URL |
| `allowed_hosts text[]` | crawl scope; within the deployment allowlist |
| `max_depth`, `max_pages` | crawl limits |
| `status` | `queued`, `running`, `cancelling`, `completed`, `failed`, `cancelled` |
| `current_level` | the BFS level being crawled |
| `admitted_count` | page budget used |
| `idempotency_key`, `request_fingerprint` | job-creation idempotency (SHA-256) |
| `failure_reason` | set when the job fails |
| timestamps | `created_at`, `updated_at`, `started_at`, `finished_at`, `cancel_requested_at`, `indexing_completed_at` |

**`crawl_urls`**

| Column | Notes |
|---|---|
| `job_id`, `normalized_url` | `COLLATE "C"` |
| `depth` | BFS distance from the seed |
| `discovered_from_url_id` | BFS parent |
| `redirect_hops` | |
| `state` | `pending`, `queued`, `in_progress`, `fetched`, `failed`, `skipped`, `cancelled` |
| `attempt_count`, `next_attempt_at` | retry control |
| `lease_epoch` (fencing token), `lease_owner`, `lease_expires_at`, `result_epoch` | lease and fencing |
| `dispatched_at`, `stream_entry_id` | task publication state |
| `http_status`, `outcome`, `last_error`, `redirect_target` | outcome |
| timestamps | `created_at`, `updated_at`, `finished_at` |

**`pages`**

| Column | Notes |
|---|---|
| `job_id`, `crawl_url_id`, `normalized_url` | the crawl URL reference |
| `http_status`, `content_type` | |
| `title`, `meta_description`, `headings`, `extracted_text` | extracted content |
| `content_hash` | SHA-256 |
| `content_version` | starts at 1 |
| `byte_size`, `fetch_duration_ms`, `fetched_at` | fetch details |
| timestamps | `created_at`, `updated_at` |

**`page_links`**

| Column | Notes |
|---|---|
| `job_id` | |
| `anchor_text` | first occurrence, ≤ 500 characters |
| `position` | order in the document |
| `is_internal` | whether the target is in the job's scope |

The uniqueness policy is one row per (source page, target URL). Repeated links on the same page
collapse to the first occurrence.

**`search_documents`**

| Column | Notes |
|---|---|
| `job_id` | |
| `status` | `pending`, `queued`, `indexed`, `failed` |
| `indexed_content_version` | the content version the vector reflects |
| `search_vector tsvector` | GIN-indexed |
| `attempt_count`, `last_error` | retry control |
| `dispatched_at`, `stream_entry_id` | publication state |
| `indexed_at` | |

The page's `content_version` versus `indexed_content_version` tells whether a page needs
(re)indexing. Setting `status` back to `pending` re-queues it.

**`crawl_events`**

| Column | Notes |
|---|---|
| `job_id`, `crawl_url_id` | `crawl_url_id` is optional |
| `event_type` | dotted lowercase, e.g. `job.created`, `url.retry_scheduled` |
| `from_state`, `to_state` | |
| `worker_id`, `lease_epoch` | |
| `detail jsonb` | an object; never secrets or page bodies |
| `occurred_at` | |

### 5.2 Constraints the database enforces

| Concern | Enforcement |
|---|---|
| **Job isolation** | Every reference between job-scoped tables is a composite foreign key that includes `job_id`, e.g. `pages (crawl_url_id, job_id, normalized_url) -> crawl_urls (id, job_id, normalized_url)`. A page, link, search document, event or BFS parent can never point into another job. |
| **Deduplication** (ADR-0009) | `UNIQUE (job_id, normalized_url)` on `crawl_urls`, used with `INSERT ... ON CONFLICT DO NOTHING`. `UNIQUE (crawl_url_id)` on `pages`. The primary key of `page_links`. |
| **URL format** | http(s), printable ASCII without spaces, at most 2048 bytes (so B-tree entries always fit). `COLLATE "C"` gives deterministic byte ordering. |
| **Page budget** | `CHECK (admitted_count BETWEEN 0 AND max_pages)`; `CHECK (current_level BETWEEN 0 AND max_depth)`. |
| **Lifecycle** | Valid `status` / `state` values. `finished_at` is set exactly when the state is terminal. `started_at` is set once started. `failed` jobs carry a `failure_reason`; cancelled jobs carry `cancel_requested_at`. Timestamps are ordered. |
| **Leases and fencing** (ADR-0010) | A lease (`lease_owner`, `lease_expires_at`) exists exactly while the URL is `in_progress`. `result_epoch` must equal `lease_epoch`, a claim must clear it, and worker outcomes (`fetched`, `failed`, `skipped`) must carry it. Triggers keep `lease_epoch` from decreasing (`0001`), and let it change only in a claim, by exactly one, with every lease grant requiring that new token (`0002`, ADR-0011). |
| **Publication** | `queued` rows have `dispatched_at`; `stream_entry_id` has the Redis entry-ID format. |
| **Search state** | `indexed` documents have a vector, an indexed version and an `indexed_at`. |
| **Audit log** | `crawl_events` rejects `UPDATE` (trigger). `event_type` is dotted lowercase, and `detail` is a JSON object. |
| **Bookkeeping** | A trigger maintains `updated_at`. Bounded text lengths (`last_error`, titles, extracted text). |
| **Referential cleanup** | Deleting a job cascades to all of its URLs, pages, links, search documents and events. |

The partial indexes serve the planned access paths:

| Index | Serves |
|---|---|
| `ix_crawl_urls_dispatch (job_id, depth, next_attempt_at) WHERE state = 'pending'` | the dispatcher: pending URLs at the current BFS level |
| `ix_crawl_urls_unfinished (job_id) WHERE state IN (pending, queued, in_progress)` | level advancement and job completion |
| `ix_crawl_urls_lease_expiry`, `ix_crawl_urls_queued_since` | recovery: expired leases, stale publications |
| `ix_search_documents_pending`, `ix_search_documents_queued_since` | the index relay and its recovery |
| `ix_search_documents_search_vector` (GIN) | full-text search |
| `ix_page_links_job_id_target (job_id, target_normalized_url)` | link analysis and BFS expansion |
| `ix_crawl_events_job_id_id (job_id, id)` | a job's events in order |

### 5.3 Why this schema supports the distributed worker architecture

1. **Publication state lives with the work.** A URL's `state`, `dispatched_at` and `stream_entry_id`
   are in the same row the scheduler publishes from. The scheduler can therefore publish only
   committed rows, and the reconciler can tell which tasks Redis may have lost. Indexing uses the
   same pattern through `search_documents`.
2. **Claims are single-row compare-and-set updates** against the database clock. Duplicate
   deliveries are harmless: a claim only succeeds for `queued` rows or expired leases.
3. **Stale workers are fenced by the database.** A result recorded under an old `lease_epoch`
   violates a CHECK, and the whole transaction (page, links, search document) rolls back. A claim
   that grants a lease without a new token, and a stale worker that issues itself one, are
   rejected by a trigger. ADR-0011 lists what the database still cannot detect.
4. **Concurrency safety comes from constraints, not application locks.** Concurrent discovery of
   one URL creates one row. Concurrent admissions cannot pass `max_pages`. Replays of page, link
   or index writes hit unique keys and become no-ops.
5. **BFS gating and completion are index lookups** (`current_level` plus the partial indexes
   above).
6. **Job isolation** survives application bugs, because it is a foreign-key property.

All of this is covered by integration tests against PostgreSQL 18 (see
[development.md](development.md#integration-tests)).

### 5.4 Transaction assumptions

- READ COMMITTED (the PostgreSQL default), one transaction per state transition, explicit
  boundaries (`Database.transaction()`), no network I/O inside a transaction.
- Correctness relies on constraints, row locks and conditional updates; SERIALIZABLE is not
  used (section 9).
- Timestamps that take part in comparisons come from PostgreSQL (`now()`, `clock_timestamp()`),
  never from a client. During Phase 2B, the Windows host clock and the clock in the WSL VM were
  observed about 160 ms apart, and a CHECK constraint caught a test that mixed the two.

### 5.5 PostgreSQL and Redis are not one transaction

Publishing a task means writing to Redis about a row in PostgreSQL. No transaction spans both
systems. These are the publication-related failure windows, and the columns that make each one
recoverable. The full list of windows is in section 10.4.

| Window | Outcome | What the schema provides |
|---|---|---|
| Relay `XADD` succeeded, commit failed | row still `pending`, so it is published again | duplicate tolerated: the second claim finds the row no longer `queued` |
| Commit succeeded, Redis later lost the entry (restart, trim) | row `queued`, no message | `dispatched_at` and `stream_entry_id` let the reconciler re-publish |
| Worker committed, but its `XACK` was lost | message redelivered | URL is terminal, so the claim matches no row and the worker only acks |
| Worker crashed while holding a lease | `in_progress` with an expired lease | `ix_crawl_urls_lease_expiry`; a takeover claim increments `lease_epoch` |
| Stale worker commits after a takeover | rejected | the `result_epoch` CHECK rolls the transaction back |
| Index message delivered twice | indexed twice | rebuilding a vector from the same content is idempotent; status and version guards |

## 6. Redis layout

| Key | Type | Purpose |
|---|---|---|
| `sitescope:crawl` | stream; group `crawlers` | crawl tasks `{v, job_id, url_id}` |
| `sitescope:index` | stream; group `indexers` | index tasks `{v, page_id}` |
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
| `queued` | `in_progress` | claim (worker) | job `running`, `attempt_count < max_attempts`; `lease_epoch` advances by exactly one (ADR-0011) |
| `in_progress` | `in_progress` | takeover claim (worker) | lease expired by the database clock; `lease_epoch` is incremented |
| `in_progress` | `fetched` / `skipped` / `failed` | result commit (worker) | fencing check passes; the database requires `result_epoch = lease_epoch` (ADR-0010) |
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

### 8.1 URL normalization (implemented in Phase 2C)

`normalize_url()` in `backend/app/crawler/urls.py` is a pure function: no network, DNS or database
access. The API applies it to seed URLs. The HTML parser and the safe fetcher apply it, through
`join_url`, to every link and redirect target after resolving it against its document (RFC 3986
section 5, honoring `<base href>`; tested against the RFC's own examples).
It runs before any scope check, deduplication, or storage.

**Rule.** Two inputs normalize to the same URL only when the URI standards define them as
equivalent (RFC 3986 sections 6.2.2 and 6.2.3; RFC 9110 section 4.2.3 for http and https), or when
an invalid character is replaced by its only valid percent-encoding.

**Changed**, because the forms are equivalent:

- the scheme and the host are lowercased;
- internationalized hosts become IDNA A-labels (IDNA 2008 with UTS #46 mapping, as in current
  browsers). The `idna` package is used because Python's built-in codec implements IDNA 2003,
  which maps distinct names such as `faß.de` and `fass.de` to the same host;
- IPv6 hosts are written in compressed form;
- empty and default ports (`:80` for http, `:443` for https) are removed;
- an empty path becomes `/`, and `.` and `..` segments are resolved;
- percent-encodings get uppercase hex digits, and encoded unreserved characters
  (`A-Z a-z 0-9 - . _ ~`) are decoded;
- characters that a URL cannot contain literally (non-ASCII, space, and
  `` " < > [ ] ^ ` { | } `` outside the host) are percent-encoded as UTF-8;
- the fragment is removed;
- leading and trailing spaces and control characters are ignored, as browsers do.

**Kept**, because they can be meaningful:

- the case of the path, trailing slashes and repeated slashes;
- the query string as written: parameter order, duplicates, empty values, `+`, and an empty `?`.
  Sorting parameters or stripping tracking parameters would merge URLs that some sites treat
  differently;
- encoded reserved characters such as `%2F`;
- a trailing dot in the host. The Phase 0 design dropped it, but `example.com.` and `example.com`
  are different authorities for HTTP (Host header, TLS name, cookies), so dropping it would merge
  distinct URLs.

**Rejected**, with a message the API returns to the client:

- relative URLs, and schemes other than http and https;
- credentials (`user:password@`);
- control characters inside the URL (tabs and line breaks included), and backslashes, which
  browsers read as `/` but other parsers do not;
- empty, malformed or percent-encoded hosts;
- numeric hosts that are not dotted-decimal IPv4, such as `127.1`, `0x7f.0.0.1` or `2130706433`,
  because parsers disagree about them;
- IPv6 zone identifiers, invalid ports, and malformed percent-encodings;
- URLs longer than 2048 bytes after normalization.

The output is printable ASCII of at most 2048 bytes, which is exactly the contract of the
database CHECK constraints. The deduplication key is the normalized URL itself (ADR-0009). Unit
tests cover every rule, idempotence, the database contract, and a fixed-seed fuzz run.

### 8.2 Scope (implemented in Phase 2C)

Two levels decide whether a URL may be crawled ([ADR-0012](adr/0012-approved-crawl-targets.md)):

1. **The deployment's approved targets** (`backend/app/crawler/scope.py`):
   - the host matches an entry of `SITESCOPE_CRAWL_ALLOWED_HOSTS` as a whole normalized name.
     `docs.example.com` approves exactly that host. `*.example.com` approves every subdomain of
     example.com, but not example.com itself;
   - the scheme is in `SITESCOPE_CRAWL_ALLOWED_SCHEMES`;
   - the effective port (explicit, or the scheme's default) is in
     `SITESCOPE_CRAWL_ALLOWED_PORTS`.
2. **The job's scope:** the host is in the job's `allowed_hosts`, which holds exactly the seed's
   host. Other hosts are outside the job, including other approved subdomains.

The API checks the seed against both levels when it creates a job. The safe fetcher checks the
deployment level again before every request, redirect targets included, so narrowing the
deployment's approvals also applies to existing jobs. The job level is applied by the crawl logic
(Phase 4). Out-of-scope links are recorded (`is_internal = false`) but never fetched.

These checks are syntactic, with no DNS lookups. They are **not** a complete SSRF defense: an
approved name can resolve to any address, and DNS answers can change. Before every request, the
safe fetcher (section 12.3) must validate the resolved address and pin the connection to it.

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

The HTTP stack never follows redirects. The safe fetcher (Phase 3) supports two modes:

- **Follow** (the default): up to `SITESCOPE_FETCH_MAX_REDIRECTS` hops (5). Every target is
  normalized and goes through the crawl scope, DNS resolution, address validation and pinning
  again. A loop or a longer chain is a failure. The result records each hop.
- **Return** (`max_redirects=0`): the 3xx response is the result, with its normalized
  `redirect_location`. Nothing connects to the target.

The crawl logic (Phase 4) chooses between the following plan, which needs the "return" mode, and
following inline:

- A 3xx response is recorded on the URL as `fetched`, with
  `outcome = redirect` and the `redirect_target`.
- The worker's result transaction admits the target, at the **same depth**, while holding the job row
  lock, provided that:
  - the normalized target is in scope;
  - the redirect chain is within the hop limit;
  - budget remains.

  A redirect is not a link hop, and the target finishes within the current level.
- The target then goes through the whole pipeline (scope, robots.txt, DNS and IP validation, rate limit),
  so a redirect cannot bypass any check.
- robots.txt (Phase 4) follows redirects inline, up to five hops, with the fetcher's "follow"
  mode.

### 8.6 Cancellation

`POST /api/v1/crawls/{id}/cancel` (planned for Phase 6) will run one transaction that takes the job row lock, moves the job from
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
| T1 | Create job (Phase 2C) | insert job + seed URL | one transaction, no Redis. Planned for Phase 6: repeating an `Idempotency-Key` returns the existing job, and a different request body returns `409` |
| T2 | Activate | compare-and-set `queued -> running` | |
| T3 | Dispatch | lock eligible rows `FOR UPDATE SKIP LOCKED` → `XADD` each → set `queued` and `stream_entry_id` → commit | a crash between `XADD` and commit causes a duplicate message, never a lost task |
| T4 | Claim | compare-and-set update (sketch below) | committed before any network I/O |
| T5 | Result | fencing check → page → links → `search_documents` row (`pending`) → URL outcome with `result_epoch` (or retry) → (for a redirect: lock job, admit target) → commit → `XACK` | all or nothing; a stale `result_epoch` makes PostgreSQL reject the whole transaction |
| T6 | Advance | lock job → verify no unfinished URLs → admit next level or set terminal | a single writer per job |
| T7 | Cancel | lock job → set status → cancel `pending` and `queued` URLs | |
| T8 | Index relay | lock `pending` `search_documents` rows `FOR UPDATE SKIP LOCKED` → `XADD` → set `queued`, `dispatched_at`, `stream_entry_id` | same publish-then-mark pattern as T3 |
| T9 | Index | write `search_vector`, `indexed_content_version`, `status = 'indexed'` → commit → `XACK` | idempotent |
| T10 | Reconcile | per-row compare-and-set: re-publish, fail, or cancel | |

Claim (sketch):

```sql
UPDATE crawl_urls AS u
SET    state            = 'in_progress',
       lease_epoch      = u.lease_epoch + 1,  -- exactly +1, and only in a claim (ADR-0011)
       attempt_count    = u.attempt_count + 1,
       lease_owner      = :consumer,
       lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds),
       result_epoch     = NULL  -- required: ck_crawl_urls_result_epoch_cleared_by_claim
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
INSERT INTO pages ...             ON CONFLICT (crawl_url_id) DO NOTHING;
INSERT INTO page_links ...        ON CONFLICT (source_page_id, target_normalized_url) DO NOTHING;
INSERT INTO search_documents ...  ON CONFLICT (page_id) DO NOTHING;  -- status 'pending'
UPDATE crawl_urls
SET    state = 'fetched', http_status = :status, outcome = :outcome,
       result_epoch = :epoch,  -- CHECK result_epoch = lease_epoch (ADR-0010)
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
| URL admission | `UNIQUE (job_id, normalized_url)` + `ON CONFLICT DO NOTHING`; the budget changes in the same transaction |
| Crawl result | fencing check, and the URL leaves `in_progress` in the same transaction, so each URL gets at most one result; PostgreSQL rejects stale epochs (ADR-0010) |
| Page and link rows | write-once keys: `crawl_url_id` and `(source_page_id, target_normalized_url)` |
| Search document | one row per page (`page_id` key); rebuilding a vector from the same content is a no-op |
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
| F1 | API crashes after commit, before responding | job exists | client retries with the same `Idempotency-Key` (planned for Phase 6; until then a retry creates a second job) | same job, no duplicate |
| F2 | Dispatcher crashes after `XADD`, before commit | URL still `pending`; message in the stream | URL is dispatched again | duplicate message; the second claim is a no-op |
| F3 | Worker crashes before claiming | message pending under a dead consumer | another worker runs `XAUTOCLAIM` | delay only |
| F4 | Worker crashes after claiming, before commit | URL `in_progress` with a running lease | `XAUTOCLAIM` after min-idle, then a takeover claim (`lease_epoch + 1`) | page fetched again; the attempt is counted |
| F5 | Worker stalls past its lease, then resumes | another worker holds the lease | the stale commit fails fencing and rolls back | no duplicate effects |
| F6 | Worker crashes after commit, before `XACK` | URL terminal; message pending | redelivered → claim fails → ack | no duplicate effects |
| F7 | Redis loses or trims messages | URL `queued` or `in_progress` with no live stream entry | reconciler re-publishes | delay only |
| F8 | Redis is unavailable | jobs are still accepted; nothing is dispatched | dispatcher, relay, and workers retry with backoff | delay only |
| F9 | PostgreSQL is unavailable | no claims or commits; messages stay pending | resumes through `XAUTOCLAIM` and the reconciler | delay; PostgreSQL is the hard dependency |
| F10 | Index relay crashes after `XADD`, before commit | search document still `pending` | published again | duplicate index task; indexing again is a no-op |
| F11 | Indexer crashes before or after commit | message pending | redelivery | idempotent upsert |
| F12 | Scheduler crashes, or two schedulers overlap | none | restart; advisory-lock leader; `SKIP LOCKED` + compare-and-set | at most duplicate messages |
| F13 | Poison message repeatedly crashes workers | delivery count grows | `attempt_count` bounds claims, ending in `failed`; unclaimable messages are dead-lettered after N deliveries | bounded work |

### 10.5 Retries and backoff

- **Retryable:** timeouts, connection errors, temporary DNS failures, truncated responses, and HTTP
  429, 500, 502, 503, and 504. Every other error is terminal. The safe fetcher reports each failure
  as a `FailureKind` with a `retryable` flag (`backend/app/crawler/errors.py`).
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

The integration, concurrency, and fault tests (Phases 4 to 6) check these after every scenario:

1. Each `(job_id, normalized URL)` appears at most once in `crawl_urls`.
2. `admitted_count` equals the job's number of `crawl_urls` rows and is at most `max_pages`.
3. Each URL's depth equals its shortest-path distance from the seed through fetched pages, and crawling a
   fixture site with 1 worker gives the same result as crawling it with N workers.
4. Each URL has at most one committed result, and each `html_stored` URL has exactly one `pages` row.
5. A terminal job has no unfinished URLs, and its data never changes afterward.
6. Every stored page has exactly one `search_documents` row, and every `pending` one is eventually
   published and indexed.
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

- Every deployment has an explicit allowlist of crawl targets, and it is empty by default. There is
  no "crawl anything" mode; developers add hosts to their local configuration. The format and the
  matching rules are in section 8.2 and [ADR-0012](adr/0012-approved-crawl-targets.md).
- Development conveniences cannot reach a public deployment. With `SITESCOPE_ENVIRONMENT=production`,
  the process refuses to start if the allowlist contains `localhost`, a `.localhost` name or an IP
  literal. Link-local addresses (including 169.254.169.254), multicast, unspecified and reserved
  addresses are refused in every environment.
- The public demo allows only the controlled demo site and any public site explicitly approved for it.
- The API validates the seed (Phase 2C), and the worker will re-check the policy before every
  fetch. Changes to the allowlist therefore apply to existing jobs too.
- The allowlist check is syntactic. Address validation and pinning (section 12.3) are what stop
  SSRF.

Implemented in Phase 3 by the safe fetcher (`backend/app/crawler/fetcher.py`,
`destinations.py`, `resolver.py`; [ADR-0013](adr/0013-safe-fetcher-pinned-connections.md)):

1. **URL policy.** Only `http` and `https`; no userinfo; only allowed ports (80 and 443 by default); a
   length limit; hosts normalized with IDNA (section 8.1). The crawl scope (section 8.2) is checked
   before any DNS lookup.
2. **Resolve and validate.** The fetcher resolves the host once per request hop and rejects the
   request if **any** resolved address is in a blocked class (see the table below). IPv4-mapped and
   NAT64 IPv6 forms are unwrapped and classified as their IPv4 address. IPv4-compatible, 6to4 and
   Teredo forms are reserved, so they are blocked.
3. **Pin the connection.** `PinnedNetworkBackend` replaces name resolution in the HTTP stack
   (`httpcore2`):
   - The TCP connection goes to a validated IP, in DNS order, falling back to the next validated
     address.
   - Any other host, port or socket type is refused.
   - The hostname is used only for the `Host` header, TLS SNI, and certificate verification.
   - There is no second DNS lookup for an attacker to answer differently, so the rebinding gap does not
     exist. Tests prove it with a scripted resolver, a recording backend, and host names that the
     operating system cannot resolve.
   - Every redirect target is resolved and validated again.
4. **No ambient configuration.** The fetcher uses `httpcore2` directly: no proxy support, no
   environment configuration (`HTTP_PROXY`, `.netrc`), no automatic redirects, no connection reuse.
   TLS 1.2 or newer, verified by the operating system's verifier (`truststore`).
5. **Network isolation.** In the public deployment, crawler workers sit on a network that can reach only
   approved targets (Phase 11).

| Destination class | production | development / test |
|---|---|---|
| Global unicast | allowed if the origin is allowlisted | same |
| Private (RFC 1918, IPv6 ULA) | only for origins configured as internal targets (e.g. the demo-site container) | same |
| Loopback | never | only for origins configured as internal targets (local fixture servers) |
| Link-local, cloud metadata endpoints (`169.254.169.254`, `fd00:ec2::254`, `100.100.100.200`, `192.0.0.192`), shared/CGNAT, multicast, reserved, unspecified | never | never |

Internal targets are exact origins (scheme, host, and port), configured with
`SITESCOPE_FETCH_INTERNAL_ORIGINS`. In production, configuration cannot relax the loopback and
link-local rules: the process refuses to start with a `localhost`, `.localhost` or IP-literal
internal origin, and loopback is never allowed. The address classes are tested explicitly, because
Python's `is_global` also counts multicast, NAT64 and IPv4-compatible forms of loopback, and
site-local addresses, as global.

### 12.4 Resource bounds

All values are configurable. The defaults are starting points, not measurements. The first five
rows are implemented by the safe fetcher and the parser (Phase 3).

| Bound | Mechanism | Initial default |
|---|---|---|
| Time | connect timeout (also bounds DNS and the TLS handshake), read/write timeout, and an overall deadline for the whole fetch, redirects included (stops slow-drip responses) | 10 s, 10 s, 60 s |
| Size | streamed read with a wire limit and a **decoded** limit; decompression in 64 KiB slices, so a bomb never inflates beyond the limit; an oversized body is rejected, never truncated | 2 MiB decoded (at most 10 MiB) |
| Content type and coding | only `text/html` and `application/xhtml+xml`; only `gzip` and `deflate` are advertised and decoded | n/a |
| Redirects | followed by the fetcher, never by the HTTP stack; each hop validated; loops detected | 5 |
| URLs and links | maximum URL length; links kept per page; text kept per page | 2048 bytes; 1000 links; 1,000,000 characters |
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
  it is not RFC 9309-compliant. Phase 4 will choose a compliant parser: either a small, well-tested
  library or our own implementation with tests.
- Page-level `<meta name="robots">` and `X-Robots-Tag` are honored. `noindex` keeps the page out of
  search, and `nofollow` stops its links from being admitted. Since Phase 3, the parser reports the
  meta directives (`ParsedPage.robots`) and the fetcher keeps the `X-Robots-Tag` header; the crawl
  logic (Phase 4) acts on them.

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

**Pipeline.** Result commit → `search_documents` row as `pending` (in the same transaction) → relay
→ `sitescope:index` → indexer → vector written, row `indexed`
([ADR-0005](adr/0005-postgresql-full-text-search.md), [ADR-0008](adr/0008-search-documents-as-index-outbox.md)). The index
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

**Link analysis.** Computed in SQL over `page_links` joined to `crawl_urls` on
`(job_id, normalized_url)`:

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
| Fetching (Phase 3) | `fetch.destination_blocked` (WARNING: host and address classes, never the URL or the addresses), `fetch.completed` (DEBUG) |
| Indexing | `outbox.published`, `index.upserted` |

**Health**

- `GET /health` and `GET /api/v1/health/live` are liveness probes: the process is up and serving
  requests. They check no dependencies. Both were implemented in Phase 1.
- `GET /api/v1/health/ready` (Phase 2B) reports whether PostgreSQL and Redis are reachable.
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
  - target policy and crawl caps (implemented in Phase 2C)
  - concurrency
  - timing
  - retries
  - politeness: rate limit, and the user agent `SiteScopeBot/<version>`
  - API: token, CORS
- `backend/.env.example` lists every variable with a safe placeholder.
- A local `backend/.env` is optional and git-ignored. It may contain only known `SITESCOPE_`
  settings; any other key stops startup.

## 15. Deployment topology

Deployment uses Docker Compose. Since Phase 2B, `backend/compose.yaml` runs PostgreSQL 18.6 and Redis
8.10.3 for development: ports bound to 127.0.0.1, named volumes, health checks, `restart:
unless-stopped`. Phase 11 runs the full stack.

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
- Before Phase 11, the Python processes run on Windows from `backend/.venv` against the PostgreSQL and
  Redis containers started by Compose.

## 16. Repository structure (proposed)

```
SITESCOPE/
|-- CLAUDE.md                    # stable context and rules for contributors and Claude
|-- README.md                    # Phase 11: overview, quickstart, design highlights
|-- .gitignore, .gitattributes  # Phase 1: ignore rules, LF line endings
|-- docker-compose.yml           # Phase 11: full stack (reuses backend/compose.yaml)
|-- .github/workflows/ci.yml     # added once a GitHub remote exists; extended in later phases
|-- backend/
|   |-- .venv/                   # existing; never committed, never recreated
|   |-- .env.example             # Phase 1: every setting, with safe placeholders
|   |-- compose.yaml             # Phase 2B: PostgreSQL + Redis for development
|   |-- pyproject.toml           # dependencies + ruff / mypy / pytest config
|   |-- alembic.ini              # Phase 2B
|   |-- alembic/                 # Phase 2B: migrations
|   |-- Dockerfile               # Phase 11
|   |-- app/
|   |   |-- main.py              # FastAPI app factory, lifespan, routers, error handlers
|   |   |-- core/                # settings, logging, domain errors
|   |   |-- api/                 # routers (crawls since Phase 2C; search, links, system), schemas
|   |   |-- services/            # use cases that own transactions: jobs, frontier, indexing, search, links
|   |   |-- db/                  # SQLAlchemy models, engine and session factory
|   |   |-- queue/               # Redis Streams adapter, versioned message schemas
|   |   |-- scheduler/           # activator, dispatcher, level advancer, relay, reconciler, leader lock
|   |   |-- crawler/             # URL normalization, scope (Phase 2C); destination policy, resolver,
|   |   |                        #   safe fetcher, content limits, encodings, HTML parser (Phase 3);
|   |   |                        #   robots.txt, rate limiter (Phase 4); no database imports
|   |   |-- indexing/            # search document building
|   |   `-- workers/             # crawler and indexer entrypoints, shared consumer runtime
|   `-- tests/
|       |-- unit/
|       |-- integration/
|       `-- fault/
|-- frontend/                    # Phase 9 (currently empty)
|-- demo-site/                   # Phases 3 and 11: static site + robots.txt, the approved demo target
|-- benchmarks/                  # Phase 10: scripts, raw results, environment notes
`-- docs/
    |-- architecture.md
    |-- development-roadmap.md
    |-- api-conventions.md       # Phase 1
    |-- development.md           # Phase 1: Windows PowerShell commands
    `-- adr/
```

- The Python package is named `app`, following FastAPI convention and the planned `backend/app/main.py`.
  Every process role is an entrypoint inside it ([ADR-0007](adr/0007-modular-monolith-process-roles.md)).
- There is no generic repository layer. Query functions live with the service that owns the transaction,
  and `db/` holds the models and session management.
- Dependency direction:
  - `api` depends on `services`, which depends on `db` and on the pure functions of `crawler`
    (URL normalization and scope).
  - `core.config` validates the approved hosts with `crawler.scope` at startup.
  - `scheduler` and `workers` depend on `services`, `queue`, `crawler`, and `indexing`.
  - `crawler` and `queue` have no domain or database dependencies, which keeps them unit-testable.

## 17. Technology choices

| Concern | Choice | Reason |
|---|---|---|
| API | FastAPI + Uvicorn (already installed) | async, Pydantic validation, generated OpenAPI docs |
| Settings | pydantic-settings | typed environment configuration, using the same validation model as the API |
| Database | PostgreSQL 18.6 (`postgres:18.6-trixie`; 18+ needed for `uuidv7()`) | transactions, row locks, `SKIP LOCKED`, constraints, and full-text search with GIN, all in one system |
| ORM / migrations | SQLAlchemy 2.1 (async) + Alembic 1.20 | typed models; SQLAlchemy Core for set-based statements |
| PostgreSQL driver | asyncpg 0.32 | mature async driver; verified in Phase 2B on Windows with the default Proactor event loop (psycopg's async mode would need the selector loop) |
| Queue | Redis 8.10.3 Streams (`redis:8.10.3-alpine3.23`; 7.0+ required) | consumer groups, pending entries list, `XAUTOCLAIM`. Redis 8 is tri-licensed (AGPLv3 / RSALv2 / SSPLv1); Valkey is a BSD-licensed alternative if that matters later |
| Redis client | redis-py 8.1 (`redis.asyncio`) | the official client; its automatic retries are disabled so failures surface immediately (callers retry deliberately) |
| HTTP | `httpcore2` 2.16, the transport layer of `httpx2` (HTTP/1.1) | async streaming and granular timeouts, and a public network-backend hook for IP pinning, which the `httpx2` client lacks (ADR-0013); `httpx2` stays the TestClient's dependency |
| TLS verification | `truststore` 0.10 (already required by `httpcore2`) | the operating system's verifier, as browsers use; OpenSSL alone failed to verify public sites on the Windows development machine |
| URL hosts | `idna` 3.20 (already installed with AnyIO and httpx2) | IDNA 2008 / UTS #46; the standard library's codec implements IDNA 2003, which merges distinct host names |
| HTML parsing | BeautifulSoup 4.15 with the stdlib `html.parser` | tolerant and has no native dependency; lxml only if benchmarks justify it. It builds the whole tree, so input is capped at the body limit and parsing should run off the event loop |
| robots.txt | chosen in Phase 4 | must be RFC 9309-compliant (§12.5) |
| Logging | stdlib `logging` + a JSON formatter | no extra dependency |
| Tests | pytest with warnings treated as errors; Starlette's TestClient (needs `httpx2`); AnyIO's bundled pytest plugin for async tests; the HTTP client's mock transport for crawler tests | no extra plugin dependency: AnyIO is already installed with Starlette |
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
| Fetching (Phase 3) | the safe fetcher against real HTTP and TLS servers on 127.0.0.1: pinned connections (recorded), SNI and `Host` header, rebinding, redirects, TLS failures (a throwaway CA from `trustme`), limits, decompression bombs, malformed, truncated and slow responses | local loopback servers only |
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
  and the Redis clock (idle times and rate limits). This matters on the development machine: the
  Windows host and the WSL VM clocks were observed about 160 ms apart.
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
- HTTP/1.1 only. A response without a `Content-Type` is not sniffed: it is refused as
  unsupported. Only the first member of a multi-member gzip body is read.
- Text hidden only by CSS counts as visible text: the parser does not render pages.
- Charsets are limited to the WHATWG label table; any other declared charset is ignored.
- Basic relevance ranking (no BM25).
- No incremental re-crawl; storage is duplicated across jobs.
- Until Phase 6 adds `Idempotency-Key`, a client that retries job creation after a lost response
  creates a second job.

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
| [0008](adr/0008-search-documents-as-index-outbox.md) | `search_documents` is both the index store and the indexing outbox |
| [0009](adr/0009-dedup-on-normalized-url.md) | Deduplicate URLs on the normalized URL itself |
| [0010](adr/0010-database-enforced-fencing.md) | Fencing is enforced by the database, not only by worker code |
| [0011](adr/0011-fencing-tokens-issued-only-by-claims.md) | Fencing tokens are issued only by claims |
| [0012](adr/0012-approved-crawl-targets.md) | Approved crawl targets are host patterns, plus allowed schemes and ports |
| [0013](adr/0013-safe-fetcher-pinned-connections.md) | The safe fetcher pins connections through httpcore2 and validates every hop |
