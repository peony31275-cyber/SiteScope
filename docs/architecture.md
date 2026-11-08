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
> - Phase 4: level-synchronous BFS with its state in PostgreSQL (claims, fenced results,
>   same-level redirects, level advancement, exact budget; sections 8 and 9, ADR-0014), robots.txt
>   (RFC 9309) and per-host politeness shared through Redis (sections 12.4 and 12.5, ADR-0015),
>   page directives (`noindex`, `nofollow`; migration `0003`), and a single-process crawl driver.
> - Phase 5: distributed execution. A scheduler process and crawler worker processes share the
>   work through a Redis stream and consumer group: publication inside the dispatch transaction,
>   explicit acknowledgment rules, `XAUTOCLAIM` takeover, reconciliation of lost messages, safe
>   stream trimming, backpressure, and graceful shutdown (sections 3, 6, 9 and 10, ADR-0016).
> - Phase 6: durable crawl retries with backoff and `Retry-After`, dead-lettering of poison
>   messages, job cancellation (`POST /api/v1/crawls/{id}/cancel`), idempotent job creation
>   (`Idempotency-Key`), renewable politeness slot leases, and shorter recovery defaults
>   (sections 3, 7 to 10, ADR-0017).
> - Phase 7: asynchronous search indexing. An index dispatcher process publishes pending search
>   documents to their own stream; indexer processes count each attempt in PostgreSQL, build the
>   weighted vector inside PostgreSQL from the locked page, and never let an older index replace
>   a newer one (migration `0004`); job indexing completion; an internal, read-only search
>   service (sections 3, 4, 7, 9 to 11, 13, ADR-0018).
> - Phase 8: the search and website-analysis REST APIs: `GET /api/v1/search`, and read-only pages,
>   links (each classified), confirmed broken links, link graph and statistics of a job, computed
>   from its stored crawl results (sections 3, 4, 13, ADR-0019).
> - Phase 9: the React dashboard (`frontend/`): crawl jobs (create, follow live, cancel), search,
>   website analysis and the link graph, on the REST API from the dashboard's own origin, with
>   types generated from the OpenAPI document; two read-only endpoints for it,
>   `GET /api/v1/crawl-policy` and `GET /api/v1/overview` (sections 3, 12.6, 12.7, 15, 18,
>   ADR-0020).
>
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
  index dispatcher <-- pending search_documents rows ---------------------+
    | XADD {v, job_id, page_id, content_version}, mark rows `queued`      |
    v                                                                     |
  Redis stream sitescope:index    (consumer group: indexers)              |
    | XREADGROUP / XACK / XAUTOCLAIM                                      |
    v                                                                     |
  indexer x N --- count the attempt, build the vector in SQL ------------>+
