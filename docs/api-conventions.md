# API Conventions

These conventions apply to every SiteScope HTTP endpoint. They were established in Phase 1. The
code lives in `backend/app/core/` and `backend/app/api/`. When a later phase introduces a new
pattern (for example, pagination), it adds that pattern here.

## URLs and versioning

- **Unversioned operational endpoints:** `GET /` (service index) and `GET /health` (liveness probe).
- **Everything else** lives under `/api/v1`.
- **Breaking changes** get a new version prefix. Additive changes do not: new endpoints and new
  optional response fields stay in the current version.

## Requests and responses

- Request and response bodies are JSON, with `snake_case` field names.
- Timestamps are UTC in ISO 8601 / RFC 3339 format with a `Z` suffix, for example
  `2026-11-06T21:13:07.788Z`.
- Every endpoint declares a Pydantic response model, so its schema appears in the OpenAPI
  document.
- Each OpenAPI `operationId` is the handler's function name (for example `get_liveness`). These
  names must be unique, so generated clients (such as the Phase 9 dashboard) get stable method
  names.

## Request IDs

- Every response carries an `X-Request-ID` header.
- A client may send its own `X-Request-ID` of 1–128 characters from `A–Z a–z 0–9 . _ -`, and the
  server reuses it. If the header is missing or contains anything else, the server generates a
  32-character hex ID.
- The same ID appears in every log line written while handling the request, and in the body of
  every error response.

## Errors: RFC 9457 problem details

Every error response has the media type `application/problem+json`. For example:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "Crawl job 019a5f3c-7a10-7d4e-9b2a-3f0c1d2e4b5a does not exist.",
  "instance": "/api/v1/crawls/019a5f3c-7a10-7d4e-9b2a-3f0c1d2e4b5a",
  "code": "not_found",
  "request_id": "1d37623ac12c4f08aa4b1109568bfdde"
}
```

| Member | Meaning |
|---|---|
| `type` | Always `about:blank`. The problem kind is identified by `status` and `code`. |
| `title` | The HTTP status phrase. |
| `status` | The HTTP status code. |
| `detail` | Explanation specific to this occurrence. Omitted when it would only repeat `title`. |
| `instance` | The request path, without the query string. |
| `code` | Stable, machine-readable error code. Clients branch on this. |
| `request_id` | Same value as the `X-Request-ID` header. Quote it when reporting a problem. |
| `errors` | Present only for `validation_failed`. Each entry has `loc`, `msg`, and `type`. |

| HTTP | `code` | When |
|---|---|---|
| 400 | `bad_request` | A generic domain error (`DomainError`) |
| 404 | `not_found` | An unknown route, or a `NotFoundError` from the service layer |
| 405 | `method_not_allowed` | Wrong method. The `Allow` header lists the permitted methods. |
| 409 | `conflict` | A `ConflictError` from the service layer |
| 409 | `idempotency_key_reused` | An `Idempotency-Key` that already created a job for a different request (Phase 6) |
| 422 | `validation_failed` | The request failed validation: its shape (schema), or a server-side rule such as an unapproved crawl target. `errors` lists each field. |
| 500 | `internal_error` | An unexpected error. Details appear only in the server logs, never in the response. |
| 503 | `dependency_unavailable` | PostgreSQL or Redis is unreachable, or rejected the connection. Sent with `Retry-After: 5`. Ordinary SQL errors are never reported this way. |
| 503 | `search_timeout` | A search took longer than `SITESCOPE_SEARCH_TIMEOUT_SECONDS` (5 s by default) and was stopped (Phase 8). A PostgreSQL connection that times out is `dependency_unavailable` instead. |
| 503 | `analysis_timeout` | An analysis request (Phase 8), or the overview (Phase 9), took longer than `SITESCOPE_ANALYSIS_TIMEOUT_SECONDS` (10 s by default) and was stopped. A PostgreSQL connection that times out is `dependency_unavailable` instead. |
| other | status name in `snake_case` | For example, 403 → `forbidden` |

### Validation errors

Schema violations and server-side rule violations look the same: `422 validation_failed`, with
one `errors` entry per field. `loc` names the field as FastAPI does (`["body", "seed_url"]`,
`["query", "cursor"]`), and `type` says what is wrong. Service rules use these `type` values:

| `type` | Field | Meaning |
|---|---|---|
| `invalid_url` | `seed_url` | Not an acceptable absolute http(s) URL; `msg` says why (architecture section 8.1) |
| `target_not_allowed` | `seed_url` | Not an approved crawl target on this server: host, scheme or port (section 8.2) |
| `limit_exceeded` | `max_depth`, `max_pages` | Above this server's configured limit |
| `invalid_cursor` | `cursor` | Not a cursor this listing returned (an altered cursor, or one of another listing) |
| `empty_query` | `q` | Nothing left of the search query after removing whitespace and invisible characters (Phase 8) |
| `query_too_long` | `q` | More than 256 characters after normalization (Phase 8) |
| `page_out_of_range` | `page` | `(page - 1) * page_size` beyond 1000, the deepest offset a search serves (Phase 8) |

Schema violations use Pydantic's types, such as `missing`, `int_type`, `greater_than_equal` or
`extra_forbidden`. A request can fail several rules at once, and all of them are reported:

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "The request failed validation.",
  "instance": "/api/v1/crawls",
  "code": "validation_failed",
  "request_id": "5c0a1d9e2f3b4a6c8d7e9f0a1b2c3d4e",
  "errors": [
    {"loc": ["body", "seed_url"], "msg": "host 'other.example' is not an approved crawl target", "type": "target_not_allowed"},
    {"loc": ["body", "max_depth"], "msg": "max_depth must be at most 5 on this server", "type": "limit_exceeded"}
  ]
}
```

