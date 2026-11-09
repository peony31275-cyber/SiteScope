# CLAUDE.md

Guidance for Claude Code and for human contributors to this repository. Keep this file short, stable, and
accurate. Details belong in `docs/`.

## Project

**SiteScope: Distributed Web Crawling & Search Infrastructure.** A small-scale but runnable system, built as
a portfolio project for backend and distributed-systems engineering. It is not a scraping tutorial.
Correctness under failure is a first-class requirement: duplicate delivery, worker crashes, lease expiry,
and divergence between Redis and PostgreSQL must all be handled.

```
React dashboard (same origin, /api proxied) -> FastAPI -> PostgreSQL (jobs, URL frontier)
  -> scheduler (dispatcher, outbox relay, level advancer, reconciler)
  -> Redis stream "sitescope:crawl" -> crawler workers (asyncio + HTTPX + BeautifulSoup)
  -> PostgreSQL (pages, links, search_documents) -> index dispatcher
  -> Redis stream "sitescope:index" -> indexer workers
  -> PostgreSQL full-text index (tsvector + GIN) -> search and website-analysis APIs (Phase 8)
  -> React dashboard: jobs, live progress, search, analysis, link graph (Phase 9)
Deployment (Phase 11): deploy/compose.yaml, one public entry (nginx), everything else internal
```

- Design: [docs/architecture.md](docs/architecture.md)
- API conventions (URLs, request IDs, problem+json errors):
  [docs/api-conventions.md](docs/api-conventions.md)
- Local development commands (Windows PowerShell): [docs/development.md](docs/development.md)
- Phase plan and current status: [docs/development-roadmap.md](docs/development-roadmap.md). Read it before
  starting any work.
- Decision records: [docs/adr/](docs/adr/README.md). To change a decision, add a new ADR that supersedes
  the old one. Never rewrite an accepted ADR.
- Measurements and their method: [docs/performance.md](docs/performance.md); metrics, heartbeats and
  diagnostics: [docs/observability.md](docs/observability.md) (Phase 10).
- Deployment, threat model and CI (Phase 11): [docs/deployment.md](docs/deployment.md),
  [docs/security.md](docs/security.md), [docs/ci.md](docs/ci.md).

## Working agreement

The project is built one phase at a time. Start a phase only when the owner requests it. For each phase:

1. Inspect the existing code first. Don't overwrite working code without a reason.
2. State the plan briefly.
3. Implement **only** the requested phase, including its tests.
4. Run the available tests and checks, and fix anything the change broke.
5. Report:
   - the files created or modified;
   - the exact commands run, with their real results;
   - how to verify the work manually.
6. Update `docs/development-roadmap.md`, and the architecture doc or ADRs if the design changed. Then stop.

**Honesty**

- Never report a command or test as passing unless it actually ran and passed in this session. Report a
  skipped test as skipped and give the reason.
- If a required tool (Docker, PostgreSQL, Redis, ...) is missing, say so and give exact setup steps.
- Don't present a placeholder implementation as a finished feature. Call test doubles (fake fetchers,
  in-memory sites) what they are.
- Never fabricate or extrapolate performance numbers. A benchmark result must come with its command,
  environment, and raw output.
- Never claim exactly-once processing.

**Safety**

- Get explicit permission before doing anything destructive, including:
  - deleting files;
  - renaming directories;
  - recreating `backend/.venv`;
  - rewriting git history.
- Don't commit or push unless asked.
- Keep secrets out of code, tests, docs, and committed configuration.

**Language.** Write code, comments, commit messages, and documentation in English. Write phase reports and
explanations for the project owner in Chinese.

## Architecture invariants (changing one requires a new ADR)

1. **PostgreSQL is the source of truth** for job, URL, page, link, and index state.
   - Redis holds only deliverable messages, rate-limit state, caches, and process heartbeats
     (ADR-0021). No decision reads a heartbeat: they are observability only.
   - Losing Redis data must never lose work (ADR-0001).
2. **At-least-once processing with idempotent effects.** Every consumer tolerates duplicate and
   redelivered messages (ADR-0003).
