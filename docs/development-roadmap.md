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
| 5 | Distributed scheduling and workers | Not started |
| 6 | Fault tolerance and recovery | Not started |
| 7 | Asynchronous search indexing | Not started |
| 8 | Search and analysis APIs | Not started |
| 9 | React dashboard | Not started |
| 10 | Testing, benchmarking, and observability | Not started |
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

## Repository state after Phase 4

Updated on 2026-11-06. The Phase 0 inspection findings are kept in this file's first commit.

| Item | State |
|---|---|
| Git | Local repository on branch `master` (the configured default). Every phase so far is committed. No remote yet. |
| `backend/app/` | FastAPI foundation (Phase 1); database models, sessions, Redis client, readiness (Phase 2B); crawl job API, URL normalization and crawl scope (Phase 2C); safe fetcher and HTML parser in `app/crawler/` (Phase 3); crawl state in `app/services/crawl_state.py`, robots.txt, politeness and page directives in `app/crawler/`, the crawl task and the single-process driver in `app/workers/` (Phase 4). |
| `backend/alembic/` | Migrations `0001` (the six-table schema), `0002` (fencing tokens issued only by claims) and `0003` (robots directive flags). |
| `backend/compose.yaml` | PostgreSQL 18.6 and Redis 8.10.3 for development, running in WSL. |
| `backend/tests/` | 1204 tests: 933 unit, 271 integration (real PostgreSQL and Redis, and local HTTP/TLS servers on 127.0.0.1). |
| `backend/.venv` | Python 3.13.7. Runtime: fastapi 0.162.0, uvicorn 0.54.0 (standard extras), pydantic-settings 2.16.2, SQLAlchemy 2.1.8, asyncpg 0.32.0, Alembic 1.20.1, redis 8.1.0, idna 3.20, httpcore2 2.16.0, truststore 0.10.5, beautifulsoup4 4.15.0. Development: pytest 9.2.0, httpx2 2.16.0, ruff 0.17.4, mypy 2.4.1, trustme 1.2.1. |
| `frontend/` | Empty. Renamed from the misspelled `fronted/` in Phase 1. |
| `docs/` | Architecture, roadmap, ADRs 0001–0015, API conventions, development guide. |
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

### Phase 5: Distributed scheduling and workers

**Goal.** Run the Phase 4 crawl across processes. The scheduler publishes committed work to Redis
Streams, and workers claim and process it.

**Scope**

- `app/queue`:
  - a Redis Streams adapter: idempotent group creation, publish, read, ack, autoclaim, and
    dead-letter;
  - versioned message schemas.
- `app/scheduler`:
  - job activation: `queued` jobs found through `ix_crawl_jobs_active` move to `running` with a
    conditional update;
  - a dispatcher that treats the frontier as the outbox, gates by level, enforces a per-job
    in-flight window, and serves jobs round-robin;
  - level advancement and job completion.
- `app/workers`:
  - a consumer runtime with bounded concurrency and graceful shutdown;
  - the crawl task flow: claim → fetch → fenced result commit → `XACK`;
  - consumer names unique per process start (ADR-0011).
- A deterministic in-memory site that implements the fetcher interface, used only as a test
  double. It is not a crawler and is not presented as one.

**Tests**

- Stream-adapter semantics.
- A duplicate message has a single effect, and publishing before commit is safe.
- 1, 2, 4, and 8 concurrent workers crawl synthetic graphs. The tests check the correctness
  invariants, and that 1-worker and N-worker results are identical.
- Repeated runs, to shake out races.

**Done when.** All invariant tests pass across repeated runs.

**Notes from Phase 4**

- Reuse `services.crawl_state` as is: `dispatch_urls` gains the `XADD` and `stream_entry_id`
  inside its transaction; workers call `claim_url`, `crawl_task.crawl_url` and `record_result`,
  and must not `XACK` a `FENCED` result (architecture section 10.3).
- The scheduler calls `advance_job` per running job; it is safe to call concurrently.
- Build the fetcher, gate and robots policy with `build_fetcher(settings, gate=build_gate(...))`
  and `build_robots_policy(...)`, one set per process. Consumer names: `worker_name()`.
