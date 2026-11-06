# Development Roadmap

SiteScope is built one phase at a time. Each phase ends with working, tested code and an honest report.
The next phase starts only when the project owner asks for it. For the design, see
[`architecture.md`](architecture.md).

## Phase status

| Phase | Title | Status |
|---|---|---|
| 0 | Repository inspection and architecture baseline | **Done** (2026-11-06) |
| 1 | Backend foundation | **Done** (2026-11-06) |
| 2 | Persistence, migrations, and the job API | Not started (needs Docker) |
| 3 | Task distribution core | Not started |
| 4 | Fault tolerance and recovery | Not started |
| 5 | Safe HTTP crawling | Not started |
| 6 | Indexing pipeline and search API | Not started |
| 7 | Link analysis | Not started |
| 8 | React + TypeScript dashboard | Not started |
| 9 | Docker Compose deployment and demo site | Not started |
| 10 | Benchmarks | Not started |
| 11 | CI hardening and documentation | Not started |

## Repository state after Phase 1

Updated on 2026-11-06. The Phase 0 inspection findings are kept in this file's first commit.

| Item | State |
|---|---|
| Git | Local repository on branch `master` (the configured default). Phase 0 and Phase 1 are committed. No remote yet. |
| `backend/app/` | FastAPI foundation: configuration, logging, errors, middleware, health endpoints. |
| `backend/tests/` | 51 unit tests. |
| `backend/.venv` | Python 3.13.7. Runtime: fastapi 0.162.0, uvicorn 0.54.0 (standard extras), pydantic-settings 2.16.2. Development: pytest 9.2.0, httpx2 2.16.0, ruff 0.17.4, mypy 2.4.1. |
| `frontend/` | Empty. Renamed from the misspelled `fronted/` in Phase 1. |
| `docs/` | Architecture, roadmap, ADRs 0001–0007, API conventions, development guide. |

| Tool on the development machine | Status |
|---|---|
| Python 3.13.7 (`backend/.venv`) | available |
| Git 2.52 | available |
| Node.js 24.14, npm 11.9 | available |
| WSL 3.0.3 with an Ubuntu distribution (WSL 2) | available |
| Docker / Docker Compose | **not installed** |
| PostgreSQL | **not installed** |
| Redis | **not installed** |

## Environment setup (needed before Phase 2)

- Phase 1 needs only the existing virtual environment.
- From Phase 2 on, the integration tests need PostgreSQL and Redis.
- Phase 9 needs Docker Compose.

SiteScope runs PostgreSQL and Redis in Docker, so Docker is the only thing to install. Choose one of the
two options below.

### Option A: Docker Desktop with the WSL 2 backend

1. Read the current requirements on Docker's
   [Windows install page](https://docs.docker.com/desktop/setup/install/windows-install/).
   - Docker documents Windows 10 22H2 (build 19045) as the minimum, and this machine runs build 19045.
   - Docker also states that it supports a Windows version only while Microsoft still services it.
     Windows 10 servicing has ended, so a current release may refuse to install or may be unsupported
     here. If that happens, use Option B.
2. Install Docker Desktop and choose the WSL 2 backend. The installed WSL is version 3.0.3; Docker
   requires at least 2.1.5.
3. Start Docker Desktop, then verify the installation in PowerShell:
   ```powershell
   docker version
   docker compose version
   docker run --rm hello-world
   ```

### Option B: Docker Engine inside the existing WSL 2 Ubuntu

This option does not depend on Docker Desktop's Windows support policy. If Docker's
[official Ubuntu guide](https://docs.docker.com/engine/install/ubuntu/) differs from the commands below,
follow the guide.

```bash
# In PowerShell, open the distribution:  wsl -d Ubuntu

# 1. Check that systemd is enabled. Recent Ubuntu WSL images enable it by default.
cat /etc/wsl.conf        # expect the lines:  [boot]  systemd=true
#    If they are missing, add them, run `wsl --shutdown` in PowerShell, and reopen Ubuntu.

# 2. Add Docker's apt repository, then install Docker Engine and the Compose plugin.
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 3. Let your user run docker without sudo, then close and reopen the Ubuntu shell.
sudo usermod -aG docker "$USER"

# 4. Verify.
docker run --rm hello-world
docker compose version
```

With Option B:

- Run Compose commands from the Ubuntu shell in `/mnt/c/Users/24419/Desktop/SITESCOPE`.
- Ports published by containers are normally reachable from Windows at `localhost`. The backend, running
  from `backend\.venv`, can then use `localhost:5432` and `localhost:6379`.
- If those ports are not reachable, use the address that `hostname -I` prints inside Ubuntu.

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

### Phase 2: Persistence, migrations, and the job API

**Goal.** Durable job state and the job lifecycle API, on a real PostgreSQL.

**Scope**

- `docker-compose.yml` with `postgres` and `redis`: pinned versions, named volumes, and Redis AOF enabled.
- SQLAlchemy 2.x with an async engine and sessions.
- Models for `crawl_jobs`, `crawl_urls`, `pages`, `page_links`, and `index_outbox`.
- An initial Alembic migration with the constraints and partial indexes from the architecture.
- URL normalization, scope checks, and allowlist validation, as pure functions with table-driven tests.
- Job API:
  - `POST /api/v1/jobs` (with `Idempotency-Key`)
  - `GET /api/v1/jobs` (keyset pagination, status filter)
  - `GET /api/v1/jobs/{id}` (state counts, current level, budget)
  - `POST /api/v1/jobs/{id}/cancel`
- `GET /api/v1/health/ready`, which checks PostgreSQL and Redis.
- New database and Redis settings in `backend/.env.example`.
- The CI workflow (deferred from Phase 1), if a GitHub remote exists by then.

**Tests**

- Unit:
  - the normalization corpus;
  - allowlist and cap validation.
- Integration:
  - migrations upgrade and downgrade;
  - the job and its seed URL are created atomically;
  - an `Idempotency-Key` replay returns the same job, and a mismatched body returns 409;
  - cancel transitions;
  - errors are returned as problem+json.

**Done when.** Unit and integration tests pass against the Compose services, and migrations can be rolled
back.

**Needs.** Docker (see Environment setup).

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

- The `index_outbox` relay and the indexer worker.
- A `search_documents` migration with a GIN index, and a weighted `tsvector` with truncation.
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
2. **Choose how to install Docker (Option A or B).** Needed before Phase 2.
3. **Choose the public demo target.** Either the self-hosted demo-site container (the current plan) or a
   separately hosted static site.
4. **Choose a license for the public repository** (for example, MIT).

Resolved in Phase 1: `fronted/` was renamed to `frontend/`, and git was initialized locally.