3. **Outbox, never dual writes** (ADR-0002, ADR-0008).
   - Request handlers never publish to Redis.
   - The state row is the outbox: crawl tasks are published from `crawl_urls`, and index tasks from
     `search_documents` rows in status `pending`. There is no separate outbox table.
   - The dispatchers (crawl and index) publish, then mark the row as published, in the same
     transaction. A claim, or an index attempt, locks the row first, so it waits for a dispatch
     of that row still in flight (ADR-0016, ADR-0018).
4. **Leases with fencing tokens.**
   - A worker claims a URL with a compare-and-set that increments `lease_epoch`.
   - Its result transaction must re-check `state = 'in_progress' AND lease_epoch = :token` under a row
     lock.
   - A fenced-out worker discards its result and does not `XACK`. No worker acknowledges an
     uncertain outcome (an error, a cancellation); the rules are in architecture section 10.3.
   - Timing must satisfy `task_deadline < lease_duration <= reclaim_min_idle`.
   - The database enforces fencing (ADR-0010, ADR-0011): an outcome must carry
     `result_epoch = lease_epoch` (CHECK), a claim must clear `result_epoch`, and `lease_epoch`
     changes only in a claim, by exactly one (triggers).
   - Workers must still send their own token. `SET result_epoch = lease_epoch`, copied from the
     column, passes every database rule and defeats fencing.
   - The result transaction starts with the fenced update (`services/crawl_state.py`,
     ADR-0014): zero rows means the lease was lost, so roll back and write nothing.
   - A retry, a worker's cancellation of its URL, and a poison-message quarantine are fenced
     updates too (ADR-0017). Only a claim counts an attempt; a retry is `pending` with
     `next_attempt_at` in PostgreSQL, never a timer.
5. **Level-synchronous BFS** (ADR-0004).
   - Only URLs at a job's `current_level` are dispatched.
   - Workers only record links. Only the scheduler admits the next level, under the job row lock.
   - The single exception: workers admit same-level redirect targets.
6. **Exact page budget.** Serialized admission plus a CHECK constraint keep `admitted_count <= max_pages`.
7. **Deduplication by constraint.** Use `UNIQUE (job_id, normalized_url)` with `INSERT ... ON CONFLICT
   DO NOTHING` (ADR-0009). Never rely on check-then-insert alone. Normalized URLs are http(s),
   printable ASCII, at most 2048 bytes; the database rejects anything else. Every URL goes through
   `app/crawler/urls.py` (`normalize_url`) before any scope check, deduplication or storage.
8. **State transitions are conditional updates** (`UPDATE ... WHERE state = :expected ... RETURNING`).
   Zero rows means the race was lost: treat it as a no-op, log it, and move on.
9. **No transaction spans a request to a crawl target.** Claim and commit, then fetch outside any
   transaction, then commit the result.
10. **Every outbound request goes through the safe fetcher** (`app/crawler/fetcher.py`;
    ADR-0006, ADR-0013), which provides:
    - the target allowlist and URL policy;
    - DNS resolution with IP validation, and the connection pinned to the validated IP;
    - no automatic redirects (the fetcher validates and follows them itself);
    - timeouts plus an overall deadline;
    - a cap on decoded body size;
    - a per-host politeness gate on every hop of a crawl (`app/crawler/politeness.py`, Redis;
      ADR-0015; slots are leases renewed while the request runs, ADR-0017). Only the one-URL
      diagnostic `app.crawler.cli` fetches without it.

    Every crawled URL is also checked against robots.txt first (`app/crawler/robots.py`, RFC
    9309), and robots.txt itself is fetched through the safe fetcher.

    The API's crawl-scope check (`app/crawler/scope.py`, ADR-0012) is syntactic. It is not an SSRF
    defense on its own, and approved targets are empty by default. Never send a request to a crawl
    target with another HTTP client, and never let the HTTP stack resolve host names: only
    `PinnedNetworkBackend` opens connections.
11. **Clocks.** Leases use the PostgreSQL clock, and rate limits use the Redis clock. A worker's local
    clock is never used for correctness.
