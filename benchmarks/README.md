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
