# ADR-0019: Website analysis from stored crawl results: link classes, confirmed broken links, read-only APIs

- Status: Accepted
- Date: 2026-11-07
- Extends: [ADR-0005](0005-postgresql-full-text-search.md) and
  [ADR-0018](0018-asynchronous-indexing-pipeline.md) (the search service gets its HTTP API)
- Amends: architecture section 13, "Link analysis" (the definition of broken links)

## Context

Phase 8 exposes the crawl, index, search and analysis results through a REST API for the React
dashboard (Phase 9). The data is all in PostgreSQL: `crawl_urls` (every admitted URL with its
final state, outcome, HTTP status and redirect target), `pages` (stored HTML documents),
`page_links` (every link of every stored page, internal or not, followed or not) and
`search_documents`.

Architecture section 13 planned the link report as "internal links whose target `failed`, or was
fetched with a status of 400 or higher". Applied to real crawl results, that definition reports
things that are not broken:

- a 5xx, 408 or 429 response may be transient, and the crawl has already retried it;
- 401 and 403 are refusals, not proof that a page is missing;
- a `failed` request (DNS, connection, TLS, timeout) never got a response at all;
- a redirect is neither broken nor working until its chain ends somewhere;
- a URL waiting for a retry keeps its last attempt's status (`crawl_state._RETRY`), which is not
  a final response.

The analysis must also stay inside the system's safety model: no request to a crawl target
outside the crawl itself (the allowlist, SSRF defenses, robots.txt and politeness apply only to
the crawl), no cross-job data, no invented numbers, and no claim the crawl did not establish (a
URL the crawl never requested is not "broken" or "working"; a page with no known inbound link is
not necessarily an orphan).

## Decision

### 1. Read-only analysis over stored rows

`backend/app/services/site_analysis.py` computes every report in SQL from the job's stored rows.
It sends no request, writes nothing, and keeps no cache or summary table. Each use case runs one
transaction `REPEATABLE READ, READ ONLY`, so its lists and counts come from one snapshot, with a
time limit (`SITESCOPE_ANALYSIS_TIMEOUT_SECONDS`, 10 s by default, shorter than the database
command timeout): PostgreSQL's `statement_timeout`, and the service's own deadline, both end as
`503 analysis_timeout`; any other `TimeoutError` (connecting to PostgreSQL) is an outage,
`503 dependency_unavailable`. Every statement also sets `plan_cache_mode = force_custom_plan` for its
transaction: asyncpg prepares statements per connection, and a generic plan, which PostgreSQL may
switch to after five executions, cannot drop the optional filters; it was measured up to 8 times
slower.

### 2. One class per link

Each stored link gets exactly one class from the crawl result of its target in the same job
(`page_links.target_normalized_url = crawl_urls.normalized_url` within the job). A target that
redirected is judged where its chain ends: a recursive query follows `redirect_target` through the
job's own crawl URLs and stops at a URL already seen (a loop), after 20 redirects (the most
`SITESCOPE_FETCH_MAX_REDIRECTS` allows; URLs admitted through links can form longer chains, and
the bound also keeps the query linear), or at a URL the job never admitted. Only a terminal result
(`fetched`, `failed`, `skipped`) decides.

| Class | Verified | When |
|---|---|---|
| `ok` | yes | the final response was 2xx |
| `broken` | yes | the final response was 404 or 410 |
| `server_error` | yes | 5xx, 408 or 429, after the crawl's retries |
| `http_error` | yes | any other status, such as 401 or 403 |
| `redirect_error` | yes | a redirect loop, more than 20 redirects, or a 3xx without a usable `Location` |
| `out_of_scope` | no | external link; target skipped as not approved or as a blocked destination; redirect leaving the job's hosts |
| `not_followed` | no | never admitted: `rel="nofollow"`, or a `nofollow` page |
| `beyond_limits` | no | never admitted (the target or the end of its redirects): beyond `max_depth`, or the page budget used up |
| `not_crawled` | no | pending, queued, in progress, cancelled, or not admitted for a reason the rows do not record |
| `robots_blocked` | no | robots.txt disallowed the target, or could not be fetched |
| `unreachable` | no | the request ended without a response |

The rules mirror the crawler's own admission rules (`crawl_state.advance_job`: a level beyond
`max_depth` is never admitted, nor anything once `admitted_count = max_pages`, nor a link that is
external, `nofollow` or on a `nofollow` page), so a class names the reason the crawl itself
applied. A link is *verified* when an HTTP response decides its class.

### 3. Confirmed broken links

A *confirmed broken link* is an internal link whose target, after its redirects, answered 404 or
410 during the crawl: exactly the `broken` class. It is listed once per stored link (source page,
target URL); `page_links` already stores repeated links of one page to one target once. The
listing is ordered by target URL, then source page URL, and reports the verified status, where
the redirects ended, and when the crawl recorded the response.