12. **An older index never replaces a newer one** (ADR-0018).
    - The indexer builds the vector inside PostgreSQL from the page row it locks (`FOR SHARE`),
      and writes it with that page's `content_version`, only while the document is `queued` and
      its `indexed_content_version` is not newer. Triggers (migration 0004) reject lowering
      `indexed_content_version` or `pages.content_version`.
    - Index attempts are counted in PostgreSQL before their work (`start_index_attempt`), with an
      advisory lease; never give up on a document because of Redis delivery counts. An attempt
      ends by its deadline, before its lease does, and an indexer runs at most as many tasks as
      its connection pool holds.
    - Search returns only documents with `indexed_content_version = pages.content_version`.
    - Indexing never changes a job's crawl status; `indexing_completed_at` is set only when the
      job is finished and none of its documents is `pending` or `queued`.
13. **Analysis reports only what the crawl observed** (ADR-0019).
    - Search and analysis read PostgreSQL only, one job at a time, in one read-only REPEATABLE
      READ transaction with a time limit. They never request a crawl target and never write.
    - A link is *broken* only if its target, after its redirects through the job's own crawl
      results, answered 404 or 410. A target never requested, or without a final response, is
      unverified: never reported as broken or as working. A non-terminal URL's status (a retry
      in waiting) decides nothing.
14. **The dashboard shows only what the API returned** (ADR-0020).
    - It calls the API on its own origin (`/api/v1`, proxied by Vite or nginx): no CORS, no
      address from user input, no secret in the bundle (`VITE_` variables are public).
    - Crawled titles, snippets, anchor text and URLs are text: `dangerouslySetInnerHTML` is
      banned by lint. A URL is a link only if it is http(s), with
      `rel="noopener noreferrer nofollow"`. Highlight ranges are code points (`Array.from`).
    - Totals come from endpoints that count them (`/overview`, `/stats`), never from a page of a
      listing. Cursors are passed back unchanged, never built or parsed.
    - Polling stops for settled jobs (crawl over and `indexing.completed_at` set), hidden tabs and
      unmounted pages, and pauses when a job shows no change; a cancellation is shown as the
      server answered it, never optimistically.
15. **Metrics never count twice** (ADR-0021).
    - State (jobs, URLs, documents) is counted from PostgreSQL in one snapshot per scrape, never
      added up from processes.
    - Process metrics stay per process (labels `role`, `process`) and are summed only in queries,
      after `rate()`. Labels come from fixed sets: never a URL, a path, a query or an ID.
    - Worker status comes from the workers' heartbeats, never from a configured count.
    - `GET /metrics` is internal: outside `/api`, not proxied, not in the OpenAPI document.
16. **The backend decides who writes** (ADR-0024).
    - The API's only writes, creating and cancelling jobs, go through `require_write_access`
      (`app/api/security.py`): `open` (development), `token` (bearer, constant-time) or
      `disabled` (production without a token).
    - The dashboard only mirrors the mode from `/crawl-policy`; no token ever reaches the
      frontend.
    - At most `SITESCOPE_CRAWL_MAX_ACTIVE_JOBS` jobs are active: a creation takes its advisory
      lock first, counts its new job after the insert, and rolls back over the cap; a replayed
      `Idempotency-Key` is always answered.
17. **One public entry** (ADR-0025).
    - In the deployment only the proxy publishes a port, and it forwards `/api/` only.
    - PostgreSQL, Redis, the API and the workers sit on an internal network without egress.
    - The application connects as a role that is neither superuser nor owner; migrations run
      once, as the owner, under an advisory lock.

## Code conventions

- **Python and types.** Python 3.13, with type annotations everywhere.
  - The target is mypy strict for `app/`.
  - Every `# type: ignore[code]` needs a stated reason.
  - Lint and format with ruff.
- **Layering.**
  - `api` handles HTTP only. `services` holds the use cases and owns transactions. `db` holds models and
    sessions.
  - `scheduler` and `workers` may use `services`, `queue`, `crawler`, and `indexing`, but never `api`.
  - `crawler` has no database imports (it uses Redis only for politeness and the robots.txt
    cache), and `queue` has no domain logic. `services` and `core.config` may use the pure
    functions of `crawler` (URL normalization, scope); `services` may use `indexing`, which is
    pure too (Phase 7: document rules, query handling). `api` may use `indexing`'s query rules
    (bounds, normalization) to validate its parameters (Phase 8).
  - Analytical SQL lives in `services` (`services/site_analysis.py`), never in route handlers.
  - `observability` (Phase 10: metrics registry, heartbeats, the Prometheus text format) depends
    on `core` only; every role may use it. `services/system_metrics.py` reads stream statistics
    and heartbeats for `GET /metrics`.
  - The operator commands (`app/scheduler/admin.py`, Phase 11) use `services` like the API,
    never raw SQL.
