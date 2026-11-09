# Continuous integration

Two GitHub Actions workflows in `.github/workflows/`. They have not run on GitHub yet: the
repository has not been pushed. They were checked locally with actionlint 1.7.8 (no errors), and
their steps were run on Linux as described under "Local equivalents".

## Workflows

**`ci.yml`: fast checks on every pull request and every push to `main` or `master`** (about 5 to
10 minutes; no database).

| Job | What it runs |
|---|---|
| `backend` | `pip install -r requirements/dev.txt`; ruff (lint, format) on the backend, the benchmarks and `deploy/`; mypy strict; `pytest -m "not integration"`. JUnit report uploaded |
| `frontend` | `npm ci`, then `npm run check`: tsc, ESLint, Prettier, Vitest, production build |
| `secrets` | gitleaks over the whole history, findings redacted (docs/security.md, "Secrets") |
| `images` | `docker compose config` of the deployment and its HTTPS overlay, with throwaway secrets; builds the three images; checks the backend image's user (10001) and that it holds no tests, `.env` or virtual environment |

**`full.yml`: the full verification, on pushes to `main` or `master`, nightly (03:17 UTC), and on
demand.**

| Job | What it runs |
|---|---|
| `services` | PostgreSQL 18.6 and Redis 8.10.3 from `backend/compose.yaml` (the development definition); one Alembic head; `pytest -m integration --require-services` (the integration and fault suites; a skip is a failure). Service logs on failure |
| `e2e` | The same services; `python -m tests.dashboard_stack --e2e` runs every process on a disposable database, then Playwright with Chromium (`E2E_BROWSER_CHANNEL=chromium`). Playwright report on failure |
| `deployment` | `docker compose -f deploy/compose.yaml up --build --wait`; `seed-demo --wait 300`; `deploy/smoke.py --read-only --demo` through the public entry; status and logs; then `down -v` (the runner is discarded anyway) |

## Rules

- **Read-only token.** `permissions: contents: read` and no repository secret. Every credential
  is a throwaway value for an ephemeral runner.
- **Pinned actions.** Each action is pinned by commit SHA, with its version in a comment. Python
  dependencies come from the Linux locks, npm dependencies from `package-lock.json`.
- **Bounded runs.** Every job has a timeout. Pull request runs of `ci.yml` cancel superseded
  ones.
- **No benchmarks.** They are measured on a known machine with their command and raw output
  (docs/performance.md). Shared runners give numbers that cannot be compared.

## Local equivalents

On the development machine (Windows), the four checks of CLAUDE.md and `npm run check` cover the
`backend` and `frontend` jobs. Run on 2026-11-08 to validate the workflows:

- **Linux.** In the CI's Python (`python:3.13.16-slim-trixie` container, `requirements/dev.txt`):
  - ruff and mypy pass. mypy found a Windows-only constant in a conditional expression,
    fixed in `tests/process_support.py`.
  - `pytest -m "not integration"`: 1478 passed.
  - `pytest -m integration --require-services` against the development servers' disposable
    test database: 643 passed. The two Redis data-loss tests were deselected because they
    need Docker inside the container; they run on Windows.
- **Images and deployment:** built, started, seeded and checked as in docs/deployment.md,
  "Validation".
- **Workflow syntax:** actionlint 1.7.8 reported no errors.

The e2e job's Chromium run is new: locally the suite runs in Microsoft Edge.