### 4. The link graph

Nodes are every admitted URL and every target of an internal link (`kind`: `page`, `crawl_url`,
`link_target`); edges are the stored internal links between returned nodes. Redirects are not
edges. Degrees count every stored internal link. The graph is cut at `max_nodes` (at most 2,000;
nodes in BFS order: depth, then URL) and `max_edges` (at most 10,000; edges by source URL,
position and target URL), with the totals and a `truncated` flag.
Nothing is called an orphan: links on pages the crawl did not fetch are unknown.

### 5. Statistics

Counted from stored rows only, in one snapshot: discovered URLs (admitted, plus internal link
targets never admitted), admitted URLs by state, stored pages, the final HTTP status classes of
the URLs whose request ended (1xx to 5xx, and no response: each URL counted once, with its own
status, so the classes add up to those URLs), URLs per BFS depth,
the indexing progress, links by class, and the ten most-linked URLs.

### 6. HTTP API

- `GET /api/v1/search` maps `page` and `page_size` to the Phase 7 service's offset and limit, and
  keeps its bounds (offset at most 1000, at most 50 results): ranked results page by offset,
  which is why search does not use cursors. Any page beyond those bounds is a `422
  page_out_of_range`, reported together with a query error if there is one. The service's
  visibility rule, ranking and time limit are unchanged.
- `GET /api/v1/crawls/{job_id}/pages`, `/links`, `/broken-links`, `/link-graph` and `/stats`. The
  listings use keyset cursors: opaque base64 of the last item's sort key, tagged with the listing
  (a cursor of another listing is `invalid_cursor`), checked strictly (integers within
  PostgreSQL's range, printable ASCII strings of at most 2,048 characters). The links listing
  filters by class, by `verified` (unchecked links are `verified=false`) and by `redirected` (the
  target answered with a 3xx), as well as by source page and internal or external.
- The job detail gains `progress.pages_stored`; its other fields are unchanged.

### 7. Indexes (migration 0005)

Two existing indexes are replaced by wider ones; the number of indexes does not change:
`pages (job_id)` becomes `pages (job_id, normalized_url)`, so that the links listing, ordered by
the linking page's URL, stops after one page of links; `page_links (job_id,
target_normalized_url)` gains `INCLUDE (is_internal)`, so that counting a job's internal links is
an index-only scan where the visibility map allows. Both were measured before they were added
(`benchmarks/results/phase8-analysis.md`).

## Consequences

- No analysis request reaches a crawl target, and a report states only what the crawl observed.
  A target that answered 404 stays broken for that job even if it recovers later.
- `broken` is a narrow, defensible definition; dashboards show the other classes separately
  instead of mixing refusals and transient failures into one "broken" number.
- Some reasons are not recorded in the rows and are reported as `not_crawled`: the redirect hop
  limit, a redirect to a URL of the job's host that is not an approved target, a job that ended
  before admitting a level. An unreachable robots.txt counts as `robots_blocked`.
- Cost: the statistics and a filtered links listing read and classify every link of the job;
  broken links classify only the links whose target ended in 404 or 410, and a page of the
  unfiltered links listing only its own links. Measured on the development machine with 50 links
  per page (`benchmarks/results/phase8-analysis.md`): under 90 ms at the median for a 1,000-page
  job (the default page budget), except the link graph at its maximum limits (about 150 ms); for
  a 10,000-page job, the statistics about 0.6 s and the slowest listing about 0.3 s. Cost grows
  with the number of links; a much larger job would reach the time limit and get `503
  analysis_timeout`, never partial numbers.
- Writes pay for the wider indexes: one larger index entry per stored page and per stored link.

## Alternatives considered

- **Architecture section 13's definition** ("failed, or a status of 400 or higher"): rejected for
  the reasons in the context.
- **Checking links again at analysis time** (for example `HEAD` requests): rejected. It would send
  requests outside the crawl, bypassing the politeness gate and robots.txt checks the crawl
  applies, and turn a read endpoint into a fetcher. The analysis describes the crawl that ran.
- **Precomputed link-analysis tables**, written by the workers or by a background pass:
  rejected for now. They add write-path work and invalidation for a cost that the measurements
  do not show at the planned scale. They remain the way forward for much larger jobs.
- **Offset pagination for the analysis listings**: rejected; offsets skip or repeat items while a
  crawl adds links, and deep offsets cost as much as the whole list.
- **Redirects as graph edges**: rejected for the link graph, which holds the links pages contain.
  The links listing reports each redirect chain (`final_url`, `redirect_hops`).
- **A new index without removing one**: rejected; the two wider indexes serve every query of the
  ones they replace.