- **Async I/O throughout.** Use SQLAlchemy 2.x async with asyncpg, `redis.asyncio`, and the safe
  fetcher (`httpcore2`) for crawl requests. Don't block the event loop with long CPU work; offload it
  when measurements show a need. HTML parsing is such work: call `parse_html` with
  `asyncio.to_thread` from async code.
- **Transactions.**
  - Use `async with session.begin()` in the service layer.
  - Rely on READ COMMITTED, row locks, and compare-and-set updates.
  - Lock a URL row before its job row.
  - Transactions that lock the job row first (cancel, level advancement) must not lock `in_progress` URL
    rows.
  - Statements that join a whole job's rows (the level admission, the analysis reads) run with
    `SET LOCAL enable_nestloop = off`: the statistics may not have seen a new job yet, and nested
    loops then go quadratic (ADR-0023). Measure a new per-job statement with stale statistics
    (`benchmarks/admission_plans.py`).
- **Schema.** Change the schema only through Alembic migrations, and never edit a migration that has
  already been applied.
- **Configuration.** Use pydantic-settings with the `SITESCOPE_` environment prefix, validated at startup.
  `.env` is git-ignored, and `.env.example` documents every variable.
- **Errors.** Map domain exceptions centrally to RFC 9457 `application/problem+json`. Never leak stack
  traces.
- **Logging.**
  - Use stdlib `logging` with a JSON formatter, writing to stdout.
  - Attach context through contextvars: `request_id`, `job_id`, `url_id`, `worker_id`, `lease_epoch`.
  - Never log secrets or page bodies.
- **Retries.**
  - Bound the number of attempts.
  - Use exponential backoff with full jitter.
  - Retry only transient failures.
  - Crawl retries live in PostgreSQL (`pending` + `next_attempt_at`), not in an in-process sleep.
- **Dependencies.** Prefer the standard library and packages that are already installed. Justify every new
  dependency with one line in the phase report.
- **Frontend** (`frontend/`, Phase 9; details in its README).
  - TypeScript strict. API types are generated from the OpenAPI document (`app.api.openapi_export`,
    then `npm run api:types`), never written by hand; the backend test
    `tests/unit/api/test_openapi_snapshot.py` fails while the snapshot is stale.
  - `lib/api` is the only code that calls `fetch`; `features/*` hold pages and their query hooks;
    `components` is the design system. Server state lives in TanStack Query.
  - Exact dependency versions with a committed `package-lock.json`; install with `npm ci`.

## Tests

| Directory | What runs there |
|---|---|
| `backend/tests/unit` | no network, database, or Redis |
| `backend/tests/integration` | real PostgreSQL and Redis, or local HTTP/TLS servers on 127.0.0.1 (marker `integration`) |
| `backend/tests/fault` | failure injection and recovery (marker `fault`) |
| `frontend/src/**/*.test.ts(x)` | the dashboard's unit and component tests (Vitest, jsdom), against a fake API (a test double of `fetch`); no network |
| `frontend/e2e` | the dashboard against the real stack (Playwright, installed Edge; `E2E_BROWSER_CHANNEL=chromium` in CI), started by `backend/tests/dashboard_stack.py --e2e` on a disposable database |
| `deploy/smoke.py` | the containerized stack through its public entry (Phase 11; CI job `deployment`) |

- A test that needs an unavailable service skips with an explicit reason, and is reported as skipped.
  `pytest --require-services` turns those skips into failures (use it whenever the services are up,
  and in CI). Select suites with `-m integration` or `-m "not integration"`.
- Tests never contact the public internet: fetcher tests use their own servers on 127.0.0.1.
- Integration tests never touch the development database. They create, migrate (with Alembic)
  and drop a disposable `<db>_test` database, and use Redis database 15.
- pytest treats warnings as errors. Starlette's `TestClient` uses `httpx2`; with plain `httpx` it
  emits a deprecation warning, which would fail the run.
- Concurrency and fault tests assert the invariants listed under "Correctness invariants" in
  `docs/architecture.md`.
