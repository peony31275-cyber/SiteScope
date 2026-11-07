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
| 2C | Job API and URL normalization | Not started |
| 3 | Task distribution core | Not started |
| 4 | Fault tolerance and recovery | Not started |
| 5 | Safe HTTP crawling | Not started |
| 6 | Indexing pipeline and search API | Not started |
| 7 | Link analysis | Not started |
| 8 | React + TypeScript dashboard | Not started |
| 9 | Docker Compose deployment and demo site | Not started |
| 10 | Benchmarks | Not started |
| 11 | CI hardening and documentation | Not started |

## Repository state after Phase 2B

Updated on 2026-11-06. The Phase 0 inspection findings are kept in this file's first commit.

| Item | State |
|---|---|
| Git | Local repository on branch `master` (the configured default). Every phase so far is committed. No remote yet. |
| `backend/app/` | FastAPI foundation (Phase 1); database models, sessions, Redis client, readiness (Phase 2B). |
| `backend/alembic/` | Migration `0001` (the six-table schema). |
| `backend/compose.yaml` | PostgreSQL 18.6 and Redis 8.10.3 for development, running in WSL. |
| `backend/tests/` | 165 tests: 89 unit, 76 integration (real PostgreSQL and Redis). |
| `backend/.venv` | Python 3.13.7. Runtime: fastapi 0.162.0, uvicorn 0.54.0 (standard extras), pydantic-settings 2.16.2, SQLAlchemy 2.1.8, asyncpg 0.32.0, Alembic 1.20.1, redis 8.1.0. Development: pytest 9.2.0, httpx2 2.16.0, ruff 0.17.4, mypy 2.4.1. |
| `frontend/` | Empty. Renamed from the misspelled `fronted/` in Phase 1. |
| `docs/` | Architecture, roadmap, ADRs 0001–0010, API conventions, development guide. |

| Tool on the development machine | Status |
|---|---|
| Python 3.13.7 (`backend/.venv`) | available |
| Git 2.52 | available |
| Node.js 24.14, npm 11.9 | available |
| WSL 3.0.3 with Ubuntu 24.04.3 LTS (WSL 2, systemd enabled) | available |
| Docker Engine 29.9.3 + Docker Compose v5.7.0, inside WSL Ubuntu | installed and verified (2026-11-06) |
| PostgreSQL, Redis | not installed locally; they will run as Docker containers (Phase 2) |

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

### Phase 2C: Job API and URL normalization

**Goal.** The remaining scope of the original Phase 2: creating, listing, inspecting and
cancelling crawl jobs, on top of the Phase 2B schema.

**Scope**

- URL normalization, scope checks and allowlist validation, as pure functions with table-driven
  tests. The output must satisfy the database's URL CHECK (ADR-0009).
- Service functions that own the transactions:
  - create the job and its seed URL atomically;
  - cancel a job (architecture section 8.6);
  - record job lifecycle events in `crawl_events`.
- Job API:
  - `POST /api/v1/jobs` (with `Idempotency-Key` and a request fingerprint)
  - `GET /api/v1/jobs` (keyset pagination, status filter)
  - `GET /api/v1/jobs/{id}` (state counts, current level, budget)
  - `POST /api/v1/jobs/{id}/cancel`
- The CI workflow (deferred from Phase 1), if a GitHub remote exists by then.

**Tests**

- Unit: the normalization corpus; allowlist and cap validation.
- Integration:
  - the job and its seed URL are created atomically;
  - an `Idempotency-Key` replay returns the same job, and a mismatched body returns 409;
  - cancel transitions;
  - errors are returned as problem+json.

**Needs.** The Phase 2B services, and a WSL session kept open.

### Phase 3: Task distribution core

**Goal.** Distributed crawl execution with correct BFS, exact budgets, and processing that is safe against
duplicates, driven by a test-double fetcher.

**Scope**

- `app/queue`:
  - a Redis Streams adapter: idempotent group creation, publish, read, ack, autoclaim, and dead-letter;
  - versioned message schemas.
- `app/scheduler`:
  - job activation;
  - a dispatcher that treats the frontier as the outbox, gates by level, enforces a per-job in-flight
    window, and serves jobs round-robin;
  - level advancement and job completion.
- Start from the claim and fenced-commit statements already exercised against the real schema in
  `tests/integration/test_leases.py`.
- `app/workers`:
  - a consumer runtime with bounded concurrency and graceful shutdown;
  - the crawl task flow: claim (compare-and-set + `lease_epoch`) → `Fetcher` protocol → fenced result
    commit → `XACK`.
- A deterministic in-memory site that implements `Fetcher`, used only as a test double. It is not a
  crawler and is not presented as one; the real fetcher arrives in Phase 5.

**Tests**

- Stream-adapter semantics.
- A duplicate message has a single effect, and publishing before commit is safe.
- 1, 2, 4, and 8 concurrent workers crawl synthetic graphs that contain cycles, diamonds, and shortcuts to
  deep nodes. The tests check:
  - the architecture's correctness invariants;
  - that 1-worker and N-worker results are identical.
- Repeated runs, to shake out races.

**Done when.** All invariant tests pass across repeated runs.

### Phase 4: Fault tolerance and recovery

**Goal.** Survive crashes, stalls, Redis data loss, and cancellation, and prove it with automated tests.

**Scope**

