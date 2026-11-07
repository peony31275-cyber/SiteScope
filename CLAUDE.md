# CLAUDE.md

Guidance for Claude Code and for human contributors to this repository. Keep this file short, stable, and
accurate. Details belong in `docs/`.

## Project

**SiteScope: Distributed Web Crawling & Search Infrastructure.** A small-scale but runnable system, built as
a portfolio project for backend and distributed-systems engineering. It is not a scraping tutorial.
Correctness under failure is a first-class requirement: duplicate delivery, worker crashes, lease expiry,
and divergence between Redis and PostgreSQL must all be handled.

```
React dashboard -> FastAPI -> PostgreSQL (jobs, URL frontier)
  -> scheduler (dispatcher, outbox relay, level advancer, reconciler)
  -> Redis stream "sitescope:crawl" -> crawler workers (asyncio + HTTPX + BeautifulSoup)
  -> PostgreSQL (pages, links, search_documents) -> Redis stream "sitescope:index"
  -> indexer worker -> PostgreSQL full-text index (tsvector + GIN) -> search API
```

- Design: [docs/architecture.md](docs/architecture.md)
- API conventions (URLs, request IDs, problem+json errors):
  [docs/api-conventions.md](docs/api-conventions.md)
- Local development commands (Windows PowerShell): [docs/development.md](docs/development.md)
- Phase plan and current status: [docs/development-roadmap.md](docs/development-roadmap.md). Read it before
  starting any work.
- Decision records: [docs/adr/](docs/adr/README.md). To change a decision, add a new ADR that supersedes
  the old one. Never rewrite an accepted ADR.

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
   - Redis holds only deliverable messages, rate-limit state, and caches.
   - Losing Redis data must never lose work (ADR-0001).
2. **At-least-once processing with idempotent effects.** Every consumer tolerates duplicate and
   redelivered messages (ADR-0003).
3. **Outbox, never dual writes** (ADR-0002, ADR-0008).
   - Request handlers never publish to Redis.
   - The state row is the outbox: crawl tasks are published from `crawl_urls`, and index tasks from
     `search_documents` rows in status `pending`. There is no separate outbox table.
   - The relay publishes, then marks the row as published, in the same transaction. A claim
     locks the URL row first, so it waits for a dispatch of that row still in flight
     (ADR-0016).
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
      ADR-0015). Only the one-URL diagnostic `app.crawler.cli` fetches without it.

    Every crawled URL is also checked against robots.txt first (`app/crawler/robots.py`, RFC
    9309), and robots.txt itself is fetched through the safe fetcher.

    The API's crawl-scope check (`app/crawler/scope.py`, ADR-0012) is syntactic. It is not an SSRF
    defense on its own, and approved targets are empty by default. Never send a request to a crawl
    target with another HTTP client, and never let the HTTP stack resolve host names: only
    `PinnedNetworkBackend` opens connections.
11. **Clocks.** Leases use the PostgreSQL clock, and rate limits use the Redis clock. A worker's local
    clock is never used for correctness.

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
    functions of `crawler` (URL normalization, scope).
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

## Tests

| Directory | What runs there |
|---|---|
| `backend/tests/unit` | no network, database, or Redis |
| `backend/tests/integration` | real PostgreSQL and Redis, or local HTTP/TLS servers on 127.0.0.1 (marker `integration`) |
| `backend/tests/fault` | failure injection and recovery (marker `fault`) |

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
- Process tests (`tests/process_support.py`) start real scheduler and worker processes. Their
  environment always names the test database: `child_environment` refuses any database that
  is not a disposable `*_test` one. Never start `python -m app.scheduler` against the
  development database: it starts every queued job there.

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
  | Read-only crawl status | `.\.venv\Scripts\python.exe -m app.scheduler.status` |
  | Lint | `.\.venv\Scripts\python.exe -m ruff check .` |
  | Format check | `.\.venv\Scripts\python.exe -m ruff format --check .` |
  | Type check | `.\.venv\Scripts\python.exe -m mypy` |

  All four checks (tests, lint, format check, type check) must pass before work is reported done.

## Repository layout (target)

```
backend/          Python package `app`: FastAPI API, scheduler, crawler and indexer workers
  app/            api/ core/ (Phase 1); db/ services/ (Phase 2B); crawler/ (Phases 2C-4: URLs,
                  scope, safe fetcher, HTML parser, robots.txt, politeness); workers/ (Phase 4:
                  crawl task, single-process driver; Phase 5: crawler worker); queue/
                  scheduler/ (Phase 5: Redis Streams, scheduler, status); indexing/ (later)
  alembic/        migrations (Phase 2B); alembic.ini next to it
  compose.yaml    PostgreSQL + Redis for development (Phase 2B)
  tests/          unit/ (Phase 1); integration/ (Phase 2B); fault/ (Phase 5: real processes)
  pyproject.toml  dependencies + ruff / mypy / pytest configuration
  .env.example    configuration template; copy to backend/.env (git-ignored)
frontend/         React + TypeScript dashboard (Phase 9; currently empty)
demo-site/        controlled static website: the approved demo crawl target
benchmarks/       reproducible benchmark scripts and raw results
docs/             architecture.md, development-roadmap.md, api-conventions.md, development.md, adr/
```

The full proposed tree and the responsibilities of each module are in `docs/architecture.md`, under
"Repository structure (proposed)".
