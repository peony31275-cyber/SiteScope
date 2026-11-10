# Benchmarks

Reproducible measurements. Each script records its command, its environment and its raw output
under `results/`. Numbers describe the machine they were measured on, and nothing else.

The Phase 10 scripts are listed first; the results and their interpretation are in
[`../docs/performance.md`](../docs/performance.md). Phases 5, 7 and 8 follow, unchanged.

## Phase 10: common ground

- **Isolation.** Every script creates the disposable database `<SITESCOPE_POSTGRES_DB>_test_bench`
  (and drops it at the end), uses Redis database 15 under a fresh key prefix per run (deleted
  afterwards) and serves its sites on 127.0.0.x. `recovery.py` stops and starts only disposable
  Redis and PostgreSQL containers it starts itself in WSL Docker. The development database and the
  development Redis keys are never touched. Run one script at a time: they share the database.
- **Shared machinery** ([`benchlib.py`](benchlib.py)): environment capture (CPU, RAM, power
  plan, WSL VM, PostgreSQL and Redis versions and settings, the code's commit); samplers of the
  machine's CPU load and effective clock (`typeperf`: `% Processor Performance`, `% Performance
  Limit`), of the WSL 2 VM and the PostgreSQL and Redis containers (`/proc/stat`, cgroup
  `cpu.stat`), of every process's CPU time and memory, and of PostgreSQL (cumulative statistics,
  sessions, lock waits); percentiles by linear interpolation (p99 only from 100 samples).
- **Code variants.** `--variant name=<backend dir>` runs a run's processes from another checkout
  (a `git worktree` of an earlier commit); several variants alternate run by run, so a drift of
  the machine affects each alike.
- **Lint**: `.\.venv\Scripts\python.exe -m ruff check --config pyproject.toml ..\benchmarks`.

## `workloads.py` and `crawl_scaling.py` (Phase 10)

Crawl throughput, latency and resource use with 1, 3 and 5 crawler workers (4 tasks each).

**Workloads** ([`workloads.py`](workloads.py)): deterministic sites (seed 2026) of `--pages` HTML
pages in BFS levels 0 to 4; every page links to its children, the root, its parent, random known
pages and one duplicate anchor; 3 % of the pages at depth 2 or more are also reached through a
301 redirect, 2 % of the pages above the last level link to a 404, 2 % of all pages answer 503
once (one retry each), 0.5 % of the last level's pages always answer 500 (retries used up). Of
all pages: the 1,000-page site has 30 redirects, 5 pages linking to a 404, 20 answering 503 once
and 4 failing; the five 400-page sites together 60, 15, 40 and 5 (each run's `expected` record).
Delays
of 40 ms (75 %), 120 ms (20 %) and 300 ms (5 %); bodies of about 4 KB (70 %), 24 KB (25 %) and
120 KB (5 %). Each run checks the crawl against the workload's expected counts (admitted URLs,
stored pages, redirects, HTTP errors, retries, requests) and the correctness invariants of every
job.

**Experiments**: `single-host` (one 1,000-page site, one job) and `multi-host` (five 400-page sites
on five hosts, one job each). Politeness: 4 requests in flight per host, 25 ms between starts.
Timings: `tests/process_support.FAST_TIMING` (the scheduler passes every 0.1 s).

```powershell
# in backend\: before/after in one session (the earlier code in a git worktree)
git worktree add --detach ..\..\sitescope-e27357a e27357a
.\.venv\Scripts\python.exe ..\benchmarks\crawl_scaling.py --experiment multi-host `
    --variant baseline=..\..\sitescope-e27357a\backend --variant current=. `
    --output ..\benchmarks\results\phase10-crawl-multi-host-ab.json
.\.venv\Scripts\python.exe ..\benchmarks\crawl_scaling.py --experiment single-host `
    --variant baseline=..\..\sitescope-e27357a\backend --variant current=. `
    --output ..\benchmarks\results\phase10-crawl-single-host-ab.json
# a size sweep (100, 500, 1,000, 2,000 pages over five hosts) with 5 workers
.\.venv\Scripts\python.exe ..\benchmarks\crawl_scaling.py --experiment multi-host `
    --pages 20,100,200,400 --workers 5 --output ..\benchmarks\results\phase10-crawl-sizes.json
# a setting for every process: --setting NAME=VALUE (without SITESCOPE_)
```

## `admission_plans.py` and `gap_monitor.py` (Phase 10)

The BFS level transitions that took 18 to 22 s in the single-host crawls, and the analysis
reads with the same cause (ADR-0023). `gap_monitor.py` watches the benchmark database beside a
running benchmark (unfinished URLs, long-running sessions, and the plans of a slow admission for
each running job, planned freely and as the scheduler runs it); `admission_plans.py` reproduces
the slow plans in isolation: an earlier job analyzed, a new job stored without analyzing (with
autovacuum off on the three tables from then on, so that the statistics change only when the
benchmark analyzes; the output records the planner's row counts and the analyze counters), then,
with stale and with fresh statistics: `EXPLAIN (ANALYZE)` of the level admission with and
without nested loops; the job's other reads timed as the API calls them (job detail and every
analysis use case); and `EXPLAIN (ANALYZE)` of every statement of every read, replaying the
read's own settings, with nested loops allowed and off.

```powershell
.\.venv\Scripts\python.exe ..\benchmarks\admission_plans.py `
    --output ..\benchmarks\results\phase10-analysis-reads.json `
    --plans ..\benchmarks\results\phase10-analysis-reads.txt
.\.venv\Scripts\python.exe ..\benchmarks\admission_plans.py --pages 20000 --links 20 --level 3 `
    --output ..\benchmarks\results\phase10-analysis-reads-large.json `
    --plans ..\benchmarks\results\phase10-analysis-reads-large.txt
.\.venv\Scripts\python.exe ..\benchmarks\admission_plans.py --pages 60000 --links 20 --level 4 `
    --output ..\benchmarks\results\phase10-analysis-reads-job11k.json `
    --plans ..\benchmarks\results\phase10-analysis-reads-job11k.txt
```

The result files: `phase10-admission-plans*.json` were run with the code before ADR-0023's
analysis reads (their timed reads are the "before"); `phase10-analysis-reads*-step1.json` with
its first step (hash joins for the reads that classify or count every link only);
`phase10-analysis-reads*.json` with the final code (every analysis read); `*-autovacuum-on.*`
are the first runs, before autovacuum was turned off (in their large case autovacuum analyzed
during the 60 s timeout, so its later arms were not stale; kept, not used).

## `index_scaling.py` (Phase 10)

Indexing with 1, 2 and 4 indexers on the Phase 7 dataset (2,000 pages of 1,000 words), trials
interleaved, with the samplers, per-document stage times, queue delay, and the GIN index's
`fastupdate` on and off (`pgstattuple` reports the pending list after each run).

```powershell
.\.venv\Scripts\python.exe ..\benchmarks\index_scaling.py --gin-fastupdate on,off `
    --output ..\benchmarks\results\phase10-indexing.json
```

## `search_latency.py` (Phase 10)

Search latency in the service and through the API: two jobs of 2,000 and 10,000 documents indexed
by the real pipeline; ten query kinds 200 times each (p50, p95, p99); pagination to the deepest
offset; `EXPLAIN (ANALYZE, BUFFERS)` of every query; 1, 4 and 16 concurrent API clients.

```powershell
.\.venv\Scripts\python.exe ..\benchmarks\search_latency.py `
    --output ..\benchmarks\results\phase10-search.json `
    --plans ..\benchmarks\results\phase10-search-plans.txt
```

## `db_queries.py` (Phase 10)

Plans and timings of the crawl pipeline's statements (claim, dispatch, admission, frontier scan,
reconciliation, cancellation check, index dispatch) and of the dashboard's reads (job list and
detail, as executed and captured, the overview and `/metrics` counts) on ten completed jobs of
5,000 pages and a running job with a frontier.

```powershell
.\.venv\Scripts\python.exe ..\benchmarks\db_queries.py `
    --output ..\benchmarks\results\phase10-db-queries.json `
    --plans ..\benchmarks\results\phase10-db-plans.txt
```

## `recovery.py` (Phase 10)

Recovery with real processes and short timings: a worker crash, an indexer crash (with content
changes during recovery), Redis stopped and started with and without its data, PostgreSQL stopped
and started, cancellation under load, and a control run without a fault; 3 trials each. The
clock starts once the processes run. The disposable containers publish a port chosen by the
script (an ephemeral one may change when a container is stopped and started); the results are
written after every run, with a failed run's error, so a failure keeps the runs before it.

```powershell
.\.venv\Scripts\python.exe ..\benchmarks\recovery.py --output ..\benchmarks\results\phase10-recovery.json
```

`phase10-recovery-run1.json` is a first full run whose first trial shared the machine with a
short test run; kept for comparison.

## `dashboard_graph.py` (Phase 10)

The dashboard's link graph at 300/1,500, 1,000/5,000 and 2,000/10,000 nodes/links, in the
installed Edge (Playwright, `frontend/e2e/graph-performance.spec.ts`, opt-in), against the real API
and a 5,000-page job: the time until every node is drawn, long tasks, heap, DOM size, and frame
intervals while panning and zooming. Needs `npm ci` in `frontend\`.

```powershell
.\.venv\Scripts\python.exe ..\benchmarks\dashboard_graph.py `
    --output ..\benchmarks\results\phase10-dashboard-graph.json
# a denser job: 10,000 links among the first 2,000 nodes
.\.venv\Scripts\python.exe ..\benchmarks\dashboard_graph.py --pages 2000 --links 10 `
    --output ..\benchmarks\results\phase10-dashboard-graph-dense.json
```

`phase10-dashboard-graph.json` and `-dense.json` measured the graph before Phase 10's labels
layer, `-after.json` and `-dense-after.json` with it, in the same way but ten hours later, on a
machine in another state (the frames' base interval differed: 18.0 ms against 16.7 ms). For the
comparison, `phase10-dashboard-graph-ab-before.json`, `-ab-after.json`, `-dense-ab-after.json` and
`-dense-ab-before.json` were measured in one session, in that order, by swapping
`GraphCanvas.tsx` between the committed version and the new one before each build.

## `charts.py` (Phase 10)

SVG charts in `results/charts/` and CSV summaries in `results/`, computed from the result files
only (medians, min-max whiskers, value labels): `.\.venv\Scripts\python.exe
..\benchmarks\charts.py`. A chart whose files are missing is skipped.

## Common to the Phase 10 scripts

- Percentiles: a p95 only from 20 samples, a p99 only from 100; fewer samples report the median,
  minimum and maximum.
- `environment.git` records the commit, whether tracked files changed, the number of untracked
  files and a digest of all changes, so that runs of uncommitted code can be told apart.
- A/B runs (`--variant`) alternate their order from trial to trial since the Phase 10 review;
  the Phase 10 A/B files ran each pair baseline first.
- Every run's Redis keys (streams, politeness state, heartbeats and their index) are deleted
  afterwards from database 15.

## `distributed_crawl.py` (Phase 5)

Crawl throughput of the distributed crawler with 1, 2 and 4 worker processes.

**Setup.** One scheduler process and N crawler worker processes (`python -m app.scheduler`,
`python -m app.workers.crawler`: separate operating-system processes, each running 4 tasks at
once) crawl generated sites: a tree of depth 3 and branching 6 (259 HTML pages per site, each page
also linking back to the root and to a sibling). The sites are served on this machine by
`tests/fetch_support.LocalHttpServer` on loopback addresses (127.0.0.2, and 127.0.0.2-127.0.0.5 for
four hosts, one job per host), and every page is answered after 100 ms, standing in for a remote
server's latency. Politeness is relaxed for the measurement (no spacing, 8 requests in flight per
host by default, `--per-host`): with the production defaults (1 s between requests, 1 request per
host) only the politeness limit would be measured.

**Measured.** The wall time from creating the jobs until every job is completed (process start-up
excluded), pages requested per second, and the pages each worker recorded, from its own log. Each
configuration runs three times; every run is reported.

**Isolation.** A disposable database `<SITESCOPE_POSTGRES_DB>_test_bench` (created, migrated and
dropped by the script), Redis database 15 under a unique key prefix (deleted afterwards), loopback
servers only. The development database is never touched.

**Run** from `backend\`, with PostgreSQL and Redis up:

```powershell
# 1 and 4 hosts, 1, 2 and 4 workers, 8 requests in flight per host, 3 runs each
.\.venv\Scripts\python.exe ..\benchmarks\distributed_crawl.py `
    --output ..\benchmarks\results\phase5-distributed.json --logs $env:TEMP\sitescope-bench-logs

# 4 hosts with 16 requests in flight per host (the settings' maximum), 3 runs each
.\.venv\Scripts\python.exe ..\benchmarks\distributed_crawl.py --hosts 4 --per-host 16 `
    --output ..\benchmarks\results\phase5-distributed-16-per-host.json --logs $env:TEMP\sitescope-bench-logs-16

# a Markdown table of a result file
.\.venv\Scripts\python.exe ..\benchmarks\summarize.py ..\benchmarks\results\phase5-distributed.json
```

The process logs of every run are written under `--logs` (not committed).

**Results.** See [`results/phase5-distributed.md`](results/phase5-distributed.md) and the raw
JSON files next to it.

## `indexing.py` (Phase 7)

Indexing throughput and latency with 1, 2 and 4 indexer processes, search latency, and the search
statement's query plans.

**Setup.** One index dispatcher process and N indexer processes
(`python -m app.scheduler.index_dispatcher`, `python -m app.workers.indexer`: separate
operating-system processes, each indexer running 4 tasks at once) index a generated dataset of
one job: 2,000 pages, each with a 5-word title, a 15-word description, 20 words of headings and a
1,000-word body, drawn from a 5,000-word pseudo-word vocabulary with a Zipf-like frequency and a
fixed seed (the same dataset in every run). The dispatcher publishes up to 1,000 tasks per pass
every 0.1 s, so that it does not limit the indexers.

**Measured.** The clock starts at the commit that stores the pages, read from PostgreSQL; each
page's latency is its `indexed_at` minus that commit time, by the same clock; throughput is pages
divided by the time to the last `indexed_at`. Process start-up is excluded. Each configuration
runs three times; every run is reported. Then, on the last run's index, after `VACUUM (ANALYZE)`
(which also merges the GIN index's pending list), eight queries (a common term, a medium and a
rare one, two terms, an OR, a phrase, no match, stop words only) run 30 times each through the
search service, one call at a time, and `EXPLAIN (ANALYZE, BUFFERS)` of the search statement is
saved for the common and the rare term.

**Isolation.** A disposable database `<SITESCOPE_POSTGRES_DB>_test_bench` (created, migrated and
dropped by the script), Redis database 15 under a unique key prefix per run (deleted afterwards).
The development database is never touched.

**Run** from `backend\`, with PostgreSQL and Redis up:

```powershell
.\.venv\Scripts\python.exe ..\benchmarks\indexing.py `
    --output ..\benchmarks\results\phase7-indexing.json `
    --plans ..\benchmarks\results\phase7-search-plans.txt `
    --logs $env:TEMP\sitescope-bench-index-logs
```

**Results.** See [`results/phase7-indexing.md`](results/phase7-indexing.md), the raw JSON
[`results/phase7-indexing.json`](results/phase7-indexing.json), the plans
[`results/phase7-search-plans.txt`](results/phase7-search-plans.txt), and the raw output of the
earlier runs [`results/phase7-indexing-earlier-runs.txt`](results/phase7-indexing-earlier-runs.txt).

## `analysis.py` and `analysis_index_ab.py` (Phase 8)

Latency and query plans of the website-analysis service (`app/services/site_analysis.py`), and
the A/B of the two indexes of migration 0005.

**Setup.** One job per size, built directly in PostgreSQL from a seeded generator (the same
dataset in every run): 1,000 and 10,000 stored pages in BFS levels of branching 10, each with 50
distinct links (80% to pages, 8% to non-page URLs, 7% to URLs never admitted, 5% external), plus
non-page URLs: 404s (5% of the pages), redirects to pages (2%), 503s, read timeouts and
robots.txt refusals (1% each). `VACUUM (ANALYZE)` after loading. No crawl runs; no HTTP server.

**Measured.** Every analysis use case (pages and links listings with and without filters and
deep cursors, broken links, the link graph at its default and maximum limits, statistics) runs 20
times in a row on one database session, as the API calls it; every call's wall time is kept.
Repeating calls on one connection exposes PostgreSQL's switch to generic plans after five
executions of a prepared statement. `EXPLAIN (ANALYZE, BUFFERS)` of every analysis statement is
saved. `analysis_index_ab.py` builds revision 0004 instead, loads the 10,000-page job once, and
measures every case with the indexes of 0004, with each candidate index, and with those of 0004
again.

**Isolation.** A disposable database `<SITESCOPE_POSTGRES_DB>_test_bench` (created, migrated and
dropped by the script). No Redis. The development database is never touched.

**Run** from `backend\`, with PostgreSQL up:

```powershell
.\.venv\Scripts\python.exe ..\benchmarks\analysis.py --pages 1000 10000 `
    --output ..\benchmarks\results\phase8-analysis.json `
    --plans ..\benchmarks\results\phase8-analysis-plans.txt
.\.venv\Scripts\python.exe ..\benchmarks\analysis_index_ab.py `
    --output ..\benchmarks\results\phase8-index-ab.json
```

**Results.** See [`results/phase8-analysis.md`](results/phase8-analysis.md), the raw JSON
[`results/phase8-analysis.json`](results/phase8-analysis.json) and
[`results/phase8-index-ab.json`](results/phase8-index-ab.json), the plans
[`results/phase8-analysis-plans.txt`](results/phase8-analysis-plans.txt), and the measurement
before the optimizations
[`results/phase8-analysis-baseline.json`](results/phase8-analysis-baseline.json) (its plans:
[`results/phase8-analysis-baseline-plans.txt`](results/phase8-analysis-baseline-plans.txt)).