Rules for code that produces errors:

- Services raise `DomainError` subclasses (`app/core/errors.py`) and never build HTTP responses.
  Rule violations on request fields raise `InvalidInputError`, which produces the shape above.
- Unexpected exceptions are logged once, with the request ID and traceback. The client receives
  only the generic 500 problem.

## Pagination

List endpoints use cursor (keyset) pagination:

- `limit`: items per page. Crawl jobs: 1–100, default 20. The analysis listings (pages, links,
  broken links; Phase 8): 1–200, default 50.
- `cursor`: optional; the `next_cursor` value of the previous page of the same listing, with the
  same filters. Cursors are opaque. Clients must not build or edit them, and an invalid cursor
  (altered, or another listing's) is rejected with `invalid_cursor`. A cursor holds the sort key
  of the last item; for the analysis listings that includes URLs the client has already
  received, so a cursor can be several kilobytes long (at most 8,192 characters).
- The response is `{"items": [...], "next_cursor": "..."}`. `next_cursor` is `null` on the last
  page.
- The order is total and stable: crawl jobs newest first (ties by ID); pages by BFS depth, then
  URL; links by source page URL, position on the page, then target URL; broken links by target
  URL, then source page URL. URLs compare byte by byte. A walk through the pages never returns an
  item twice, and returns every item that existed when the walk began. Items created meanwhile
  do not shift later pages; while a crawl is still running, new items may sort before or after
  the cursor. A filter on a value that changes while the crawl or indexing runs
  (`classification`, `verified`, `index_status`, ...) is applied to each page when it is read:
  an item that changes during a walk can enter or leave the filter.

**Search is the exception** (Phase 8). Its results are ranked by relevance, so it pages with
`page` (1-based) and `page_size` (1–50, default 10), and serves results up to offset 1000:
any page with `(page - 1) * page_size` above 1000 is refused with `page_out_of_range`, together
with a query error if the query is invalid too (every match is ranked before a page is cut, so
deeper pages would cost as much as the whole result set). A page beyond
the last result is empty, with the real `total`. Pages are consistent while the index does not
change; a search during indexing may find more on a later request.

## Crawl jobs

| Endpoint | Success | Errors |
|---|---|---|
| `POST /api/v1/crawls` | `201`, with a `Location` header pointing to the new job; `200` for a replayed `Idempotency-Key` | `409`, `422`, `503` |
| `GET /api/v1/crawls` | `200`: a page of jobs, newest first | `422`, `503` |
| `GET /api/v1/crawls/{job_id}` | `200`: the job and its progress | `404`, `422` (malformed ID), `503` |
| `POST /api/v1/crawls/{job_id}/cancel` | `202` while the job is `cancelling`; `200` once it is `cancelled`, or if it was already finished | `404`, `422` (malformed ID), `503` |
| `GET /api/v1/crawl-policy` | `200`: the approved targets and job limits (Phase 9) | none |

Creating a job:

```json
{"seed_url": "https://docs.example.com/", "max_depth": 3, "max_pages": 500}
```

- All three fields are required. Unknown fields are rejected, and the numbers must be JSON
  integers (`"3"`, `3.0` and `true` are rejected).
- `seed_url` must normalize to an approved crawl target. The stored `seed_url` is the normalized
  form, and `allowed_hosts` holds its host.
- `max_depth` and `max_pages` must not exceed the server's limits (`SITESCOPE_CRAWL_MAX_DEPTH_LIMIT`,
  `SITESCOPE_CRAWL_MAX_PAGES_LIMIT`).
