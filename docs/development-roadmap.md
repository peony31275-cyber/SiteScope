# Development Roadmap

SiteScope is built one phase at a time. Each phase ends with working, tested code and an honest report.
The next phase starts only when the project owner asks for it. For the design, see
[`architecture.md`](architecture.md).

## Phase status

| Phase | Title | Status |
|---|---|---|
| 0 | Repository inspection and architecture baseline | **Done** (2026-11-06) |
| 1 | Backend foundation | **Done** (2026-11-06) |
| 2A | Docker environment (WSL 2) | **Done** (2026-11-06) |
| 2B | PostgreSQL, Redis, and database infrastructure | **Done** (2026-11-06) |
| 2C | Crawl job API, URL normalization, and transactional job creation | **Done** (2026-11-06) |
| 3 | Safe HTTP fetching and HTML parsing | **Done** (2026-11-06) |
| 4 | BFS crawling, persistence and politeness | **Done** (2026-11-06) |
| 5 | Distributed scheduling and workers | **Done** (2026-11-06) |
| 6 | Fault tolerance and recovery | **Done** (2026-11-07) |
| 7 | Asynchronous search indexing | **Done** (2026-11-08) |
| 8 | Search and analysis APIs | **Done** (2026-11-08) |
| 9 | React dashboard | **Done** (2026-11-08) |
| 10 | Performance engineering, reliability validation and observability | **Done** (2026-11-08) |
| 11 | Final deployment and documentation | Not started |

Phases 3–11 were renumbered in Phase 2C at the owner's request. Earlier reports and commits use
the old numbers:

| Old phase | Now |
|---|---|
| 3 Task distribution core | 4 (BFS crawl logic) and 5 (scheduler, Redis Streams, workers) |
| 4 Fault tolerance and recovery | 6 |
| 5 Safe HTTP crawling | 3 |
| 6 Indexing pipeline and search API | 7 (indexing) and 8 (search API) |
| 7 Link analysis | 8 |
| 8 React + TypeScript dashboard | 9 |
| 9 Docker Compose deployment and demo site | 11 (the first demo-site content arrives in 3) |
| 10 Benchmarks | 10 |
| 11 CI hardening and documentation | 11 |

## Repository state after Phase 10

Updated on 2026-11-08 (Phase 10). The Phase 0 inspection findings are kept in this file's first commit.

