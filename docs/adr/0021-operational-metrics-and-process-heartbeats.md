# ADR-0021: Operational metrics from the source of truth, and process heartbeats in Redis

- Status: Accepted; extended by [ADR-0025](0025-container-packaging-and-deployment-topology.md)
  (container health checks from heartbeats)
- Date: 2026-11-08
- Amends: [ADR-0001](0001-postgresql-source-of-truth.md) (Redis also holds process heartbeats);
  extends [ADR-0016](0016-distributed-execution-redis-streams.md) (diagnostics of the processes)

## Context

Phase 10 measures SiteScope and makes it observable. Operators need to answer: is the API up,
are PostgreSQL and Redis healthy, are tasks progressing, are workers working, is indexing falling
behind, do failures repeat, has the system recovered. Until now the answers were spread over log
lines (`worker.stats`, `scheduler.stats`, `index_dispatcher.stats`) and the read-only status
command, and nothing told a live worker from one that had silently died: the consumer group keeps
a crashed consumer for an hour, and its idle time grows the same way while a busy worker runs long
tasks.

Metrics raise three questions in this architecture:

- **Several independent processes.** Scheduler, crawler workers, index dispatcher, indexers and
  API run as separate processes, possibly on several machines. A counter kept by each process
  must be collected without counting any process twice, and without losing the meaning of a
  counter when a process restarts.
- **One source of truth.** Job, URL and document state lives in PostgreSQL (ADR-0001). Counting
  the same state again in each process would make a second, divergent source.
- **Where state may live.** Invariant 1 limits Redis to deliverable messages, rate-limit state
  and caches.

## Decision

1. **State is counted at the source.** `GET /metrics` reads crawl jobs by status, admitted URLs
   by state, URLs waiting for a retry, expired leases, the oldest queued task, and search
   documents by status from PostgreSQL, in one read-only REPEATABLE READ snapshot under the
   analysis time limit (`app/services/system_metrics.py`). These counts are complete whatever
   process did the work, and no process's count can be added twice. Stream statistics (length,
   undelivered entries, pending entries, consumers, dead letters) are read from Redis, and are
   approximate (ADR-0016).
2. **Process metrics stay in their process.** Each process records counters and histograms in an
   in-memory registry (`app/observability/metrics.py`): messages handled by reason, task and
   stage durations, queue delay, database operation durations, crawl and index results,
   recoveries (lease takeovers, reclaimed messages, republished tasks), errors, pass durations,
   API request durations. Labels come from small fixed sets (a stage, an outcome, a status
   class, a route template); never a URL, a job ID or a request path. Each family caps its
   series.
3. **Heartbeats carry them out.** Every long-running process writes a heartbeat to Redis every
   `SITESCOPE_HEARTBEAT_INTERVAL_SECONDS` (default 10 s; `app/observability/heartbeat.py`): its
   role, version, uptime, a small state (tasks in flight, time since its last finished task, or
   since its last pass and its last pass that did work, its error backoff, jobs whose step failed
   in its last pass, when it last recovered) and a snapshot of its registry. The key
   expires after three intervals; a graceful stop deletes it. Time is Redis `TIME`.
4. **One endpoint renders everything, per process.** `GET /metrics` renders the state, the
   streams, and every live heartbeat's snapshot in the Prometheus text format (version 0.0.4),
   each process's series labelled `role` and `process` (its unique name). Nothing is summed
   before exposure: a query sums processes after taking rates (`sum by (role) (rate(...))`),
   which treats a restarted process (a new name) as a new series and a crashed one as a series
   that ends. Any API replica returns the same complete view, since it reads Redis.
5. **Redis may hold heartbeats.** Invariant 1 is amended: Redis also holds process heartbeats.
   They are observability only: no claim, lease, acknowledgment, dispatch, reconciliation or
   other decision reads them, and losing them (Redis restarted empty) loses nothing but the view
   until each process's next heartbeat.
6. **Queue delay is measured by the PostgreSQL clock.** The claim statement returns
   `clock_timestamp() - dispatched_at` for a claim of a queued URL, and the index attempt
   statement the same for a first attempt; a takeover or a later attempt reports none. A
   republish by the reconciliation sets `dispatched_at` again, so the delay counts from the last
   publish; time lost with a message shows as a republish count instead. Windows
   and the WSL 2 VM clocks differ (Phase 2B measured about 160 ms), so a delay computed from a
   stream entry ID (Redis clock) and a worker's clock would not be trustworthy.
7. **Worker status comes from heartbeats, never from configuration.** The status command
   (`app.scheduler.status`) lists live processes from their heartbeats and assesses health
   (`services.diagnostics.assess`) from PostgreSQL counts and those heartbeats; when a source
   cannot be read, its answers are `unknown`, never healthy.
8. **No new dependency, no monitoring stack.** The registry and the text format are about 300
   lines of standard-library code. `prometheus_client` was not added: its multi-process mode
   shares files on one machine, which SiteScope's processes need not share, and its default
   registry would add process-specific collectors to every process. No Prometheus server,
   Grafana or alerting is deployed by this project; any Prometheus can scrape the endpoint.
9. **`/metrics` is internal.** It is served by the API outside `/api` (the dashboard's proxy and,
   in Phase 11, nginx forward `/api` only), excluded from the OpenAPI document, and has no
   authentication: a deployment must not expose the API port itself.

## Consequences

- A process that crashes or freezes disappears from `/metrics` and from the status command
  within three heartbeat intervals; a busy worker stays visible, since its heartbeat does not
  depend on reading the stream.
- A scrape costs one snapshot that counts `crawl_jobs`, `crawl_urls` and `search_documents`
  (linear in the deployment's data, like the dashboard's overview), a few Redis reads, and one
  read per live process. Phase 10 measured it (docs/performance.md); a much larger deployment
  would need counters maintained by the writers instead.
- Redis carries one small hash per live process, rewritten every interval.
- Per-process series churn when processes restart. Their number is bounded by the processes
  alive within three intervals, and each family's series by its cap.
- Counters live in memory: a process's counts end with it. Totals over the whole deployment come
  from PostgreSQL (the state gauges), not from summing counters.

## Alternatives considered

- **A `/metrics` endpoint in every process.** The standard Prometheus model, but every worker
  would open an HTTP port (an attack surface, and a port to allocate per process on a developer
  machine), and service discovery would be needed to find them. Kept as the way to grow if a
  deployment adopts Prometheus with discovery: the registries are already per process.
- **Summing the processes' counters in the API.** The sum drops when a process exits, which a
  rate reads as a counter reset, and counts again: the double counting this decision avoids.
- **A Pushgateway.** It keeps the last push of a dead process forever unless it is deleted, and
  adds a component.
- **Heartbeats in PostgreSQL.** Durable and joinable with leases, but a write per process every
  interval on the database every worker depends on, and a migration; and Redis's expiry removes
  dead processes without a cleanup job.
- **Consumer idle times from `XINFO CONSUMERS` as liveness.** Free, but a worker whose task slots
  are all busy does not read, so its idle time grows exactly like a dead consumer's; and the
  scheduler, the index dispatcher and the API are not consumers.