- Process tests (`tests/process_support.py`) start real scheduler, crawler worker, index
  dispatcher and indexer processes. Their
  environment always names the test database: `child_environment` refuses any database that
  is not a disposable `*_test` one. Never start `python -m app.scheduler` against the
  development database: it starts every queued job there.
- `tests/dashboard_stack.py` (Phase 9) runs the whole stack for the dashboard's end-to-end
  tests on its own disposable database, `<db>_test_ui`, and Redis database 15 under its own key
  prefix; it drops the database and deletes its keys at the end.
- Benchmarks (`benchmarks/`, Phase 10) use their own disposable database `<db>_test_bench`, Redis
  database 15 under fresh key prefixes, and disposable WSL containers for the Redis and
  PostgreSQL outages; a number is reported only with its command and raw output.
- Never send `FLUSHALL` (or `FLUSHDB`) to the development Redis; database 15 does not isolate
  it. Destructive Redis tests start their own disposable container and check that it is not
  the development server first (`tests/fault/test_redis_data_loss.py`).

## Environment (Windows 10 development machine)

- Repository root: `C:\Users\24419\Desktop\SITESCOPE`.
- The default shell is Windows PowerShell 5.1, which has no `&&`. Use `;` or `if ($?) { ... }` instead.
  Git Bash is also available.
- Python lives in `backend\.venv` (Python 3.13.7). Run tools through it, e.g.
  `backend\.venv\Scripts\python.exe -m pytest`, so the correct interpreter is always used.
  `Activate.ps1` is blocked by the default execution policy on this machine.
- Node.js 24 and npm 11 are installed (for the frontend, Phase 9).
- Docker Engine 29.9.3 and the Compose plugin run **inside WSL Ubuntu 24.04**. There is no Docker
  Desktop. Run Docker from an Ubuntu terminal, or with `wsl docker ...` from PowerShell.
- PostgreSQL 18.6 and Redis 8.10.3 run as containers from `backend/compose.yaml`, with ports bound
  to 127.0.0.1. Credentials come from `backend/.env`, which the backend and Compose both read.
- WSL stops Ubuntu, and Docker with it, about 15–30 s after the last WSL session closes. Keep a WSL
  session open while anything needs the containers. Details: `docs/development.md`, section
  "Docker (WSL 2)".
- Docker Hub limits anonymous pulls per hour. `mirror.gcr.io/library/<image>:<tag>` serves the
  same official images; tag the pulled image with its Docker Hub name before a build.
