# Continuous integration

Two GitHub Actions workflows in `.github/workflows/`. They have run on GitHub since the
repository was pushed on 2026-11-09 ("Runs on GitHub" below). Before that, they were checked
locally with actionlint 1.7.8 (no errors), and their steps were run on Linux as described under
"Local equivalents".

## Workflows

**`ci.yml`: fast checks on every pull request and every push to `main` or `master`** (about 1
to 2 minutes on GitHub; no database).

| Job | What it runs |
|---|---|
| `backend` | `pip install -r requirements/dev.txt`; ruff (lint, format) on the backend, the benchmarks and `deploy/`; mypy strict; `pytest -m "not integration"`. JUnit report uploaded |
| `frontend` | `npm ci`, then `npm run check`: tsc, ESLint, Prettier, Vitest, production build |
| `secrets` | gitleaks over the whole history, findings redacted (docs/security.md, "Secrets") |
| `images` | `docker compose config` of the deployment and its HTTPS overlay, with throwaway secrets; builds the three images; checks the backend image's user (10001) and that it holds no tests, `.env` or virtual environment |

**`full.yml`: the full verification, on pushes to `main` or `master`, nightly (03:17 UTC), and on
demand** (about 7 minutes on GitHub; the three jobs run in parallel).

| Job | What it runs |
|---|---|
| `services` | PostgreSQL 18.6 and Redis 8.10.3 from `backend/compose.yaml` (the development definition); one Alembic head; `pytest -m integration --require-services` (the integration and fault suites; a skip is a failure). Service logs on failure |
| `e2e` | The same services; `python -m tests.dashboard_stack --e2e` runs every process on a disposable database, then Playwright with Chromium (`E2E_BROWSER_CHANNEL=chromium`). On failure, the Playwright report and the stack's process logs |
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

## Runs on GitHub

GitHub-hosted `ubuntu-24.04` runners (Python 3.13.18), while the repository is private. Start
times are UTC: 04:03 UTC on 2026-11-10 was the evening of 2026-11-09 on the development machine.

| Started (UTC) | Workflow | Run | Commit | Result | Wall time |
|---|---|---|---|---|---|
| 2026-11-10 04:03 | CI | [42753341885](https://github.com/peony31275-cyber/SiteScope/actions/runs/42753341885) | `2781c57` | Passed: 1,503 unit tests and mypy; 175 Vitest tests and the build; the images; gitleaks over 16 commits | 1 min 29 s |
| 2026-11-10 04:03 | Full verification | [42753341887](https://github.com/peony31275-cyber/SiteScope/actions/runs/42753341887) | `2781c57` | Failed (below). The deployment job passed; the run was cancelled | 11 min 6 s |
| 2026-11-10 04:34 | CI | [42756685965](https://github.com/peony31275-cyber/SiteScope/actions/runs/42756685965) | `38c9579` | Passed: 1,504 unit tests and mypy; 175 Vitest tests and the build; the images; gitleaks over 19 commits | 1 min 6 s |
| 2026-11-10 04:34 | Full verification | [42756686057](https://github.com/peony31275-cyber/SiteScope/actions/runs/42756686057) | `38c9579` | Passed: 648 integration and fault tests, none skipped; 8 end-to-end tests in Chromium (the 12 opt-in benchmark and screenshot specs skipped); the deployment built, seeded and smoke-tested (17 checks) | 7 min 11 s |
| 2026-11-10 04:44 | CI | [42757799150](https://github.com/peony31275-cyber/SiteScope/actions/runs/42757799150) | `8fd7f1e` | Passed | 1 min 22 s |
| 2026-11-10 04:44 | Full verification | [42757799332](https://github.com/peony31275-cyber/SiteScope/actions/runs/42757799332) | `8fd7f1e` | Failed: one end-to-end test (below). The 648 integration and fault tests and the deployment passed | 10 min 39 s |

Later runs, from `5980fdb` on, are in the repository's Actions tab.

**The first failure.** The first full verification failed every test that starts child
processes (the 7 that ran), and the dashboard's end-to-end stack never became ready: its
`/api/v1/health/ready`, through the dashboard's proxy, gave no answer below 500 within 30 s.
`child_environment` (`tests/process_support.py`) drops the inherited `SITESCOPE_` variables and
passed only the Redis password back. The children read the PostgreSQL password from
`backend/.env`, which the development machine has and a runner does not: the workflows set the
credentials as environment variables. It now passes the test's PostgreSQL user and password as
well, and `tests/unit/test_process_support.py` builds a child's settings from that environment
alone (the test fails without the fix). The run was cancelled once the cause was known: each
failing process test had waited out its timeouts, about 1.4 minutes on average, and 23 more
were still to run against the job's 45-minute limit. The e2e job now also uploads the stack's
process logs on failure; the first time, they stayed on the runner.

**A flaky end-to-end test.** On `8fd7f1e`, a documentation change, the search test failed in
938 ms with "Not an input element". It clicked the job page's Search link, then looked the job
selector up by its label, "Crawl job", which the job page's header region carries too. The
runner was slow that time (installing Chromium took 9 min 25 s, against 38 s and 2 min 18 s in
the earlier runs), and the assertion ran before the search page had replaced the job page: the
locator found the region. The uploaded Playwright report shows the search page a moment later,
with the job selected. Since `5980fdb` the test looks the selector up by its role, `combobox`,
which the region cannot match, so it waits for the selector.

**Minutes.** GitHub bills each job's time rounded up to the minute (for this account, the run
timing API reports no billable time). A passing CI run is about 5 billed minutes: four jobs of
11 to 84 seconds. A passing full verification is about 14: jobs of 7 min 7 s, 2 min 18 s and
2 min 14 s. GitHub Free includes 2,000 minutes a month for private repositories, and standard
runners are free for public ones. While the repository is private, the nightly full
verification alone uses about 420 minutes a month (30 × 14), and each push to `main` about 19.

## Local equivalents

On the development machine (Windows), the four checks of
[development.md](development.md#tests-lint-format-and-type-check) and `npm run check` cover the
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

Locally the end-to-end suite runs in Microsoft Edge; on GitHub, in Chromium ("Runs on GitHub").
