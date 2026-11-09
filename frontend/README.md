# SiteScope dashboard

The React dashboard of SiteScope (Phase 9): create and cancel crawl jobs, follow their crawl and
indexing live, search a job's pages, and read its website analysis and link graph. Every number
comes from the SiteScope REST API; nothing is computed from a partial page or invented.

Design decisions: [ADR-0020](../docs/adr/0020-dashboard-same-origin-typed-client.md). API
contract: [docs/api-conventions.md](../docs/api-conventions.md).

## Stack

| Concern              | Choice                                                                                    |
| -------------------- | ----------------------------------------------------------------------------------------- |
| Build and dev server | Vite 8 (`@vitejs/plugin-react`)                                                           |
| UI                   | React 19, TypeScript 5.9 (strict, `noUncheckedIndexedAccess`)                             |
| Styling              | Tailwind CSS 4 (design tokens in `src/styles/index.css`)                                  |
| Routing              | React Router 8 (data router, one lazy chunk per page)                                     |
| Server state         | TanStack Query 5                                                                          |
| Charts               | Recharts 3                                                                                |
| Icons                | lucide-react (tree-shaken)                                                                |
| Tests                | Vitest 5 + React Testing Library (jsdom); Playwright 1.65 for the end-to-end run          |
| Quality              | ESLint 10 (typescript-eslint strict type-checked, react-hooks, jsx-a11y strict), Prettier |

TypeScript stays on 5.9: typescript-eslint and openapi-typescript do not support TypeScript 7
yet. `eslint-plugin-jsx-a11y` declares ESLint 9 as its peer; `package.json` overrides that peer
(its rules run on ESLint 10 and are exercised by `npm run lint`). `minimatch` 10 is overridden to
11.0.5 for GHSA-g3hv-75wg-m249 (development tooling only).

## Commands

Node.js 22.12 or newer (developed with Node.js 24). From `frontend\`:

| Task                                               | Command                                               |
| -------------------------------------------------- | ----------------------------------------------------- |
| Install (exact versions from the lockfile)         | `npm ci`                                              |
| Development server, with `/api` proxied to the API | `npm run dev` (http://127.0.0.1:5173)                 |
| Type check                                         | `npm run typecheck`                                   |
| Lint                                               | `npm run lint`                                        |
| Format check / format                              | `npm run format:check` / `npm run format`             |
| Unit and component tests                           | `npm test`                                            |
| Production build                                   | `npm run build` (output in `dist/`)                   |
| Serve the build, with the same proxy               | `npm run preview` (http://127.0.0.1:4173)             |
| All of the above checks                            | `npm run check`                                       |
| Regenerate API types                               | `npm run api:types` (after the backend export, below) |

The development and preview servers proxy `/api` to `SITESCOPE_API_URL` (default
`http://127.0.0.1:8000`): run the API first (`docs/development.md`, "Run the API").

## Configuration

`.env.example` lists every variable; copy it to `.env.local` to change one.

| Variable                | Default                 | Meaning                                                                     |
| ----------------------- | ----------------------- | --------------------------------------------------------------------------- |
| `VITE_API_BASE_URL`     | `/api/v1`               | The API path as the browser sees it. Public: embedded in the bundle.        |
| `VITE_POLL_INTERVAL_MS` | `3000`                  | Default auto-refresh interval for running jobs (1000 to 60000). Public.     |
| `SITESCOPE_API_URL`     | `http://127.0.0.1:8000` | Dev and preview servers only: where `/api` is proxied. Never in the bundle. |

No secret belongs in a `VITE_` variable: Vite embeds them in the JavaScript every visitor loads.

## Architecture

```
src/
  app/            shell (sidebar, header, API status, refresh controls), routes, providers,
                  viewer settings (auto-refresh interval), notifications
  components/     the design system: Button, Card, Badge, Table, Field, Tabs, ConfirmDialog,
                  Meter, SegmentedBar, StatTile, Pager, ExternalUrl, feedback states, charts/
  features/
    overview/     deployment counts (GET /overview), status and indexing charts, newest job
    crawls/       job form, job list, job detail, cancellation, polling rules, job selector
    search/       search page, code-point highlight ranges
    analysis/     statistics, pages, links (filters), confirmed broken links
    graph/        layered layout, SVG canvas, node list and details
  hooks/          selected job (?job=), cursor walks, a shared clock
  lib/api/        HTTP client, error descriptions, one function per operation, query keys,
                  generated types (schema.d.ts) and their source (openapi.json)
  tests/          test setup, a fake API (test double of fetch), typed fixtures, render helper
e2e/              Playwright specs for the real stack (tests.dashboard_stack)
```