- A new job is `queued`. It stays `queued` until the scheduler (Phase 5) starts it. The API never
  reports a job as running when nothing runs it.

**Idempotent creation** (Phase 6). The optional `Idempotency-Key` request header makes a retried
creation safe:

- The key is 1 to 255 visible ASCII characters (`!` to `~`, no spaces); anything else is a `422`
  (`validation_failed`, `loc` `["header", "Idempotency-Key"]`). A random UUID per intended job is
  a good key.
- The first request with a key creates the job (`201`). A later request with the same key and an
  equivalent body (the same seed URL after normalization, the same `max_depth` and `max_pages`)
  creates nothing and returns that job: `200`, the same `Location`, and the header
  `Idempotent-Replayed: true`. Concurrent requests with one key create one job.
- The same key with a different body is refused: `409` with code `idempotency_key_reused`. The key
  is not echoed in the error.
- Keys are stored in PostgreSQL with the job, in its transaction, and remembered for
  `SITESCOPE_IDEMPOTENCY_KEY_TTL_SECONDS` (24 hours by default). After that the key may create a
  new job; the old job is unchanged.
- Without the header, every request creates a new job, as before.

**Cancelling a job** (Phase 6). `POST /api/v1/crawls/{job_id}/cancel` has no body and returns the
job summary:

- A `queued` job is `cancelled` at once (`200`).
- A `running` job stops dispatching and claiming, and its waiting URLs are cancelled at once. If
  none of its URLs is being crawled, it is `cancelled` at once (`200`); otherwise it is
  `cancelling` (`202`) until the URLs in flight are final (normally within seconds; up to one lease
  duration if a worker crashed), and then `cancelled`.
- Cancelling again, or cancelling a `completed`, `failed` or `cancelled` job, changes nothing and
  returns the job (`202` while it is still `cancelling`, `200` otherwise).
- A cancelled URL has the state `cancelled` and the outcome `job_cancelled`. Pages already stored
  are kept.

The job detail adds `progress`, counted from the job's URL rows when the request is served:
`urls_total`, `urls_finished`, `urls_by_state` with a count for each of the seven URL states, and
(since Phase 8) `pages_stored`, the HTML pages stored. `status`, `started_at` and `finished_at`
describe the crawl only; its indexing has its own status and end, below.

Since Phase 7 it also adds `indexing`, counted from the job's search documents in the same
statement:

```json
"indexing": {
  "status": "running",
  "documents_total": 12,
  "documents_by_status": {"pending": 2, "queued": 3, "indexed": 7, "failed": 0},
  "active": 1,
  "completed_at": null
}
```

- `documents_total`: the pages to index, every stored page except `noindex` ones.
- `active`: documents an indexer is working on now.
- `status`: `pending` (nothing published or indexed yet), `running` (under way, or waiting for the
  crawl or the index dispatcher), `indexed` (the job is finished and every page is indexed) or
  `failed` (the job is finished, indexing is over, and at least one page could not be indexed; the
  others are searchable). It never changes the job's own `status`: a job is fully complete when
  its `status` is `completed` and its indexing `status` is `indexed`.
- `completed_at`: when the indexing of the finished job was found complete; `null` until then.

**The crawl policy** (Phase 9). `GET /api/v1/crawl-policy` returns what `POST /api/v1/crawls`
accepts on this server, read from its settings; nothing is resolved or fetched. The dashboard's
job form uses it to offer the approved targets and to check the limits before it sends:

```json
{
  "approved_hosts": ["docs.example.com", "*.example.org"],
  "allowed_schemes": ["https"],
  "allowed_ports": [443],
  "max_depth_limit": 5,
  "max_pages_limit": 1000
}
```