- The fixture graphs and expected results in `tests/integration/test_local_crawler.py` are the
  reference for the 1-worker vs N-worker tests across processes.

### Phase 6: Fault tolerance and recovery

**Goal.** Survive crashes, stalls, Redis data loss, and cancellation, and prove it with automated
tests.

**Scope**

- An `XAUTOCLAIM` recovery loop and lease takeover, with fencing that rejects stale workers.
- Durable retries with exponential backoff and full jitter, and attempt limits.
- Dead-letter streams.
- A reconciler for lost or trimmed messages, abandoned leases, and exhausted attempts.
- Cancellation end to end: `POST /api/v1/crawls/{id}/cancel` (`cancelling -> cancelled`).
- `Idempotency-Key` on `POST /api/v1/crawls` (failure window F1). The columns exist since
  Phase 2B.
- Stream trimming and cleanup of idle consumers.
- A scheduler leader lock.
- Startup validation of the timing order.

**Tests.** One automated test per failure window F1–F13 in the architecture, including:

- a crash before claim, after claim, and after commit but before ack;
- a stalled worker that resumes after another worker has taken over;
- `FLUSHALL` mid-crawl, after which the crawl completes with identical results;
- a dispatcher crash between `XADD` and commit;
- a poison message that lands in the DLQ;
- cancellation while tasks are in flight;
- killing a real worker subprocess.

**Done when.** Every failure window has a passing test, or is documented as untested together with
the reason.

### Phase 7: Asynchronous search indexing

**Scope**

- The index relay, which publishes `pending` `search_documents` rows (ADR-0008), and the indexer
  worker.
- A weighted `tsvector` with truncation. The `search_documents` table and its GIN index exist
  since Phase 2B.
- Index writes guarded by the content version, so a slow indexer cannot overwrite a newer version
  (Phase 2C review finding).
- Indexing progress on the job detail, and `indexing_completed_at`.

**Tests**

- The relay crash window (duplicate publish).
- Duplicate and replayed index events.
- A stale index write is rejected.

### Phase 8: Search and analysis APIs

**Scope**

- `GET /api/v1/search`:
  - `websearch_to_tsquery` for parsing and `ts_rank_cd` for ranking;
  - safe `ts_headline` snippets;
  - pagination;
  - scoped to a job.
- The pages of a job.
- Link analysis:
  - reports on broken, redirecting, unchecked, and external links;
  - link statistics: in-link and out-link counts, the depth distribution, and the most-linked
    pages;
  - API endpoints, and the indexes that support them.

**Tests**

- A title match outranks a body match.
- Arbitrary query strings never cause an error.
- Search over the fixture crawl.
- A fixture site with known broken links, redirect chains, and depth-limited links, checked against
  exact expected reports.

### Phase 9: React dashboard

**Needs.** Node.js (installed).

**Scope**

- A Vite + React + TypeScript app.
- A jobs list and a create-job form limited to approved targets and caps.
- A job detail page with polled progress (state counts, current level, budget, indexing progress),
  the page list, and link reports.
- A search page with safe highlighting.
- Loading, error, and empty states.

**Tests**

- Vitest + Testing Library for components and the API client.
- `tsc --noEmit`.
- A production build.

### Phase 10: Testing, benchmarking, and observability

**Scope**

- Benchmarks: a `benchmarks/` harness that runs against the demo site, with a documented,
  development-only higher rate limit.
- Measurements:
  - crawl throughput vs. worker count (1, 2, 4, 8);
  - job completion time;
  - dispatch-to-claim latency;
  - indexing throughput;
  - search latency p50/p95/p99 over a fixed query set.
- Environment capture (CPU, RAM, OS, versions, settings), raw results committed to the
  repository, and `docs/benchmarks.md` describing the methodology and its limitations.
- Observability: queue statistics (approximate, from Redis) next to progress (from PostgreSQL),
  context fields in logs (`job_id`, `url_id`, `worker_id`, `lease_epoch`), and process heartbeats.
- Test hardening: the end-to-end and fault suites run repeatedly.

**Rule.** Every reported number comes from a run in this repository and is published with its
command and raw output. Nothing is extrapolated.

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