- Commands, run from `backend\` (full guide: `docs/development.md`):

  | Task | Command |
  |---|---|
  | Install | `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"` |
  | Start PostgreSQL + Redis | `wsl docker compose up -d --wait` |
  | Apply migrations | `.\.venv\Scripts\python.exe -m alembic upgrade head` |
  | Run the API | `.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload` |
  | Tests | `.\.venv\Scripts\python.exe -m pytest --require-services` |
  | Run one crawl job in-process | `.\.venv\Scripts\python.exe -m app.workers.local_crawler <job-id>` |
  | Run the scheduler (never against the development database) | `.\.venv\Scripts\python.exe -m app.scheduler` |
  | Run a crawler worker (one per window) | `.\.venv\Scripts\python.exe -m app.workers.crawler` |
  | Run the index dispatcher (starts no job) | `.\.venv\Scripts\python.exe -m app.scheduler.index_dispatcher` |
  | Run an indexer (one per window) | `.\.venv\Scripts\python.exe -m app.workers.indexer` |
  | Read-only crawl and index status, live processes, health assessment | `.\.venv\Scripts\python.exe -m app.scheduler.status` |
  | Metrics (Prometheus text, internal) | `GET http://127.0.0.1:8000/metrics` |
  | Benchmarks (disposable database; commands in `benchmarks/README.md`) | `.\.venv\Scripts\python.exe ..\benchmarks\crawl_scaling.py --experiment multi-host --output <file>` |
  | Lint the benchmark scripts | `.\.venv\Scripts\python.exe -m ruff check --config pyproject.toml ..\benchmarks` |
  | Read-only search of one job | `.\.venv\Scripts\python.exe -m app.scheduler.search <job-id> "query"` |
  | Lint | `.\.venv\Scripts\python.exe -m ruff check .` |
  | Format check | `.\.venv\Scripts\python.exe -m ruff format --check .` |
  | Type check | `.\.venv\Scripts\python.exe -m mypy` |
  | Dashboard: install (in `frontend\`) | `npm ci` |
  | Dashboard: dev server, `/api` proxied to :8000 | `npm run dev` |
  | Dashboard: type check, lint, format check, tests, build | `npm run check` |
  | Dashboard end to end (real stack, disposable DB) | `.\.venv\Scripts\python.exe -m tests.dashboard_stack --e2e` |
  | Regenerate the dashboard's API types | `.\.venv\Scripts\python.exe -m app.api.openapi_export ..\frontend\src\lib\api\openapi.json`, then `npm run api:types` in `frontend\` |
  | Operator commands (create, cancel, seed the demo) | `.\.venv\Scripts\python.exe -m app.scheduler.admin seed-demo --base-url <url> --wait 600` |
  | Full stack in containers (own project and port; `docs/deployment.md`) | `wsl docker compose -p sitescope-try -f ..\deploy\compose.yaml --env-file <file> up -d --build --wait` |
  | Regenerate the Linux dependency locks (in WSL, repository root) | `docker run --rm -v "$PWD/backend:/src" -w /src python:3.13.16-slim-trixie python requirements/lock.py` |

  All four checks (tests, lint, format check, type check) must pass before work is reported done;
  when the dashboard changed, `npm run check` too.

## Repository layout (target)

```
backend/          Python package `app`: FastAPI API, scheduler, crawler and indexer workers
  app/            api/ core/ (Phase 1); db/ services/ (Phase 2B); crawler/ (Phases 2C-4: URLs,
                  scope, safe fetcher, HTML parser, robots.txt, politeness); workers/ (Phase 4:
                  crawl task, single-process driver; Phase 5: crawler worker; Phase 6: retry
                  policy; Phase 7: shared consumer loop, indexer); queue/ scheduler/ (Phase 5:
                  Redis Streams, scheduler, status; Phase 7: index dispatcher, search command);
                  services/crawl_cancellation.py (Phase 6); services/search_index.py and
                  search.py, indexing/ (Phase 7); api/v1/search.py and analysis.py,
                  services/site_analysis.py (Phase 8: search and website-analysis APIs);
                  api/v1/crawl_policy.py and overview.py, services/overview.py,
                  api/openapi_export.py (Phase 9: the dashboard's endpoints and its types);
                  observability/, api/metrics.py, services/system_metrics.py (Phase 10:
                  metrics, heartbeats, GET /metrics)
  alembic/        migrations (Phase 2B); alembic.ini next to it
  compose.yaml    PostgreSQL + Redis for development (Phase 2B)
  Dockerfile      Phase 11: one image for every Python role; requirements/ holds the Linux locks
  tests/          unit/ (Phase 1); integration/ (Phase 2B); fault/ (Phase 5: real processes);
                  dashboard_stack.py (Phase 9: the whole stack for the dashboard's e2e run)
  pyproject.toml  dependencies + ruff / mypy / pytest configuration
  .env.example    configuration template; copy to backend/.env (git-ignored)
frontend/         React + TypeScript dashboard (Phase 9): src/app (shell, routes), components (design
                  system), features (overview, crawls, search, analysis, graph), hooks, lib/api
                  (client, generated types); e2e/ (Playwright)
demo-site/        controlled static website: the approved demo crawl target (Phase 11: its container)
deploy/           Phase 11: compose.yaml (the full stack), compose.https.yaml + Caddyfile, nginx/,
                  postgres/app-role.sql, .env.example, smoke.py
.github/workflows Phase 11: ci.yml (fast checks), full.yml (services, e2e, deployment)
benchmarks/       reproducible benchmark scripts and raw results (crawl, indexing, search, analysis,
                  database plans, recovery, the dashboard's graph; Phase 10: docs/performance.md)
docs/             architecture.md, development-roadmap.md, api-conventions.md, development.md, adr/,
                  deployment.md, security.md, ci.md, resume-evidence.md, images/ (Phase 11)
```

The full proposed tree and the responsibilities of each module are in `docs/architecture.md`, under
"Repository structure (proposed)".
