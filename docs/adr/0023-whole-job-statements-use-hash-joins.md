# ADR-0023: The level admission and the analysis reads are planned with hash joins

- Status: Accepted
- Date: 2026-11-08
- Extends: [ADR-0004](0004-level-synchronous-bfs.md) (the level admission) and
  [ADR-0019](0019-website-analysis-from-stored-crawl-results.md) (the analysis reads)

## Context

PostgreSQL plans a statement from table-wide statistics, which `ANALYZE` (usually autovacuum)
refreshes after enough rows change, typically within a minute. Until then a new job is unknown
to them. When the statistics hold only a few jobs, all of them in the most-common-values list,
the planner estimates a new job's rows at one in every table. With one row per table, nested
loops look cheapest, and it chains them with the join conditions as filters: each loop scans the
job's rows again for every row of the outer side. The work grows with the product of the job's
table sizes.

Phase 10 found this twice (docs/performance.md, `benchmarks/admission_plans.py`; an earlier job
analyzed, a new job stored, autovacuum off so that nothing analyzes during the measurement):

- **The level admission** (`crawl_state._ADMIT_NEXT_LEVEL`) took 5.2 s instead of 29 ms for 977
  URLs, and over 60 s instead of 342 ms for 11,866. In the crawl benchmarks this stopped BFS
  for 18 to 22 s between levels.
- **The analysis reads** (statement by statement, `EXPLAIN (ANALYZE)`): for a job of 372 URLs
  and 1,332 links, the statistics read took 5.6 s instead of 15 ms, and the links of one class
  5.8 s instead of 11 ms. For a job of 2,302 URLs and 22,220 links, both and the broken links
  ran into the analysis time limit (10 s, an HTTP 503 for the dashboard), and the first page of
  pages or of links took 225 to 310 ms instead of 8 to 10 ms. For a job of 11,111 pages and
  about 220,000 links, every read but the graph ran into the limit, a page of links included.

With fresh statistics the planner itself joined the whole job for most of these reads (a
classification of every link, statistics, pages ordered by depth across two tables, the graph's
nodes), so hash joins cost them little or nothing. Only a page of links can stop early, walking
the job's pages in URL order through an index: under a millisecond with fresh statistics.

## Decision

1. **The level admission and every analysis read run with `SET LOCAL enable_nestloop = off`**
   in their own transaction: `crawl_state.advance_job` around the admission (reset right after
   it), and `site_analysis._read_only` for every analysis use case (statistics, pages, links,
   broken links, the graph). Hash joins are linear in the job, whatever the estimates. `SET
   LOCAL` ends with the transaction, so no pooled connection keeps it.
2. **A page of links pays for it.** With fresh statistics it took 19.5 ms instead of 0.5 ms for
   22,220 links, and 181 ms instead of 0.6 ms for about 220,000; without the setting it ran into
   the time limit at that size whenever the statistics were stale. A bounded, predictable cost
   is preferred to an HTTP 503 that lasts until the next analyze.
3. **Regression tests check plans and statements, not time**: the admission and the
   classification counts do quadratic work when planned freely with stale statistics and linear
   work under the setting, and the services run them under it
   (`tests/integration/test_admission_plan.py`, `test_analysis_read_plans.py`).

## Consequences

- With fresh statistics the admission and most reads cost what they cost before (within the
  measurements' noise; the broken links 29 instead of 20 ms for 22,220 links); a page of links
  costs more (above). With stale statistics none of them goes quadratic.
- Their work is linear in the whole job, not in what they return: a level's admission reads all
  of the job's links, pages and URLs (through the `job_id` indexes in a database of many jobs).
  In a tree that grows level by level the last level dominates, so that is little more than the
  level itself.
- `enable_nestloop = off` discourages nested loops; it does not forbid them. A join without an
  equality condition (the job row, joined to every link) is still a nested loop over one row.
- Statements added later that join a job's rows should run under the same setting, and be
  measured with stale statistics (`benchmarks/admission_plans.py`).

## Alternatives considered

- **Hash joins for the whole-job reads only**, nested loops for a page of links (tried first:
  `phase10-analysis-reads*-step1.json`). Faster with fresh statistics, but a page of links ran
  into the time limit for a job of about 220,000 links when the statistics were stale.
- **Running `ANALYZE` from the scheduler** after large levels. It would fix every statement,
  including a page of links, but it is maintenance work in the request path of the scheduler,
  needs the table owner's (or the `MAINTAIN`) privilege, and still leaves the window before it
  runs.
- **Lower per-table autovacuum thresholds** (a migration). They shorten the window but do not
  close it: autovacuum still runs at most once per `autovacuum_naptime`.
- **No statistics for `job_id`** (`ALTER COLUMN job_id SET STATISTICS 0`): every job would be
  estimated at the same default share of a table. It changes every plan that filters by job,
  which would have to be measured again across the board.
- **Rewriting the statements** so that the misestimate cannot pick a quadratic plan. Fragile:
  the planner's choice still follows its estimates.
- **Plan hints** (`pg_hint_plan`) are an extension that is not part of PostgreSQL.