- `approved_hosts` are the normalized patterns of `SITESCOPE_CRAWL_ALLOWED_HOSTS` (ADR-0012):
  `docs.example.com` approves exactly that host, `*.example.org` its subdomains but not
  `example.org`. An empty list approves nothing.
- `allowed_schemes` and `allowed_ports` apply to every approved host; a URL's port is its explicit
  port or its scheme's default.
- It is guidance for clients: the server checks every creation request again. Internal fetch
  origins (`SITESCOPE_FETCH_INTERNAL_ORIGINS`) and every other setting stay private.

## Search (Phase 8)

`GET /api/v1/search?job_id=<uuid>&q=<query>[&page=1][&page_size=10]` searches the indexed pages
of one crawl job with the Phase 7 search service (PostgreSQL full-text search, architecture
section 13). Errors: `404` (unknown job), `422` (the validation types above, plus schema rules
such as `uuid_parsing` or `missing`), `503` (`search_timeout`, `dependency_unavailable`).

- `q` uses web-search syntax: words (all must match), `"quoted phrases"`, `or`, and `-word` to
  exclude a word. The operators of PostgreSQL's own query syntax (`&`, `|`, `!`, `<->`, `:*`) are
  plain text; no input is a syntax error. Words match by their English stem (`crawling` finds
  `crawled`), and English stop words are ignored.
- A query with no searchable term (only stop words, or only excluded words) is valid: the response
  has `"searchable": false`, `total` 0 and no results.