```

The system has two planes:

- **State plane: PostgreSQL.** Holds everything that must survive a crash: jobs, the URL frontier and
  each URL's lifecycle, pages, links, outbox rows, and the search index.
- **Delivery plane: Redis Streams.** Carries messages that announce "this row needs work". A message is
  a pointer (`job_id` + `url_id`, or `job_id` + `page_id` + `content_version`), never the state
  itself, and the consumer re-reads PostgreSQL.

Every message published to Redis comes from a PostgreSQL row, and every effect of a message lands in
PostgreSQL through a guarded transaction. That is why Redis can be treated as disposable
([ADR-0001](adr/0001-postgresql-source-of-truth.md)).

## 3. Components and responsibilities

| Component | Process / entrypoint | Responsibilities | Scaling |
|---|---|---|---|
| Dashboard (Phase 9) | `frontend/`: static SPA (React, Vite); development: `npm run dev`; Phase 11: behind nginx | create and cancel jobs; follow crawl and indexing progress (bounded polling); search; website analysis (statistics, pages, links, broken links); link graph; deployment overview | static files, n/a |
| API | `api`: `uvicorn app.main:app` | input validation, job commands, read models; since Phase 8 search and read-only website analysis (pages, links, broken links, link graph, statistics) | stateless, N replicas |
| Scheduler (Phase 5) | `scheduler`: `python -m app.scheduler` | job activation, crawl dispatch, BFS level advancement, job completion, reconciliation, stream maintenance | 1..N, no leader (ADR-0016) |
| Index dispatcher (Phase 7) | `index-dispatcher`: `python -m app.scheduler.index_dispatcher` | publishes pending search documents, republishes lost index tasks, completes the indexing of finished jobs, maintains the index stream; starts no job | 1..N, no leader (ADR-0018) |
| Crawler worker (Phase 5) | `crawler-worker`: `python -m app.workers.crawler` | consume the crawl stream, claim URL leases, fetch safely, parse, commit results (fenced), ack, adopt abandoned messages | N processes, each with bounded concurrency |
| Local crawl driver (Phase 4) | `python -m app.workers.local_crawler <job-id>` | runs one job in one process: dispatch without a stream, claims, crawl tasks, fenced results, level advancement | development and tests only |
| Diagnostics (Phase 5) | `python -m app.scheduler.status` | read-only JSON report: jobs, URL states, live leases per worker, stream and consumer-group statistics; since Phase 7 search documents by status, index attempts per indexer and the index stream | on demand |
| Search command (Phase 7) | `python -m app.scheduler.search <job-id> "query"` | read-only search of one job's index, as JSON (the same service as `GET /api/v1/search`) | on demand |
| Indexer worker (Phase 7) | `indexer-worker`: `python -m app.workers.indexer` | consume the index stream, count each attempt in PostgreSQL, build the weighted `tsvector` inside PostgreSQL, ack after the commit | N processes, each with bounded concurrency |
| PostgreSQL | service | durable state, constraints, row locks, full-text index | single instance |
| Redis | service | streams and consumer groups, per-host rate-limit state, robots.txt cache | single instance, AOF on |
| Demo site | static web server | approved crawl target for the public demo and the end-to-end tests | n/a |

**API**

- Never contacts crawl targets and never publishes tasks.
- Creating a job is a single PostgreSQL transaction (job row + seed URL), so it works while Redis is
  down.
- Uses Redis only for queue statistics and request rate limiting.
- Enforces the target allowlist and the server-side caps on depth, pages, and running jobs.
- Accepts an `Idempotency-Key` header on job creation (Phase 6, ADR-0017): a retried request
  returns the job the first one created (`200`, `Idempotent-Replayed: true`), and a key reused
  for a different request is refused (`409 idempotency_key_reused`). PostgreSQL stores the keys.
- Cancels jobs (`POST /api/v1/crawls/{id}/cancel`, Phase 6, section 8.6) with one transaction.
- Follows [`api-conventions.md`](api-conventions.md): versioned routes under `/api/v1`, an
  `X-Request-ID` header on every response, and RFC 9457 problem+json errors.

**Scheduler** (Phase 5: `app/scheduler/scheduler.py`). One asyncio process. Every
`SITESCOPE_SCHEDULER_INTERVAL_SECONDS` it runs one pass, each step its own short transaction:

- *Activator*: moves the oldest jobs from `queued` to `running` while fewer than
  `SITESCOPE_SCHEDULER_MAX_RUNNING_JOBS` run. A transaction-level advisory lock makes the cap
  exact across schedulers.
- *Level advancer / finalizer*: when a running job has no unfinished URLs, admits the next BFS level from
  the recorded links, or completes the job (T6).
- *Cancellation finalizer* (Phase 6): for each `cancelling` job, cancels its `pending` and `queued`
  URLs and its in-progress URLs whose lease expired, and marks the job `cancelled` once none of its
  URLs is unfinished (T11, section 8.6).
- *Dispatcher*: publishes eligible frontier rows to the crawl stream and marks them `queued` (T3). A row
  is eligible when its state is `pending`, its depth equals the job's `current_level`, and
  `next_attempt_at <= now()`; a URL waiting for a retry becomes eligible when its backoff ends
  (Phase 6). Backpressure: a per-job in-flight window
  (`SITESCOPE_SCHEDULER_MAX_IN_FLIGHT_PER_JOB`), a cap on queued and in-progress URLs over all jobs
  (`SITESCOPE_SCHEDULER_MAX_OUTSTANDING`), and a batch size per job and pass. The free room is shared
  fairly among jobs, oldest first.
- *Reconciler*: every `reconcile_after / 4` (at most 30 s), publishes again the tasks without progress
  for `SITESCOPE_SCHEDULER_RECONCILE_AFTER_SECONDS` whose stream entry is gone (F7 in section 10.4).
  Abandoned leases and exhausted attempts are recovered by the workers, through `XAUTOCLAIM` and the
  rules of section 10.3.
- *Maintenance*: every `retention / 4` (at most 60 s), trims the stream (never an undelivered or pending
  entry), removes idle consumers that have nothing pending, forgets expired idempotency keys
  (Phase 6), and logs statistics.
- The index relay planned here runs as a process of its own since Phase 7: the index dispatcher
  (below, ADR-0018).

There is no leader (ADR-0016). Several schedulers may run at once: activation is serialized by the
advisory lock, level advancement by the job row lock, dispatch and reconciliation skip rows another
scheduler holds (`SKIP LOCKED`), and trimming is idempotent. Overlapping schedulers produce at most
duplicate messages; the backpressure limits become approximate.

A pass looks at every running job, not only as many as the cap (which limits activation only). A job
whose step fails for a reason of its own is logged (`scheduler.job_failed`) and skipped for that pass,
so one broken job cannot stop the others; a transient error (PostgreSQL or Redis unreachable) fails the
whole pass, which is retried with backoff.

**Crawler worker** (Phase 5: `app/workers/crawler.py`)

- Has a name unique per process start, `crawler-<host>-<pid>-<random>`: its consumer name, its lease
  owner, and a field of every log line of its tasks.
- Reads from the crawl stream with `XREADGROUP` (blocking up to `SITESCOPE_STREAM_READ_BLOCK_MS`),
  never requesting more messages than it has free task slots (`SITESCOPE_CRAWL_CONCURRENCY`).
- Handles each message as: claim (compare-and-set) → policy checks, robots.txt, per-host rate limit →
  fetch (outside any transaction) → parse (in a worker thread) → fenced result commit → `XACK`.
  Section 10.3 lists when it acknowledges a message.
- Runs `XAUTOCLAIM` periodically to adopt messages abandoned by crashed or stalled consumers. A
  reclaimed message delivered more than `SITESCOPE_STREAM_MAX_DELIVERIES` times whose URL can never be
  claimed again (queued, attempts used up) is poison: the URL is failed through a fenced quarantine,
  and the message is dead-lettered (Phase 6). Any other URL follows the usual rules: its attempts
  bound the work.
- Asks the retry policy (`app/workers/retry.py`, Phase 6) after a failed attempt: a retryable failure
  with attempts left goes back to `pending` with a backoff, through the same fenced update as a result.
- Checks every `SITESCOPE_CRAWL_CANCEL_CHECK_SECONDS` whether the jobs of its running tasks are being
  cancelled, stops those tasks, and records their URLs `cancelled` under its own token (Phase 6).
- After a PostgreSQL or Redis error, leaves the message pending and pauses reading (exponential backoff
  with full jitter), so an outage does not churn through the stream. An error before the claim is first
  handled again in the same process a few times (Phase 6).
- On SIGINT or SIGTERM (Windows: Ctrl+C or `CTRL_BREAK_EVENT`), stops reading, lets running tasks finish
  within `SITESCOPE_SHUTDOWN_GRACE_SECONDS`, cancels the rest, and exits. A cancelled task keeps its
  lease and its pending message, and other workers recover it.

**Index dispatcher** (Phase 7: `app/scheduler/index_dispatcher.py`, ADR-0018). One asyncio
process with the scheduler's pass loop. Every `SITESCOPE_INDEX_DISPATCHER_INTERVAL_SECONDS`:

- *Dispatch* (I1): locks `pending` documents (`FOR UPDATE SKIP LOCKED`, oldest change first),
  publishes one `{v, job_id, page_id, content_version}` message each, and marks them `queued` with
  their entry IDs, in one transaction. At most `SITESCOPE_INDEX_DISPATCH_BATCH_SIZE` per pass and
  `SITESCOPE_INDEX_MAX_OUTSTANDING` queued in all.
- *Complete* (I5): marks the indexing of finished jobs complete (`indexing_completed_at`) once none
  of their documents is `pending` or `queued`.
- *Reconcile*, every `reconcile_after / 4` (at most 30 s): publishes again the `queued` documents
  without a live attempt whose stream entry is gone.
- *Maintain*, every `retention / 4` (at most 60 s): trims the index stream, removes idle indexer
  consumers, and logs `index_dispatcher.stats` from PostgreSQL and Redis.

It never starts a job or touches crawl state, so it is safe to run where the scheduler must not.

**Indexer worker** (Phase 7: `app/workers/indexer.py`, ADR-0018)

- Has a name unique per process start (`indexer-<host>-<pid>-<random>`): its consumer name, the
  owner of its leases, and the `worker_id` of its log lines and events.
- Shares the crawler workers' consumer loop (`app/workers/consumer.py`): bounded reads,
  `XAUTOCLAIM`, settling, error pauses, graceful shutdown.
- Handles each message in two transactions: it *starts an attempt* (I2: counts it in PostgreSQL
  and takes the document's lease), then *indexes* (I3: one statement locks the page row, builds the
  vector `title A || description B || headings C || body D` from it, and stores it with the page's
  content version, only while the document is `queued` and its index is not newer). It
  acknowledges after the commit. Section 10.8 lists every disposition.
- Never reads page text into the process: the vector is built in PostgreSQL. Each task holds one
  database connection at a time; `SITESCOPE_INDEX_CONCURRENCY` tasks run at once.

### 3.1 Dashboard (Phase 9)

`frontend/` ([ADR-0020](adr/0020-dashboard-same-origin-typed-client.md); its README has the
details) is a single-page React application. It holds no state of its own beyond the viewer's
preferences: every number comes from the API, and the address holds the view (`?job=`, filters,
tabs, search terms), so a view can be shared.

- **API access.** The browser calls `/api/v1` on the dashboard's own origin; the Vite server
  (development, preview) or nginx (deployment) proxies it to the API, so no CORS is needed and no
  address comes from user input. `lib/api/client.ts` builds URLs with `URLSearchParams`, applies
  timeouts and cancellation, and turns RFC 9457 problems into typed errors.
- **Types** are generated from the OpenAPI document (`app/api/openapi_export.py` writes the
  snapshot, `openapi-typescript` generates `schema.d.ts`); a backend test fails while the
  snapshot is stale.
- **Server state** lives in TanStack Query: one request per key at a time, newer requests cancel
  older ones, transient failures retried twice with backoff. Polling follows the job lifecycle
  (section 7.1 and 7.3): the viewer's interval while the crawl is active, slower while only its
  indexing runs, none once both are over or the tab is hidden; a job whose reads show no change
  for several minutes' worth of refreshes pauses its polling and says which process is probably
  not running (reads are counted, so time in a hidden tab never counts).
- **Two read-only endpoints** were added for it: `GET /api/v1/crawl-policy` (the approved
  targets and job limits that job creation enforces) and `GET /api/v1/overview` (every job,
  admitted URL, stored page and search document counted in one read-only snapshot, under the
  analysis time limit). The job listing still has no totals; each listed row reads its job's
  detail.

## 4. End-to-end flow

1. **Create** (implemented in Phase 2C). `POST /api/v1/crawls` normalizes the seed URL and checks it
   against the approved targets and the server's caps, without any network access. One transaction
   inserts the job (`queued`, `current_level = 0`, `admitted_count = 1`) and the seed URL
   (`pending`, depth 0). Nothing is published to Redis.
2. **Activate** (Phase 5). The scheduler finds `queued` jobs in PostgreSQL (index `ix_crawl_jobs_active`) and
   sets each to `running` with `current_level = 0` once the running-jobs cap allows it.
3. **Dispatch** (Phase 5). The dispatcher locks eligible rows (`FOR UPDATE SKIP LOCKED`) and
   `XADD`s one `{v, job_id, url_id}` message per row. It then marks the rows `queued` with their entry
   IDs and commits.
4. **Claim.** A worker receives the message and claims the URL with a compare-and-set:
   - the state becomes `in_progress`;
   - `lease_epoch` and `attempt_count` are incremented;
   - `lease_expires_at` is set from the database clock.

   The claim commits immediately. It first locks the row, which waits for a dispatch of that row still in
   flight (ADR-0016).
5. **Fetch.** Outside any transaction: check robots.txt, wait for a per-host rate-limit slot, resolve DNS
   and validate the IP, then send the HTTP request pinned to that IP. The body is streamed under size and
   time limits.
6. **Commit.** One transaction does all of this (T5 in section 9, implemented in Phase 4):
   - the fenced update of the URL row comes first: it checks the lease and records the outcome.
     If it matches no row, the transaction rolls back and nothing is written;
   - stores the page, if the response was an HTML page;
   - records every discovered link with its position, a scope flag and its `nofollow` flag;
   - inserts the page's `search_documents` row as `pending` (the indexing outbox), unless the
     page is `noindex`;
   - for a redirect, admits its target at the same depth (section 8.5).

   A retryable failure with attempts left returns the URL to `pending` instead, with a
   `next_attempt_at` from the retry policy (Phase 6, section 10.5): the same fenced update, and a
   `url.retry_scheduled` event. Then the worker sends `XACK`, never before the commit.
7. **Advance.** When none of the job's URLs is unfinished, the scheduler takes the job row lock. It
   admits the next level's URLs from links recorded by the current level, in deterministic order, up to
   the remaining page budget. If nothing can be admitted, it marks the job `completed`, or `failed` if
   the seed itself was never fetched.
8. **Publish** (Phase 7). The index dispatcher locks `pending` `search_documents` rows, `XADD`s
   one `{v, job_id, page_id, content_version}` message per row to `sitescope:index`, marks the rows
   `queued` with their entry IDs, and commits.
9. **Index** (Phase 7). An indexer receives the message, starts an attempt (counted in
   PostgreSQL, with a lease), then builds the weighted vector inside PostgreSQL from the locked
   page and stores it with the page's content version; the document becomes `indexed`. Then it
   sends `XACK`. Once a finished job has no `pending` or `queued` document, the dispatcher sets
   its `indexing_completed_at`.
10. **Query.** The search service returns a job's indexed pages whose index matches their current
   content, ranked, with snippets (Phase 7: the service and a read-only command; Phase 8:
   `GET /api/v1/search`). The analysis endpoints (Phase 8) report the job's pages, its classified
   links, its confirmed broken links, its link graph and its statistics from the stored rows.

## 5. Data model (implemented in Phase 2B)

The schema is created by migration `0001` (`backend/alembic/versions/0001_initial_schema.py`).
Migration `0002` adds a fencing trigger (Phase 2C, ADR-0011), `0003` adds the robots directive flags
of pages and links (Phase 4, ADR-0014), and `0004` the index pipeline's version triggers, index
leases and a partial index (Phase 7, ADR-0018). The ORM models in `backend/app/db/models.py` mirror
it, and an integration test compares the two (`alembic.autogenerate.compare_metadata` must report no
differences). Six tables, nothing more.

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
| `byte_size`, `fetch_duration_ms`, `fetched_at` | fetch details (`fetched_at` from the database clock) |
| `robots_noindex`, `robots_nofollow` | page directives (meta robots, `X-Robots-Tag`; `0003`): a `noindex` page has no `search_documents` row; the links of a `nofollow` page are never admitted |
| timestamps | `created_at`, `updated_at` |

**`page_links`**

| Column | Notes |
|---|---|
| `job_id` | |
| `anchor_text` | first occurrence, ≤ 500 characters |
| `position` | order in the document |
| `is_internal` | whether the target is in the job's scope (the seed's host) and an approved target |
| `nofollow` | `rel="nofollow"` on this link: recorded, never admitted (`0003`) |

The uniqueness policy is one row per (source page, target URL). Repeated links on the same page
collapse to the first occurrence.

**`search_documents`**

| Column | Notes |
|---|---|
| `job_id` | |
| `status` | `pending`, `queued`, `indexed`, `failed` |
| `indexed_content_version` | the content version the vector reflects; never moves back (`0004`) |
| `search_vector tsvector` | GIN-indexed |
| `attempt_count`, `last_error` | indexing attempts since the row was last set `pending`, counted before each attempt's work (Phase 7); the last error, a stable token |
| `dispatched_at`, `stream_entry_id` | publication state |
| `indexed_at` | |
| `lease_owner`, `lease_expires_at` | the indexer running the current attempt, until when (database clock; `0004`). Advisory, not a fencing token (ADR-0018) |

The page's `content_version` versus `indexed_content_version` tells whether a page needs
(re)indexing, and search shows a document only when the two are equal. Setting `status` back to
`pending` re-queues it. `crawl_jobs.indexing_completed_at` is set by the index dispatcher once the
job is finished and none of its documents is `pending` or `queued` (Phase 7).

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
| **Search state** | `indexed` documents have a vector, an indexed version and an `indexed_at`. An index lease (owner and expiry together) exists only while a document is `queued` (`0004`). Triggers keep `indexed_content_version` and `pages.content_version` from moving back (`0004`, ADR-0018). |
| **Audit log** | `crawl_events` rejects `UPDATE` (trigger). `event_type` is dotted lowercase, and `detail` is a JSON object. |
| **Bookkeeping** | A trigger maintains `updated_at`. Bounded text lengths (`last_error`, titles, extracted text). |
| **Referential cleanup** | Deleting a job cascades to all of its URLs, pages, links, search documents and events. |

The partial indexes serve the planned access paths:

| Index | Serves |
|---|---|
| `ix_crawl_urls_dispatch (job_id, depth, next_attempt_at) WHERE state = 'pending'` | the dispatcher: pending URLs at the current BFS level |
| `ix_crawl_urls_unfinished (job_id) WHERE state IN (pending, queued, in_progress)` | level advancement and job completion |
| `ix_crawl_urls_lease_expiry`, `ix_crawl_urls_queued_since` | recovery: expired leases, stale publications |
| `ix_search_documents_pending`, `ix_search_documents_queued_since` | the index dispatcher and its reconciliation |
| `ix_crawl_jobs_indexing_open (finished_at) WHERE finished_at IS NOT NULL AND indexing_completed_at IS NULL` | job indexing completion (`0004`) |
| `ix_search_documents_search_vector` (GIN) | full-text search |
| `ix_page_links_job_id_target (job_id, target_normalized_url) INCLUDE (is_internal)` | link analysis: links to a URL, and index-only counts of a job's internal links (`0005`) |
| `ix_pages_job_id_normalized_url (job_id, normalized_url)` | a job's pages; the links listing's order, by the linking page's URL (`0005`, replacing `ix_pages_job_id`) |
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
| Commit succeeded, Redis later lost the entry (restart, trim) | row `queued`, no message | `dispatched_at` and `stream_entry_id` let the reconciler find the task, check its entry, and re-publish it (Phase 5) |
| Worker committed, but its `XACK` was lost | message redelivered | URL is terminal, so the claim matches no row and the worker only acks |
| Worker crashed while holding a lease | `in_progress` with an expired lease | `ix_crawl_urls_lease_expiry`; a takeover claim increments `lease_epoch` |
| Stale worker commits after a takeover | rejected | the `result_epoch` CHECK rolls the transaction back |
| Index message delivered twice | one attempt at a time; the duplicate finds a live lease or the document indexed | the advisory lease, the `status` and version guards; rebuilding a vector from the same content is idempotent (ADR-0018) |

## 6. Redis layout

| Key | Type | Purpose |
|---|---|---|
| `sitescope:crawl` | stream; group `crawlers` | crawl tasks `{v, job_id, url_id}` |
| `sitescope:index` | stream; group `indexers` | index tasks `{v, job_id, page_id, content_version}` (Phase 7) |
| `sitescope:crawl:dlq`, `sitescope:index:dlq` | streams | dead letters, kept for inspection and capped near 10,000 entries: the original entry ID, the reason and kind, and the contract fields (Phase 5: malformed messages, and messages naming an unknown URL or another job; Phase 6: poison messages, delivered more than `SITESCOPE_STREAM_MAX_DELIVERIES` times, with the delivery count and the consumer; Phase 7: index tasks naming an unknown page, another job's page, or a content version the page never had). Other fields are listed by name only, never copied |
| `sitescope:rate:{<host>}:last` | string with TTL | the start of the host's latest request, in microseconds of Redis time (Phase 4) |
| `sitescope:rate:{<host>}:active` | sorted set with TTL | the host's politeness slots in use: slot token -> expiry (Phase 4). Since Phase 6 each slot is a lease that its holder renews while its request runs |
| `sitescope:rate:{<host>}:queue` | sorted set with TTL | requests waiting for a slot, in arrival order: slot token -> first attempt (Phase 4) |
| `sitescope:rate:{<host>}:seen` | sorted set with TTL | when each waiter last asked, to drop waiters of crashed processes (Phase 4) |
| `sitescope:rate:{<host>}:delay` | string with TTL | the host's crawl delay: the largest robots.txt `Crawl-delay` of its origins (Phase 4) |
| `sitescope:robots:<crawler name>:<origin>` | string with TTL | a cached robots.txt result, not authoritative: status, the crawler's rules, crawl delay (Phase 4) |

- Messages are small and versioned (`v`). Consumers re-read state from PostgreSQL.
- Consumer groups are created idempotently at startup with `XGROUP CREATE <stream> <group> 0 MKSTREAM`,
  ignoring `BUSYGROUP` errors.
- Dead-lettering is one Lua script: it acknowledges the entry and copies it to the dead-letter stream
  only if that acknowledgment removed it from the pending list, so an entry is copied at most once
  (Phase 6).
- Each worker process uses a unique consumer name. Maintenance removes idle consumers that have no pending
  entries, in one Lua script that checks and deletes atomically (`XGROUP DELCONSUMER` would drop the
  pending entries of a consumer that resumed in between).
- Replies are decoded leniently: bytes that are not UTF-8 become U+FFFD. Strict decoding failed a whole
  `XREADGROUP` or `XAUTOCLAIM` batch on one such entry, leaving the batch's valid tasks pending forever
  (Phase 5 review); now the entry is malformed and dead-lettered.
- Streams are trimmed by age with `XTRIM ... MINID ~ ...` (Phase 5). The trim point is capped by the
  oldest pending entry and by each group's last-delivered ID, so trimming never deletes an entry that
  is undelivered or pending. Entries lost in other ways (Redis restarted without its data) are
  recovered by the reconciler, because PostgreSQL still records the task as outstanding.
- Redis 7.0 or later is required. `XAUTOCLAIM` arrived in 6.2, and 7.0 added reporting of entries that
  were deleted from the stream.
- The key prefix is configurable (`SITESCOPE_REDIS_KEY_PREFIX`), so test runs can be isolated:
  each test uses its own prefix in Redis database 15. The `{<host>}` hash tag keeps one host's
  rate keys in one Redis Cluster slot, as the Lua script requires.

## 7. Lifecycle state machines

### 7.1 Job

| From | To | Trigger (actor) | Guard / effect |
|---|---|---|---|
| (none) | `queued` | create (API) | valid, allowlisted seed within caps; seed URL inserted in the same transaction |
| `queued` | `running` | activation (scheduler) | running jobs below the cap |
| `queued` | `cancelled` | cancel (API) | seed URL is cancelled |
| `running` | `cancelling` | cancel (API) | the same transaction cancels every `pending` and `queued` URL; if no URL is in progress, the job continues to `cancelled` in that transaction |
| `running` | `completed` | level advancer | no unfinished URLs, no further level to admit, seed was fetched |
| `running` | `failed` | level advancer | no unfinished URLs and the seed was never fetched (e.g. disallowed or unreachable) |
| `cancelling` | `cancelled` | finalizer (scheduler, or the single-process driver) | no URL is unfinished; the finalizer first cancels `pending` and `queued` URLs again (a retry scheduled meanwhile) and in-progress URLs whose lease expired |

Terminal states (`completed`, `failed`, `cancelled`) are absorbing. After a job becomes terminal, its URLs,
pages, and links never change. Indexing may still be catching up; its progress is reported separately
(section 7.3): `indexing_completed_at` is set once the job is terminal and none of its documents is
`pending` or `queued`, and indexing never changes a job's status (ADR-0018).

Phase 4 implements activation, level advancement and completion (`backend/app/services/
crawl_state.py`); until the Phase 5 scheduler exists, the single-process driver calls them.
Phase 6 implements cancellation (`backend/app/services/crawl_cancellation.py`, section 8.6).

### 7.2 Crawl URL

| From | To | Trigger (actor) | Guard |
|---|---|---|---|
| (none) | `pending` | admission: seed (API), next level (scheduler), or redirect target (worker) | unique per job, depth ≤ `max_depth`, budget remaining, job active |
| `pending` | `queued` | dispatch (scheduler) | job `running`, depth = `current_level`, `next_attempt_at <= now()`, within the in-flight window |
| `queued` | `in_progress` | claim (worker) | job `running`, `attempt_count < max_attempts`; `lease_epoch` advances by exactly one (ADR-0011) |
| `in_progress` | `in_progress` | takeover claim (worker) | lease expired by the database clock; `lease_epoch` is incremented |
| `in_progress` | `fetched` / `skipped` / `failed` | result commit (worker) | fencing check passes; the database requires `result_epoch = lease_epoch` (ADR-0010) |
| `in_progress` | `pending` | retryable failure with attempts left (worker; Phase 6) | fencing check passes; `next_attempt_at = now() + backoff` (database clock); `attempt_count` unchanged: the next claim counts the next attempt |
| `in_progress` | `failed` | attempts exhausted after crashes (worker) | lease expired by the database clock; a recovery claim issues a new token, which records `max_attempts_exceeded` (ADR-0014) |
| `queued` | `failed` | poison message (worker; Phase 6) | delivered more than `SITESCOPE_STREAM_MAX_DELIVERIES` times, and no claim can take the URL (its attempts are used up); a quarantine claim issues a new token, which records `max_deliveries_exceeded` |
| `pending`, `queued` | `cancelled` | job cancel (API), or the cancellation finalizer (scheduler) | job `cancelling` or `cancelled` |
| `in_progress` | `cancelled` | the lease holder (worker; Phase 6) | job `cancelling`; fenced update with the worker's own token (`job_cancelled`) |
| `in_progress` | `cancelled` | cancellation finalizer (scheduler) | job `cancelling`, and the lease has expired |

Terminal outcomes:

- `fetched`: an HTTP response was received and recorded, whatever its status code. The `outcome` column
  says more: `html_stored`, `non_html`, `too_large`, `redirect`, and so on.
- `skipped`: no request was sent because of a policy decision (`robots_disallowed`,
  `robots_unreachable`, `target_not_allowed`, `blocked_destination`).
- `failed`: no usable response after bounded retries (`read_timeout`, `connection_error`,
  `dns_error`, `rate_limited`, `infrastructure_error`, `parse_error`, `max_attempts_exceeded`,
  `max_deliveries_exceeded`, ...). Since Phase 6 a retryable failure is final only on the last
  allowed attempt (section 10.5); before, it was final at once.

- `cancelled` (outcome `job_cancelled`): the job was cancelled before this URL was finished.

The full mapping from fetch results to outcomes is in `backend/app/workers/crawl_task.py`.

### 7.3 Search document and job indexing (Phase 7)

| From | To | Trigger (actor) | Guard / effect |
|---|---|---|---|
| (none) | `pending` | result commit (crawler worker, T5) | the page is not `noindex` |
| `pending` | `queued` | dispatch (index dispatcher, I1) | `dispatched_at`, `stream_entry_id` set in the same transaction as the `XADD` |
| `queued` | `queued` | attempt start (indexer, I2) | the message's content version is the page's, attempts left, no live lease; `attempt_count + 1`, lease taken |
| `queued` | `indexed` | index (indexer, I3) | page row locked; the indexed version is not newer; vector, version and `indexed_at` written together |
| `queued` | `failed` | indexer (I4) | a deterministic failure (`tsvector_too_large`), or attempts used up with no live attempt (`max_attempts_exceeded`); a `page.index_failed` event |
| `indexed`, `failed` | `pending` | reindex (I6) | under the job row lock; attempts reset; `indexing_completed_at` cleared |
| any | `pending` | content change (ADR-0008; no producer yet: there is no re-crawl) | the same transaction increments `pages.content_version` |

A job's indexing status is derived, never stored: `pending` (nothing published or indexed),
`running`, `indexed` (`indexing_completed_at` set, no failure) or `failed` (set, at least one
document failed). The job detail of the API reports it with the counts per status.

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
(Phase 4): a link is internal, and can be admitted, only if its host is the seed's host and it is
an approved target; the same rule decides whether a redirect target is admitted. Out-of-scope
links are recorded (`is_internal = false`) but never fetched.

These checks are syntactic, with no DNS lookups. They are **not** a complete SSRF defense: an
approved name can resolve to any address, and DNS answers can change. Before every request, the
safe fetcher (section 12.3) must validate the resolved address and pin the connection to it.

### 8.3 BFS depth correctness (implemented in Phase 4)

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
1-worker crawl with an N-worker crawl. Phase 4 compares 1 and 6 concurrent tasks of the
single-process driver; Phase 5 repeats it across processes.

### 8.4 Page budget (`max_pages`, implemented in Phase 4)

- `max_pages` bounds the number of URLs admitted to a job, seed included. Each admitted URL produces at
  most one result, so a job stores at most `max_pages` pages.
- URLs are admitted in only three places: job creation, level advancement, and same-level redirect
  admission. Each of these runs while holding the job row lock and increments `admitted_count` in the
  same transaction. `CHECK (admitted_count <= max_pages)` is the backstop.
- Level advancement admits candidates in a deterministic order: by source URL, then by link position. If
  the budget runs out partway through a level, the admitted set is a deterministic prefix of BFS order.
- URLs that robots.txt disallows still consume budget: they are admitted first and then `skipped`.
- Tests: 1 and 4 concurrent tasks admit the same prefix, and eight concurrent redirect
  admissions competing for the last slot admit exactly one URL.

### 8.5 Redirects

The HTTP stack never follows redirects. The safe fetcher (Phase 3) supports two modes:

- **Follow** (the default): up to `SITESCOPE_FETCH_MAX_REDIRECTS` hops (5). Every target is
  normalized and goes through the crawl scope, DNS resolution, address validation and pinning
  again. A loop or a longer chain is a failure. The result records each hop.
- **Return** (`max_redirects=0`): the 3xx response is the result, with its normalized
  `redirect_location`. Nothing connects to the target.

Phase 4 crawls pages in the "return" mode ([ADR-0014](adr/0014-crawl-state-transactions-and-redirects.md)):

- A 3xx response is recorded on the URL as `fetched`, with
  `outcome = redirect` and the `redirect_target`.
- The worker's result transaction admits the target, at the **same depth**, while holding the job row
  lock (taken after the URL row), provided that:
  - the normalized target is in the job's scope (the seed's host, and an approved target);
  - the redirect chain is within the hop limit: the redirecting URL's `redirect_hops` is below
    `SITESCOPE_FETCH_MAX_REDIRECTS`, and the target gets `redirect_hops + 1`;
  - budget remains, and the job is still running.

  A redirect is not a link hop, and the target finishes within the current level.
- The target then goes through the whole pipeline (scope, robots.txt, DNS and IP validation, rate limit),
  so a redirect cannot bypass any check.
- robots.txt follows redirects inline, up to five hops, with the fetcher's "follow" mode
  (section 12.5).

### 8.6 Cancellation (implemented in Phase 6)

`POST /api/v1/crawls/{id}/cancel` runs one transaction (T7,
`backend/app/services/crawl_cancellation.py`, ADR-0017). It takes the job row lock, moves the job from
`queued` to `cancelled`, or from `running` to `cancelling`, and cancels every `pending` and `queued`
URL (outcome `job_cancelled`). If none of the job's URLs is in progress, the job becomes `cancelled`
in the same transaction. The response is `202` while the job is `cancelling`, otherwise `200`. After
that:

- **Nothing new starts.** Dispatch, claims, reconciliation, redirect admission and level advancement
  all require a `running` job. A message already in the stream, or a stale or duplicate one, meets a
  refused claim and is acknowledged; it cannot revive the job.
- **In-flight tasks** are stopped by their workers, which check for cancelled jobs every
  `SITESCOPE_CRAWL_CANCEL_CHECK_SECONDS` and record the URL `cancelled` with their own token (a
  fenced update that also requires the job to be `cancelling`). A task that is already committing
  finishes, and its result is kept (it admits nothing new).
- **The finalizer** (every scheduler pass, T11) cancels `pending` and `queued` URLs again (a retry may
  have been scheduled meanwhile), cancels in-progress URLs whose lease expired (their worker is gone;
  if it was only frozen, its late result is fenced out), and marks the job `cancelled` once none of
  its URLs is unfinished. A job is never `cancelled` while a URL could still change. Cancellation
  normally completes within one check interval plus one scheduler pass; a URL of a crashed worker
  delays it by up to one lease duration.
- **Idempotent.** Cancelling again, or cancelling a terminal job, changes nothing and returns the
  current state.
- **Redis is not touched.** Stream entries of a cancelled job are acknowledged as no-ops when they
  arrive, and retention trims them.

### 8.7 Terminal completion (implemented in Phase 4)

A running job completes when no URL is `pending`, `queued`, or `in_progress` and no further level can be
admitted, because the depth limit was reached, the budget ran out, or there are no new in-scope links.

The check and the state change happen in one transaction that holds the job row lock. A worker commits
its URL's terminal state together with any redirect admission, in a single transaction. So no snapshot
can show "nothing in progress" while new work is about to appear.

## 9. Transactions and concurrency control

| | Transaction | Steps | Notes |
|---|---|---|---|
| T1 | Create job (Phase 2C) | (an expired key is cleared) → insert job `ON CONFLICT (idempotency_key) DO NOTHING` → insert seed URL, or load the job that holds the key | one transaction, no Redis. Phase 6: repeating an `Idempotency-Key` with an equivalent request returns the existing job, a different request returns `409`; concurrent requests serialize on the unique index |
| T2 | Activate | compare-and-set `queued -> running`; the scheduler's batch version holds a transaction-level advisory lock, so the running-jobs cap is exact | Phase 4; batch in Phase 5 |
| T3 | Dispatch | lock eligible rows `FOR UPDATE SKIP LOCKED` → `XADD` each → set `queued` and `stream_entry_id` → commit | a crash between `XADD` and commit causes a duplicate message, never a lost task (Phase 5). The single-process driver marks rows `queued` without `XADD` and hands the IDs to in-process tasks |
| T4 | Claim | lock the URL row (it waits for a dispatch of the row in flight) → compare-and-set update (sketch below) | committed before any network I/O; a takeover is logged as a `url.recovered` event (Phase 4; the row lock since Phase 5) |
| T5 | Result | fenced update of the URL (outcome, `result_epoch = :token`) → page → links → `search_documents` row (`pending`, not for `noindex`) → (for a redirect: lock job, admit target) → commit → `XACK`. Or, for a retryable failure with attempts left (Phase 6): the same fenced update back to `pending` with `next_attempt_at`, and a `url.retry_scheduled` event | all or nothing (Phase 4, ADR-0014); zero rows from the fenced update: roll back, discard, do not `XACK`; a stale `result_epoch` makes PostgreSQL reject the whole transaction |
| T6 | Advance | lock job → verify no unfinished URLs → admit next level or set terminal | a single writer per job (Phase 4) |
| T7 | Cancel (Phase 6) | lock job (`FOR NO KEY UPDATE`) → set status → cancel `pending` and `queued` URLs → if no URL is in progress, `cancelling -> cancelled` | never locks an `in_progress` URL; reading them takes no lock. `FOR NO KEY UPDATE` lets foreign-key checks of other transactions through (see lock order) |
| T8 (I1) | Index dispatch (Phase 7) | lock `pending` `search_documents` rows `FOR UPDATE SKIP LOCKED` (a `MATERIALIZED` selection is not needed: no `UPDATE` follows in the same statement) → `XADD` → set `queued`, `dispatched_at`, `stream_entry_id` → commit | same publish-then-mark pattern as T3 (ADR-0018) |
| T9 (I2, I3) | Index (Phase 7) | (I2) lock the document row (it waits for a dispatch in flight) → `attempt_count + 1` and the lease, if `queued`, the message's version is not newer than the page's, attempts are left and no live lease → commit; (I3) one statement, under what remains of the attempt's deadline (`statement_timeout`): lock the page row `FOR SHARE`, build the weighted vector from it, write vector, version and `indexed` while the row is `queued` and its index is not newer → commit → `XACK` | two transactions, so that a crashed attempt is counted; an older index can never replace a newer one (guard and triggers); the attempt ends before its lease |
| T10 | Reconcile | lock stale `queued` and long-expired `in_progress` rows `FOR UPDATE SKIP LOCKED` → check their stream entries → `XADD` the lost ones → set `dispatched_at`, `stream_entry_id` | Phase 5: re-publishes only; exhausted URLs are failed by the workers (recovery claim) |
| T11 | Settle cancellation (Phase 6) | (1) cancel `pending` and `queued` URLs; (2) cancel `in_progress` URLs whose lease expired (`SKIP LOCKED`); (3) lock job → `cancelling -> cancelled` if no URL is unfinished | three short transactions per `cancelling` job and pass; (2) holds no job lock |
| T12 | Quarantine (Phase 6) | lock the URL row → quarantine claim (new token, no attempt counted; a `queued` URL of a running job whose attempts are used up) → fenced failure `max_deliveries_exceeded` → `url.dead_lettered` event → commit → dead-letter the message | a URL with attempts left, or with a lease, is never quarantined |
| T13 | Cancel a claimed URL (Phase 6) | fenced update `in_progress -> cancelled` with the worker's token, if the job is `cancelling` → commit → `XACK` | zero rows: the lease was lost; nothing is written and the message is kept |
| T14 (I4) | Fail a document (Phase 7) | `queued -> failed` with `last_error` → `page.index_failed` event → commit | after a deterministic error, or once attempts are used up and no lease is live |
| T15 (I5) | Complete job indexing (Phase 7) | lock the finished job row (`FOR NO KEY UPDATE`) → in a fresh statement, check that no document is `pending` or `queued` → set `indexing_completed_at` → `job.indexing_completed` event | a finished job gets no new pages, so the check cannot be overtaken |
| T16 (I6) | Reindex (Phase 7) | lock the job row (`FOR NO KEY UPDATE`) → `indexed`, `failed` -> `pending` (attempts reset) → clear `indexing_completed_at` → `job.reindex_requested` event | serialized with T15 by the job row lock |
| T17 | Index reconciliation (Phase 7) | lock stale `queued` documents without a live lease `FOR UPDATE SKIP LOCKED` → check their entries → `XADD` the lost ones with the page's current version → set `dispatched_at`, `stream_entry_id` | as T10 |

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

Fenced result commit (as implemented in Phase 4, `record_result`; ADR-0014):

```sql
BEGIN;
UPDATE crawl_urls
SET    state = 'fetched', http_status = :status, outcome = :outcome,
       result_epoch = :token,  -- the worker's own token; CHECK result_epoch = lease_epoch
       finished_at = clock_timestamp(), lease_owner = NULL, lease_expires_at = NULL
