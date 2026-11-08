# Benchmarks

Reproducible measurements. Each script records its command, its environment and its raw output
under `results/`. Numbers describe the machine they were measured on, and nothing else.

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