- An `XAUTOCLAIM` recovery loop and lease takeover, with fencing that rejects stale workers.
- Durable retries with exponential backoff and full jitter, and attempt limits.
- Dead-letter streams.
- A reconciler for lost or trimmed messages, abandoned leases, and exhausted attempts.
- Cancellation end to end (`cancelling -> cancelled`).
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

**Done when.** Every failure window has a passing test, or is documented as untested together with the
reason.

### Phase 5: Safe HTTP crawling

**Goal.** Replace the test double with the real, safe fetcher.

**Scope**

- An HTTPX client with `trust_env=False`, no automatic redirects, and HTTP/1.1.
- DNS resolution and the destination policy, with each connection pinned to the validated IP. TLS SNI and
  certificate checks still use the hostname.
- Request limits:
  - per-operation timeouts plus a task deadline;
  - a streamed limit on decoded body size;
  - a content-type allowlist.
- robots.txt handling (RFC 9309), cached in Redis.
- Per-host rate limiting with a GCRA Lua script based on Redis time.
- HTML extraction with BeautifulSoup:
  - title, headings, and text;
  - links with their positions;
  - `<base href>`;
  - meta robots.
- Redirects handled through same-level admission.
- A local fixture site for tests (loopback is allowed only in the test configuration), plus the first
  demo-site content with its own robots.txt.
- Findings carried over from Phase 1:
  - choose between `httpx` and its successor `httpx2`; Starlette 1.x's TestClient already uses
    `httpx2`;
  - set the HTTP client's logger to WARNING, because at INFO it logs full request URLs, including
    query strings.

**Tests**

- SSRF table tests:
  - IPv4 and IPv6 address classes;
  - mapped and embedded forms;
  - decimal, octal, and hex host forms;
  - redirects to private addresses;
  - DNS rebinding through a scripted resolver.
- Size limits and slow-response limits.
- robots.txt semantics, the rate limiter, and the parser.
- An end-to-end crawl of the fixture site over real HTTP, with the exact expected URLs and depths.

**Done when.** All safety tests pass and the end-to-end crawl produces the expected result.

### Phase 6: Indexing pipeline and search API

**Scope**

- The index relay, which publishes `pending` `search_documents` rows (ADR-0008), and the indexer
  worker.
- A weighted `tsvector` with truncation. The `search_documents` table and its GIN index exist
  since Phase 2B.
- `GET /api/v1/search`:
  - `websearch_to_tsquery` for parsing and `ts_rank_cd` for ranking;
  - safe `ts_headline` snippets;
  - pagination;
  - scoped to a job.
- Indexing progress shown on the job detail.

**Tests**

- The relay crash window (duplicate publish).
- Duplicate and replayed index events.
- A title match outranks a body match.
- Arbitrary query strings never cause an error.
- Search over the fixture crawl.

### Phase 7: Link analysis

**Scope**

- Reports on broken, redirecting, unchecked, and external links.
- Link statistics: in-link and out-link counts, the depth distribution, and the most-linked pages.
- API endpoints, and the indexes that support them.

**Tests.** A fixture site with known broken links, redirect chains, and depth-limited links, checked
against exact expected reports.

### Phase 8: React + TypeScript dashboard

**Needs.** Node.js (installed).

**Scope**

- A Vite + React + TypeScript app.
- A jobs list and a create-job form limited to allowlisted targets and caps.
- A job detail page with polled progress (state counts, current level, budget, indexing progress), the
  page list, and link reports.
- A search page with safe highlighting.
- Loading, error, and empty states.

**Tests**

- Vitest + Testing Library for components and the API client.
- `tsc --noEmit`.
- A production build.

### Phase 9: Docker Compose deployment and demo site

**Scope**

- A backend image: one image for every Python role, running as a non-root user.
- A frontend image: built, then served by nginx with an `/api` reverse proxy.
- A demo-site container.
- A full `docker-compose.yml` with:
  - health checks;
  - a one-shot migration service;
  - internal networks (in the public profile, workers have no general egress);
  - worker scaling.
- Quickstart documentation.

**Tests.** A scripted smoke test: `docker compose up` → create a job → wait for completion → check that
search returns results → `docker compose down`.

### Phase 10: Benchmarks

**Scope**

- A `benchmarks/` harness that runs against the demo site. It uses a documented, development-only higher
  rate limit.
- Measurements:
  - crawl throughput vs. worker count (1, 2, 4, 8);
  - job completion time;
  - dispatch-to-claim latency;
  - indexing throughput;
  - search latency p50/p95/p99 over a fixed query set.
- Environment capture: CPU, RAM, OS, versions, and settings.
- Raw results committed to the repository.
- `docs/benchmarks.md`, describing the methodology and its limitations.

**Rule.** Every reported number comes from a run in this repository and is published with its command and
raw output. Nothing is extrapolated.

### Phase 11: CI hardening and documentation

**Scope**

- CI:
  - backend lint, type check, and unit tests;
  - integration and fault tests against PostgreSQL and Redis service containers;
  - frontend type check, tests, and build;
  - image builds;
  - optional dependency audits (`pip-audit`, `npm audit`).
- Documentation:
  - a README with an overview, diagram, quickstart, design highlights, and limitations;
  - an operations runbook covering recovery procedures and DLQ inspection;
  - a review of the ADRs.

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