WHERE  id = :url_id AND state = 'in_progress' AND lease_epoch = :token
RETURNING normalized_url, depth, redirect_hops;
-- no row: fenced out -> ROLLBACK, discard the result, do NOT XACK
INSERT INTO pages ... RETURNING id;   -- a second page would break an invariant: it rolls back
INSERT INTO page_links ...;
INSERT INTO search_documents ...;     -- status 'pending'; not for a noindex page
-- a redirect in scope: SELECT ... FROM crawl_jobs WHERE id = :job FOR UPDATE, then
-- INSERT INTO crawl_urls ... ON CONFLICT (job_id, normalized_url) DO NOTHING, admitted_count + 1
COMMIT;       -- then XACK
```

The fenced update comes first, so a lost lease writes nothing at all. `result_epoch` is always
bound to the token the claim returned; `SET result_epoch = lease_epoch` would satisfy every
database rule and defeat fencing (ADR-0011). A URL whose lease expired after its last allowed
attempt is failed by a *recovery claim* (a new token, no attempt counted) followed by the same
fenced update.

Rules:

- **Isolation.** Transactions run at READ COMMITTED, the PostgreSQL default, combined with explicit row
  locks, unique constraints, and compare-and-set updates. SERIALIZABLE is not used. Every invariant is
  already protected by a constraint or a lock, and avoiding serialization-failure retries keeps the
  worker code simple.
- **Lock order.** A worker locks its own URL row first, then the job row, and the latter only when it
  admits a redirect target. Cancellation and level advancement lock the job row first, but afterwards
  touch only rows that no worker holds: `pending` and `queued` URLs, and newly inserted rows. There is no
  cycle, so there is no deadlock. Any new transaction must also lock a URL row before its job row, or
  show that it touches disjoint rows. Foreign-key checks count too: inserting a row that references a
  job (an event, a page) takes `FOR KEY SHARE` on the job row. A quarantine (T12) holds a `queued` URL
  row while its event insert does so, which is why cancellation (T7) locks the job row `FOR NO KEY
  UPDATE`: with `FOR UPDATE`, the two deadlocked (Phase 6 review; regression test in
  `integration/test_cancellation.py`). The index transactions (Phase 7) lock a page row before its
  `search_documents` row (T9, and any content change), and a job row before its documents (T16);
  T8, T14, T17 and the attempt start lock document rows only, and T15 only the job row. Their
  event inserts take `FOR KEY SHARE` on the job row, which conflicts with none of these.
- **Implicit waits.** A unique check waits for any transaction that is updating a row with the same
  key, so `INSERT ... ON CONFLICT` can wait like a lock. While a transaction holds the job row lock,
  it must not insert a key that may exist: redirect admission checks for a known target with a plain
  `SELECT` first, and level advancement inserts only targets that `NOT EXISTS` found absent
  (ADR-0014).
- **Contention.** The dispatchers and the reconcilers (crawl and index) select rows with `SKIP LOCKED`, so
  concurrent instances never block each other and never pick the same rows. Such a selection is a
  `MATERIALIZED` CTE when an `UPDATE` follows: `UPDATE ... WHERE id IN (SELECT ... LIMIT n FOR UPDATE
  SKIP LOCKED)` updated more than `n` rows, because PostgreSQL may rescan the subquery per outer row
  and a rescan skips the rows already updated (found in Phase 5; ADR-0016).
- **Dispatch and claim.** The dispatcher publishes before it commits, so a worker can read a message
  while the row is still `pending` in committed state. The claim therefore locks the row first
  (`SELECT ... FOR NO KEY UPDATE`): it waits for the dispatch to commit or roll back, then its
  compare-and-set sees the outcome.
- **Uniqueness.** Unique constraints keep racing duplicate inserts safe even when application-level
  checks race.
- **No network I/O inside a transaction.** The only exceptions are the short Redis calls of T3, T8,
  T10 and T17 (`XADD`, and the reconcilers' entry check), each bounded by the Redis socket timeout.
  The sessions of the scheduler and the index dispatcher end themselves after twice that timeout
  plus 5 s idle inside a transaction (`idle_in_transaction_session_timeout`), so a stalled
  dispatcher cannot keep its row locks; an indexer's after 5 s (its statements run back to back). The
  workers' sessions do so after the lease margin (10 s): their transactions never wait on the network,
  and a worker frozen inside its claim would otherwise block every takeover of its URL (Phase 5
  review).

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
| Search document | one row per page (`page_id` key); an attempt is counted before its work, a duplicate meets a live lease, and the vector is rebuilt from the locked current content: the same content gives the same result, and the version guard and triggers keep an older index out (Phase 7) |
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

Implemented: `SITESCOPE_CRAWL_LEASE_SECONDS` (90) must be at least the fetch deadline plus
`SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS` (10), and each task runs under a deadline that ends that margin
before its lease (Phase 4). Phase 5 adds `SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS`,
`SITESCOPE_SCHEDULER_RECONCILE_AFTER_SECONDS` and `SITESCOPE_STREAM_RETENTION_SECONDS` (3600),
and the startup check `task_deadline < lease <= reclaim_min_idle <= reconcile_after <= retention`.
Phase 6 shortens the first two defaults from 120 s to 100 s and from 600 s to 300 s (ADR-0017):
the scan intervals stay the same, so recovery is sooner without more queries.

Leases are never renewed (ADR-0016). The deadline already bounds every task, so a live task never needs
a longer lease, and a worker that kept renewing while stuck would hold its URL forever.

Initial defaults are starting points to validate in tests, not measured values:

| Setting | Default |
|---|---|
| task deadline | 80 s: the lease minus 10 s (the fetch alone: 60 s) |
| lease duration | 90 s |
| reclaim min-idle | 100 s (120 s until Phase 6) |
| reconcile after | 5 min (10 min until Phase 6) |
| attempts | 3 |
| retry backoff (Phase 6) | full jitter, base 2 s, capped at 60 s; `Retry-After` up to 300 s |
| poison threshold (Phase 6) | 10 deliveries |
| cancellation check (Phase 6) | every 2 s |
| politeness slot lease (Phase 6) | 15 s, renewed every 5 s |

### 10.3 How the crawler worker handles a message

Implemented in Phase 5 (`judge_unclaimed` in `backend/app/workers/crawler.py`; ADR-0016). The rule:
acknowledge only when no work is left that the message stands for.

1. **Malformed message.** Dead-letter it and ack.
2. **Poison message** (Phase 6). A reclaimed message delivered more than
   `SITESCOPE_STREAM_MAX_DELIVERIES` times whose URL is `queued` with its attempts used up: fail the
   URL through a fenced quarantine (`max_deliveries_exceeded`), then dead-letter the message (and ack,
   in one script). Any other URL goes through the rules below: a delivery count also grows while
   PostgreSQL is down, so it never fails a URL that a claim could still take. If the delivery counts
   cannot be read, the reclaimed messages are handled as first deliveries.
3. **Claim succeeds.** Process the URL, run the fenced commit, then ack.
   - A retryable failure with attempts left is committed as a retry (back to `pending`), then acked
     (Phase 6).
   - If the job is cancelled meanwhile, the worker stops the task, records the URL `cancelled` with its
     token, then acks (Phase 6).
   - If the commit is fenced out, discard the result and **do not ack**. The message now belongs to the
     new lease holder, and `XACK` acts on the whole group, so acking would hide it.
   - On any error, or a cancellation at shutdown, **do not ack** either: the outcome may be uncertain
     (a commit whose reply was lost). The message is redelivered, and the rules below sort it out.
4. **Claim fails.** Read the URL row (`inspect_url`) and act on what it shows:
   - the URL is terminal, or the job is no longer running: ack;
   - the URL is `pending`: the dispatch that published this message rolled back (the claim waited for
     it, section 9), or the URL waits for a retry. Ack; the dispatcher publishes the task again when
     it is due;
   - the URL is `queued`, or its lease expired with attempts left: claim again (a takeover);
   - another live worker holds the lease: leave the message pending, to be re-examined after
     `reclaim_min_idle`;
   - the lease expired after the last attempt: fail the URL as `max_attempts_exceeded` (a fenced
     recovery claim) and ack;
   - the URL does not exist, or belongs to another job: dead-letter it and ack.
5. **The ack fails.** Log it. The message stays pending, and its redelivery is acknowledged once the
   claim finds the URL finished.

A PostgreSQL or Redis error before the claim is handled again in the same process up to five times,
with backoff, within half the reclaim idle time; after that the message stays pending (Phase 6). A
Redis error during the task becomes a retryable `infrastructure_error` attempt.

### 10.4 Failure windows

| # | Failure | State left behind | Recovery | Outcome |
|---|---|---|---|---|
| F1 | API crashes after commit, before responding | job exists | client retries with the same `Idempotency-Key` (Phase 6) | same job, no duplicate (without a key, a retry creates a second job) |
| F2 | Dispatcher crashes after `XADD`, before commit | URL still `pending`; message in the stream | URL is dispatched again | duplicate message; the second claim is a no-op |
| F3 | Worker crashes before claiming | message pending under a dead consumer | another worker runs `XAUTOCLAIM` | delay only |
| F4 | Worker crashes after claiming, before commit | URL `in_progress` with a running lease | `XAUTOCLAIM` after min-idle, then a takeover claim (`lease_epoch + 1`) | page fetched again; the attempt is counted |
| F5 | Worker stalls past its lease, then resumes | another worker holds the lease | the stale commit fails fencing and rolls back | no duplicate effects |
| F6 | Worker crashes after commit, before `XACK` | URL terminal; message pending | redelivered → claim fails → ack | no duplicate effects |
| F7 | Redis loses messages, or they are deleted | URL `queued` or `in_progress` with no live stream entry | the reconciler re-publishes once the entry is gone (trimming never deletes undelivered or pending entries) | delay only |
| F8 | Redis is unavailable | jobs are still accepted; nothing is dispatched | the dispatchers and the workers retry with backoff; a task whose politeness or robots.txt call failed is a retryable attempt (Phase 6) | delay; a politeness slot whose release failed stays held until its lease ends (below) |
| F9 | PostgreSQL is unavailable | no claims or commits; messages stay pending | resumes through `XAUTOCLAIM` and the reconciler | delay; PostgreSQL is the hard dependency |
| F10 | Index dispatcher crashes after `XADD`, before commit | search document still `pending`; message in the stream | published again | duplicate index task: the orphan finds the document `pending` (ack) or does the work once (Phase 7) |
| F11 | Indexer crashes during an attempt, or after its commit before `XACK` | attempt counted, lease held; or document `indexed` | `XAUTOCLAIM` after the reclaim idle time; the lease has expired by then, so the next attempt takes over; or the redelivery finds the document indexed | the page is indexed once more at most; attempts bound a document that keeps crashing indexers (Phase 7) |
| F12 | Scheduler crashes, or schedulers overlap | a dispatch may roll back after its `XADD` | restart, or another scheduler continues; no leader: advisory lock (activation), job row lock (advancement), `SKIP LOCKED` + compare-and-set (dispatch, reconciliation) | at most duplicate messages |
| F13 | Poison message repeatedly crashes workers | delivery count grows | `attempt_count` bounds claims, ending in `failed`; malformed and unmatched messages are dead-lettered (Phase 5); a message whose URL can no longer be claimed is quarantined and dead-lettered, once, after `SITESCOPE_STREAM_MAX_DELIVERIES` deliveries (Phase 6). A long PostgreSQL outage inflates delivery counts without harm (Phase 6 review) | bounded work |
| F14 | Worker crashes holding a URL of a cancelling job (Phase 6) | URL `in_progress`, job `cancelling` | the finalizer cancels the URL once its lease expires | the job is `cancelled` within about one lease |
| F15 | Scheduler crashes during a backoff or a cancellation (Phase 6) | `pending` retries and `cancelling` jobs in PostgreSQL | any scheduler dispatches the retries when due and finishes the cancellation | delay only |

Phase 5 tests the crawl-stream windows with real services, most of them with real processes:

| Window | Test (backend/tests/...) |
|---|---|
| F2 | `integration/test_task_dispatch.py` (a publish whose transaction fails); `fault/test_recovery.py` (a scheduler killed exactly between `XADD` and `COMMIT`, held there by a table lock; a scheduler killed mid-crawl, D) |
| F3 | `fault/test_recovery.py` (a message abandoned before its claim, B); `integration/test_crawler_worker.py` |
| F4 | `fault/test_recovery.py` (a worker killed during a fetch, B; a worker stopped after its grace period) |
| F5 | `fault/test_recovery.py` (a worker frozen mid-fetch is fenced out, C); `integration/test_crawler_worker.py` |
| F6 | `integration/test_crawler_worker.py` (a lost `XACK`, repaired by the redelivery) |
| F7 | `integration/test_task_dispatch.py`, `integration/test_scheduler.py` (lost entries re-published, live ones left alone) |
| F8, F9 | `fault/test_recovery.py` (Redis, then PostgreSQL, cut off through a TCP proxy, G) |
| F12 | `integration/test_distributed.py` (three scheduler processes, F); `integration/test_task_dispatch.py` |
| F13 | dead-lettering in `integration/test_crawler_worker.py` and `integration/test_streams.py`, including entries that are not UTF-8; poison quarantine and idempotent dead-lettering (Phase 6) |
| F7 with real processes (Phase 6) | `fault/test_redis_data_loss.py`: `FLUSHALL` of a disposable Redis container mid-crawl |
| F14, F15, retries, cancellation (Phase 6) | `fault/test_retries_and_cancellation.py` (flaky pages and strict BFS, exhaustion, `Retry-After`, simultaneous failures, a scheduler restart during backoff, a worker killed during a retry, duplicates during backoff, a killed worker's politeness slot, cancellation with live processes and across a scheduler restart); `integration/test_retries.py`, `integration/test_cancellation.py`, `integration/test_crawler_worker.py` |

**Politeness slots after a crash or an outage.** A worker that dies, or loses Redis, while it holds a
politeness slot cannot release it. Until Phase 6 the slot stayed held until its TTL (the fetch deadline +
10 s, 70 s by default), and requests that waited longer than `SITESCOPE_POLITENESS_MAX_WAIT_SECONDS`
(30 s) failed as `rate_limited` for good. Since Phase 6 (ADR-0017) a slot is a lease of
`SITESCOPE_POLITENESS_SLOT_LEASE_SECONDS` (15 s) that its holder renews while its request runs, never
longer than the old TTL in total for a live request: a dead holder's slot frees within one lease, and
`rate_limited` is retried. The process test of a killed worker measures the next request to the host
about one lease after the kill. Limits:

- A holder frozen for longer than a lease loses its slot, and may then overlap with one other request
  to its host when it resumes.
- A holder that stalls at the very moment of a renewal can keep its slot up to one lease past the old
  TTL. The concurrency limit holds either way.
- A dead process's *waiting* requests are separate: they stay in the host's queue until their waiter
  entries expire (`waiter_ttl`: the longest sleep of a live waiter plus 5 s, 15 s by default; Phase
  4). A pre-check run measured this: 6.95 s with the test timings.

### 10.5 Retries and backoff (implemented in Phase 6)

The retry policy is `backend/app/workers/retry.py` (ADR-0017); both the crawler workers and the
single-process driver use it.

- **Retryable:** temporary DNS failures (`dns_error`), connection errors, connect and read timeouts,
  `deadline_exceeded` (the fetch deadline or the task deadline), truncated responses, `rate_limited`
  (no politeness slot in time), `infrastructure_error` (Redis failed during the task), and HTTP
  statuses in `SITESCOPE_RETRY_HTTP_STATUSES` (429, 500, 502, 503 and 504 by default; the settings
  accept only 5xx, 408, 425 and 429). Everything else is final at once: invalid URLs, scope and SSRF
  refusals, `dns_not_found`, robots.txt decisions, TLS errors, redirect problems, unsupported or
  oversized content, parse errors, and other HTTP statuses such as 404 and 410.
- **Attempts:** a claim counts one (crashed attempts included). A retryable failure of attempt `n <
  SITESCOPE_CRAWL_MAX_ATTEMPTS` is retried; that of the last attempt is the URL's final result, with
  its own outcome.
- **Delay:** `uniform(0, min(SITESCOPE_RETRY_MAX_DELAY_SECONDS, SITESCOPE_RETRY_BASE_DELAY_SECONDS *
  2^(attempt - 1)))`, i.e. full jitter, and at least a valid `Retry-After` of a 429 or 503 response
  (`backend/app/crawler/retry_after.py`: delay-seconds, or an HTTP-date counted from the response's
  `Date`). A `Retry-After` above `SITESCOPE_RETRY_AFTER_MAX_SECONDS` (300 s) is not waited for: the
  failure is final.
- **Durability:** a retry sets the URL back to `pending` with `next_attempt_at` by the database clock,
  in the fenced transaction (T5). No timer exists anywhere: the dispatcher publishes the URL when it
  is due, any scheduler can, a crash loses nothing, and a waiting retry occupies no worker slot. A
  retry is an ordinary new task: robots.txt, politeness, the safe fetcher and the worker's concurrency
  limit all apply again.
- **BFS:** a URL waiting for a retry is unfinished, so its level stays open (section 8.3).
- **Infrastructure calls:** a PostgreSQL or Redis error before the claim is handled again in the same
  process up to five times with backoff, within half the reclaim idle time; then the message stays
  pending and the worker pauses reading with exponential backoff and full jitter (base 0.5 s, cap
  10 s), and the message is recovered as in F3 and F4. The scheduler retries its whole pass with
  backoff (cap 30 s). The single-process driver stops on a PostgreSQL or Redis error (its leases
  expire, and the next run takes them over).

### 10.6 Redis and PostgreSQL consistency limitations

- No commit spans both systems. The outbox gives at-least-once publication, so duplicates are expected
  and tolerated.
- With AOF `everysec`, a Redis crash can lose about a second of writes, and more with weaker persistence
  settings. Correctness does not depend on Redis persistence, because the reconciler restores liveness:
  a task whose entry was lost is published again `reconcile_after` after its last progress.
- A message re-published as a new entry starts with a fresh delivery count. PostgreSQL's `attempt_count`
  is therefore the authoritative retry limit; the delivery count only detects poison messages
  (Phase 6).
- Queue statistics read from Redis (stream lengths, pending counts) are approximate. Job progress always
  comes from PostgreSQL.
- PostgreSQL is a single point of failure for progress: while it is down nothing advances, but nothing is
  lost.

### 10.7 Not guaranteed

- Exactly-once fetching or processing.
- Any particular order of tasks within a level, or of index events.
- A bound on total crawl time against slow or rate-limited targets. Only the work per URL is bounded.
- Durability of anything that exists only in Redis.

### 10.8 The index pipeline (implemented in Phase 7)

ADR-0018 records the design; `backend/app/workers/indexer.py` (`judge_unstarted`) and
`backend/app/services/search_index.py` implement it. The rule is the crawler's: acknowledge only
when no work is left that the message stands for. Retries are bounded by attempts counted in
PostgreSQL before each attempt's work, never by Redis delivery counts.

| Situation | Disposition | Reason |
|---|---|---|
| malformed message; unknown page; a page of another job; a content version the page never had | dead-letter | `malformed`, `unknown_document`, `job_mismatch`, `version_ahead` |
| the document is indexed (a duplicate, or a redelivery after a lost `XACK`) | ack | `already_indexed` |
| the document failed already | ack | `already_failed` |
| the document is `pending` (its dispatch rolled back, or it was reset) | ack | `not_dispatched` |
| another attempt holds a live lease | keep | `leased` |
| attempts used up, no live lease | fail the document (`max_attempts_exceeded`), ack | `max_attempts_exceeded` |
| the attempt indexed the page | ack after the commit | `indexed` |
| the document left `queued` while the attempt ran | ack | `not_queued` |
| the vector exceeds PostgreSQL's limit even for a quarter of the body | fail the document (`tsvector_too_large`), ack | `index_failed` |
| the attempt's deadline passed (PostgreSQL stopped a statement, or the indexer gave up waiting) | record the error, release the lease, keep | `deadline_exceeded` |
| PostgreSQL or Redis error | keep, pause reading | `error` |

A message for an older content version of a queued document starts an attempt like the
current one: the attempt indexes the current content either way, and the current version need not
have a message of its own (a dispatch racing a content change can publish the older version;
independent review).

**Timing.** `index task deadline (20 s) < index lease (40 s: halfway) < reclaim idle (60 s) <=
reconcile after (300 s) <= stream retention (3600 s)`; the reclaim idle time is at least the
deadline + 2 s, so a lease outlasts its attempt by at least 1 s, and the deadline is shorter than
the database command timeout (30 s). The deadline covers the whole attempt: each statement of the
index transaction gets what remains of it (`statement_timeout`), and the indexer abandons the
transaction when it passes. An indexer runs at most as many tasks as its connection pool holds
(`SITESCOPE_DB_POOL_SIZE + SITESCOPE_DB_MAX_OVERFLOW`), so no task waits for a connection after
its attempt was counted. The processes refuse to start otherwise.

**Failure windows and their tests** (backend/tests/...):

| Window | Test |
|---|---|
| dispatcher killed between `XADD` and commit (F10) | `integration/test_search_index.py` (a publish whose transaction fails); `fault/test_indexing_processes.py` (a dispatcher process killed there, held by a table lock) |
| an attempt waits for a dispatch in flight | `integration/test_search_index.py` (synchronized on the row lock) |
| indexer crash during an attempt (F11) | `integration/test_indexer_worker.py` (an abandoned attempt taken over); `fault/test_indexing_processes.py` (an indexer process killed while its index transaction waits on a held page lock) |
| commit without `XACK` (F11) | `integration/test_indexer_worker.py` |
| duplicates, out-of-order messages, an older message when the current version has none | `integration/test_index_versions.py`, `integration/test_indexer_worker.py` |
| an attempt outliving its lease (the deadline across statements, a lock wait) | `integration/test_search_index.py`, `integration/test_indexer_worker.py` |
| a content change racing the index transaction | `integration/test_index_versions.py` (both orders, forced with row locks) |
| Redis lost the index stream | `integration/test_indexer_worker.py` (stream deleted); `fault/test_redis_data_loss.py` (`FLUSHALL` of a disposable Redis container after the dispatch) |
| PostgreSQL or Redis unreachable | `integration/test_indexer_worker.py` (PostgreSQL through a TCP proxy); `fault/test_indexing_processes.py` (Redis, then PostgreSQL, cut for the dispatcher and two indexers) |
| a poison document (every attempt times out) | `integration/test_indexer_worker.py` |
| a job cancelled during indexing | `fault/test_indexing_processes.py` |

## 11. Correctness invariants

The integration, concurrency, and fault tests (Phases 4 to 6) check these after every scenario:

1. Each `(job_id, normalized URL)` appears at most once in `crawl_urls`.
2. `admitted_count` equals the job's number of `crawl_urls` rows and is at most `max_pages`.
3. Each URL's depth equals its shortest-path distance from the seed through fetched pages, and crawling a
   fixture site with 1 worker gives the same result as crawling it with N workers.
4. Each URL has at most one committed result, and each `html_stored` URL has exactly one `pages` row.
5. A terminal job has no unfinished URLs, and its data never changes afterward.
6. Every stored page without `noindex` has exactly one `search_documents` row, a `noindex` page has
   none (ADR-0014), and every `pending` one is eventually published and indexed.
7. No request is ever sent to a blocked destination. Tests assert this with a recording resolver and a
   recording transport.
8. A document's `indexed_content_version` never decreases, and its vector is always built from the
   page content of that version (Phase 7).
9. A job's `indexing_completed_at` is set only once the job is finished and none of its documents is
   `pending` or `queued`; indexing never changes a job's status (Phase 7).
10. Search returns only documents of the requested job whose `indexed_content_version` equals
    their page's `content_version` (Phase 7).

Phase 4 checks invariants 1, 2, 4, 5 and 6, the parent-depth rule behind 3, and 7 (every
connection recorded) after every end-to-end crawl, and compares the exact expected depths
(`backend/tests/integration/test_local_crawler.py`).

Phase 5 checks invariants 1, 2, 4, 5 and 6 and the parent-depth rule after every multi-process crawl
(`backend/tests/integration/test_distributed.py`, `backend/tests/fault/test_recovery.py`), with the
exact expected URLs and depths. From the test servers' side it also checks that each page was
requested once in failure-free runs and with injected duplicates, that every level was requested
only after the previous one was finished (strict BFS across processes), and the per-host politeness
limits (requests in flight, spacing) across processes.

Phase 6 checks the same invariants after crawls with flaky pages, exhausted retries, cancellations,
restarts during backoff and a full loss of Redis data (`backend/tests/fault/`), and, from the
servers' side, that a level is requested only after every retry of the previous level has finished,
and that each flaky page was requested exactly once per attempt.

Phase 7 checks invariants 8 to 10 deterministically (row locks force each interleaving, and
`pg_stat_activity` shows the waits), and after the multi-process scenarios that every indexable
page is indexed exactly once per counted attempt, that every index message is acknowledged in the
end, and that the job's indexing completes (`backend/tests/integration/test_index_versions.py`,
`test_search_index.py`, `test_indexer_worker.py`, `test_search.py`,
`backend/tests/fault/test_indexing_processes.py`).

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
rows are implemented by the safe fetcher and the parser (Phase 3), the politeness row by Phase 4.

| Bound | Mechanism | Initial default |
|---|---|---|
| Time | connect timeout (also bounds DNS and the TLS handshake), read/write timeout, and an overall deadline for the whole fetch, redirects included (stops slow-drip responses) | 10 s, 10 s, 60 s |
| Size | streamed read with a wire limit and a **decoded** limit; decompression in 64 KiB slices, so a bomb never inflates beyond the limit; an oversized body is rejected, never truncated | 2 MiB decoded (at most 10 MiB) |
| Content type and coding | pages: only `text/html` and `application/xhtml+xml`; robots.txt: any media type, first 500 KiB kept (Phase 4); only `gzip` and `deflate` are advertised and decoded | n/a |
| Redirects | followed by the fetcher, never by the HTTP stack; each hop validated; loops detected | 5 |
| URLs and links | maximum URL length; links kept per page; text kept per page | 2048 bytes; 1000 links; 1,000,000 characters |
| Crawl size | per-job `max_depth` and `max_pages`, under server-side caps | set per deployment |
| Concurrency | per-worker semaphore, per-job in-flight window, cap on running jobs | set per deployment |
| Politeness | per host, shared by all workers: a first-come-first-served queue, spacing between request starts, and a cap on requests in flight, in one Redis Lua script using Redis server time; robots.txt `Crawl-delay` raises the interval, capped; a request without a slot within the maximum wait fails as `rate_limited` before anything is sent (ADR-0015) | 1 request/s and 1 in flight per host; 30 s maximum wait; crawl delay capped at 10 s |

### 12.5 robots.txt (RFC 9309; implemented in Phase 4)

`backend/app/crawler/robots.py`, [ADR-0015](adr/0015-robots-txt-and-per-host-politeness.md):

- robots.txt is fetched through the same safe fetcher (scope check, DNS validation, pinning,
  politeness) with the `ROBOTS_TXT` content policy: any media type, the first 500 KiB parsed and
  the rest ignored. The last line of a truncated file is dropped, because the cut may have
  changed its meaning. Redirects are followed for up to five hops, each validated again.
- Results are cached in Redis per origin and crawler name, for at most 24 hours. A process also
  keeps them in memory (60 s at most, never past the Redis entry's expiry), and concurrent checks
  of one origin share one fetch.
- Groups follow the RFC grammar: consecutive `user-agent` lines form one group until its first
  rule, and other records (`sitemap`, `crawl-delay`, unknown keys) never end a group. The
  crawler's name (`SITESCOPE_CRAWLER_NAME`) is matched case-insensitively against each value's
  product token; matching groups are combined; without one, the `*` groups apply.
- `Allow` and `Disallow` match the path and query, case-sensitively, after both sides get the same
  percent-encoding. The longest pattern wins and `Allow` wins ties; `*` and a final `$` are
  wildcards; `/robots.txt` is always allowed. Matching uses no regular expressions; rule sets
  above 1,000 rules are matched in a worker thread (a hostile 500 KiB file measured about 20 ms
  per check).
- Status handling:
  - 2xx: the rules are parsed.
  - 4xx (other than 429), more than five redirects, or a redirect loop: no restrictions.
  - 5xx, 429, network, TLS, protocol or size failures, a redirect to an unapproved target, or a
    blocked address: complete disallow, cached for `SITESCOPE_ROBOTS_RETRY_SECONDS` (300 s), then
    fetched again. Treating 429 like a 5xx is a deliberately conservative choice.
  - No politeness slot in time (`rate_limited`): not cached; that URL fails.
- A disallowed URL is `skipped` (`robots_disallowed`); every URL of an origin whose robots.txt is
  unreachable is `skipped` (`robots_unreachable`). Both still consume budget.
- Python's `urllib.robotparser` applies the first matching rule and does not support path wildcards, so
  it is not RFC 9309-compliant. SiteScope uses its own small parser, tested against the RFC's
  examples.
- Page-level directives: `<meta name="robots">`, `<meta name="<crawler name>">` and
  `X-Robots-Tag` headers (a `name:` prefix scopes a header's directives to that crawler).
  `noindex` keeps the page out of search (no `search_documents` row), `nofollow` stops its links
  from being admitted, and `none` means both. `rel="nofollow"` stops one link. Links are recorded
  in every case (ADR-0014).

### 12.6 Untrusted content in the dashboard

- Crawled titles, snippets, anchor text, and URLs are rendered as text through React's escaping.
  `dangerouslySetInnerHTML` is never used for crawled content.
- Search highlighting: the API returns plain-text snippets and `[start, end)` ranges of Unicode
  code points (Phase 8). The dashboard splits the snippet with `Array.from`, clamps and merges the
  ranges, and renders each part as text, inside `<mark>` or not (Phase 9).
- A crawled URL is rendered as a link only if it is http(s), and always with
  `rel="noopener noreferrer nofollow"` in a new tab; anything else stays text.
- A lint rule rejects `dangerouslySetInnerHTML`, and the dashboard's tests check that markup in
  crawled titles and snippets is shown as text.

### 12.7 API surface and secrets

- Every input is validated with Pydantic, request sizes are limited, and server-side caps apply.
- In public deployments, mutating endpoints can require a bearer token. Read endpoints are public.
- Job creation is rate-limited per client (Phase 11; not implemented yet). The dashboard calls the
  API on its own origin through a reverse proxy, so the API sends no CORS headers (Phase 9,
  ADR-0020); a deployment that splits the origins needs an explicit allowlist, never a wildcard.
  CORS is not authentication.
- `GET /api/v1/crawl-policy` exposes the approved host patterns, schemes, ports and job limits:
  the targets of a public demo are public anyway. Internal fetch origins and every other setting
  stay private.
- Errors are returned as `application/problem+json` without internal details. SQL is issued only with
  bound parameters.
- Configuration comes from environment variables. `.env` is git-ignored, and secrets never appear in
  code, tests, docs, or logs.
- PostgreSQL and Redis are never reachable from outside the Compose network.

## 13. Search, indexing, and link analysis

Indexing and the search service are implemented in Phase 7 (ADR-0018, which amends ADR-0005);
the HTTP search API and link analysis in Phase 8 (ADR-0019, which replaces this section's original
definition of broken links).

**Pipeline.** Result commit → `search_documents` row as `pending` (in the same transaction) → index
dispatcher → `sitescope:index` → indexer → vector written with its content version, row `indexed`
([ADR-0005](adr/0005-postgresql-full-text-search.md),
[ADR-0008](adr/0008-search-documents-as-index-outbox.md),
[ADR-0018](adr/0018-asynchronous-indexing-pipeline.md)). The index lives in its own table so that
GIN maintenance stays off the crawl write path, and so that re-indexing (for example, with new
weights) needs no re-crawl.

**Document** (`backend/app/indexing/documents.py`), built inside PostgreSQL from the locked page:

```sql
setweight(to_tsvector('english', coalesce(title, '')), 'A')
  || setweight(to_tsvector('english', coalesce(meta_description, '')), 'B')
  || setweight(to_tsvector('english', headings), 'C')
  || setweight(to_tsvector('english', <body cut to the limit at a word boundary>), 'D')