| Item | State |
|---|---|
| Git | Local repository on branch `master` (the configured default). Every phase so far is committed. No remote yet. |
| `backend/app/` | FastAPI foundation (Phase 1); database models, sessions, Redis client, readiness (Phase 2B); crawl job API, URL normalization and crawl scope (Phase 2C); safe fetcher and HTML parser in `app/crawler/` (Phase 3); crawl state in `app/services/crawl_state.py`, robots.txt, politeness and page directives in `app/crawler/`, the crawl task and the single-process driver in `app/workers/` (Phase 4); the Redis Streams adapter in `app/queue/`, the scheduler and its status command in `app/scheduler/`, the crawler worker in `app/workers/crawler.py` (Phase 5); the retry policy in `app/workers/retry.py`, `Retry-After` parsing in `app/crawler/retry_after.py`, cancellation in `app/services/crawl_cancellation.py`, idempotent creation in `app/services/crawl_jobs.py` (Phase 6); index state and search in `app/services/search_index.py` and `app/services/search.py`, document and query rules in `app/indexing/`, the index dispatcher, the shared pass loop and the search command in `app/scheduler/`, the shared consumer loop and the indexer in `app/workers/` (Phase 7); the search and website-analysis endpoints in `app/api/v1/search.py` and `app/api/v1/analysis.py`, the analysis queries in `app/services/site_analysis.py` (Phase 8); the crawl policy and overview endpoints in `app/api/v1/crawl_policy.py` and `app/api/v1/overview.py`, the overview counts in `app/services/overview.py`, the OpenAPI export in `app/api/openapi_export.py` (Phase 9); the metrics registry, exposition format and heartbeats in `app/observability/`, `GET /metrics` in `app/api/metrics.py`, its PostgreSQL and Redis reads in `app/services/system_metrics.py`, the health assessment in `app/services/diagnostics.py` (Phase 10). |
| `backend/alembic/` | Migrations `0001` (the six-table schema), `0002` (fencing tokens issued only by claims), `0003` (robots directive flags) `0004` (index version triggers, index leases, indexes for indexing completion; Phase 7) and `0005` (two indexes widened for the analysis queries; Phase 8). |
| `backend/compose.yaml` | PostgreSQL 18.6 and Redis 8.10.3 for development, running in WSL. |
| `backend/tests/` | 2082 tests: 1448 unit, 634 integration and fault (real PostgreSQL and Redis, local HTTP/TLS servers on 127.0.0.1, real scheduler, crawler worker, index dispatcher and indexer processes, and a disposable Redis container); `tests/dashboard_stack.py` runs the whole stack for the dashboard's end-to-end tests (Phase 9). |
| `benchmarks/` | The distributed crawl throughput benchmark (Phase 5), the indexing and search benchmark (Phase 7), the analysis latency benchmark with its index A/B (Phase 8), and the Phase 10 harness (crawl scaling, indexing, search, database statements, stale statistics, recovery, the dashboard's graph, charts), with their raw results. |
| `backend/.venv` | Python 3.13.7. Runtime: fastapi 0.162.0, uvicorn 0.54.0 (standard extras), pydantic-settings 2.16.2, SQLAlchemy 2.1.8, asyncpg 0.32.0, Alembic 1.20.1, redis 8.1.0, idna 3.20, httpcore2 2.16.0, truststore 0.10.5, beautifulsoup4 4.15.0. Development: pytest 9.2.0, httpx2 2.16.0, ruff 0.17.4, mypy 2.4.1, trustme 1.2.1. |
| `frontend/` | The React dashboard (Phase 9): Vite, React 19, TypeScript, Tailwind CSS, React Router, TanStack Query, Recharts; 167 Vitest tests; Playwright end-to-end specs for the real stack. Renamed from the misspelled `fronted/` in Phase 1. |
| `docs/` | Architecture, roadmap, ADRs 0001–0023, API conventions, development guide, performance (Phase 10), observability (Phase 10). |
| `demo-site/` | The demo site: 14 static HTML pages and a robots.txt (Phase 4). |

| Tool on the development machine | Status |
|---|---|
| Python 3.13.7 (`backend/.venv`) | available |
| Git 2.52 | available |
| Node.js 24.14, npm 11.9 | available |
| WSL 3.0.3 with Ubuntu 24.04.3 LTS (WSL 2, systemd enabled) | available |
| Docker Engine 29.9.3 + Docker Compose v5.7.0, inside WSL Ubuntu | installed and verified (2026-11-06) |
| PostgreSQL 18.6, Redis 8.10.3 | Docker containers inside WSL (`backend/compose.yaml`, Phase 2B) |

## Environment setup (done, 2026-11-06)

**Decision.** Docker Engine was installed inside the WSL 2 Ubuntu distribution, from Docker's official
apt repository, together with the official Compose plugin. Docker Desktop was not used, for two
reasons:

- Docker supports Docker Desktop only on Windows versions that Microsoft still services, and
  Windows 10 servicing has ended.
- Docker Engine inside WSL also avoids a second VM and a GUI application.

**Prerequisites checked before installing**

- Windows 10 Home 22H2, build 19045.6466
- WSL 3.0.3
- Ubuntu 24.04.3 LTS, a release Docker supports
- systemd already running as PID 1
- no existing Docker, containerd, runc, or Podman
- 230 GB free on `C:`
- Docker's apt repository and Docker Hub both reachable

**Verified as the normal user, without sudo**

| Check | Result |
|---|---|
| `docker --version` | Docker version 29.9.3, build 5457199 |
| `docker compose version` | Docker Compose version v5.7.0 |
| `docker info` | logging driver `local`, systemd cgroup driver (cgroup v2), no warnings |
| `docker run --rm hello-world` | succeeded; the image was pulled from Docker Hub |
| Port published on `127.0.0.1` inside WSL | reachable from Windows at `127.0.0.1` and `localhost` |

**Limitation that shapes the workflow.** WSL stops Ubuntu about 15–30 s after the last WSL session
closes, and Docker and every container stop with it. Keep a WSL session open while developing.
Services with `restart: unless-stopped` come back automatically when Ubuntu starts again.

Commands, workflow, and troubleshooting are in [`development.md`](development.md#docker-wsl-2).

## Phases

Every phase follows the working agreement in [`CLAUDE.md`](../CLAUDE.md):

1. Inspect.
2. Plan.
3. Implement only the current phase.
4. Test and run the checks.
5. Fix what broke.
6. Report honestly.
7. Update this file, then stop.

### Phase 0: Repository inspection and architecture baseline (done)

- Inspected the repository and the toolchain. The findings are in this file's Phase 0 version,
  commit `a5f8936`.
- Added `CLAUDE.md`, [`docs/architecture.md`](architecture.md), this roadmap, and ADRs 0001–0007 in
  [`docs/adr/`](adr/README.md).
- This phase produced documentation only, so there was no code to test.

### Phase 1: Backend foundation (done)

**Goal.** A runnable, tested FastAPI service skeleton containing the cross-cutting pieces every later
phase relies on. No database yet.

**Delivered**

- **Repository preparation:**
  - renamed `fronted/` to `frontend/`;
  - added `.gitignore` and `.gitattributes`;
  - initialized a local git repository, with the approved Phase 0 docs as the first commit.
- `backend/pyproject.toml`: runtime dependencies, a `dev` extra, and ruff, mypy, and pytest
  configuration. The project is installed in editable mode.
- `app/main.py`: the application factory, a lifespan handler (start time, startup and shutdown events),
  and the module-level ASGI app.
- `app/core/config.py`: `Settings` built on pydantic-settings, with the `SITESCOPE_` prefix and an
  optional `backend/.env`. Settings are immutable, and the process fails fast on invalid values or
  unknown keys.
- `app/core/logging.py`: JSON log lines on stdout with request-ID context. Uvicorn's own logs go
  through the same handler.
- `app/core/middleware.py`:
  - assigns request IDs;
  - writes one access-log line per request, without query strings;
  - returns a last-resort problem+json 500.
- `app/core/errors.py`: the `DomainError` hierarchy, and RFC 9457 problem responses for domain errors,
  HTTP errors, and validation errors.
- **Endpoints:** `GET /`, `GET /health`, and `GET /api/v1/health/live`. OpenAPI is served at
  `/openapi.json`, `/docs`, and `/redoc`.
- **Docs:** [`api-conventions.md`](api-conventions.md) and [`development.md`](development.md).

**Verification** (2026-11-06; Windows 10, Python 3.13.7)

- pytest: 51 passed. These are unit tests and involve no PostgreSQL or Redis.
- `ruff check`: no findings. `ruff format --check`: all files formatted.
- mypy (strict, over `app` and `tests`): no issues.
- Uvicorn, started with `uvicorn app.main:app`, answered every endpoint, `/docs`, `/redoc`, and
  `/openapi.json` over HTTP. The endpoints were re-checked from Windows PowerShell with
  `Invoke-RestMethod`.

**Deviations from the plan**

- **CI workflow:** deferred until a GitHub remote exists, because it cannot be verified before then.
  At the owner's request, no remote has been created yet.
- **`.env.example` location:** it lives in `backend/`, next to the code that reads it, rather than at
  the repository root.
- **Readiness endpoint path:** the endpoint planned for Phase 2 moves from `GET /ready` to
  `GET /api/v1/health/ready`, beside the new versioned liveness endpoint.

### Phase 2A: Docker environment in WSL 2 (done)

Docker Engine 29.9.3 and Compose v5.7.0 run inside WSL Ubuntu 24.04 and have been verified. See
"Environment setup" above and [development.md](development.md#docker-wsl-2).

### Phase 2B: PostgreSQL, Redis, and database infrastructure (done)

**Goal.** A reliable database and infrastructure foundation: the services, the schema and its
migrations, connection management, readiness, and integration tests. No crawling, scheduling or
workers.

**Delivered**

- **Infrastructure:** `backend/compose.yaml`.
  - Official, pinned images: PostgreSQL 18.6 and Redis 8.10.3.
  - Named volumes; Redis persists with AOF (`everysec`) and requires a password.
  - Health checks, `restart: unless-stopped`, ports bound to `127.0.0.1` only, a dedicated network.
  - Credentials come from `backend/.env`, which the backend reads too.
- **Configuration:**
  - `SITESCOPE_POSTGRES_*`, `SITESCOPE_DB_*` (pool), `SITESCOPE_REDIS_*`,
    `SITESCOPE_READINESS_TIMEOUT_SECONDS`.
  - Passwords are secrets, and are mandatory in production, where template placeholders are
    rejected.
- **PostgreSQL access:** `app/db/session.py`.
  - An async engine with a connection pool (pre-ping, recycle) and an asyncpg connect timeout and
    command timeout.
  - Per-request sessions (`get_session`), per-operation units of work (`Database.transaction()`),
    `ping()` and `dispose()`.
- **Redis access:** `app/core/redis.py`. One pooled async client per process, connecting lazily,
  with redis-py's hidden retries disabled.
- **Schema:** migration `0001`, mirrored by `app/db/models.py`.
  - Six tables: `crawl_jobs`, `crawl_urls`, `pages`, `page_links`, `search_documents`,
    `crawl_events`.
  - 52 CHECK constraints and 7 foreign keys; every reference between job-scoped tables is a
    composite key that includes `job_id`.
  - 27 indexes, including partial and GIN indexes; 6 triggers from 3 functions.
  - See architecture section 5.
- **Lifecycle:** the lifespan creates and closes both clients. The application starts even while
  a dependency is down: liveness stays up, and readiness reports the outage.
- **Readiness:** `GET /api/v1/health/ready` checks PostgreSQL and Redis concurrently under a
  deadline. It answers 200 or 503 with the same schema, and never exposes connection details.
- **Errors:** connection failures on the request path become `503 dependency_unavailable`
  (problem+json, `Retry-After`). Ordinary SQL errors still become 500.
- **Decisions:** ADR-0008 (indexing outbox), ADR-0009 (deduplication key), ADR-0010
  (database-enforced fencing).

**Verification** (2026-11-06; Windows 10, Python 3.13.7, services in WSL)

- `pytest --require-services`: 165 passed. That is 89 unit tests and 76 integration tests against
  PostgreSQL 18.6 and Redis 8.10.3. The integration tests cover:
  - connectivity, persistence, transactions and rollback, session lifecycle;
  - recovery from a connection the server killed mid-transaction;
  - every constraint family, job isolation, cascades;
  - leases and fencing, including a stale worker rejected by the database;
  - concurrent discovery, competing claims, the page budget;
  - migrations and Redis;
  - readiness success, and failure with wrong passwords, closed ports and a hanging server.
- `ruff check` and `ruff format --check`: clean. `mypy` strict over `app`, `tests` and `alembic`
  (45 files): no issues.
- Alembic:
  - `upgrade head` on the development database gives `0001 (head)`; the history is linear.
  - Tested on disposable databases: upgrade from empty, models matching the migrated schema,
    downgrade to base and upgrade again, and an interrupted migration leaving no partial schema.
- `docker compose config --quiet` is valid, and both services are healthy.
- End to end on this machine, with the API running on Windows:
  - readiness 200 with both services up;
  - `docker compose stop redis` gave readiness 503 while liveness stayed 200;
  - after Redis restarted, readiness returned to 200;
  - no passwords appeared in the logs.

**Findings and design adjustments**

- **Scope split.** This phase covers the infrastructure only. The job API and URL normalization
  from the original Phase 2 move to Phase 2C.
- **Indexing outbox (ADR-0008).** `search_documents` replaces the planned `index_outbox`: one
  table holds both the indexing state and the publication state.
- **Deduplication key (ADR-0009).** Deduplication uses `UNIQUE (job_id, normalized_url)`, guarded
  by an ASCII and length CHECK, instead of a hash column.
- **Fencing (ADR-0010).** Fencing is enforced by the database: `result_epoch` CHECKs plus a
  monotonic `lease_epoch` trigger.
- **Primary keys.** UUIDv7 keys come from PostgreSQL 18's `uuidv7()`, instead of the bigint
  identity keys sketched in Phase 0.
- **Compose location.** The Compose file lives in `backend/`, so Compose and the backend share one
  `.env` file.
- **redis-py retries.** redis-py retries `ConnectionError` by default, and `AuthenticationError`
  is a subclass of it. A wrong password pushed readiness into a timeout, and an unreachable Redis
  made one request take 27 s. Automatic retries are now disabled.
- **Clock skew.** The Windows host and the WSL VM clocks differed by about 160 ms. A CHECK
  constraint caught a test helper that mixed the two clocks, so comparable timestamps now always
  come from PostgreSQL.
- **Refused connections on Windows.** A refused localhost connection takes about 2 s to fail on
  Windows. The tests and the documentation account for this.

### Phase 2C: Crawl job API, URL normalization, and transactional job creation (done)

**Goal.** Create, inspect and list crawl jobs on top of the Phase 2B schema, with a URL normalizer
and a crawl-scope validator that the crawler will reuse. No crawling, fetching, scheduling, Redis
publication or workers.

**Delivered**

- **URL normalization:** `app/crawler/urls.py`.
  - A pure function, with no network, DNS or database access, reusable by the Phase 3 crawler.
  - It merges only forms that the standards define as equivalent. It keeps the case of the path,
    the query string, and a trailing dot in the host. It rejects credentials, ambiguous numeric
    hosts, control characters and backslashes.
  - IDNA 2008 through the `idna` package, and an output that always satisfies the database URL
    CHECK.
  - The full policy is in architecture section 8.1.
- **Crawl scope:** `app/crawler/scope.py`, decided in ADR-0012.
  - Approved hosts with exact and `*.domain` subdomain matching, plus scheme and port
    restrictions.
  - Empty by default. Development-only entries (localhost, IP literals) are refused in production,
    and some address classes are refused everywhere.
  - Documented as a syntactic check that is not an SSRF defense.
- **Configuration:**
  - `SITESCOPE_CRAWL_ALLOWED_HOSTS`, `_SCHEMES` and `_PORTS` (comma-separated lists), plus
    `SITESCOPE_CRAWL_MAX_DEPTH_LIMIT` and `_MAX_PAGES_LIMIT`, all validated at startup.
  - A test now checks that `.env.example` documents every setting with a valid value.
- **Service:** `app/services/crawl_jobs.py`.
  - One transaction creates the job (`queued`, level 0, `admitted_count` 1) and its depth-0
    `pending` seed URL. Nothing is published to Redis.
  - Reads run in their own short transactions. The job detail is a single SQL statement, so the
    job and its URL counts come from one snapshot.
  - Listing uses keyset pagination on `(created_at, id)`, served by `ix_crawl_jobs_created_at_id`.
- **API:** `app/api/v1/crawls.py` and `crawl_schemas.py`.
  - `POST /api/v1/crawls` answers 201 with a `Location` header. `GET /api/v1/crawls` pages with a
    cursor, at most 100 jobs per page. `GET /api/v1/crawls/{job_id}` reports progress counted from
    the URL rows, and a 404 problem for an unknown job.
  - The request schema is strict: unknown fields and non-integer numbers are rejected.
  - Rule violations are reported like schema errors (`422 validation_failed`, field-level
    `errors`), and the OpenAPI document describes error bodies as `ProblemDetails`.
- **Database fix:** migration `0002` (ADR-0011) adds a trigger that lets `lease_epoch` change only
  in a claim, by exactly one, and requires a new token for every lease grant. See the findings.
- **Phase plan:** Phases 3–11 renumbered to the owner's list (table above).

**Verification** (2026-11-06; Windows 10, Python 3.13.7, services in WSL)

- `pytest --require-services`: 520 passed (417 unit, 103 integration). New tests include:
  - normalization and scope tables, idempotence, the database URL contract, and a fixed-seed fuzz
    run of 3000 inputs;
  - API validation without a database;
  - against PostgreSQL:
    - creation stores both rows in one transaction;
    - an injected failure of the seed insert rolls back the job, both in the service and as a
      generic 500 through the API;
    - rejected requests write nothing;
    - the same seed in several jobs, sequentially and in 8 concurrent creations;
    - URL counts per job;
    - pagination: newest first, stable while jobs are created, ties broken by ID;
    - the listing query plan uses `ix_crawl_jobs_created_at_id`;
    - creation works while Redis is down, and an unreachable PostgreSQL gives a 503 problem.
- `ruff check` and `ruff format --check`: clean. `mypy` strict (57 files): no issues.
- Alembic:
  - the development database was upgraded from `0001` to `0002 (head)`;
  - on disposable databases: a linear history, a step down to `0001` and back, and a downgrade to
    base and upgrade again.
- Fencing gaps:
  - the four new regression tests failed against `0001` (`DID NOT RAISE`) and pass with `0002`;
  - a scenario script on a disposable database showed the stale worker's commit succeeding under
    `0001` and rejected under `0002`.
- Real server (uvicorn on Windows, against the development database):
  - `/health`, `/api/v1/health/live` and `/api/v1/health/ready`: 200.
  - `POST /api/v1/crawls`: 201, with the normalized seed. The detail reports one `pending` URL,
    and pagination works with a cursor.
  - Rejected with 422: an ftp URL, credentials, an unapproved host, the
    `docs.example.com.evil.test` suffix trick, the 169.254.169.254 metadata address, and limits
    above the caps. An unknown job gave 404, a malformed ID and a forged cursor gave 422.
  - `psql` showed exactly the created jobs and seed URLs, written in one transaction each.
  - The logs held no passwords and no rejected URLs.
- `docker compose config --quiet` is valid, and both services are healthy.
- **Left in the development database:** three test jobs whose seeds are on `docs.example.com`
  (reserved for documentation). They stay `queued`, and nothing crawls them. They were not
  deleted, because that would be a destructive operation on the development database. If wanted,
  remove them with `DELETE FROM crawl_jobs WHERE seed_url LIKE 'https://docs.example.com/%'`;
  the seed URLs follow by cascade.

**Findings and design adjustments**

- **Fencing gaps (fixed, ADR-0011).** Under `0001`, the database accepted two kinds of statement:
  - a claim or takeover that granted a lease without a new token. The stalled previous holder's
    correctly fenced commit then succeeded, and the real lease holder was fenced out;
  - a stale worker recording its result under an epoch it issued to itself.

  No production code was affected, because no worker exists yet. ADR-0011 also lists what no
  constraint can catch, such as `result_epoch = lease_epoch` copied from the column.
- **Other review results (no change needed now):**
  - Job status transitions are protected by consistency CHECKs, not by transition rules. The
    scheduler (Phase 5) must use conditional updates (invariant 8).
  - `admitted_count` is bounded by a CHECK, but its equality with the number of URL rows is kept
    by the services.
  - `search_documents` lets `indexed_content_version` move backwards. The Phase 7 indexer must
    write with a version guard.
- **URL policy.** A trailing dot in the host is now kept: the Phase 0 design dropped it, which
  would merge distinct authorities. Python's built-in IDNA codec (IDNA 2003) maps `faß.de` to
  `fass.de`, so the `idna` package is used. `urllib.parse.urlsplit` silently deletes tabs and line
  breaks, so URLs are split with the RFC 3986 grammar and control characters are rejected.
- **`ipaddress` quirk.** Python counts `::1` as reserved (it lies in `::/8`). A unit test caught
  IPv6 loopback being treated as "never allowed" instead of development-only.
- **Deferred from the earlier 2C plan** (not in the owner's 2C request):
  - `Idempotency-Key` and the cancel endpoint go to Phase 6;
  - lifecycle records in `crawl_events` go to Phase 4, with the first state transitions;
  - a status filter on the listing is not scheduled;
  - the CI workflow goes to Phase 11 and needs a GitHub remote;
  - request body size limits go to Phase 11 (only `seed_url` is bounded, at 4096 characters).

**Not done, by design:** crawling, fetching, BFS, Redis publication, workers, indexing, the
frontend and benchmarks.

### Phase 3: Safe HTTP fetching and HTML parsing (done)

**Goal.** Fetch approved URLs safely, and extract what the crawler needs from HTML, before any
distributed machinery exists. No BFS, persistence, Redis, workers or retries.

**Delivered** (all in `backend/app/crawler/`; design in ADR-0013)

- **Safe fetcher** (`fetcher.py`, `results.py`, `errors.py`, `content.py`):
  - `httpcore2` (the transport layer of `httpx2`) used directly: HTTP/1.1, no proxy support, no
    environment configuration, no connection reuse.
  - Each request hop:
    1. checks the crawl scope;
    2. resolves the host once;
    3. checks *every* address;
    4. connects only to a validated address through `PinnedNetworkBackend`. SNI, certificate
       verification and the `Host` header use the host name.
  - Redirects:
    - followed by the fetcher, never by the HTTP stack;
    - every target validated again;
    - loops and the hop limit enforced;
    - `max_redirects=0` returns the redirect instead of following it.
  - Limits:
    - a connect timeout (DNS, TCP and TLS) and a read timeout;
    - an overall deadline;
    - a wire limit and a decoded-size limit, with decompression in 64 KiB slices;
    - HTML media types and gzip or deflate only;
    - oversized bodies rejected, never truncated.
  - Results and failures:
    - typed `FetchResult` and `Redirect`;
    - one `FetchError` with a `FailureKind` (fits `crawl_urls.outcome`) and a `retryable` flag
      (architecture section 10.5).
  - TLS 1.2 or newer, verified by the operating system's verifier (`truststore`).
- **Destination policy** (`destinations.py`):
  - address classes, with mapped and NAT64 IPv4 forms unwrapped and explicit cloud metadata
    endpoints;
  - private addresses only for internal origins; loopback likewise, and only outside production;
  - fail closed on any disallowed answer.
- **Resolver** (`resolver.py`): `getaddrinfo` in the thread pool, separating "does not exist"
  from temporary failures.
- **Link resolution:** `urls.join_url` and `resolve_reference`, implementing RFC 3986
  section 5.2 strictly and tested against all of the RFC's examples.
- **Charset detection** (`encoding.py`):
  - precedence: BOM, then HTTP, then `<meta>`, then UTF-8 or windows-1252;
  - only labels from the WHATWG table are accepted;
  - decoding never fails, and replacement characters are reported.
- **HTML parser** (`html_parser.py`):
  - BeautifulSoup with `html.parser`;
  - metadata: title, description, canonical URL, language, and meta robots (reported, not
    enforced);
  - headings and visible text;
  - links: resolved against `<base href>`, normalized, deduplicated with positions, anchor text
    and `nofollow`;
  - statistics and caps.
- **Manual-check command:** `python -m app.crawler.cli <url>` (development.md, "Safe fetcher and
  HTML parser").
- **Configuration:** `SITESCOPE_FETCH_*` and `SITESCOPE_PARSE_*`. Internal origins are validated
  at startup, and production refuses local ones.
- **Logging:** the `httpx2` and `httpcore2` loggers are capped at WARNING (a Phase 1 finding:
  they log full URLs).
- **Dependencies:**
  - `httpcore2` and `truststore`: already installed with `httpx2`, now declared;
  - `beautifulsoup4` (new, with `soupsieve`);
  - development only: `trustme` (new, with `cryptography`).

**Verification** (2026-11-06; Windows 10, Python 3.13.7, services in WSL)

- `pytest --require-services`: 848 passed (679 unit, 169 integration). Phase 3 added 328 tests (262
  unit, 66 integration).
  - Integration: the real fetcher against HTTP and TLS servers on 127.0.0.1 (no services or
    internet needed). They cover:
    - pinned connections (every connect target recorded), the `Host` header and SNI;
    - DNS rebinding: one lookup per hop; a changed answer refused, even between redirect hops;
    - unsafe answers refused before connecting: private, loopback, link-local, metadata,
      IPv4-mapped, NAT64, and mixed answers;
    - redirect chains, loops, limits, out-of-scope and unusable targets;
    - TLS: wrong host name, untrusted authority, stalled handshake;
    - status classification, content types and codings, size limits, a 50 MB gzip bomb;
    - truncated, malformed, silent, slow-drip and refused responses;
    - the system resolver, and environment proxies ignored.
  - Unit:
    - URL resolution (RFC 3986 section 5.4), address classification, the allow matrix;
    - bounded decoding in any chunking, and charset sniffing;
    - the parser on static fixtures (malformed, Unicode, large and deeply nested documents);
    - settings validation.
- `ruff check` and `ruff format --check`: clean. `mypy` strict (73 files): no issues.
- Manual checks with `python -m app.crawler.cli`:
  - **local fixture server**: by IP literal; and through `localhost` and the real resolver, with
    fallback from the refused `::1` to `127.0.0.1`. Refused once the internal origin was removed.
  - **public site**: `https://example.com/` over real DNS and TLS, 200 with a gzip body. An
    unapproved host (`www.example.com`) was refused.
- The Phase 2C API tests still pass (`tests/integration/test_crawl_jobs.py`).

**Findings and design adjustments**

- **`httpx2` client:** it has no public hook for the network backend. Pinning through it would
  mean replacing a private attribute, and a rename could silently disable pinning, so the
  fetcher uses `httpcore2` directly (ADR-0013).
- **TLS on Windows:** OpenSSL with `ssl.load_default_certs()` could not verify
  `https://example.com/` on this machine ("unable to get local issuer certificate"), because
  Windows installs most roots only when its own verifier needs them. The fetcher therefore uses
  `truststore`, the OS verifier, which `httpcore2` already depends on.
- **Python `ipaddress`:** `is_global` is true for multicast, the NAT64 form of loopback, the
  IPv4-compatible `::127.0.0.1` and site-local `fec0::/10`. The classifier tests each class
  explicitly. (`::1` counts as reserved, as found in Phase 2C.)
- **httpcore2 exception chains:**
  - The pool re-raises connect errors `from None`, so TLS causes are found through
    `__context__`.
  - h11's message for a cut-short body separates retryable `truncated` failures from
    `malformed_response`.
  - Tests pin both behaviors.
- **Parser bug found by a fixture:** `</head>` is optional in HTML, and `html.parser` then nests
  the body inside `<head>`. Removing `<head>` before extracting text deleted the whole body. The
  parser now removes only titles, scripts, styles, templates and `hidden` elements, and has a
  regression test.
- **Deflate detection:** choosing between zlib and raw DEFLATE depended on how the body was
  chunked; a test that fed one byte at a time found it. It now inspects the 2-byte zlib header.
- **Smaller issues:**
  - BeautifulSoup warns about short bodies that look like URLs or file names, and pytest treats
    warnings as errors. A trailing newline avoids it, thread-safely.
  - `urllib.parse.urljoin` strips tabs and line breaks silently, so link resolution has its own
    RFC 3986 implementation.
- **Parsing cost:** BeautifulSoup builds the whole tree, so CPU time and memory grow with the
  input. Input is capped at the body limit, and the Phase 5 worker must parse in a thread
  (`asyncio.to_thread`, as the CLI does).
- **Moved to Phase 4.** The owner's Phase 3 scope covered secure fetching and parsing only, so
  these move to Phase 4, where the crawl loop that needs them is built:
  - robots.txt (an RFC 9309 parser and a Redis cache);
  - the per-host rate limiter (GCRA in Redis);
  - the first demo-site content.

  The fetcher already provides what robots.txt needs: a validated fetch that follows redirects.

### Phase 4: BFS crawling, persistence and politeness (done)

**Goal.** Crawl a site breadth-first and persist pages and links with the architecture's
correctness rules, in one process, before the work is distributed; obey robots.txt and limit the
request rate per host across processes.

**Delivered** (design in ADR-0014 and ADR-0015)

- **Crawl state** (`backend/app/services/crawl_state.py`): the use cases that the Phase 5
  scheduler and workers will share, each one short transaction:
  - `activate_job` (T2) and `dispatch_urls` (T3: `FOR UPDATE SKIP LOCKED`, current level only,
    no stream yet);
  - `claim_url` (T4): compare-and-set with a new fencing token and an attempt limit; a takeover
    of an expired lease is recorded as `url.recovered`;
  - `record_result` (T5): the fenced update comes first, bound to the worker's own token; then
    the page, its links, its `search_documents` row (not for `noindex`) and same-level redirect
    admission under the job row lock. Zero rows from the fenced update: roll back, `FENCED`;
  - `advance_job` (T6): the next level in BFS order (source URL, link position), up to the
    remaining budget, or `completed` / `failed`;
  - `recover_exhausted_url`: a recovery claim (new token) records `max_attempts_exceeded`;
  - lifecycle events in `crawl_events`.
- **Crawl task** (`backend/app/workers/crawl_task.py`): scope, robots.txt, one request with
  redirects returned (`max_redirects=0`), parsing in a thread, page directives, links with their
  job-scope flag, and a terminal outcome for every result.
- **Single-process driver** (`backend/app/workers/local_crawler.py`) and its command,
  `python -m app.workers.local_crawler <job-id>`: bounded concurrency, a task deadline 10 s
  inside the lease, and pickup of `queued` URLs and expired leases left by a stopped run.
- **robots.txt** (`backend/app/crawler/robots.py`): an RFC 9309 parser and matcher of our own
  (no regular expressions), fetched through the safe fetcher with the new `ROBOTS_TXT` content
  policy; status mapping; a Redis cache (24 h, 300 s when unreachable), memory and single flight.
- **Politeness** (`backend/app/crawler/politeness.py`): a request gate on every fetch hop; one
  Redis Lua script grants slots first come, first served, with spacing between request starts
  and a cap on requests in flight; crawl delays; `rate_limited` after the maximum wait.
- **Page directives** (`backend/app/crawler/directives.py`): meta robots (also
  `<meta name="<crawler name>">`) and `X-Robots-Tag` per header, with user-agent scoping;
  `noindex`, `nofollow`, `none`.
- **Fetcher:** `ContentPolicy` (`HTML_PAGE`, unchanged and the default; `ROBOTS_TXT`),
  `FetchResult.truncated` and `robots_tags`, the `RequestGate` hook, the retryable
  `FailureKind.RATE_LIMITED`, and the User-Agent from `SITESCOPE_CRAWLER_NAME`.
- **Schema:** migration `0003` adds `pages.robots_noindex`, `pages.robots_nofollow` and
  `page_links.nofollow` (constant defaults, no table rewrite). The development database was
  upgraded; its three queued jobs were left untouched (checked before and after).
- **Settings:** `SITESCOPE_CRAWLER_NAME`, `SITESCOPE_CRAWL_CONCURRENCY`,
  `SITESCOPE_CRAWL_LEASE_SECONDS`, `SITESCOPE_CRAWL_MAX_ATTEMPTS`, `SITESCOPE_POLITENESS_*`,
  `SITESCOPE_ROBOTS_*` and `SITESCOPE_REDIS_KEY_PREFIX`, with startup checks of the timing
  order (lease at least the fetch deadline + 10 s; interval and crawl-delay cap within the
  maximum wait).
- **Logging:** a task context variable adds `job_id`, `url_id`, `worker_id` and `lease_epoch` to
  every log line of a task, the fetcher's included.
- **Demo site** (`demo-site/`): 14 static HTML pages and a robots.txt, crawled exactly by a test.
- **Dependencies:** none added.

**Verification** (2026-11-06; Windows 10, Python 3.13.7, PostgreSQL 18.6 and Redis 8.10.3 in WSL)

- `pytest --require-services`: 1204 passed (933 unit, 271 integration), in three consecutive
  full runs after the last change. Phase 4 added 356 tests (254 unit, 102 integration).
  Among them:
  - crawl state on PostgreSQL (38): claims and takeovers; fencing, including a forged token, a
    stale worker after a takeover, and a capture of the SQL showing that both the fencing
    condition and `result_epoch` are bound to the worker's token; rollback of a failing result;
    redirect admission; budget and deduplication with eight concurrent transactions; level
    advancement in BFS order, concurrently too; recovery of exhausted leases;
  - local crawls (14): fixture graphs with cycles, a diamond and a shortcut, robots rules, a
    redirect, directives and failures, the budget prefix with 1 and 4 tasks, 1 vs 6 tasks,
    redirect chains and loops, a crashed worker's lease taken over (and its late result
    fenced), politeness spacing measured at the server, the demo site, and the command line
    against the standard library's static file server. The section 11 invariants are checked
    after every crawl;
  - robots.txt: the RFC 9309 examples and rules (unit), and the policy against local servers
    and Redis (status codes, redirects, truncation, blocked addresses, single flight, caching);
  - politeness: the gate in fake and virtual time (27 unit tests), and the Lua scripts on
    Redis (13), fairness and clock steps included.
- `ruff check` and `ruff format --check`: clean. `mypy` strict (91 files): no issues.
- The Phase 2C and Phase 3 suites still pass, the fetcher's security tests included (66).
- Manual checks, on a separate database (`sitescope_manual`, dropped afterwards) and Redis
  database 15 with its own key prefix:
  - a demo-site job created through the API and run with the command line, with the default
    politeness settings: exit status 0, 14 URLs (12 stored pages, a 404, one URL skipped by
    robots.txt), 14 GET requests at the demo server (13 pages and robots.txt once, never the
    disallowed or nofollow-only pages), 16.6 s, run again after the review fixes;
  - the API reports the same progress from PostgreSQL;
  - read-only API checks against the development database after its upgrade to `0003`.

**Review findings and fixes.** An independent review (a separate agent, without my
conclusions) and my own review found:

- **Politeness starvation (medium).** Slots were reserved for future starts, so a task that had
  just released one re-reserved before the waiters woke up. Measured on Redis with the defaults:
  four tasks got 71, 1, 1 and 1 grants in 70 s, and the waiters failed as `rate_limited`, which
  loses URLs. Replaced by a first-come-first-served queue (ADR-0015). A copy of the old
  algorithm reproduces the measurement in virtual time; the regression tests pass on the new
  one (virtual time and real Redis).
- **Deadlock in same-level redirect admission.** Admitting a target that another task was
  recording waited for that task's transaction while holding the job lock. Fixed with a plain
  check before the insert (ADR-0014); a test reproduces the wait, and fails without the fix.
- **Truncated robots.txt.** The cut last line was parsed and could change meaning (`Allow: /`).
  It is now dropped; a test cuts a line exactly at 500 KiB.
- **`advance_job` and a reused session** read a stale ORM copy of the job; it now reads plain
  columns under the lock.
- **Clock steps (found by an intermittent full-suite failure).** The WSL 2 VM clock, shared by
  PostgreSQL and Redis, stepped back about one second during a test run: a finished job violated
  `ck_crawl_jobs_timestamps_ordered` (`finished_at` before `created_at`), and a politeness waiter
  was told to wait for the step as well as the interval. Job timestamps are now clamped to
  `created_at`, and the politeness script records a last start in the future as "now" and keeps
  the queue's arrival order. Tests simulate both steps; a first version of the politeness fix
  waited one interval forever, and its test caught that.
- **`X-Robots-Tag` with an `unavailable_after` date:** its commas turned off the following
  directives; a prefix is now a user agent only if it is a product token.
- **Smaller fixes:** the slot or queue place is released even if the caller is cancelled
  mid-acquisition; the crawl delay is written before robots.txt is cached, and a host keeps the
  largest delay of its origins; rule sets above 1,000 rules are matched in a worker thread
  (measured: about 20 ms per check for a hostile 500 KiB file); a truncating body reader no longer
  rejects the chunk that crosses both limits (found by a test); the driver no longer counts
  recoveries as started tasks; the command line reports an unknown job with exit status 2.

**Known limitations (until later phases)**

- Retryable failures (timeouts, connection errors, HTTP 429 and 5xx, `rate_limited`) are
  terminal; Phase 6 adds durable retries.
- An unreachable robots.txt disallows its origin for 300 s, and again after each failed retry.
- A task may need two slow fetches (robots.txt, then the page) and end as `deadline_exceeded`.
- Politeness is per host name, not per IP address.
- The driver stops on a PostgreSQL or Redis error; the next run continues once the stopped run's
  leases expire.
- Separate processes may each fetch an expired robots.txt once.

### Phase 5: Distributed scheduling and workers (done)

**Goal.** Run the crawl across independent processes: a scheduler publishes committed work to a
Redis stream, crawler worker processes claim and process it, and the system recovers from
crashes, stalls and outages on its own.

**Scope, reconciled with the plan.** The planned scope is delivered, plus the minimum of Phase 6
that a distributed crawler needs to be recoverable at all: `XAUTOCLAIM` takeover, the reconciler
for lost messages, dead-lettering of malformed messages, safe trimming and consumer cleanup,
startup validation of the timing order, and crash tests with real processes. Retries,
dead-lettering by delivery count, cancellation, `Idempotency-Key` and a leader lock stay in
Phase 6. The planned in-memory site test double was not built: the tests use real processes
and local HTTP servers instead.

**Delivered** (design in ADR-0016; architecture sections 3, 6, 9 and 10)

- **Message and stream** (`backend/app/queue/`): the versioned task message `{v, job_id,
  url_id}`; the stream adapter: idempotent group creation, pipelined `XADD`, blocking
  `XREADGROUP`, `XACK`, `XAUTOCLAIM` with a cursor, a capped dead-letter stream, pending
  summaries, trimming that never removes undelivered or pending entries, atomic removal of idle
  consumers (Lua), and `delivering`, the liveness check the reconciler uses.
- **Crawl state** (`backend/app/services/crawl_state.py`): dispatch with publication inside the
  transaction (`XADD`, then mark `queued` with the entry ID); a claim that locks the URL row
  first and so waits for a dispatch still in flight; `republish_stale` (only tasks whose entry is
  gone, keyset paging); batch activation under an advisory lock; frontier counts,
  `outstanding_tasks`, `inspect_url`.
- **Worker** (`python -m app.workers.crawler`, `backend/app/workers/crawler.py`): a unique name per
  process start (consumer name and lease owner); reads only as many messages as it has free
  slots; the acknowledgment rules (`judge_unclaimed`, architecture section 10.3); `XAUTOCLAIM`
  takeover; dead-lettering; backoff after errors; graceful shutdown with a grace period
  (Windows: Ctrl+C or `CTRL_BREAK_EVENT`).
- **Scheduler** (`python -m app.scheduler`): activation, level advancement, dispatch with
  per-job, global and per-pass limits shared fairly among jobs, reconciliation, trimming and
  consumer cleanup, statistics; safe to run several at once, without a leader. Read-only
  diagnostics: `python -m app.scheduler.status`.
- **Shared code:** `app/core/shutdown.py` (stop signals on Windows and POSIX), `process.py`
  (unique names, error classification without messages), `backoff.py` (exponential, full
  jitter); `app/workers/runtime.py` (crawl context, task deadline, the workers' database);
  `Database` server settings and `hide_parameters`; lenient Redis decoding.
- **Settings:** `SITESCOPE_CRAWL_LEASE_MARGIN_SECONDS`, `SITESCOPE_STREAM_*`,
  `SITESCOPE_SCHEDULER_*`, `SITESCOPE_SHUTDOWN_GRACE_SECONDS`, with the startup check
  task deadline < lease <= reclaim min idle <= reconcile after <= retention.
- **Single-process driver:** shares the runtime with the workers; unchanged behaviour.
- **Tests:** `tests/fault/` (marker `fault`) and the process harness `tests/process_support.py`
  (real processes, `CTRL_BREAK_EVENT`, kill, freeze, a TCP proxy that cuts PostgreSQL or Redis
  off); the service fixtures moved to `tests/service_fixtures.py`, shared by `integration/` and
  `fault/`.
- **Benchmark:** `benchmarks/distributed_crawl.py` (1, 2, 4 workers; 1 and 4 hosts).
- **Dependencies:** none added.

**Verification** (2026-11-06; Windows 10, Python 3.13.7, PostgreSQL 18.6 and Redis 8.10.3 in WSL)

- `pytest --require-services`: 1381 passed (1030 unit, 351 integration and fault), in three
  consecutive full runs after the last change, 197 to 208 s each; nothing skipped. Phase 5 added
  177 tests (97 unit, 80 integration and fault). The 11 multi-process tests
  (`tests/integration/test_distributed.py`, `tests/fault/test_recovery.py`) also ran two more
  times on their own, all passing (about 120 s each). Among them:
  - **A**, three worker processes crawl one job (40 pages): participation from each worker's
    own log (each recorded pages, every URL recorded by exactly one), the consumer group, each
    page requested once, strict BFS and politeness from the server's request timeline;
  - **1 vs 4 vs 8 workers**: the same budget-limited crawl (25 of 40 pages, cut in the middle of
    level 3) gives identical results;
  - **B**, a worker killed during a fetch, and a message abandoned before its claim, both taken
    over by the surviving worker (new token, `url.recovered`);
  - **C**, a worker frozen mid-fetch (`NtSuspendProcess`): its lease is taken over, and when it
    resumes its result is fenced out and its message not acknowledged;
  - **D**, the scheduler killed mid-crawl, and a scheduler killed exactly between `XADD` and
    `COMMIT` (held there by a table lock): only duplicates, each page fetched once;
  - **E**, every task published twice more while the job runs: each page fetched and stored
    once;
  - **F**, three scheduler processes at once: the running-jobs cap held (checked by replaying the
    lifecycle events in sequence order), every URL published exactly once;
  - **G**, Redis, then PostgreSQL, cut off for 3 s through a TCP proxy: the processes back off,
    recover, and the job completes with the invariants intact;
  - graceful stops with a task in flight, within and after the grace period.
- `ruff check` and `ruff format --check`: clean. `mypy` strict (122 files): no issues.
- The Phase 2C, 3 and 4 suites still pass, the fetcher's security tests included.
- Manual check (scripted, `sitescope_test_manual`, dropped afterwards; Redis database 15 under
  its own prefix): the demo site served by `http.server`, the API under uvicorn, one scheduler and
  two worker processes, default timing and politeness. The job completed in 14.7 s
  with 14 URLs (13 fetched, one disallowed by robots.txt), the same result as Phase 4; the server
  saw 14 requests (13 pages, robots.txt once); the workers recorded 9 and 5 URLs; the scheduler
  and both workers exited with status 0 on `CTRL_BREAK_EVENT`; the status command showed nothing
  pending.
- The development database was not touched: its fingerprint (jobs and URLs) was the same before
  and after the phase, and its three queued jobs are still queued.
- Benchmark ([`benchmarks/results/phase5-distributed.md`](../benchmarks/results/phase5-distributed.md),
  raw JSON next to it; this machine only): one scheduler and N worker processes (4 tasks each)
  crawled generated sites (259 pages per host, every page answered after 100 ms, politeness
  relaxed). Medians of three runs, in pages per second:

  | Hosts | In flight per host | 1 worker | 2 workers | 4 workers |
  |---|---|---|---|---|
  | 1 | 8 | 25.4 | 41.7 | 38.1 |
  | 4 | 8 | 27.3 | 49.2 | 51.5 |
  | 4 | 16 | 27.1 | 49.1 | 87.3 |

  With one host, its politeness limit caps throughput from 2 workers on (the documented
  limitation). With four hosts and 8 requests per host the fourth worker added little; with 16
  per host the same tasks were 1.8 times faster, so the per-host limit, not the workers, was the
  bottleneck (likely because a job's tasks are published together and workers crowd onto one
  host). The CPU was not the bottleneck: 25-43 % busy during a 4-worker run.

**Review findings and fixes.** An independent review (a separate agent, given the code and the
invariants, not my conclusions) and my own testing found:

- **Over-dispatch (found by a test while building this phase).** `UPDATE ... WHERE id IN (SELECT
  ... LIMIT n FOR UPDATE SKIP LOCKED)` updated more than `n` rows: PostgreSQL rescanned the
  subquery per outer row, and a rescan skips rows the statement already updated. Batch activation
  exceeded the running-jobs cap, and the Phase 4 dispatch could queue more URLs than the local
  driver had slots. Both statements now select in a `MATERIALIZED` CTE; regression tests.
- **The dispatch race (found while designing).** `XADD` precedes the commit, so a worker could
  read a message for a row still `pending` and acknowledge it as stale, losing the task. The
  claim now locks the row first and waits for the dispatch; tests hold a dispatch open and check
  both outcomes.
- **A non-UTF-8 stream entry (review, medium, confirmed).** Strict decoding failed every batch the
  entry was in, leaving the batch's valid tasks pending forever and stalling the job. Replies
  are now decoded leniently and the entry is dead-lettered.
- **A worker frozen inside a transaction (review, medium-low).** It kept its row lock and blocked
  every takeover of the URL. Worker sessions now end after being idle in a transaction for the
  lease margin; a test freezes a session inside a claim and checks that another claim succeeds.
- **One broken job stopped every job's scheduling (review, low).** Per-job errors are now
  isolated; transient errors still fail the pass.
- **Running jobs beyond the cap were never scheduled (review, low).** A pass now looks at every
  running job.
- **Consumer cleanup was check-then-act (review, low)**; now one Lua script.
- **The reconciler's timeout budget (review, low):** the liveness check is now one round trip,
  matching the budget of the scheduler's idle-transaction timeout.
- **Test gaps (review):** added the 1/4/8-worker comparison with a budget cut, the forced
  `XADD`-to-`COMMIT` crash, wiring tests for the idle-transaction timeouts, a clock-independent
  cap check, a wider politeness tolerance, and a stricter test-database guard (the `extra`
  settings of the process tests can no longer name the database). Each regression test was run
  against the code without its fix and failed.
- **Test harness on Windows (found by the tests).** `.venv\Scripts\python.exe` is a launcher that
  runs the real interpreter as a child, so freezing the launcher froze nothing; the harness now
  freezes and kills the interpreter itself (its PID is part of its unique name).

**Known limitations (until later phases)**

- A worker that dies, or loses Redis, while it holds a politeness slot leaves the slot held until
  its TTL (70 s by default). With one slot per host, that host's requests wait, and those waiting
  longer than 30 s fail as `rate_limited`, which stays terminal until Phase 6 retries. The outage
  test observed it.
- Recovery is unhurried with the defaults: a crashed worker's tasks resume after 120 s, a lost
  message after 10 minutes, and a task whose claim met a transient PostgreSQL error after 120 s.
- With several schedulers the backpressure limits are approximate.
- Retryable failures are still terminal (Phase 6).

**Notes for Phase 6**

- Retries: return a retryable failure to `pending` with `next_attempt_at`; the dispatcher already
  respects `next_attempt_at`, and the claim already counts attempts. `rate_limited` and
  `deadline_exceeded` must be retryable.
- Dead-lettering by delivery count: `XAUTOCLAIM` replies do not carry delivery counts;
  `XPENDING` does.
- Cancellation: `cancelling` already counts toward the running-jobs cap, and workers acknowledge
  messages of jobs that are not running.
- The process harness (`tests/process_support.py`) and the `clusters` fixture are ready for new
  failure scenarios.

### Phase 6: Fault tolerance and recovery (done)

**Goal.** Retry what failed for a transient reason, dead-letter what can never be processed,
cancel jobs, make job creation idempotent, and prove the failure windows with automated tests.

**Scope, reconciled with the plan.** The planned scope is delivered. Added: renewable politeness
slot leases (the Phase 5 known limitation), shorter recovery defaults, and cancellation in the
single-process driver. The optional scheduler leader lock was not built: correctness does not
need it (ADR-0016), and duplicate work stays bounded.

**Delivered** (design in ADR-0017; architecture sections 3, 6 to 10, 14, 18 and 19)

- **Retries** (`backend/app/workers/retry.py`, `record_result(..., retry_delay)` in
  `crawl_state.py`): retryable failures go back to `pending` with `next_attempt_at` by the
  database clock, through the same fenced update as a result; only claims count attempts; the
  last attempt's failure is final; full jitter and `Retry-After` (`app/crawler/retry_after.py`,
  read by the fetcher on 429 and 503). No migration: the existing columns, constraints and
  triggers allow the transition.
- **Dead letters and poison messages:**
  - delivery counts come from `XPENDING`;
  - a message delivered too often whose URL can never be claimed again (queued, attempts used
    up) gets a fenced quarantine claim, which fails the URL as `max_deliveries_exceeded`; any
    other URL is claimed as usual;
  - one Lua script acknowledges and copies an entry at most once;
  - only the contract fields keep their values, other fields are listed by name;
  - the status command lists dead letters (`--dead-letters`).
- **Cancellation** (`app/services/crawl_cancellation.py`, `POST /api/v1/crawls/{id}/cancel`):
  T7, the workers' cancellation watcher and fenced cancel, the scheduler's finalizer, and the
  driver's settlement.
- **`Idempotency-Key`** on `POST /api/v1/crawls` (`submit_crawl_job`): replays (`200`,
  `Idempotent-Replayed: true`), conflicts (`409 idempotency_key_reused`), expiry and cleanup.
- **Politeness slot leases** (`RENEW_SCRIPT` and the renewal task in `politeness.py`;
  `app/core/tasks.py`).
- **Worker:** local handling of errors before the claim (bounded by half the reclaim idle time),
  `infrastructure_error` attempts, logged consumer-group recreation.
- **Settings:** `SITESCOPE_RETRY_*`, `SITESCOPE_STREAM_MAX_DELIVERIES`,
  `SITESCOPE_CRAWL_CANCEL_CHECK_SECONDS`, `SITESCOPE_IDEMPOTENCY_KEY_TTL_SECONDS`,
  `SITESCOPE_POLITENESS_SLOT_LEASE_SECONDS`; reclaim idle 120 → 100 s, reconcile 600 → 300 s.
- **Tests:** `integration/test_retries.py`, `test_cancellation.py`, `test_idempotency.py`, new
  cases in `test_crawler_worker.py`, `test_fetcher.py`, `test_politeness_redis.py`,
  `test_streams.py`, `test_local_crawler.py`; `fault/test_retries_and_cancellation.py` and
  `fault/test_redis_data_loss.py` (real processes; `FLUSHALL` only on a disposable container);
  unit tests for `Retry-After`, the retry policy, jitter, settings, slot renewal and
  `cancel_and_wait`. `LocalHttpServer` takes reply sequences for flaky pages.
- **Dependencies:** none added.

**Verification** (2026-11-07; Windows 10, Python 3.13.7, PostgreSQL 18.6 and Redis 8.10.3 in WSL)

- `pytest --require-services` on the committed code: **1631 passed** (1157 unit, 474 integration and fault) in 288 s; nothing skipped. Phase 6 added 250 tests (127 unit, 123 integration and fault).
- Before that, three consecutive full runs after the review fixes:
  - 1631 passed in 288 s and 1631 passed in 295 s;
  - 1630 passed and 1 failed in 277 s.
- The real-process tests (`tests/integration/test_distributed.py`, `test_process_sessions.py`, `tests/fault/`: 24 tests) ran three more times on their own: 23 passed and 1 failed, then 24 passed (208 s), then 24 passed (199 s).
- **The two failures** were one test assertion: `DBSIZE == 0` right after the `FLUSHALL` of the data-loss test. The live processes write keys again at once (it saw 4 and 2 keys), so the check raced them.
  - The test now proves the flush with a marker key that only it writes; no application code changed.
  - The fixed test then passed in the third process-test run, in 5 consecutive runs of its own (about 20 s each, no container left behind), and in the full run above.
- A pre-check run before the review fixes: 1620 passed, 1 failed. The killed worker's politeness slot freed within its lease, but its second, waiting task held the host's queue until its waiter entry expired (6.95 s, against a bound of 4 s). The test now isolates the slot lease, and the waiter rule is documented.
- Among the new process tests:
  - **Retries with strict BFS:** flaky pages (503 twice, 503 with `Retry-After`, 500, 429) on a 13-page tree, with two schedulers and three workers. Every page is stored, with one request per attempt and none duplicated, and every level is requested only after all retries of the previous one (from the server's timeline).
  - **Exhaustion:** a page that always answers 500 gets three requests and a final `http_error`; the next level starts after its last attempt.
  - **`Retry-After`:** both forms work through the real safe fetcher; the next attempt comes at least 1.9 s after a 2 s value.
  - **Simultaneous failures:** ten pages failing at once on three workers get jittered retry delays.
  - **Restarts and crashes:** a scheduler killed during a backoff, a worker killed during a retry attempt (the takeover's third attempt succeeds), and duplicate messages during a backoff (two requests only).
  - **Politeness slot leak:** after a worker holding a slot is killed, the next request to the host comes about one lease (1 s) later instead of 16 s.
  - **Cancellation:** with live workers, fetches longer than the fetch deadline are stopped and the job is cancelled in under 4 s, with every message settled. Across a scheduler restart, the job stays `cancelling` until a new scheduler finalizes it.
  - **Redis data loss:** `FLUSHALL` of a disposable Redis container mid-crawl (21 pages). The consumer group is created again, the lost tasks are republished, and the job completes with every invariant intact; a sentinel key in the development Redis survived.
- **Static checks:** `ruff check`, `ruff format --check`: clean. `mypy` strict (134 files): no issues. Alembic head `0003`; no new migration, and the migration tests pass.
- **Earlier suites:** the Phase 2C to 5 tests still pass, the fetcher's security tests included. Earlier tests were changed only to follow a changed default or extend an enumeration, and none was removed or weakened:
  - the reclaim and reconcile defaults, and the timing-order boundaries derived from them (119 → 99, 599 → 299);
  - the OpenAPI endpoint and status lists;
  - a comment of test G;
  - stronger assertions: `ensure_group`'s return value, and the local driver's request counts per attempt.
- **Development database:** untouched. Its fingerprint is `e574f91fa587b2ea` before and after the phase, and its three queued jobs are still queued.
- **Secrets:** no secret value from `backend/.env` in any changed file, and `backend/.env` is not tracked.

**Review findings and fixes**

*Self-review* (my own checks, and failures of the existing tests):

- **Cancellation watcher:** it stopped every task whose job was no longer running, so a frozen worker's stale task of a completed job was reported as cancelled (found by Phase 5 fault test C). Only `cancelling` and `cancelled` jobs stop tasks now; regression test added.
- **Errors before the claim:** handling them locally for longer than the reclaim idle time let `XAUTOCLAIM` hand the message out again while it was still being handled (found by a Phase 5 test timing out). The local handling is now bounded by half the reclaim idle time.
- **Slot renewal task:** a cancellation of the caller could be swallowed while waiting for it (`app/core/tasks.py`; regression test added), and the hold time counted the wait for the slot.
- **Dead-letter record:** it copied up to 128 characters of every original field. Now only the contract fields keep their values; other fields are listed by name.
- **Consumer group:** the scheduler recreated a lost group silently (found by the new `FLUSHALL` test); it is now logged.
- **Deadlock:** cancellation (T7) could deadlock with the quarantine of a queued URL. T7's `FOR UPDATE` on the job row conflicted with the foreign-key check of the quarantine's event insert. A regression test reproduced it (`deadlock detected`), and T7 now takes `FOR NO KEY UPDATE`. The independent review found it too.

*Independent review.* A separate agent reviewed the code against the invariants and the focus areas, not my conclusions. It worked read-only and ran unit tests only. It was interrupted when a session ended and resumed from its transcript. Each confirmed finding got a regression test that failed before its fix:

1. **High:** a PostgreSQL outage longer than ten reclaim intervals turned queued URLs into poison messages on recovery.
   - `XAUTOCLAIM` counts a delivery even when the handling fails before the claim, so URLs were quarantined without a single attempt; a seed would fail its job.
   - Now only a URL that can never be claimed again (queued, attempts used up) is quarantined; the others are claimed as usual, and their attempts bound the work.
2. **Medium:** a `Retry-After` or `Date` header such as `Fri, 31 Dec 9999 23:59:59 -0100` raised `OverflowError` out of the safe fetcher.
   - The task failed with a traceback and kept its lease until it expired, and a robots.txt answered that way would never have been cached.
   - The parser now ignores such dates.
3. **Medium:** the T7/quarantine deadlock above.
4. **Low:** when `XPENDING` failed right after an `XAUTOCLAIM`, the reclaimed batch was dropped: hidden for another reclaim interval, with one more delivery counted each round. It is now handled as first deliveries.
5. **Test quality:**
   - missing extreme-date cases: added;
   - no outage test for delivery counts: added;
   - three wall-clock bounds: made discriminating, with margins;
   - `sleep` plus `not done()` lock checks: they now wait for a lock wait that `pg_stat_activity` shows.
6. **Minor:**
   - an expired last attempt was quarantined instead of recovered: fixed with finding 1;
   - a holder that stalls at the very moment of a renewal can keep its slot one lease past `slot_ttl`: documented, and the concurrency limit stays safe.

**Known limitations**

- **Errors before the claim:** a message whose handling fails before its claim on every delivery (a bug, not an outage) is not dead-lettered; it keeps failing visibly.
- **Slow cancellation:** a cancelled job stays `cancelling` while a crashed worker's lease runs (up to 90 s by default), and until a scheduler or the single-process driver finalizes it.
- **Dead waiters:** the waiting requests of a dead process block their host's politeness queue until their waiter entries expire (15 s by default).
- **Not retried:** a `Retry-After` beyond 300 s is final, and `robots_unreachable` is not retried per URL.
- **Delivery counts:** they are read only on reclaims, and a republished message starts counting again.
- **Cancelling a large job:** all its waiting URLs are updated in one transaction.
- **Leader lock:** the optional scheduler leader lock was not built.

**Notes for Phase 7**

- **Index relay:** it can reuse the dispatch pattern (publication inside the transaction, `SKIP LOCKED`, `MATERIALIZED` CTEs) and the stream adapter (dead letters, delivery counts, trimming). The index stream needs its own consumer group and dead-letter stream.
- **Cancelled jobs:** their stored pages are kept, so decide whether they are indexed.
- **Index writes:** they still need the content-version guard (Phase 2C review finding).
- **Lock order:** any new transaction that holds a URL row and inserts rows that reference the job takes `FOR KEY SHARE` on the job row (lock order, architecture section 9).

### Phase 7: Asynchronous search indexing (done)

**Goal.** Index crawled pages asynchronously and reliably: committed pages only, no lost or
duplicated effects across crashes, duplicates and Redis data loss, no older index ever replacing
a newer one, and a job's indexing reported separately from its crawl.

**Scope, reconciled with the plan.** The planned scope is delivered: the relay of `pending`
`search_documents` rows (ADR-0008) and the indexer, a weighted `tsvector` with truncation,
version-guarded index writes (the Phase 2C review finding), indexing progress on the job detail
and `indexing_completed_at`. Changed from the plan: the relay is a process of its own, the index
dispatcher, rather than a scheduler step (ADR-0018). Added: an internal search service with a
read-only command (the HTTP API stays in Phase 8), indexing statistics in the status command,
and a benchmark. Not done: Compose services for the new roles (with the backend image, Phase 11)
and a command for reindexing (`request_reindex` is a service function only).

**Delivered** (design in ADR-0018; architecture sections 3, 4, 5, 7.3, 9, 10.8, 11, 13 to 15, 18
and 19)

- **Index dispatcher** (`app/scheduler/index_dispatcher.py`, `python -m app.scheduler.index_dispatcher`):
  dispatch inside the transaction (`SKIP LOCKED`, `XADD`, mark `queued`), backpressure,
  reconciliation of lost messages, job indexing completion, stream maintenance, statistics. It
  shares the scheduler's pass loop (`app/scheduler/passes.py`, extracted; the scheduler uses it
  too, with unchanged events).
- **Indexer** (`app/workers/indexer.py`, `python -m app.workers.indexer [--concurrency N]`) on the
  consumer loop extracted from the crawler worker (`app/workers/consumer.py`; the crawler worker
  uses it too, with unchanged behaviour and log lines): attempts counted in PostgreSQL before any
  work, advisory leases, the vector built inside PostgreSQL from the locked page, one deadline
  for the whole attempt, dispositions for every case (architecture section 10.8).
- **Index state** (`app/services/search_index.py`): I1 to I6, reconciliation, progress and
  statistics. **Search** (`app/services/search.py`; command `python -m app.scheduler.search`):
  per job, `websearch_to_tsquery`, `ts_rank`, a total order, bounded paging, the visibility rule,
  plain-text snippets with highlight ranges, one REPEATABLE READ snapshot, a timeout. Pure
  helpers in `app/indexing/`.
- **Streams and messages:** a generic `TaskStream` (`CrawlStream`, `IndexStream`: `<prefix>:index`,
  group `indexers`, dead letters in `<prefix>:index:dlq`); `IndexTask` `{v, job_id, page_id,
  content_version}`.
- **Migration `0004`:** triggers that keep `pages.content_version` and
  `search_documents.indexed_content_version` from moving back; the index lease columns
  (`lease_owner`, `lease_expires_at`) with their CHECK constraints; the partial indexes
  `ix_crawl_jobs_indexing_open` and `ix_search_documents_open`.
- **Job lifecycle:** the job detail's `indexing` block (status `pending`, `running`, `indexed`,
  `failed`; counts per document status; `completed_at`), unchanged crawl statuses; cancelled jobs'
  pages stay indexed (policy, ADR-0018).
- **Diagnostics:** the status command's `indexing` and `index_stream` sections; the dispatcher's
  `index_dispatcher.stats`; events `job.indexing_completed`, `job.reindex_requested`,
  `page.index_failed`.
- **Settings:** `SITESCOPE_INDEX_CONCURRENCY`, `_MAX_ATTEMPTS`, `_MAX_BODY_CHARS`,
  `_TASK_DEADLINE_SECONDS`, `_RECLAIM_MIN_IDLE_SECONDS`, `_RECONCILE_AFTER_SECONDS`,
  `_DISPATCH_BATCH_SIZE`, `_MAX_OUTSTANDING`, `_DISPATCHER_INTERVAL_SECONDS` (all
  `SITESCOPE_INDEX_*`), and `SITESCOPE_SEARCH_TIMEOUT_SECONDS`, with startup checks of their
  timing order, of the indexer's concurrency against its connection pool, and of both deadlines
  against the database command timeout.
- **Tests:** unit tests for messages, query normalization, snippets, the indexing state, the
  indexer's rules and configuration, settings and commands; integration tests
  `test_search_index.py`, `test_index_versions.py` (every interleaving forced with row locks),
  `test_search.py`, `test_indexer_worker.py`; real-process tests
  `fault/test_indexing_processes.py` and a second scenario in `fault/test_redis_data_loss.py`.
  `tests/process_support.py` starts index dispatchers and indexers.
- **Benchmark:** `benchmarks/indexing.py`, results in
  [`benchmarks/results/phase7-indexing.md`](../benchmarks/results/phase7-indexing.md).
- **Dependencies:** none added.

**Verification** (2026-11-08 UTC; Windows 10, Python 3.13.7, PostgreSQL 18.6 and Redis 8.10.3 in WSL)

- `pytest --require-services` on the final code, twice: **1867 passed** in 356 s, and 1867 passed
  in 369 s; nothing skipped or failed. That is 1291 unit tests and 576 integration and fault
  tests; Phase 7 added 236 (134 unit, 102 integration and fault).
- Before the independent review's fixes, one full run: 1853 passed in 367 s.
- The real-process tests (`tests/fault`, `test_distributed.py`, `test_process_sessions.py`: 30
  tests), twice more on their own: 30 passed in 258 s, and 30 passed in 261 s.
- **Static checks:** `ruff check` and `ruff format --check`: clean (155 files). `mypy` strict
  (155 files): no issues.
- **Migrations:** head `0004`. The migration tests pass (upgrade from an empty database, the schema
  equals the models, downgrade and upgrade again, an interrupted migration leaves nothing).
- **`wsl docker compose config --quiet`:** valid; services `postgres` and `redis`, unchanged.
- **Development database:** upgraded from `0003` to `0004` with `alembic upgrade head`, as in
  Phase 4. Its fingerprint is `e574f91fa587b2ea` before and after, and its three queued jobs are
  still queued.
- **Secrets:** no value from `backend/.env` in any of the 55 changed or new files, and
  `backend/.env` is not tracked.
- **Earlier suites:** the Phase 2C to 6 tests still pass. Earlier tests were changed only to extend
  an enumeration (migration revisions, the trigger count, the commands checked) or to give the new
  settings their fast values in the timing tests; none was removed or weakened.
- **Benchmark:** see below and
  [`benchmarks/results/phase7-indexing.md`](../benchmarks/results/phase7-indexing.md).

**Review findings and fixes**

*Self-review* (my own checks, and failures of the new tests):

- **A split word was indexed:** cutting the body at the limit indexed the part of a word before
  the cut ("al" of "alpha"; found by the truncation test). The cut now drops it.
- **The vector size limit:** my first measurement used `pg_column_size`, but PostgreSQL's 1 MB
  limit counts lexemes and positions. Remeasured: distinct hyphenated CJK compounds need about
  6.7 bytes per character, so the maximum body setting (150,000) can exceed the limit for such
  text; the quarter-body fallback stays, and the documentation gives the measured numbers.
- **Stop-word-only queries were run:** `querytree()` returns an empty string, not `T`, for an
  empty query (found by a search test).
- **The job detail failed at run time:** two lateral joins made SQLAlchemy's FROM clause
  ambiguous; mypy passed, the API integration test did not. An explicit FROM fixes it.

*Independent review.* A separate agent reviewed the code against the invariants, read-only,
running unit tests only. It confirmed that the index writes, the version guard and triggers,
completion, lock ordering, search visibility and the refactors of the crawler worker and the
scheduler held under every interleaving it traced, and found:

1. **Medium:** an indexer could run more tasks than its connection pool holds; a task that then
   waited too long for a connection, after its attempt was counted, used up the attempt without
   indexing. Startup now refuses `SITESCOPE_INDEX_CONCURRENCY` or `--concurrency` above
   `SITESCOPE_DB_POOL_SIZE + SITESCOPE_DB_MAX_OVERFLOW`.
2. **Medium-low:** the deadline applied to each statement, not to the attempt, so an attempt on
   the quarter-body fallback path could outlive its lease; on the last attempt another indexer
   could fail a document whose attempt was about to succeed. The statements now share one budget
   (each gets what remains as `statement_timeout`), the indexer abandons the attempt when it is
   spent, and the reclaim idle time must exceed the deadline by at least 2 s (the lease outlasts
   the attempt by at least 1 s).
3. **Low:** a client-side timeout of a search surfaced as a generic error. The search has one
   deadline in all, both kinds of timeout map to `SearchTimeoutError`, and the search timeout must
   be shorter than the database command timeout.
4. **Low, latent:** a dispatch racing a content change could publish the previous version while
   the document waited for the current one; the indexer then dropped that message as `outdated`,
   and the document waited for the reconciler. An older message now starts an attempt like the
   current one: the attempt indexes the current content anyway.
5. **Low:** the dispatcher looked only at the 100 oldest unfinished jobs, so jobs still being
   indexed could delay the completion of newer ones. It now looks only at jobs with nothing left
   to index (`ix_search_documents_open`).
6. **Very low:** a word longer than the 100-character look-back that the cut split was indexed in
   part. The look-back is now 2,048 characters; a longer run of non-space characters is left
   whole, and the parser ignores it.
7. **Test quality:** a test of the single snapshot of a search, and of a client-side timeout,
   were missing (added); the reconciliation test's documents were labelled the wrong way round
   (fixed, and each entry's state is now asserted); the killed-indexer test now asserts exact
   attempt counts; the cancellation test no longer depends on relative speeds (a slow page holds
   its BFS level open); the duplicate-message test runs one task at a time.

Each finding has a regression test. With each defect put back into the code (findings 2 to 6,
and the snapshot), its test fails; all seven were checked that way and the files restored.

**Known limitations**

- **Language:** English-first. Other languages are not stemmed; Chinese, Japanese and other
  languages written without spaces are not segmented (a run of characters is one token). No
  multilingual search.
- **Ranking:** `ts_rank` with field weights only: no IDF or BM25, no typo tolerance. Positions are
  capped at 16,383 by PostgreSQL.
- **Search cost:** every match is ranked, so a common term costs more than a rare one; measured
  only on 2,000 pages (median 37 ms for a term in every page, 19 ms for a rare one).
- **Body limit:** only the first `SITESCOPE_INDEX_MAX_BODY_CHARS` characters of a body are
  searchable.
- **Duplicate work:** a page can be indexed twice (an attempt that outlived its lease and its
  successor), with the same result.
- **Cancelled jobs** keep being indexed for the pages they stored (the policy).
- **Content versions above 1** are exercised by tests only: nothing changes stored content yet
  (no re-crawl).
- **An attempt's start** waits for the document row lock without a deadline of its own; every
  transaction that holds that row is bounded.
- **Scaling:** indexing throughput grew 1.33x with 2 indexers and 1.66x with 4 (medians of three
  runs, all processes on one laptop), and whole runs on this laptop differed by up to a third;
  where the limit lies is not measured.
- **Compose:** `backend/compose.yaml` still runs only PostgreSQL and Redis (Phase 11).

**Notes for Phase 8**

- **The search endpoint** can call `search_job`: `InvalidInputError` (422), `NotFoundError` (404)
  and `SearchTimeoutError` (503) are domain errors already. Snippets are plain text with
  highlight ranges; never render them as HTML.
- **Paging:** limit 1 to 50 and offset at most 1,000 (`app/indexing/queries.py`); the results
  carry the job's indexing progress, for freshness.
- **The job detail** already has the `indexing` block.
- **Reindexing** (`request_reindex`) has no endpoint yet.

### Phase 8: Search and analysis APIs (done)

**Goal.** Expose search and website analysis of each crawl job through a stable, read-only REST
API for the dashboard, computed from the stored crawl results, without a second search engine,
without any request to a crawl target, and without reporting anything the crawl did not observe.

**Scope, reconciled with the plan.** Delivered as planned: `GET /api/v1/search` on the Phase 7
service, the pages of a job, link reports (broken, redirecting, unchecked and external links,
through each link's classification), link statistics (in- and out-link counts, the depth
distribution, the most-linked URLs), the endpoints and the indexes that support them, and the
planned tests. Changed from the plan:

- **Broken links** are narrower than architecture section 13 planned ("failed, or a status of 400
  or higher"): only a 404 or 410 at the end of the target's redirects. Every other outcome has its
  own class (ADR-0019, which amends section 13).
- **Search pages with `page` and `page_size`**, not a cursor: results are ranked, and the
  service's offset bound (1,000) stays.
- Added: the link graph endpoint, a statistics endpoint, `progress.pages_stored` on the job
  detail, `SITESCOPE_ANALYSIS_TIMEOUT_SECONDS`, a benchmark of the analysis queries, and an
  end-to-end test of the whole pipeline through the API with real processes.
- Added after the review: the `verified` and `redirected` filters of the links listing.
- Not done: "pages reached through only one in-link" (the graph gives every in-degree), grouping
  external links by host, and a reindex endpoint.

**Delivered** (design in ADR-0019; architecture: the status note and sections 3 to 5, 13, 16,
18 to 20; api-conventions "Search" and "Website analysis")

- **Endpoints** (`app/api/v1/search.py`, `app/api/v1/analysis.py`, schemas next to them):
  `GET /api/v1/search`; `GET /api/v1/crawls/{job_id}/pages`, `/links` (filters: source page,
  internal, class, `verified`, `redirected`), `/broken-links`, `/link-graph`, `/stats`.
  Explicit response models with examples, typed and bounded parameters, problem+json errors
  (`404`, `422` with `invalid_cursor`, `empty_query`, `query_too_long`, `page_out_of_range`;
  `503` with `search_timeout` or `analysis_timeout`), OpenAPI tags with the terms (page,
  discovered URL, link, document, broken, unverified).
- **Analysis service** (`app/services/site_analysis.py`): every query in SQL, one read-only
  REPEATABLE READ transaction per call, a statement timeout and a client deadline, forced custom
  plans. Link classification in eleven classes, redirect chains followed through the job's own
  results (loops detected), confirmed broken links, the link graph with its limits and
  truncation, statistics. Keyset pagination with typed, validated cursors.
- **Migration `0005`:** `pages (job_id)` becomes `pages (job_id, normalized_url)`;
  `page_links (job_id, target_normalized_url)` gains `INCLUDE (is_internal)`. The number of
  indexes is unchanged; both were measured first.
- **Job detail:** `progress.pages_stored`; clearer descriptions of the crawl's `finished_at`
  versus the indexing's `completed_at`.
- **Configuration:** `SITESCOPE_ANALYSIS_TIMEOUT_SECONDS` (10 s, shorter than the database
  command timeout), validated at startup, documented in `.env.example`.
- **Benchmarks:** `benchmarks/analysis.py` and `benchmarks/analysis_index_ab.py`, with raw
  results and plans (`benchmarks/results/phase8-*`).
- **Docs:** ADR-0019; architecture, API conventions, development guide, CLAUDE.md (invariant 13:
  analysis reports only what the crawl observed).

**Performance** (`benchmarks/results/phase8-analysis.md`; this laptop, synthetic data, 50 links per
page, p50 of 20 calls, final code): under 90 ms at the median for a 1,000-page job (the default
page budget), except the link graph at its maximum limits (148 ms). For a 10,000-page job (500,000 links): the
pages and links listings 9 to 17 ms, broken links 185 ms, a class-filtered link listing 323 ms,
the link graph 156 ms (500 nodes) and 247 ms (2,000 nodes), statistics 593 ms. Before the
optimizations, the same link listing took 2.1 s after five calls on one connection
(PostgreSQL's generic plan), and the statistics 1.2 s.

**Tests** (1985 in all: 1373 unit, 612 integration and fault; 118 more than after Phase 7)

- **Unit:** cursor round trips and every rejection (another listing's cursor, altered, non-ASCII,
  NUL, booleans, out-of-range integers, deep nesting), every parameter bound through HTTP, the
  search page arithmetic, response mapping, OpenAPI (operations, problem responses, parameters,
  response models, examples, tags), the analysis timeout setting, timeouts against session test
  doubles, and that neither search terms nor cursors are logged.
- **Integration (real PostgreSQL):** every link class from stored results, redirect chains (2xx
  and 404 at the end, loops, self-redirects, chains leaving the job's hosts or never admitted, a
  300-URL chain), retry-pending targets, the depth and budget limits, nofollow links and pages,
  every listing paged without gaps or repeats, filters, broken links, the graph and its limits,
  statistics (all counts, the status buckets, an empty job, one snapshot under a concurrent
  commit), job isolation, unknown jobs, statement and client timeouts, the transaction settings;
  the same through HTTP with pages indexed by the Phase 7 services; migration 0005 up and down.
- **End to end (real processes):** `tests/fault/test_api_end_to_end.py` creates a job through the
  API on the demo site served on 127.0.0.1, runs the scheduler, two crawler workers, the index
  dispatcher and two indexers, waits for indexing to complete, and checks every endpoint against
  the demo site's known structure (14 URLs, 12 pages, 11 indexed, 26 links, one 404, one
  robots.txt refusal, one nofollow target, two external links).

**Independent review** (read-only; it ran the unit tests only: 1359 passed). No high-severity
finding. Every finding was low and is fixed, each with a regression test:

1. A final 1xx response (httpcore2 returns a 101 as final) fell in no HTTP status bucket, so the
   buckets did not add up to the finished requests. Added `informational_1xx`.
2. A `TimeoutError` that was not the call's own deadline (asyncpg's connect timeout, 5 s) was
   reported as `analysis_timeout`, and in the Phase 7 search service as `search_timeout`. Only the
   expired deadline or PostgreSQL's statement timeout is a timeout now; anything else reaches the
   API's outage handler (`503 dependency_unavailable`, `Retry-After`).
3. `page=1002` and above got `less_than_equal`, not `page_out_of_range`, and a query error was
   not reported with a page error. The page has no schema maximum now; both are checked together.
4. Benchmark and ADR summaries contradicted the raw data (a 149 ms case under a "90 ms" claim,
   "within 6%" for a 12% spread, broken links said to classify every link). Corrected, and both
   benchmarks were run again on the final code.
5. Six doc statements were imprecise: filtered cursor walks while values change, the order the
   graph's edges are cut in, `final_url` of a self-redirect, what `queued` means, which requests
   read every link, and what a depth's `fetched` counts. Corrected.
6. URLs admitted through links can form redirect chains of any length, and the chain query grew
   with the square of their length. Chains are followed for at most 20 redirects
   (`redirect_error` beyond; the most `SITESCOPE_FETCH_MAX_REDIRECTS` allows).
7. Scope: there was no filter for redirecting links. The links listing gained `redirected` and
   `verified` (unchecked links are `verified=false`).

**Mutation check.** Sixteen defects were put back into the code one at a time: ten against the
design rules (a retry's status deciding the class, every 4xx counted as broken, READ COMMITTED
statistics, no forced custom plans, a link's target joined across jobs, the broken-link
prefilter forgetting 410, the budget ignored, loop detection removed, a nested cursor escaping
as a 500, an off-by-one in the deepest search page) and six against the review fixes and
filters. Each made its test fail; every file was restored and checked by hash.

**Known limitations**

- **A snapshot of the crawl:** a target that answered 404 stays broken for that job; nothing is
  checked again (by design: no request outside the crawl).
- **Reasons not recorded** in the stored rows are reported as `not_crawled`: the redirect hop
  limit, a redirect to an unapproved URL of the job's host, a job that ended before admitting a
  level. An unreachable robots.txt counts as `robots_blocked`.
- **Cost grows with the links of a job:** statistics, broken links and class-filtered listings
  read all of them (0.2 to 0.6 s at 500,000 links on this laptop). A much larger job would reach
  the 10 s limit and get `503 analysis_timeout`; precomputed summaries would be the next step.
- **Index-only scans** need the visibility map: right after a crawl, before autovacuum, the
  statistics read the heap as before.
- **No caching** of analysis responses; the link graph at its maximum limits is about a megabyte.
- **Highlights** are Unicode code point offsets (JavaScript must index `Array.from(snippet)`).
- **Cursors** carry URLs, so they can be several kilobytes long (at most 8,192 characters).
- **Search** keeps the Phase 7 limits (English-first, `ts_rank`, results up to offset 1,000).

**Notes for Phase 9**

- Generate the client from `/openapi.json`; operation IDs are the handler names
  (`search_pages`, `list_crawl_pages`, `list_crawl_links`, `list_crawl_broken_links`,
  `get_crawl_link_graph`, `get_crawl_stats`).
- Render snippets and every URL, title and anchor text as text, never as HTML (architecture
  section 12.6).
- Show crawl status and indexing status separately; poll the job detail while either runs.
- Pass `next_cursor` back unchanged with the same filters; search uses `page` and `page_size`.

### Phase 9: React dashboard

**Status: done (2026-11-08).** Design: [ADR-0020](adr/0020-dashboard-same-origin-typed-client.md);
details and commands: `frontend/README.md` and [development.md](development.md), "Dashboard".

**Delivered**

- **The dashboard** (`frontend/`): Vite 8, React 19, TypeScript 5.9 (strict), Tailwind CSS 4,
  React Router 8, TanStack Query 5, Recharts 3, lucide-react. Five views in a shell with a
  sidebar, a header (page title, job selector, API readiness, auto-refresh interval, refresh) and
  loading, empty, error and retry states everywhere:
  - **Overview:** counts over every job (jobs by status, admitted and failed URLs, stored pages,
    indexed documents), the crawl status and indexing charts, the newest job's HTTP status and
    BFS depth distributions, the recent jobs.
  - **Crawl jobs:** the creation form (approved targets offered from the server's crawl policy;
    every value checked against the policy and sent unchanged; an `Idempotency-Key` per
    submission, reused when the outcome of a request is unknown), the cursor-paged job list with
    each row's progress and indexing status, and the job detail (configuration, crawl progress,
    URL states, indexing progress, failures, timestamps, cancellation).
  - **Search explorer:** ranked results with plain-text snippets, highlights from code-point
    ranges, scores, numbered pages up to the deepest page served, stop-word and empty-result
    states, indexing-in-progress notes.
  - **Site analysis:** statistics (discovered and admitted URLs, pages, depth, successes and
    failures; HTTP status classes including 1xx; depth distribution; indexing; links by class with
    verified and unverified kept apart; most linked URLs), and cursor-paged pages, links (filters:
    class, verified, internal, redirected, source page) and confirmed broken links.
  - **Link explorer:** the bounded internal link graph in SVG, laid out by BFS depth, with pan,
    zoom, node selection (outgoing and incoming links), node details from the API only, a
    keyboard-reachable node list, a truncation warning with the real totals, and no orphan claim.
- **Polling** by job lifecycle: the viewer's interval (default 3 s) while the crawl is active,
  twice that (at least 5 s) while only indexing runs, none once settled; nothing in hidden tabs or
  after a page is left; a pause, with the reason, when a job shows no change for minutes.
- **Backend additions** (read-only, with tests): `GET /api/v1/crawl-policy` (approved targets and
  limits from the settings job creation enforces), `GET /api/v1/overview` (counts over every job
  in one read-only REPEATABLE READ snapshot under the analysis time limit; `read_only_snapshot`
  extracted from the analysis service for it), `python -m app.api.openapi_export` (the OpenAPI
  snapshot the frontend's types are generated from) and a test that fails while the snapshot is
  stale, `Cluster.api()` and `tests/dashboard_stack.py` (the whole stack on a disposable
  database for the end-to-end run). No CORS: the dashboard uses the API's origin through a proxy.

**Tests**

- **Frontend unit and component tests** (Vitest 5, React Testing Library, jsdom; 162 in 15 files):
  the API client (URLs and encoding, problem documents, proxy errors, network failures, timeouts,
  cancellation, readiness 503), error descriptions (reads and actions), Unicode highlights (ASCII,
  Chinese, emoji, supplementary-plane characters, several, overlapping and out-of-range ranges),
  the polling rules and their lifecycle on fake timers (interval, stop when settled, hidden tab,
  unmount, pause and resume, a failed refresh), the job form (policy checks, IPv6, exact values,
  idempotent retries, server field errors, focus), the job list (cursor pages, page size, outage
  and retry), the job detail (separate crawl and indexing states, failures, not found) and
  cancellation (confirmation, 202, 200, a job that ended first, errors, focus, never another
  job's dialog), search (requests, highlights, markup shown as text, empty and stop-word queries,
  page limits, validation errors, timeouts, job switch), analysis (statistics, 1xx, link classes,
  confirmed broken links only, filters and cursors, tabs), the graph (layout, node kinds,
  selection from the list and the drawing, truncation, limits), the shell (navigation, job
  selection, API status, not found, drawer, auto-refresh), the overview, and the components
  (dialog focus and Escape, safe links, meters). The API is a test double of `fetch`
  (`src/tests/fakeApi.ts`) with fixtures typed by the generated schema.
- **Backend** (15 more tests; 2000 in all): the crawl policy (defaults, normalization, the limits
  job creation enforces, nothing private), the overview (counts over four kinds of jobs, an empty
  deployment, one snapshot under a concurrent commit, the transaction settings, a statement
  timeout, HTTP), the snapshot test of the OpenAPI document, and the overview in the read-timeout
  tests.
- **End to end with the real stack** (`python -m tests.dashboard_stack --e2e`; Playwright 1.65 in
  the installed Microsoft Edge 157): 8 tests against the API, PostgreSQL, Redis, the scheduler,
  two crawler workers, the index dispatcher and two indexers on a disposable database, with the
  demo site and a slow site on 127.0.0.x: the dashboard reaches the API; a job created in the form
  is crawled and fully indexed while its page follows it, and every number shown equals the API's
  (14 URLs, 12 pages, 11 indexed); search (a match marked, `noindex` pages absent); the statistics,
  the one confirmed broken link (`/about.html` to `/missing.html`, 404) and a robots-blocked link
  shown as unverified; the link graph (15 nodes, 24 links, a selected node's details, the
  truncation warning at 5 nodes); a running crawl of the slow site cancelled through the dialog
  and confirmed `cancelled` by the API; no page scrolls sideways at 1440 or 390 pixels; unknown
  jobs and pages. Opt-in screenshots of every page (`E2E_SCREENSHOTS=1`) served the visual
  review.

**Visual review.** Screenshots of every page at desktop and phone widths showed tables widening
the page: screen-reader-only text inside a table (absolutely positioned) escaped its scroll
container, and a control's full-width class beat the width given to it. Both fixed; the
end-to-end suite now checks every page for sideways scrolling.

**Independent review** (read-only; it ran the frontend tests, type check and lint, and the backend
unit tests, ruff and mypy). One high-severity finding, four medium, and low ones; all fixed, the
confirmed ones with regression tests:

1. **High:** the cancel dialog could cancel another job. On a job's page, with the dialog open,
   the browser's back button to a job already cached kept the dialog open for that job. The job
   page now renders one component tree per job (`key`), and the dialog names the job.
2. Short job IDs were the first 8 characters of UUIDv7 IDs: a timestamp, the same for jobs
   created within about a minute. They are the random tail now.
3. The stall guard was a wall-clock timer: a tab hidden for five minutes (no polling) came back
   paused. It counts unchanged reads now (`useStallWatch`), so hidden time never counts.
4. Search kept another job's results on screen while the newly selected job's search ran.
5. An edit that changed nothing replaced the `Idempotency-Key` of an uncertain submission, so a
   retry could create a second job. Requests are compared by value now.
6. Low: an approved IPv6 target could not be entered (the server keeps brackets); error texts for
   actions promised automatic retries (and error notifications vanished after 10 s); prototype
   keys passed the link-class filter; statistics stopped refreshing while indexing still ran; the
   graph re-centered on a stale node after a reload and could not fit very tall graphs; the client
   gave up on slow analysis requests before the server's limit and retried them; every table row
   re-rendered each second; invalid fields were not focused, and focus was lost after a
   cancellation; tabs referenced panels that were not rendered; the auto-refresh selector could
   show another interval than the one in use; clearing the job selection did not stick; never
   requested targets read "no response"; the stack runner's cleanup could stop early (a second
   Ctrl+C, a failed migration, a Redis error) and did not pin the API path of the build; two
   schema descriptions said "the job's" where the overview counts every job.

**Mutation check.** Nine of the review's defects were put back into the code one at a time; each
made its regression test fail, and every file was restored and checked by hash.

**Known limitations**

- No authentication, and job creation is not rate-limited: a public deployment needs both
  (Phase 11, architecture section 12.7). The approved-target model is unchanged: the form only
  guides, the server enforces.
- A job list page reads one job detail per row (bounded by the page size); the overview counts
  whole tables under the analysis time limit. Batched summaries would be the next step for much
  larger deployments.
- Cursor walks are kept in memory: a reload starts a listing from its first page.
- The graph is drawn in SVG; at the API's maximum limits (2000 nodes, 10000 links) the first
  drawing takes noticeably longer than at the defaults (300, 1500).
- Dark theme only; English only.

### Phase 10: Performance engineering, reliability validation and observability

**Status: done (2026-11-08).** Measurements, method and limits:
[performance.md](performance.md); operations: [observability.md](observability.md); decisions:
[ADR-0021](adr/0021-operational-metrics-and-process-heartbeats.md),
[ADR-0022](adr/0022-dispatch-window-follows-host-politeness.md),
[ADR-0023](adr/0023-whole-job-statements-use-hash-joins.md). The owner's Phase 10 brief replaced
the earlier scope (1, 3 and 5 workers instead of 1, 2, 4 and 8; `docs/performance.md` instead
of `docs/benchmarks.md`).

**Delivered**

- **Benchmark harness** (`benchmarks/`, Phase 10 scripts and `benchlib.py`): deterministic
  generated sites (nested, duplicate and cyclic links, redirects, 404s, 503s and 500s, delays,
  page sizes) served on loopback addresses; disposable databases and Redis key prefixes; code
  variants from a `git worktree` with interleaved runs; samplers of the machine's CPU load and
  effective clock, the WSL VM, the PostgreSQL and Redis containers, every process's CPU and
  memory, PostgreSQL's statistics and sessions; raw JSON, plans as text, CSV summaries and SVG
  charts computed from them. Scripts: crawl scaling, indexing scaling, search latency, database
  statements, level admission and analysis reads with stale statistics, recovery from failures,
  the dashboard's link graph.
- **Measurements** (all in performance.md, each with its command and raw output): crawling with
  1, 3 and 5 workers on one host and on five, and job sizes of 100 to 2,000 pages; indexing with
  1, 2 and 4 indexers and the GIN index's `fastupdate` on and off; search latency by query kind
  and concurrency; the pipeline's and the dashboard's statements on a 1,000,000-link database;
  worker and indexer crashes, Redis and PostgreSQL outages, cancellation under load; the link
  graph at 300 to 2,000 nodes and 1,500 to 10,000 links.
- **Optimizations, each measured before and after:** the dispatch window follows the host's
  politeness slots (ADR-0022: five hosts with 5 workers, 32.6 to 90.8 pages per second); the
  level admission and every analysis read are planned with hash joins (ADR-0023: a level
  transition of 18 to 22 s, and analysis reads that ran into the 10 s time limit, when the
  statistics had not seen a job); the link graph labels only the nodes in and around the view.
- **Observability** (ADR-0021): a process-local metrics registry with bounded labels; process
  heartbeats in Redis; `GET /metrics` in the Prometheus text format, state counted from
  PostgreSQL in one snapshot and per-process series labelled `role` and `process`, so that
  nothing is counted twice; stage times, queue delay (PostgreSQL clock) and failure categories in
  every `task.handled` line; `python -m app.scheduler.status` lists live processes and assesses
  health (PostgreSQL, Redis, the API, crawl progress, workers, indexing, repeated failures,
  recovery), from PostgreSQL and heartbeats only.

**Tests.** Backend: 2,082 tests (82 new): 1,448 unit, 634 integration and fault. Dashboard: 167
Vitest tests (5 new); the 8 end-to-end tests unchanged. New:

- **Unit:** the metrics registry (labels, bucket edges, the series cap, positional values) and
  the text format (escaping, names, shapes, damaged snapshots); `GET /metrics` rendering, its
  absence from the OpenAPI document and the request histogram by route template; every rule of
  the health assessment, including the review's three defects; the pass loop's heartbeat state;
  the dispatch window and the reports' `progressed`; the politeness gate's timings; failure
  categories; the new settings.
- **Integration** (real PostgreSQL and Redis): heartbeats (expiry, retirement, the index);
  `/metrics` with real data and with Redis down; the queue delay by the PostgreSQL clock; the
  scheduler's window (two jobs get 8 tasks each, interleaved in the stream); the level admission
  and the analysis reads with statistics that have not seen the job (plans and statements, not
  time); the crawler's stage times and failure categories.
- **Fault** (real processes): heartbeats per role, per-process completed counts that add up to
  PostgreSQL's, a killed worker's heartbeat expiring, a stopped one's removed at once.
- **Dashboard:** the labelled region (`viewport.test.ts`), and a click on a label selecting its
  node.

The first full run after the changes found two failures caused by this phase, both fixed: an
index attempt compared with `==` now carried its measured queue delay (excluded from equality
since: it is a measurement), and the Redis data-loss fault test lost its precondition with the
smaller dispatch window (no dispatched task left to lose; it now runs with the wider window it
needs). **Mutation checks:** the fixes of the review's defects and of the analysis reads were
removed one at a time; each made its regression test fail, and every file was restored and
verified by hash.

**Independent review** (read-only, with a second reader for the benchmarks). Three confirmed
backend defects, all in the health assessment, fixed with regression tests and mutation-checked:
with Redis down and PostgreSQL up, crawl progress and indexing could still read "ok"; a job
whose step failed in every scheduler pass looked like progress and hid the failure; a scheduler
pass that hangs was not seen (its heartbeat stays fresh), nor a worker with tasks in flight that
never finished one. Benchmark defects, fixed before the final runs: the recovery containers came
back on another port after a restart; autovacuum refreshed the "stale" statistics mid-run; the
query benchmark timed the admission without the production setting; the indexing samplers
included the bulk load; and several low-severity ones. Suggestions taken: the crash cost of the
smaller dispatch window is documented in ADR-0022; the queue delay is documented as measured
from the last publish.

**Known limitations**

- One laptop: every process, both databases and the test sites share its cores; scaling numbers
  mix the code's scaling with the machine's. Synthetic sites on loopback, development
  politeness. Recovery used the process tests' short timings.
- The deployment-wide counts of `/metrics` and the overview read whole tables.
- Analysis reads now cost time linear in the job also when the statistics are fresh (a page of
  links: 19.5 ms instead of 0.5 ms for 22,220 links).
- No Prometheus server, Grafana or alerting is part of the project.

### Phase 11: Final deployment and documentation

**Scope**

- Images: one backend image for every Python role, running as a non-root user; a frontend image
  served by nginx with an `/api` reverse proxy; a demo-site container.
- A full `docker-compose.yml` with:
  - health checks;
  - a one-shot migration service;
  - internal networks (in the public profile, workers have no general egress);
  - worker scaling;
  - request size limits at the proxy;
  - a PostgreSQL role for the application that is not a superuser (ADR-0011).
- CI, once a GitHub remote exists:
  - backend lint, type check, and unit tests;
  - integration and fault tests against PostgreSQL and Redis service containers;
  - frontend type check, tests, and build;
  - image builds;
  - optional dependency audits (`pip-audit`, `npm audit`).
- Documentation:
  - a README with an overview, diagram, quickstart, design highlights, and limitations;
  - an operations runbook covering recovery procedures and DLQ inspection;
  - a review of the ADRs.

**Tests.** A scripted smoke test: `docker compose up` → create a job → wait for completion →
check that search returns results → `docker compose down`.

**Done when**

- CI is green on GitHub.
- A new reader can run the system from the README alone.

## Definition of done (every phase)

1. Only the requested phase is implemented, and it includes tests.
2. Every check that can run in this environment was run. The report lists the exact commands and their
   real results.
3. A test that could not run (for example, because Docker is missing) is reported as **not run**, with
   the reason and the setup steps. It is never reported as passed.
4. This roadmap is updated. `architecture.md` is updated if the design changed, and a changed decision
   gets a new ADR.
5. The phase report lists the files created or modified and the steps for verifying them manually.

## Decisions pending from the project owner

1. **Create a GitHub repository and push.** The local repository exists, but a remote is needed
   before the CI workflow can be added and verified.
2. **Choose the public demo target.** Either the self-hosted demo-site container (the current plan) or a
   separately hosted static site.
3. **Choose a license for the public repository** (for example, MIT).

Resolved:

- Phase 1: `fronted/` was renamed to `frontend/`, and git was initialized locally.
- 2026-11-06: Docker Engine was installed inside WSL Ubuntu (see Environment setup).