**Routes.** `/` overview, `/crawls` jobs (form and list), `/crawls/:jobId` job detail, `/search`,
`/analysis` and `/graph`. The last three work on one job, named in the address (`?job=`), so a
view can be shared; the header's selector changes it, and the last job viewed is remembered in
the browser. Filters, tabs, search terms and graph limits are in the address too.

**API integration** (`src/lib/api/`). `client.ts` builds every URL with `URLSearchParams` (IDs
are encoded as one path segment), sends JSON, applies a timeout (15 s by default), forwards
cancellation from TanStack Query, and turns failures into `ApiError`: `problem` (an RFC 9457
document, with `code`, `request_id` and field errors), `http` (a non-problem answer, such as a
proxy's 502), `network`, `timeout` or `invalid_response`. `errors.ts` explains each for people
and decides what may be retried: only transient failures (unreachable API, 502 to 504,
`dependency_unavailable`), at most twice, with exponential backoff and jitter. A 404, a 422 or a
query time limit (`analysis_timeout`, `search_timeout`) is shown at once.

**Types** come from the backend's OpenAPI document, never by hand:

```powershell
# in backend\
.\.venv\Scripts\python.exe -m app.api.openapi_export ..\frontend\src\lib\api\openapi.json
# in frontend\
npm run api:types
```

The backend test `tests/unit/api/test_openapi_snapshot.py` fails while `openapi.json` differs
from what the API serves.

**Polling** (`features/crawls/hooks.ts`, `status.ts`). A job refreshes at the viewer's interval
while its crawl is queued, running or cancelling; at twice that (at least 5 s) once the crawl has
ended but its indexing has not; never once both are over. Rows of the job list refresh at half
that rate, the overview and statistics at most every 10 s. Nothing polls in a hidden tab or after
its page is left. A job (or the overview) whose reads show no change over 5 minutes' worth of
refreshes (3 for indexing, at least 10 reads) pauses and says which process is probably not
running; "Resume" or new data restarts it. Reads are counted, not minutes, so time in a hidden
tab never counts (`hooks/useStallWatch.ts`). The
cancellation dialog shows only what the server answered: `202` (cancelling: in-flight URLs are
settling), `200 cancelled`, or `200` with a job that had already ended.

**Search highlighting** (`features/search/highlight.ts`). The API's highlight ranges count Unicode
code points; JavaScript strings count UTF-16 units (an emoji is two). The snippet is split with
`Array.from`, ranges are clamped, empty ones dropped, overlapping ones merged, and each part is a
text node, inside `<mark>` or not. Tests cover ASCII, Chinese, emoji and supplementary-plane
characters.

**Cursor pagination** (`hooks/useCursorPager.ts`). The analysis listings and the job list page
with opaque `next_cursor` values: the dashboard passes them back unchanged, keeps the ones it
followed to go back, and starts over when the job, a filter or the page size changes. Search pages
by number, up to the deepest page the server serves.

**Link graph** (`features/graph/`). The server cuts the graph at `max_nodes` and `max_edges`
(the dashboard asks for 300 and 1500; up to 2000 and 10000) and the page says when it did. Nodes
are laid out in one column per BFS depth, never-admitted targets last, with one barycenter pass:
deterministic, linear in the size of the graph. Shapes carry the node kind (circle: stored page;
square: admitted URL without a page, colored by its response; dashed ring: never admitted).
Pan and zoom change a single transform without re-rendering the nodes; a selection draws its
links on top (outgoing blue, incoming orange). Labels have their own layer: zoomed in, only the
nodes in and around the view are labelled (`viewport.ts`; the region is computed again only when
the view leaves it), and a graph of up to 40 nodes labels every node. Phase 10 measured why:
labelling all 2,000 nodes made 7 to 10 % of the frames while zooming in take two intervals or
more (the slowest 67 ms); with the labels layer, 0 to 3 % (the slowest 18 to 33 ms;
docs/performance.md). The node list is the keyboard path to every node.
A node without inbound links is not called an orphan: only links on stored pages are known.

**Errors and empty states.** Every query shows loading, empty, and error states; errors give a
title, a message, field details for validation errors, the request ID, and a retry where it can
help. No stack trace or server text beyond the problem's `detail` is shown.

## Design system

A dark operations console: charcoal surfaces in four steps (`canvas`, `surface`, `raised`,
`overlay`), hairline borders, three text inks (`ink`, `ink-2`, `ink-3`, all at least 4.5:1 on the
surfaces), a blue accent (`accent-solid` under white text: 5.4:1). Status colors (`good`,
`warning`, `serious`, `critical`, `neutral`) mean state only and always come with a label or a
shape. Chart series use the validated dark categorical slots blue, orange and aqua (checked with
a palette validator on the card surface: CVD separation 9.4, normal vision 26.5, contrast at least
3:1). Charts follow fixed mark specs (bars at most 20 px, 4 px rounded data ends, hairline grids,
values at bar tips) and every chart has a table twin. Text uses the system UI font; IDs and URLs
use the system monospace font. Focus is always visible; transitions are short and switched off
with `prefers-reduced-motion`.

## Tests

- `npm test`: unit and component tests (Vitest, jsdom). Component tests render the whole app
  (router, providers, lazy pages) against `src/tests/fakeApi.ts`, a test double of `fetch` that
  answers like the API (JSON, problem documents) and records requests. It is not the backend.
  `src/tests/render.tsx` loads every page's chunk before a file's tests (Phase 11): a cold
  import of a page and its charts once exceeded Testing Library's 3 s wait under a full
  parallel run.
- The end-to-end run uses the real backend, on a disposable database:

  ```powershell
  # in backend\, with PostgreSQL and Redis up (wsl docker compose up -d --wait)
  .\.venv\Scripts\python.exe -m tests.dashboard_stack --e2e
  ```

  It starts the demo site and a slow site, the API, the scheduler, two crawler workers, the index
  dispatcher, two indexers and `vite preview`, runs `e2e/dashboard.spec.ts` in the installed
  Microsoft Edge (Playwright channel `msedge`; no browser download), then stops everything and
  drops the database. Without `--e2e` it serves the stack until Ctrl+C. `E2E_SCREENSHOTS=1` adds
  screenshots of every page at desktop and phone widths (`test-results/screens/`). Each page must
  load without a Content-Security-Policy violation or a page error.

- Against the containerized, read-only stack (Phase 11; docs/deployment.md), after `seed-demo`:
  `E2E_SCREENSHOTS=1 E2E_READ_ONLY=1 E2E_BASE_URL=http://127.0.0.1:8080` runs the same
  screenshots on the seeded demo. Two more variables control the output:
  `E2E_SCREENSHOTS_DIR` sets the output directory, and `E2E_SCREENSHOTS_FULL_PAGE=0` gives
  viewport-sized images (the README's `docs/images/`). CI uses Chromium instead of Edge
  (`E2E_BROWSER_CHANNEL=chromium`).

- **Link graph performance** (Phase 10, opt-in): `e2e/graph-performance.spec.ts` loads the Link
  Explorer at 300/1,500, 1,000/5,000 and 2,000/10,000 nodes/links and measures drawing, long
  tasks, heap, DOM size and frame intervals while panning and zooming. It is skipped unless
  `E2E_GRAPH_PERF=1`; `benchmarks/dashboard_graph.py` (in `backend\`) sets that up on a disposable
  database with a large job, runs it and keeps the raw results.

## Known limitations

- The dashboard has no login. On a deployment whose writes are not open (ADR-0024) it shows a
  read-only notice instead of the create form, and no cancel button; the API enforces the same.
  It never holds the admin token, so an operator writes with an HTTP client or the operator
  commands.
- A job list page reads one job detail per row (bounded by the page size); see ADR-0020.
- The cursor walk lives in memory: reloading a listing page starts from its first page.
- Highlights split text by code point; a grapheme cluster (an emoji with a modifier) could in
  principle be split by a range boundary. The backend highlights whole words, so this does not
  occur with its snippets.
- The graph is drawn in SVG: at the API's limits (2000 nodes, 10000 links) the first drawing
  took 0.85 to 0.94 s in headless Edge, with one long task of 0.23 to 0.27 s, against 0.34 to
  0.67 s at the defaults (300, 1500); panning kept the display's rate at the median
  (docs/performance.md).
- Only the dark theme exists.
