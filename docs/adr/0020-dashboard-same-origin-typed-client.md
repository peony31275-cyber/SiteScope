# ADR-0020: The dashboard: a same-origin client typed from the OpenAPI document, with bounded polling

- Status: Accepted
- Date: 2026-11-08
- Extends: [ADR-0019](0019-website-analysis-from-stored-crawl-results.md) (the dashboard is its
  client) and [ADR-0012](0012-approved-crawl-targets.md) (the crawl policy becomes readable)

## Context

Phase 9 builds the React dashboard: the main way to see the system work. It creates and cancels
crawl jobs, follows their progress, searches, and shows the website analysis of Phase 8. Every
number it shows must come from the backend, so the design questions are about the boundary:

- How does the browser reach the API, and what does that mean for CORS and for a public demo
  that must not crawl arbitrary URLs?
- How do the frontend's types stay equal to the API contract?
- What does the dashboard need that the Phase 8 API does not provide?
- How often may a page ask for updates without loading the backend, and when does it stop?
- How is crawled content (titles, snippets, anchor text, URLs) shown safely?

## Decision

1. **One origin.** The browser calls the API at `/api/v1` on the dashboard's own origin. In
   development and preview, Vite's server proxies `/api` to the API (`SITESCOPE_API_URL`, a
   server-side setting, never in the bundle); in a deployment, nginx does
   ([architecture section 15](../architecture.md)). The API therefore needs no CORS, and no CORS
   configuration is added. The API base path is a build-time constant (`VITE_API_BASE_URL`,
   default `/api/v1`); no request address ever comes from user input.
2. **Types generated from the OpenAPI document.** `python -m app.api.openapi_export` writes the
   application's OpenAPI document to `frontend/src/lib/api/openapi.json`; `openapi-typescript`
   generates `schema.d.ts` from it (`npm run api:types`). Both are committed. The backend test
   `tests/unit/api/test_openapi_snapshot.py` fails while the snapshot differs from the served
   document, so an API change cannot leave the dashboard's types behind. Fixtures in the frontend
   tests are typed by the same schema.
3. **Two read-only endpoints, nothing else in the backend.**
   - `GET /api/v1/crawl-policy`: the approved host patterns, schemes, ports, and the `max_depth`
     and `max_pages` caps, read from the settings that job creation enforces. The form offers
     the approved targets and checks limits before sending; the server checks every request
     again. Internal fetch origins and every other setting stay private.
   - `GET /api/v1/overview`: every job by status, every admitted URL by state, the stored pages,
     and the search documents by status, counted in one read-only REPEATABLE READ snapshot under
     the analysis time limit (`SITESCOPE_ANALYSIS_TIMEOUT_SECONDS`, `503 analysis_timeout`).
     The job listing is cursor-paged without totals; the dashboard never adds up a page of jobs
     and calls it a total. Discovered URLs are not counted over all jobs (that reads every stored
     link): they stay a per-job statistic.

   The job listing is not extended: each listed row reads its job's detail (the one source of
   progress), at most one page of rows, and refreshes only while that job can change.
4. **Server state in TanStack Query, with bounded polling.** One request per query key at a time;
   an invalidation cancels an older request, so a stale answer never overwrites a newer one;
   polling stops when the last observer unmounts and while the tab is hidden. A job is refreshed
   at the viewer's interval (default 3 s, `VITE_POLL_INTERVAL_MS`, 2 to 30 s or off) while its
   crawl is queued, running or cancelling; at twice that (at least 5 s) once the crawl has ended
   but its indexing has not; and never once both are over. A job, or the overview, that shows no
   change over 5 minutes' worth of reads (3 for indexing, and at least 10 reads) pauses its
   polling and says why (a process is probably not running); new data or "Resume" restarts it.
   Reads are counted, not minutes, so time in a hidden tab never counts. Statistics refresh at
   most every 10 s, while the crawl or its indexing runs; listings and the graph refresh on
   request.
5. **Crawled content is text.** Titles, snippets, anchor text and URLs are React text nodes;
   `dangerouslySetInnerHTML` is banned by a lint rule. A URL becomes a link only if it is
   http(s), always with `rel="noopener noreferrer nofollow"` and a new tab. Search highlights
   are `[start, end)` ranges of Unicode code points: the snippet is split with `Array.from`,
   ranges are clamped and merged, and every part is rendered once, as text inside `<mark>` or
   not.
6. **The link graph is bounded and laid out by depth.** The server cuts the graph
   (`max_nodes` 1 to 2000, `max_edges` 0 to 10000; the dashboard asks for 300 and 1500 by
   default) and says when it did; the dashboard says so too. Nodes are placed in one column per
   BFS depth (never-admitted targets last), ordered by one barycenter pass: deterministic, linear
   in the graph's size, and drawn once in SVG. Pan and zoom change one transform; above 1500
   edges, edges are drawn as two combined paths. A node list beside the drawing reaches every
   node with the keyboard. In-degree 0 is never called an orphan.
7. **Cursors are opaque.** A listing walk keeps the cursors it followed (to go back) and starts
   over when the job, a filter or the page size changes; it never builds or parses a cursor and
   offers no "page N".

## Consequences

- The dashboard and the API share an origin in every environment; a deployment that serves them
  from different origins needs an explicit CORS allowlist then (never a wildcard), and CORS
  would still not be authentication.
- A contract change is a three-step change by design: the API, the snapshot, the generated
  types. The backend test enforces the first two; the TypeScript build enforces the rest.
- A job list page of 10 rows makes up to 10 detail requests when it loads, and then only for the
  rows still running; the list endpoint stays a cheap index scan. A deployment with many
  concurrent running jobs would rather have a batched progress summary (not built).
- The overview reads whole tables. At the sizes measured in Phase 8 this is milliseconds; a much
  larger deployment would reach the analysis time limit and get `503 analysis_timeout` rather
  than wrong numbers. Precomputed counters would be the next step (not built).
- Polling never runs unbounded: settled jobs stop, hidden tabs stop, stalled ones pause.
- The public demo still crawls only approved targets: the form only guides; the server enforces
  the allowlist, the limits, and every other Phase 2C to 6 rule. Creating jobs is not
  rate-limited and needs no authentication yet: a public deployment needs both (architecture
  section 12.7; Phase 11).

## Alternatives considered

- **CORS with an allowlist of dashboard origins:** needed only when the origins differ; adds
  configuration and a preflight per request for no benefit in this topology.
- **Hand-written TypeScript types:** they drift silently from the Pydantic models.
- **WebSockets or server-sent events for progress:** progress lives in PostgreSQL and changes at
  most a few times a second; bounded polling of one cheap statement is enough, works through any
  proxy, and needs no new server component.
- **A force-directed layout (d3-force, Cytoscape.js):** a new dependency, a simulation that
  keeps running and moves nodes, and a layout that differs on every load. The crawl is a BFS:
  depth columns show its structure directly.
- **Totals from the job listing:** wrong as soon as there is a second page.
