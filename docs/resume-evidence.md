# Resume evidence

Every figure a résumé or an interview may quote about SiteScope, with where it comes from. Each
source holds the command, the environment and the raw output.

All performance figures come from one laptop (docs/performance.md, "Method"): an AMD Ryzen 7
5800U with 16 GB, PostgreSQL and Redis in Docker in WSL 2, sharing the machine with every
process. None is a capacity claim for other hardware. The project is a portfolio system: it
has not been deployed publicly or run in production.

## System

| Claim | Figure | Source |
|---|---|---|
| Scope of the system | 5 process roles (API, scheduler, crawler worker, index dispatcher, indexer) plus a React dashboard. 11 containers in the deployment | docs/architecture.md; `deploy/compose.yaml` |
| Code size (lines, comments included, 2026-11-08) | Backend application 19.1k Python, backend tests 28.1k, dashboard 8.0k TypeScript, its tests 2.6k (plus 0.5k e2e), benchmarks 6.1k | `git ls-files` with `wc -l` (Phase 11 report) |
| Design records | 25 ADRs, 5 migrations | `docs/adr/`, `backend/alembic/versions/` |
| Delivery semantics | At-least-once with idempotent effects, database-enforced fencing tokens (CHECK constraint and triggers). Never exactly-once | ADR-0003, ADR-0010, ADR-0011 |

## Performance (Phase 10)

| Claim | Figure | Raw output |
|---|---|---|
| Crawl throughput, five hosts, 2,000 pages, 1 / 3 / 5 workers | 29.6 / 68.6 / 90.8 pages/s, medians of 3 interleaved trials | `benchmarks/results/phase10-crawl-multi-host-ab.json` |
| Same, before the Phase 10 fixes | 28.4 / 29.1 / 32.6 pages/s: ×2.79 at 5 workers after the fixes | same file (variant `baseline`: commit `e27357a`, recorded under its pre-publication ID `37ee301`) |
| Two causes found and fixed | A dispatch window larger than a host's politeness slots (ADR-0022). Nested-loop plans when statistics had not seen a new job (ADR-0023) | docs/performance.md, "Cause 1" and "Cause 2" |
| BFS level admission with stale statistics | 977 URLs: 5,174 ms → 28.7 ms. 11,866 URLs: over 60 s → 346 ms | `benchmarks/results/phase10-admission-plans*.{json,txt}` |
| Indexing, 2,000 pages of 1,000 words, 1 / 2 / 4 indexers | 151.9 / 217.0 / 266.3 pages/s | `benchmarks/results/phase10-indexing.json` |
| Search, 10,000 documents | A term in every document: p50 / p95 / p99 86.8 / 93.7 / 98.4 ms. A rare term: p50 18.1 ms. Through the HTTP API: p50 91.2 ms | `benchmarks/results/phase10-search.json` |
| Recovery, with the tests' short timings (7 s leases) | A killed worker's URLs taken over after 7.5 s. Progress 0.6 s after a Redis restart, 0.8 s after Redis lost its data, 0.8 s after a PostgreSQL restart. Cancellation settled in 0.23 s | `benchmarks/results/phase10-recovery.json` |
| Dashboard graph at 2,000 nodes | Slow frames while zooming: 10.2 % → 3.0 % (dense graph), 7.1 % → 0 % | `benchmarks/results/phase10-dashboard-graph-*.json` |

## Deployment and security (Phase 11)

| Claim | Figure | Source |
|---|---|---|
| Containerized stack | 11 long-running containers healthy in 25.6 s after `up --wait` (images built), 66.1 s with the build from a fresh copy. Demo seeded, crawled and indexed in 31.8 s and 32.1 s | docs/deployment.md, "Validation" |
| Footprint at rest | About 600 MiB for the whole stack after the demo crawl (601.6 and 615.4 MiB in two runs) | same |
| Isolation | Only the proxy publishes a port. A crawler container cannot reach the internet: `Network is unreachable` | same |
| Public read-only mode | Writes refused with 403 through the proxy, even with a bearer token. With an admin token: 401 without it, 429 after the write burst | same; `tests/unit/api/test_write_access.py` |
| Active-jobs cap | Exact under concurrency: 8 concurrent creations with a cap of 3 created exactly 3 | `tests/integration/test_active_jobs_cap.py` |
| Secret scan | gitleaks over all commits and the files to commit: no secret; one reviewed false positive | docs/security.md, "Secrets" |

## Tests

From the Phase 11 verification, 2026-11-08 (docs/development-roadmap.md, Phase 11).

| Claim | Figure | Command |
|---|---|---|
| Backend tests | 2,127 passed: 1,480 unit, 647 integration and fault, against real PostgreSQL, Redis and processes, with skips counted as failures | `python -m pytest --require-services` (backend/) |
| Dashboard tests | 175 Vitest tests. 8 end-to-end tests against the real stack (Playwright) | `npm run check`; `python -m tests.dashboard_stack --e2e` |
| Linux | 1,478 unit and 643 integration and fault tests in `python:3.13.16-slim-trixie`, before the review fixes | docs/ci.md, "Local equivalents" |
| CI on GitHub | Not run yet: the repository has not been pushed | docs/ci.md |

## Suggested résumé bullets

Each bullet uses only the figures above.

- Built SiteScope, a distributed breadth-first web crawler and search platform (Python 3.13,
  FastAPI, PostgreSQL 18, Redis Streams, React and TypeScript). It delivers tasks at least
  once, with database-enforced fencing tokens and a transactional outbox, and recovers from
  worker crashes and Redis or PostgreSQL outages, verified by fault-injection tests with real
  processes.
- Profiled crawl scaling and fixed two bottlenecks: a dispatch window larger than per-host
  politeness, and nested-loop plans on unanalyzed jobs. Five-worker throughput rose from 32.6
  to 90.8 pages/s (×2.8), and BFS level admission fell from 5.2 s to 29 ms, on one benchmarked
  laptop.
- Designed asynchronous indexing with version-guarded writes and PostgreSQL full-text search
  (weighted `tsvector`, GIN): 266 pages/s with four indexers and 87 ms p50 search latency over
  10,000 documents on a laptop.
- Packaged the system as an 11-container Docker Compose deployment, with:
  - non-root multi-stage images and a single nginx entry;
  - a no-egress internal network and a backend-enforced read-only public mode;
  - rate limits and a strict CSP;
  - GitHub Actions CI covering linting, strict typing, unit, integration and e2e tests, a
    whole-history secret scan and a deployment smoke test.