- Only pages whose current content is indexed are found (the visibility rule of ADR-0018). Pages
  not indexed yet, `noindex` pages and pages of other jobs are never returned, and nothing falls
  back to unindexed text. `indexing` (the job's indexing progress, in the same snapshot) tells
  whether more pages may become searchable.
- Results are ordered by `score` (`ts_rank`), then URL, then page ID. A `score` is only meaningful
  within one search.
- `snippet` is plain text, never HTML. `highlights` are `[start, end)` ranges of Unicode code
  points in `snippet` (in JavaScript, index `Array.from(snippet)`, not the string).
- The query is never logged: request logs record the path without the query string.

```json
{
  "query": "consumer groups",
  "job_id": "0199a1b2-7c3d-7e4f-8a9b-0c1d2e3f4a5b",
  "searchable": true,
  "total": 1,
  "page": 1,
  "page_size": 10,
  "results": [
    {
      "page_id": "0199a1b2-8d4e-7f50-9b0c-1d2e3f4a5b6c",
      "url": "https://docs.example.com/streams.html",
      "title": "Redis Streams",
      "snippet": "Consumer groups let several workers share one stream",
      "highlights": [{"start": 0, "end": 8}, {"start": 9, "end": 15}],
      "score": 0.6079271,
      "crawled_at": "2026-11-07T10:15:02.418Z",
      "indexed_at": "2026-11-07T10:15:03.977Z"
    }
  ],
  "indexing": {
    "status": "indexed",
    "documents_total": 11,
    "documents_by_status": {"pending": 0, "queued": 0, "indexed": 11, "failed": 0},
    "active": 0,
    "completed_at": "2026-11-07T10:15:05.120Z"
  }
}
```

## Website analysis (Phase 8)

Read-only endpoints under `/api/v1/crawls/{job_id}`. They read the job's stored crawl results in
PostgreSQL and never send a request to a crawl target (ADR-0019). Each response comes from one
read-only REPEATABLE READ transaction, so its lists and counts agree with each other. Every
endpoint answers `404` for an unknown job, `422` for invalid parameters, and `503` with
`analysis_timeout` (or `dependency_unavailable`).

| Endpoint | Returns | Parameters |
|---|---|---|
| `GET .../pages` | the job's stored pages, by depth then URL; never their bodies | `limit`, `cursor`, `depth`, `index_status` |
| `GET .../links` | the job's stored links, with their classification | `limit`, `cursor`, `source_page_id`, `internal`, `classification`, `verified`, `redirected` |
| `GET .../broken-links` | the confirmed broken links, with `total` | `limit`, `cursor` |
| `GET .../link-graph` | the internal link graph | `max_nodes` (1–2000, default 500), `max_edges` (0–10000, default 2000) |
| `GET .../stats` | crawl, HTTP status, depth, indexing and link statistics | none |

### Terms

| Term | Meaning |
|---|---|
| discovered URL | a URL the job knows: admitted, or the target of an internal link on a stored page (`urls_discovered`) |
| admitted URL | a URL the crawl took on: a `crawl_urls` row (`urls_admitted`, the job's `admitted_count`) |
| page | a fetched 2xx HTML response the job stored; redirects, errors, other content types and skipped URLs have no page |
| link | one stored link from a page to a target URL; repeated links to one target on a page are stored once, at their first position |
| internal link | a link whose target is in the job's scope (its hosts, and an approved crawl target); external links are recorded, never crawled |
| document | a page's search-index entry; `noindex` pages have none |
| verified link | a link an HTTP response classifies: `ok`, `broken`, `server_error`, `http_error`, `redirect_error` |
| unverified link | every other link: never requested, or no response |
| broken link | an internal link whose target, after its redirects, answered 404 or 410 during the crawl |

### Link classification

Each link gets exactly one class from the stored crawl result of its target in the same job. A
target that redirected is judged where its redirect chain ends, following the job's own crawl
results (never fetched again). A URL waiting for a retry keeps its last attempt's status, which
decides nothing until its result is final.

| Class | Verified | When |
|---|---|---|
| `ok` | yes | the final response was 2xx (also a 2xx that was not HTML, too large, or not parsed) |
| `broken` | yes | the final response was 404 or 410 |
| `server_error` | yes | the final response was 5xx, 408 or 429, after the crawl's retries: possibly transient |
| `http_error` | yes | any other final status, such as 401 or 403: refused, not proven missing |
| `redirect_error` | yes | a redirect loop, more than 20 redirects, or a 3xx without a usable `Location` |
| `out_of_scope` | no | an external link, a target skipped as not approved or as a blocked destination, or a redirect that leaves the job's hosts |
| `not_followed` | no | never admitted: the link is `rel="nofollow"`, or its page is `nofollow` |
| `beyond_limits` | no | never admitted (the target, or the end of its redirects): one level beyond `max_depth`, or the page budget was used up |
| `not_crawled` | no | not requested (yet): pending, queued, in progress or cancelled, or never admitted for a reason the stored results do not record |
| `robots_blocked` | no | robots.txt disallowed the target, or could not be fetched |
| `unreachable` | no | the request ended without a response: DNS, connection, TLS or protocol errors, timeouts, attempts used up |

The links listing gives each report of a link checker: `classification=broken` (or the
broken-links endpoint), `redirected=true` (targets that answered with a 3xx; their chain's end is
in `final_url`), `verified=false` (unchecked: never requested, or no response) and
`internal=false` (external links, never requested).

**Confirmed broken links** are exactly the `broken` class: one item per stored link (source page,
target URL), ordered by target URL, then source page URL. A page that links twice to a missing URL
is one item; a missing URL linked from three pages is three items. A 5xx, a 401 or 403, a target
the crawl never requested, and a request without a response are never reported as broken; the
links listing reports them by class.

```json
{
  "total": 1,
  "items": [
    {
      "source_page_id": "0199a1b2-8d4e-7f50-9b0c-1d2e3f4a5b6c",
      "source_url": "https://docs.example.com/about.html",
      "anchor_text": "Our old team page",
      "position": 4,
      "target_url": "https://docs.example.com/missing.html",
      "status": 404,
      "final_url": null,
      "redirect_hops": 0,
      "target_http_status": 404,
      "checked_at": "2026-11-07T10:15:04.551Z"
    }
  ],
  "next_cursor": null
}
```

### Link graph

Nodes are every admitted URL and every target of an internal link, in BFS order (depth, then URL;
never-admitted targets last). `kind` is `page` (a stored page), `crawl_url` (admitted, without a
page) or `link_target` (never admitted, never requested). Edges are the stored internal links
between returned nodes; `source` and `target` are node `id`s. Redirects are not edges.
`in_degree` and `out_degree` count every stored internal link, also when nodes or edges were cut;
`truncated` is true when the graph was cut at `max_nodes` or `max_edges`. Only links on stored
pages are known, so an `in_degree` of 0 never proves that nothing links to a URL.

### Statistics

- `crawl`: `urls_discovered`, `urls_admitted`, `urls_by_state`, `pages_stored`, `current_depth`,
  `max_depth`, `max_pages`.
- `http_status`: the final status of every URL whose request ended (state `fetched` or `failed`),
  each counted once with its own status: 1xx (rare), 2xx, 3xx (redirects), 4xx, 5xx, and
  `no_response`. The six numbers add up to those URLs.
- `depths`: per BFS depth, the admitted URLs, those in state `fetched`, and the stored pages.
- `indexing`: as in the job detail.
- `links`: all links, internal and external links, distinct internal targets, `broken`,
  `verified`, `unverified`, and the count of each class.
- `most_linked`: the 10 URLs with the most stored internal links to them (one per linking page),
  most first, then by URL.

## Overview (Phase 9)

`GET /api/v1/overview` counts every crawl job by status, every admitted URL by state, the stored
pages, and the search documents by status, in one read-only REPEATABLE READ snapshot, so the
numbers agree with each other. It takes no parameters. Errors: `503` with `analysis_timeout` (the
same time limit as the analysis endpoints) or `dependency_unavailable`.

```json
{
  "jobs": {
    "total": 4,
    "by_status": {"queued": 0, "running": 1, "cancelling": 0, "completed": 2, "failed": 0, "cancelled": 1}
  },
  "urls": {
    "total": 52,
    "by_state": {"pending": 3, "queued": 2, "in_progress": 1, "fetched": 40, "failed": 1, "skipped": 2, "cancelled": 3}
  },
  "pages_stored": 36,
  "documents": {
    "total": 34,
    "by_status": {"pending": 1, "queued": 2, "indexed": 31, "failed": 0},
    "active": 1
  },
  "counted_at": "2026-11-07T10:15:05.120Z"
}
```

- `jobs.by_status` is the crawl's status of each job; indexing is counted in `documents`.
- `urls.total` counts the URLs every job admitted, seeds included (the sum of `admitted_count`).
  Link targets a job never admitted are not counted: discovered URLs are a per-job statistic
  (`crawl.urls_discovered` of `GET /api/v1/crawls/{job_id}/stats`).
- `documents`: one per stored page except `noindex` pages; `active` counts those an indexer is
  working on now.
- `counted_at` is when the snapshot was read, by the database clock.
- Each count reads its whole table (`crawl_jobs`, `crawl_urls`, `pages`, `search_documents`), so
  the time grows with the deployment's data. Nothing is cached or estimated.

The job listing (`GET /api/v1/crawls`) still has no totals: a client must not add up a page of
jobs and call it a total.

## Health endpoints

| Endpoint | Purpose | Checks dependencies? |
|---|---|---|
| `GET /health` | Liveness probe for load balancers and container health checks | No |
| `GET /api/v1/health/live` | Liveness with service name, version, and uptime | No |
| `GET /api/v1/health/ready` | Readiness: runs `SELECT 1` on PostgreSQL and `PING` on Redis, concurrently, each within `SITESCOPE_READINESS_TIMEOUT_SECONDS` (default 2 s) | Yes |

Liveness means only that the process is serving requests. It never reports anything about
PostgreSQL or Redis. An outage of either therefore never makes an orchestrator restart a healthy
API process.

Readiness answers `200` when both dependencies respond, and `503` otherwise. It is the one
endpoint that answers `503` with its own schema instead of problem+json, so that probes and
dashboards parse a single shape:

```json
{
  "status": "not_ready",
  "checks": {
    "postgres": {"status": "ok", "latency_ms": 4.11},
    "redis": {"status": "unavailable", "latency_ms": 0.39}
  },
  "checked_at": "2026-11-06T23:53:41.147208Z"
}
```

- Each check's `status` is `ok`, `unavailable` (refused, rejected credentials, …) or `timeout`.
- The body never contains hosts, ports, database names, users, passwords or exception text. Those
  details go to the server log, which records only the exception type.
- Responses carry `Cache-Control: no-store`.

## Documentation endpoints

| Format | Path |
|---|---|
| Swagger UI | `/docs` |
| ReDoc | `/redoc` |
| OpenAPI 3.1 JSON | `/openapi.json` |

Error responses are documented with the `ProblemDetails` schema instead of FastAPI's default
validation schema. FastAPI lists that schema under `application/json`; the actual responses use
`application/problem+json`.

The dashboard's TypeScript types are generated from this document (Phase 9): a snapshot is
committed as `frontend/src/lib/api/openapi.json` (`python -m app.api.openapi_export`), and the
unit test `tests/unit/api/test_openapi_snapshot.py` fails while it differs from what the API
serves.