```

- **Size.** PostgreSQL limits a vector's lexemes and positions to 1 MB. Title, description and
  headings (at most 23,000 characters together) are indexed whole; the body is cut to
  `SITESCOPE_INDEX_MAX_BODY_CHARS` (100,000 by default, at most 150,000) before `to_tsvector`, at
  the last whitespace before the limit (looking back at most 2,048 characters). The densest text measured, distinct hyphenated CJK compounds,
  needs about 6.7 bytes per character, so the default stays below the limit for every text
  measured; a vector over the limit is built again once from a quarter of the body, and a document
  that still does not fit fails (`tsvector_too_large`).
- **Languages.** The `english` configuration stems English words and drops English stop words.
  Other languages written with spaces are split into words but not stemmed. Chinese, Japanese and
  other languages written without spaces are not segmented: a run of such characters is one token,
  which a query matches only whole (or as a prefix with `:*`). There is no multilingual search.

**Query** (`backend/app/services/search.py`)

- The query is normalized (invisible characters removed, whitespace collapsed). An empty query,
  one over 256 characters, or paging out of range (limit 1 to 50, offset 0 to 1000) is refused,
  never silently cut.
- `websearch_to_tsquery('english', :q)` builds the query; it never raises a syntax error on user
  input. A query with no term an index can look up (only stop words, or only excluded words) is not
  run, and the result says so.
- `search_vector @@ query` matches documents through the GIN index, within one job, and only
  documents whose `indexed_content_version` equals their page's `content_version` (the visibility
  rule).
- Results are ordered by `ts_rank(search_vector, query) DESC` (weights 1.0, 0.4, 0.2, 0.1 for A to
  D; no length normalization), then by URL (byte order), then page ID: a total order, so pages are
  stable.
- `ts_headline` runs only for the rows of the returned page, on at most 20,000 characters of the
  body (or the description when there is no body), with control-character markers removed from the
  text beforehand; the service returns plain text and highlight ranges, never HTML.
- One read-only REPEATABLE READ transaction returns the results and the job's indexing progress
  from the same snapshot. A search has `SITESCOPE_SEARCH_TIMEOUT_SECONDS` in all: PostgreSQL stops
  each statement after that long, and the service abandons the transaction then too.

**Ranking limits.** `ts_rank` uses term frequency, positions and field weights, not corpus
statistics, so there is no IDF or BM25. There is no fuzzy matching, and only the `english`
configuration is used. Every match is ranked before a page is cut, so the cost of a search grows
with its number of matches. Any ranking change, such as boosting pages by in-link count, must be
justified by an evaluation rather than assumed.

**HTTP search API** (Phase 8). `GET /api/v1/search` maps `page` and `page_size` to the service's
offset and limit (at most offset 1000 and 50 results, as above) and returns its results unchanged:
the visibility rule, the ranking and the time limit are the service's. Details:
[`api-conventions.md`](api-conventions.md), section "Search".

**Link analysis** (Phase 8, `backend/app/services/site_analysis.py`,
[ADR-0019](adr/0019-website-analysis-from-stored-crawl-results.md)). Computed in SQL from the
job's stored rows (`crawl_urls`, `pages`, `page_links`, `search_documents`); nothing is fetched
again, and nothing is written. Each request is one read-only REPEATABLE READ transaction with a
time limit (`SITESCOPE_ANALYSIS_TIMEOUT_SECONDS`, 10 s by default).

- **Classification.** Every stored link gets one class from the crawl result of its target in the
  same job (`page_links.target_normalized_url` = `crawl_urls.normalized_url`, within the job):
  `ok`, `broken`, `server_error`, `http_error` and `redirect_error` are decided by an HTTP response
  (*verified*); `out_of_scope`, `not_followed`, `beyond_limits`, `not_crawled`, `robots_blocked` and
  `unreachable` are not (*unverified*). Only a terminal result decides: a URL waiting for a retry
  keeps its last attempt's status.
- **Redirects.** A target that redirected is judged where its chain ends: a recursive query follows
  `redirect_target` through the job's own crawl URLs and stops at a URL already seen (a loop, class
  `redirect_error`), after 20 redirects (also `redirect_error`; this bounds the query, since URLs
  admitted through links can form chains of any length), or at a URL the job never admitted
  (`out_of_scope` when it leaves the job's hosts, `beyond_limits` when the page budget was used
  up, `not_crawled` otherwise).
- **Broken links** are internal links whose target, after its redirects, answered 404 or 410: one
  item per stored link. The original definition here ("failed, or a status of 400 or higher") was
  replaced: 5xx, 408 and 429 may be transient, 401 and 403 are refusals, and a failed request has
  no response (ADR-0019).
- **External links** are recorded and never fetched (the allowlist), so they are `out_of_scope`,
  never broken.
- **Structure.** The link graph (nodes: admitted URLs and internal link targets; edges: stored
  internal links; in- and out-degrees over every stored link; cut at `max_nodes` and `max_edges`),
  the depth distribution, and the most-linked URLs. Only links on stored pages are known, so the
  analysis never calls a page an orphan.
- **Cost.** The statistics and the filtered links listings classify every link of the job, and
  the broken links follow the redirect chains of every crawl URL; a page of the unfiltered links
  listing classifies only its own links, and the pages listing reads one page of pages. The page
  budget bounds the rest (`SITESCOPE_CRAWL_MAX_PAGES_LIMIT`); measurements:
  `benchmarks/results/phase8-analysis.md`.

## 14. Observability and configuration

**Logs.** JSON lines on stdout with `timestamp`, `level`, `logger`, `event`, and `service`, plus context
fields: `request_id`, `job_id`, `url_id`, `worker_id`, `lease_epoch`, `attempt`. Key events:

| Area | Events |
|---|---|
| Jobs | `job.created`, `job.level_advanced`, `job.completed` |
| Tasks | `task.dispatched`, `task.claimed`, `task.committed`, `task.fenced`, `task.retry_scheduled`, `task.dead_lettered` |
| Recovery | `lease.taken_over`, `reconcile.republished` |
| Fetching (Phase 3) | `fetch.destination_blocked` (WARNING: host and address classes, never the URL or the addresses), `fetch.completed` (DEBUG) |
| Crawl (Phase 4) | `crawl.run_started`, `crawl.run_finished`, `job.started`, `job.level_advanced`, `job.completed`, `job.failed`, `task.completed`, `task.fenced`, `task.parse_failed`, `lease.taken_over`, `url.failed`, `robots.fetched`, `robots.fetch_failed`, `politeness.rate_limited`, `politeness.release_failed` |
| Distributed (Phase 5) | `worker.started`, `worker.stats`, `worker.error`, `worker.recovered`, `worker.stopping`, `worker.tasks_cancelled`, `worker.stopped`; `task.handled` (one line per message: disposition, reason, message ID, consumer, epoch, depth, outcome, duration), `task.reclaimed`, `task.reclaim_deleted`, `task.settle_failed`, `task.cancelled`, `task.dispatched`, `task.republished`; `scheduler.started`, `scheduler.pass`, `scheduler.pass_failed`, `scheduler.recovered`, `scheduler.stats`, `scheduler.stopped`; `stream.group_recreated`, `stream.consumers_removed` |
| Retries, dead letters, cancellation, idempotency (Phase 6) | `task.retry_scheduled` (and the `url.retry_scheduled` event), `task.infrastructure_error`, `url.dead_lettered`, `task.stopping_cancelled`, `url.cancelled`, `job.cancel_requested`, `job.cancelled`, `job.replayed`, `api.idempotency_keys_expired`, `politeness.renew_failed`, `politeness.slot_lost`, `stream.group_created` |
| Indexing (Phase 7) | `index.dispatched`, `index.republished`, `index.lease_taken_over`, `index.vector_too_large`, `index.failed`, `job.indexing_completed`, `job.reindex_requested`; `index_dispatcher.started`, `index_dispatcher.pass`, `index_dispatcher.pass_failed`, `index_dispatcher.recovered`, `index_dispatcher.job_failed`, `index_dispatcher.stats`, `index_dispatcher.stopped`; the indexers' `worker.*` lines and one `task.handled` line per message (job, page, content version, attempt, worker, duration, disposition, reason; for an indexed page the version indexed, lexemes and whether the body was cut) |

Since Phase 4, a crawl task's log lines carry `job_id`, `url_id`, `worker_id` and `lease_epoch`
from a context variable (`task_context_var`), including lines logged by the fetcher. The job
lifecycle (`job.started`, `job.level_advanced`, `job.completed`, `job.failed`) and recovery
(`url.recovered`, `url.failed`) are also written to `crawl_events`, and since Phase 6 retries
(`url.retry_scheduled`), quarantines (`url.dead_lettered`) and cancellation
(`job.cancel_requested`, `job.cancelled`); since Phase 7 `page.index_failed`,
`job.indexing_completed` and `job.reindex_requested`. Log lines and events never contain page
text.

**Health**

- `GET /health` and `GET /api/v1/health/live` are liveness probes: the process is up and serving
  requests. They check no dependencies. Both were implemented in Phase 1.
- `GET /api/v1/health/ready` (Phase 2B) reports whether PostgreSQL and Redis are reachable.
- The workers log `worker.stats` every 60 s, and the scheduler `scheduler.stats` at each
  maintenance (Phase 5). `python -m app.scheduler.status` prints jobs, URL states, live leases
  per worker, stream and consumer-group statistics, and (Phase 6) the newest dead letters,
  read-only. Since Phase 7 the index dispatcher logs `index_dispatcher.stats` (documents by
  status, attempts running, the oldest pending and queued documents, finished jobs whose indexing
  is not complete, the index stream's length and pending messages), and the status command adds
  the same from PostgreSQL (`indexing`) and the index stream (`index_stream`). Every figure is
  counted from PostgreSQL or Redis when it is reported. Compose health checks arrive in Phase 11.

**Progress views**

- Job detail shows, from PostgreSQL: counts per URL state, the current level, admitted vs. maximum pages,
  and (Phase 7) indexing progress: documents per status, attempts running, the derived indexing
  status and `completed_at`, read in the same statement as the URL counts.
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
  - retries (Phase 6): base and maximum delay, `Retry-After` cap, retried HTTP statuses
  - crawling (Phase 4): concurrency of a crawl process, lease duration and margin, attempts;
    the cancellation check interval (Phase 6)
  - streams and scheduler (Phase 5): read block time, reclaim interval, idle time and batch,
    retention, consumer idle time; scheduler interval, batch size, per-job and overall in-flight
    limits, running-jobs cap, reconcile time; shutdown grace period; the poison threshold
    (deliveries, Phase 6)
  - politeness (Phase 4): interval, requests in flight per host, maximum wait, robots.txt cache
    and retry times, crawl-delay cap, the slot lease (Phase 6), and the crawler name for the user
    agent `<name>/<version>` (default `SiteScopeBot`)
  - indexing (Phase 7): indexer concurrency, attempts, body limit, task deadline, reclaim idle
    time, reconcile time, dispatch batch, outstanding cap, dispatcher interval; the search timeout
  - API: idempotency key retention (Phase 6); token and CORS later
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
| `index-dispatcher` | internal | no | Phase 7: `python -m app.scheduler.index_dispatcher` |
| `indexer-worker` (N replicas) | internal | no | Phase 7: `python -m app.workers.indexer` |
| `migrate` | internal | no | one-shot `alembic upgrade head`; other services wait until it succeeds |
| `postgres` | internal | no | named volume |
| `redis` | internal | no | AOF on, named volume |
| `demo-site` (nginx, static) | crawl | optional | the approved demo target |

- One backend image runs every Python role (with different commands) as a non-root user.
- In the public profile, the `crawl` network has no general internet egress.
- Before Phase 11, the Python processes run on Windows from `backend/.venv` against the PostgreSQL and
  Redis containers started by Compose, and the dashboard runs on the Vite server (`npm run dev`,
  which proxies `/api` to the API as nginx will). `backend/compose.yaml` therefore still defines only those two
  services in Phase 7: the index dispatcher and indexers are started like the scheduler and the
  crawler workers (`docs/development.md`).
- Connections: each process opens at most `SITESCOPE_DB_POOL_SIZE + SITESCOPE_DB_MAX_OVERFLOW`
  (10 by default) PostgreSQL connections, and an indexer task holds one at a time. The sum over all
  processes must stay below PostgreSQL's `max_connections` (100 in the development container).

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
|   |   |-- api/                 # routers and schemas: crawls (Phase 2C); search and website
|   |   |                        #   analysis (Phase 8)
|   |   |-- services/            # use cases that own transactions: jobs, frontier, cancellation
|   |   |                        #   (Phase 6), indexing, search (Phase 7), site analysis (Phase 8)
|   |   |-- db/                  # SQLAlchemy models, engine and session factory
|   |   |-- queue/               # Phase 5: Redis Streams adapter, versioned message schema
|   |   |-- scheduler/           # Phase 5: scheduler passes (activation, advancement, dispatch,
|   |   |                        #   reconciliation, maintenance), read-only status command;
|   |   |                        #   Phase 7: the shared pass loop, the index dispatcher, the
|   |   |                        #   read-only search command
|   |   |-- crawler/             # URL normalization, scope (Phase 2C); destination policy, resolver,
|   |   |                        #   safe fetcher, content limits, encodings, HTML parser (Phase 3);
|   |   |                        #   robots.txt, politeness gate, page directives (Phase 4);
|   |   |                        #   Retry-After parsing (Phase 6);
|   |   |                        #   no database imports (Redis for politeness and robots.txt)
|   |   |-- indexing/            # Phase 7: search document rules (configuration, weights,
|   |   |                        #   limits) and query handling (normalization, paging, snippets);
|   |   |                        #   pure functions, no database
|   |   `-- workers/             # crawl task, single-process driver (Phase 4); crawler worker,
|   |                            #   shared runtime (Phase 5); retry policy (Phase 6); the shared
|   |                            #   consumer loop and the indexer (Phase 7)
|   `-- tests/
|       |-- unit/
|       |-- integration/
|       `-- fault/               # Phase 5: crash and outage recovery with real processes
|-- frontend/                    # Phase 9: React + TypeScript dashboard (src/app, components,
|                               #   features, hooks, lib/api), Vitest tests, e2e/ (Playwright)
|-- demo-site/                   # Phase 4: static site + robots.txt; Phase 11: its container
|-- benchmarks/                  # Phase 5: distributed throughput; Phase 10: the full set
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
  - `scheduler` and `workers` depend on `services`, `queue`, `crawler`, and `indexing`;
    `services` uses the pure functions of `indexing` (Phase 7).
  - `crawler` and `queue` have no domain or database dependencies, which keeps them unit-testable.
    The crawler's politeness gate and robots.txt cache use Redis through small interfaces, so
    their logic is tested with test doubles too.

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
| robots.txt | own parser and matcher (`app/crawler/robots.py`, Phase 4) | RFC 9309-compliant (§12.5), tested against the RFC's examples; `urllib.robotparser` is not compliant, and a small module avoids a native dependency (ADR-0015) |
| Logging | stdlib `logging` + a JSON formatter | no extra dependency |
| Tests | pytest with warnings treated as errors; Starlette's TestClient (needs `httpx2`); AnyIO's bundled pytest plugin for async tests; the HTTP client's mock transport for crawler tests | no extra plugin dependency: AnyIO is already installed with Starlette |
| Quality | ruff (lint and format), mypy | |
| Frontend | React 19 + TypeScript 5.9 + Vite 8; Tailwind CSS 4; React Router 8; TanStack Query 5; Recharts 3; Vitest + Testing Library; Playwright (installed Edge) | server state with request deduplication, cancellation and bounded polling; charts with an accessibility layer; no other UI framework (ADR-0020) |
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
| Crawl state (Phase 4) | claims, takeovers, fenced results and rollback, redirect admission, level advancement, recovery; budget and deduplication under concurrent transactions; the SQL sent binds the worker's token | PostgreSQL |
| Politeness and robots.txt (Phase 4) | the gate on a fake clock with an in-memory store (no real waiting); the Lua script on real Redis, with exact spacing read from Redis; robots.txt parsing, matching and status mapping; the policy against local servers | nothing; Redis; local servers |
| Local crawls (Phase 4) | the single-process driver over fixture graphs and the demo site on 127.0.0.1: exact URL sets and depths, budget prefix, directives, failures, redirects, robots.txt, crash recovery with a short lease, politeness spacing, 1 vs N concurrent tasks; invariants after every crawl | PostgreSQL, Redis, local servers |
| Streams and workers (Phase 5) | the stream adapter on real Redis (groups, blocking reads, acks, `XAUTOCLAIM`, deleted entries, dead letters, trimming, `delivering`); dispatch with publication and its failure windows; a claim waiting for a dispatch; reconciliation; the worker's acknowledgment rules with a test-double fetcher; scheduler passes | PostgreSQL, Redis |
| Processes (Phase 5) | real scheduler and worker processes crawling local sites: participation from the workers' own logs, injected duplicates, competing schedulers, a killed worker, a frozen worker (fenced out), a killed scheduler, Redis and PostgreSQL cut off through a TCP proxy, graceful stops; the invariants, and BFS order and politeness from the servers' request timelines | PostgreSQL, Redis, local servers |
| Retries, dead letters, cancellation, idempotency (Phase 6) | the retry transition and its fencing, rollback and races; exhaustion; strict BFS with retries; poison quarantine and idempotent dead-lettering; cancellation (T7, the worker's fenced cancel, the finalizer, races and lock order, the API); `Idempotency-Key` (replays, conflicts, concurrent requests, expiry, rollback, restarts); `Retry-After` through the real fetcher; slot lease renewal on real Redis | PostgreSQL, Redis, local servers |
| Processes (Phase 6) | real processes with flaky local sites: retries and strict BFS, exhaustion, `Retry-After`, simultaneous failures, a scheduler restart during backoff, a worker killed during a retry, duplicates during backoff, a killed worker's politeness slot, cancellation with live workers and across a scheduler restart; `FLUSHALL` of a disposable Redis container mid-crawl | PostgreSQL, Docker (WSL), local servers |
| Indexing and search (Phase 7) | the indexing outbox (dispatch inside the transaction, `SKIP LOCKED`, rollback after `XADD`); attempts and leases; the weighted vector, size limits and the fallback; stale-index prevention with every interleaving forced by row locks; triggers; job indexing completion and reindexing; reconciliation; the indexer's dispositions in-process (duplicates, dead letters, a lost `XACK`, an abandoned attempt, a poison document, a PostgreSQL outage); relevance, ties, paging, visibility, isolation, snippets and awkward queries | PostgreSQL, Redis |
| Processes (Phase 7) | real scheduler, crawler worker, index dispatcher and indexer processes: crawl, persist, publish, index and search a local site; an indexer killed mid-attempt; an index dispatcher killed between `XADD` and commit; Redis and PostgreSQL cut off; a job cancelled during indexing; `FLUSHALL` of a disposable Redis container after the dispatch | PostgreSQL, Redis, Docker (WSL), local servers |
| End-to-end | crawl a local fixture site over real HTTP, then check search results (Phase 7) and, through the HTTP API, the pages, links, broken links, link graph, statistics and search of the demo site (Phase 8, `tests/fault/test_api_end_to_end.py`) | full stack |
| Frontend (Phase 9) | the API client (URLs, problems, timeouts, cancellation), Unicode highlight ranges, the polling rules and their lifecycle on fake timers, the job form (policy checks, idempotent retries), cancellation answers, cursor walks, link classes, the graph layout and node details, the shell; against a fake API (a test double of `fetch`); type check, lint (with accessibility rules), production build | Node.js |
| Dashboard end to end (Phase 9) | `tests/dashboard_stack.py --e2e`: the real API and processes on a disposable database, the demo site and a slow site; Playwright in the installed Edge creates a job in the form, follows it to fully indexed, searches, reads the analysis and the graph, cancels a running crawl, and checks that no page scrolls sideways | PostgreSQL, Redis, Node.js, Edge |

Tests that need unavailable services skip with an explicit reason. Benchmarks are kept separate from
tests: fixed parameters, a recorded environment, and raw output committed to the repository
(`benchmarks/`; the first one in Phase 5, the full set in Phase 10). The concurrency row above was
planned with an in-memory site; Phase 5 uses real processes and local HTTP servers instead. Phase 6
runs `FLUSHALL` with real processes, against a disposable Redis container only: it is never sent to
the development Redis, whose test database 15 would not isolate it.

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
  Windows host and the WSL VM clocks were observed about 160 ms apart, and in Phase 4 the WSL VM
  clock (the clock of PostgreSQL and Redis) stepped back about one second during a test run.
  Clocks can step: job timestamps are therefore clamped to `created_at` (ADR-0014), and the
  politeness script turns a backward step into at most one interval of extra wait (ADR-0015).
  Leases are safe either way: a backward step lengthens them, and a forward step shortens them,
  which can cause an early takeover, but fencing still rejects the old holder's result.
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
- A retryable failure is retried at most `SITESCOPE_CRAWL_MAX_ATTEMPTS - 1` times; a server that
  asks for a `Retry-After` beyond the cap (300 s) is not retried (Phase 6).
- An unreachable robots.txt disallows its origin for the retry time (300 s), and again after each
  failed retry. A job whose seed is affected fails. `robots_unreachable` is not retried per URL: the
  origin's cached result would mostly answer the same.
- `noindex` pages are stored (without a search document); only search leaves them out.
- Separate processes may each fetch an expired robots.txt once (those fetches still pass the
  politeness gate).
- Politeness is per host name: two names served by one machine are limited separately. A host's
  crawl delay is the largest `Crawl-delay` of its origins' robots.txt; a lower one takes effect
  only when the stored value expires (at most 24 hours).
- A task may need two fetches, robots.txt and then the page. If both are slow, the task deadline
  (lease minus 10 s) can end it first, as `deadline_exceeded`, which is retried (Phase 6).
- The single-process driver stops on a PostgreSQL or Redis error. The next run continues once the
  stopped run's leases expire.
- A worker that dies, or loses Redis, while it holds a politeness slot leaves the slot held until its
  lease ends (15 s by default since Phase 6; 70 s before). A holder frozen for longer than a lease
  loses its slot and may overlap with one other request to its host when it resumes (section 10.4).
- Recovery is deliberately unhurried with the defaults: a crashed worker's tasks resume after the
  reclaim idle time (100 s), a lost message after the reconcile time (5 minutes). A transient
  PostgreSQL error before a claim is handled again in the process for a short while, and after that
  the task waits for the reclaim idle time too.
- A cancelled job stays `cancelling` until its in-flight URLs are final: within seconds while their
  workers live, up to one lease when a worker crashed, and until a scheduler (or the single-process
  driver) runs the finalizer.
- Poison messages are recognized by their delivery count only when they are reclaimed; a message
  republished as a new entry starts counting again (`attempt_count` still bounds the work).
- With several schedulers, the backpressure limits (per job, overall) are approximate.
- A graceful stop on Windows needs a console event (Ctrl+C or `CTRL_BREAK_EVENT`). Terminating the
  process instead is safe, but running tasks then wait for their leases to expire.
- HTML only, no JavaScript rendering, and English-only text search.
- HTTP/1.1 only. A response without a `Content-Type` is not sniffed: it is refused as
  unsupported. Only the first member of a multi-member gzip body is read.
- Text hidden only by CSS counts as visible text: the parser does not render pages.
- Charsets are limited to the WHATWG label table; any other declared charset is ignored.
- Basic relevance ranking (no BM25). Search ranks every match before it cuts a page, so its cost
  grows with the number of matching documents (bounded by the search timeout).
- Text search is English-first (Phase 7): other languages are not stemmed, and Chinese, Japanese and
  other languages written without spaces are not segmented into words (a run of characters is one
  token). Positions are capped at 16,383 by PostgreSQL, so proximity counts less near the end of long
  documents. Only the first `SITESCOPE_INDEX_MAX_BODY_CHARS` characters of a body are searchable.
- A page can be indexed more than once (an attempt that outlived its lease and its successor); the
  result is the same. A document fails after `SITESCOPE_INDEX_MAX_ATTEMPTS` attempts that crashed or
  timed out, or when its vector exceeds PostgreSQL's limit even for a quarter of its body; a reindex
  (`request_reindex`, a service function) sets it back to `pending`.
- Indexing recovery is unhurried with the defaults: an abandoned index message resumes after the
  reclaim idle time (60 s), a lost one after the reconcile time (5 minutes).
- Cancelling a job does not stop the indexing of the pages it stored.
- No incremental re-crawl; storage is duplicated across jobs.
- Link analysis reflects the crawl when it ran: a target that answered 404 stays broken for that
  job even if it recovers later, and an unverified link stays unverified (Phase 8). Reasons the
  stored results do not record (the redirect hop limit, a redirect to a URL of the job's host
  that is not an approved target, a job that ended before admitting a level) are reported as
  `not_crawled`. A robots.txt that could not be fetched counts as `robots_blocked`.
- The statistics, the broken links and the filtered links listings read every link or crawl URL
  of their job, so their cost grows with the job (about 0.6 s for the statistics of a job with
  500,000 links on the development machine); the link graph's response can reach about a megabyte
  at its maximum limits; responses are not cached (Phase 8).
- A client that retries job creation without an `Idempotency-Key` creates a second job; keys are
  remembered for 24 hours by default. The dashboard always sends one, and reuses it when it retries
  a submission whose outcome it does not know (Phase 9).
- The dashboard (Phase 9) has no authentication, and job creation is not rate-limited: a public
  deployment needs both (Phase 11). A job list page reads one job detail per row (at most one page
  of rows); the overview counts whole tables under the analysis time limit; listing walks are kept
  in memory (a reload starts from the first page).

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
| [0014](adr/0014-crawl-state-transactions-and-redirects.md) | Crawl state transactions, same-level redirects, and page directives |
| [0015](adr/0015-robots-txt-and-per-host-politeness.md) | robots.txt (RFC 9309) and per-host politeness through Redis |
| [0016](adr/0016-distributed-execution-redis-streams.md) | Distributed execution over Redis Streams: publication, acknowledgment, recovery |
| [0017](adr/0017-retries-dead-letters-cancellation-idempotency.md) | Durable retries, dead letters, cancellation, and idempotent job creation |
| [0018](adr/0018-asynchronous-indexing-pipeline.md) | The asynchronous indexing pipeline: dispatch, counted attempts, versioned writes |
| [0019](adr/0019-website-analysis-from-stored-crawl-results.md) | Website analysis from stored crawl results: link classes, confirmed broken links, read-only APIs |
| [0020](adr/0020-dashboard-same-origin-typed-client.md) | The dashboard: a same-origin client typed from the OpenAPI document, with bounded polling |
