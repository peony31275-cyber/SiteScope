# Phase 5: distributed crawl throughput (1, 2 and 4 workers)

Measured on 2026-11-07 (UTC) with `benchmarks/distributed_crawl.py`; method in
[`../README.md`](../README.md). These numbers describe this development machine only. They are
not a capacity claim for any other setup.

## Environment

- AMD Ryzen 7 5800U (8 cores, 16 threads), 15.3 GB RAM, Windows 10 Home 10.0.19045.
- Python 3.13.7 (`backend/.venv`). PostgreSQL 18.6 and Redis 8.10.3 in Docker inside WSL 2
  (Ubuntu 24.04), reached through WSL's localhost forwarding.
- Every process on the same machine: the scheduler, the workers, the benchmark process (which
  also serves the four test sites), PostgreSQL and Redis.
- Sites: trees of depth 3 and branching 6, 259 pages each, every page answered after 100 ms.
  One job per host (`127.0.0.2` to `127.0.0.5`). Each worker runs 4 tasks at once. Politeness:
  no spacing between requests; 8, then 16, requests in flight per host.

## Results

Wall time from creating the jobs until all are completed (process start-up excluded). Every run
is listed; raw output: [`phase5-distributed.json`](phase5-distributed.json) (8 per host; the
script then had 8 built in, now the default of `--per-host`) and
[`phase5-distributed-16-per-host.json`](phase5-distributed-16-per-host.json).

**8 requests in flight per host**

| Hosts | Workers (x4 tasks) | Pages | Runs: seconds | Runs: pages/s | Median pages/s |
|---|---|---|---|---|---|
| 1 | 1 | 259 | 10.3, 10.1, 10.2 | 25.2, 25.5, 25.4 | 25.4 |
| 1 | 2 | 259 | 6.1, 6.2, 6.2 | 42.5, 41.5, 41.7 | 41.7 |
| 1 | 4 | 259 | 6.7, 6.8, 6.8 | 38.8, 38.1, 38.1 | 38.1 |
| 4 | 1 | 1036 | 38.0, 38.4, 37.8 | 27.3, 27.0, 27.4 | 27.3 |
| 4 | 2 | 1036 | 20.9, 21.4, 21.0 | 49.6, 48.3, 49.2 | 49.2 |
| 4 | 4 | 1036 | 20.3, 20.1, 20.1 | 51.1, 51.5, 51.5 | 51.5 |

**16 requests in flight per host** (the settings' maximum)

| Hosts | Workers (x4 tasks) | Pages | Runs: seconds | Runs: pages/s | Median pages/s |
|---|---|---|---|---|---|
| 4 | 1 | 1036 | 38.2, 38.6, 37.4 | 27.1, 26.8, 27.7 | 27.1 |
| 4 | 2 | 1036 | 20.9, 21.1, 21.4 | 49.6, 49.1, 48.5 | 49.1 |
| 4 | 4 | 1036 | 11.9, 11.8, 12.1 | 87.3, 88.1, 85.5 | 87.3 |

Every run completed every job, the servers received exactly one request per page (robots.txt
aside), and every process exited with status 0. The work was shared evenly: with 2 workers each recorded 509 to 527 of the 1036 pages,
with 4 workers 253 to 267 (the per-run lists are in the JSON files and from
`summarize.py`).

## Observations

- **One host.** Going from 1 to 2 workers raised throughput from 25 to 42 pages/s. A third and
  fourth worker did not help (38 pages/s): 2 workers x 4 tasks already fill the host's 8 slots, and
  more tasks only wait for them. This is the documented limitation that one host's throughput is
  bounded by its politeness limit, not by the number of workers.
- **Four hosts, 8 per host.** 27, 49, 52 pages/s for 1, 2, 4 workers: the fourth worker added
  almost nothing.
- **Four hosts, 16 per host.** 27, 49, 87 pages/s. The same 16 tasks are now 1.8 times faster:
  the per-host limit, not the workers, was the bottleneck with 8 per host. A likely reason
  (consistent with the data, not proven): the scheduler publishes each job's tasks as one
  batch, so consecutive stream messages belong to one job, that is one host; workers reading 16
  messages in a row crowd onto one host's 8 slots while the other hosts idle. Interleaving the
  hosts in the stream is a candidate optimization for Phase 10.
- **Not the CPU.** A diagnostic run (4 hosts, 4 workers, 8 per host;
  [`phase5-probe-4-hosts-4-workers.json`](phase5-probe-4-hosts-4-workers.json): 20.7 s, like the
  runs above) sampled CPU once per second
  ([`phase5-probe-cpu-samples.txt`](phase5-probe-cpu-samples.txt)): the machine was 25-43 %
  busy, each Python process used about 1-2 % of the machine (a third of one core at most), and
  the WSL VM running PostgreSQL and Redis 6-13 %. In that run a task took 350 ms (median, from
  the workers' `task.handled` lines) for a 100 ms page. With 16 slots per host the same 16
  tasks completed 87 pages/s, about 180 ms per task on average, so a large part of the extra
  time was waiting for a politeness slot.
- With 1 worker, 4 tasks and 100 ms pages, the ceiling is 40 pages/s; 25-27 pages/s were
  measured, the difference being each task's own work (claim, robots.txt check, politeness
  slot, parsing, the result transaction, the acknowledgment) and the level barriers of BFS.
