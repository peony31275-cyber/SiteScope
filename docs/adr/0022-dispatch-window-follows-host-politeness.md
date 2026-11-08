# ADR-0022: A job's dispatch window follows its host's politeness slots

- Status: Accepted
- Date: 2026-11-08
- Extends: [ADR-0016](0016-distributed-execution-redis-streams.md) (the scheduler's
  backpressure) and [ADR-0015](0015-robots-txt-and-per-host-politeness.md) (per-host politeness)

## Context

The scheduler keeps at most `SITESCOPE_SCHEDULER_MAX_IN_FLIGHT_PER_JOB` (default 100) URLs of a
job queued or in progress, and publishes a job's room as one batch per pass, job after job. A job
crawls one host (its seed's), and the politeness gate lets at most
`SITESCOPE_POLITENESS_MAX_CONCURRENT_PER_HOST` requests (default 1) reach that host at a time.
Workers read the stream in order and run a task as soon as they read it; a task that waits for a
politeness slot waits inside the worker, holding one of its task slots.

Phase 10 measured the consequence (`benchmarks/crawl_scaling.py`, docs/performance.md): five
jobs on five hosts (400 pages each, 4 requests in flight per host, 25 ms between starts), crawled
by 1, 3 and 5 workers of 4 tasks each, reached 28.8, 29.3 and 33.0 pages per second (medians of
three runs). The servers' request timelines showed each host idle 73 % to 80 % of the time, with
0.47 requests in flight on average out of 4: the hosts were crawled about one at a time. With 5
workers, 98 % of their task slots were busy, mostly waiting for the slots of the one host whose
batch came first in the stream; the median task took 637 ms for pages answered in 40 to 300 ms.
Changing only the configuration, to 8 tasks in flight per job, gave 31.1, 68.6 and 87.6 pages per
second on the same code, and the median time from dispatch to result fell from 10.6 s to 0.21 s.

## Decision

The scheduler limits a job's queued and in-progress tasks to the smaller of
`SITESCOPE_SCHEDULER_MAX_IN_FLIGHT_PER_JOB` and `SITESCOPE_SCHEDULER_TASKS_PER_HOST_SLOT`
(default 2) times `SITESCOPE_POLITENESS_MAX_CONCURRENT_PER_HOST`
(`app/scheduler/scheduler.py: in_flight_per_job`). Two tasks per slot: one fetching, one ready to
take the slot the moment it frees, without waiting for the scheduler's next pass.

Nothing else changes: dispatch is still one transaction per job and pass (ADR-0002), the global
`SITESCOPE_SCHEDULER_MAX_OUTSTANDING` and the per-pass batch still apply, and the window is a
limit, not a reservation.

## Consequences

- Each pass publishes at most a few tasks per job, job after job, so the stream mixes hosts, and
  a job cannot occupy much more worker capacity than its host can use. Measured after the change:
  docs/performance.md.
- Tasks spend less time queued: the stream holds about two tasks per host slot instead of 100 per
  job.
- With the production politeness defaults (one request at a time, one second apart), a job has
  two tasks in flight; the host's limit, not the window, sets its pace.
- A host that answers very fast with a long scheduler interval could see its window run dry
  between passes: raise `SITESCOPE_SCHEDULER_TASKS_PER_HOST_SLOT` (the benchmarks use a 0.1 s
  interval and 25 ms spacing, and 2 per slot sufficed).
- Several jobs on one host each get their own window; their tasks share the host's slots.
- The single-host case is unchanged in kind: one host's slots bound its throughput whatever the
  number of workers (docs/performance.md).
- **A crash can hold a job's whole window.** A worker killed while it holds all of a job's
  in-flight tasks (two with the defaults) leaves them `in_progress` until their leases are taken
  over (`SITESCOPE_STREAM_RECLAIM_MIN_IDLE_SECONDS` plus up to one reclaim interval: 100 to
  115 s with the defaults); meanwhile
  that job dispatches nothing. With the old window of 100, the job's other queued tasks went on
  during the wait. Either way the level cannot end before the takeover; what the smaller window
  costs is the rest of the level's progress during that wait, once per crash, bounded by the
  takeover time. Correctness is unchanged: the window is only a dispatch limit. Lost messages
  are not affected in kind: a Redis that restarts empty loses every queued message whatever the
  window, and the reconciliation publishes them again after `reconcile_after`
  (docs/performance.md, "Recovery", measures both).

## Alternatives considered

- **Round-robin dispatch across jobs within a pass** (small batches of each job in turn). It
  mixes the stream, but takes many more transactions per pass when levels open, and still lets a
  job's 100 queued tasks fill workers that then wait for one host's slots.
- **Workers that skip or requeue a task whose host is busy.** It changes the consumer's model
  (a message read is a message handled), adds acknowledgment cases, and moves politeness out of
  the fetcher.
- **Documenting a configuration only.** The default of 100 would stay a trap for every
  multi-host deployment.
