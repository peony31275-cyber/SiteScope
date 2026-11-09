# Observability

How to tell what SiteScope is doing, and whether it is healthy: structured logs, the metrics
endpoint, process heartbeats, the health probes and the status command. The design is
[ADR-0021](adr/0021-operational-metrics-and-process-heartbeats.md); measurements of the system
are in [performance.md](performance.md).

## Questions, and where the answers are

| Question | Answer |
|---|---|
| Is the API available? | `GET /health` or `GET /api/v1/health/live` (the process serves requests); `sitescope_process_up{role="api"}` |
| Are PostgreSQL and Redis healthy? | `GET /api/v1/health/ready` (each checked under a 2 s deadline); `sitescope_source_up{source}` |
| Are tasks progressing? | `sitescope_crawl_urls{state}` changing; `sitescope_crawl_oldest_queued_seconds`; `sitescope_crawl_leases_expired`; `sitescope_process_last_pass_seconds{role="scheduler"}`; status `assessment.crawl_progress` |
| Are workers processing tasks? | `sitescope_process_up{role="crawler"}`, `sitescope_process_last_progress_seconds`, `sitescope_process_in_flight_tasks`; status `processes` and `assessment.crawler_workers` |
| Is indexing falling behind? | `sitescope_search_documents{status="pending"|"queued"}`, `sitescope_index_oldest_pending_seconds`; status `assessment.indexing` |
| Are failures repeating? | `rate(sitescope_crawl_results_total{state="failed"}[5m])`, `rate(sitescope_worker_errors_total[5m])`, `sitescope_process_failing`, `sitescope_stream_dead_letters`; status `assessment.repeated_failures` |
| Has the system recovered? | `sitescope_process_failing` back to 0; `rate(sitescope_recoveries_total[5m])`; status `assessment.recovery` |

Worker status always comes from the workers' own heartbeats, never from a configured count.

## Logs

Every process writes one JSON object per line to stdout (`app/core/logging.py`): `timestamp`,
`level`, `logger`, `message`, `service`, the event name (`event`) and its fields. Context
variables add `request_id` (API), and for a task `job_id`, `url_id` or `page_id`, `worker_id`,
`consumer`, `message_id`, `lease_epoch`, `depth` and `attempt`. An indexer's `worker_id` is the
indexer's own unique name (`indexer-<host>-<pid>-<random>`); `service` tells the role. Logs never
contain credentials or page text, and request paths are logged without their query string.

Each stream message ends with one `task.handled` line: its `disposition` (`ack`, `keep`,
`dead_letter`), `reason`, `duration_ms`, `deliveries`, `reclaimed`, and since Phase 10:

| Field | Crawler | Indexer |
|---|---|---|
| `queue_delay_ms` | last publish to claim, by the PostgreSQL clock (claims of a queued URL only) | last publish to the first attempt |
| stage times | `claim_ms`, `crawl_ms` (robots.txt, politeness wait, request, parsing), `politeness_wait_ms`, `record_ms` | `start_ms` (the attempt's start), `index_ms` (building and storing the vector) |
| `failure_category` | for an unsuccessful result: `http_4xx`, `http_5xx`, `content`, `policy`, `timeout`, `network`, `politeness`, `infrastructure`, `parse`, `other` | |
| `stage`, `error_type` | on an error, the step that failed (`claim`, `inspect`, `crawl`, `record`, ...) | the same (`start_attempt`, `index_document`, ...) |

Recovery events: `lease.taken_over`, `index.lease_taken_over`, `task.reclaimed`,
`task.republished`, `index.republished`, `worker.recovered`, `scheduler.recovered`,
`index_dispatcher.recovered`, `heartbeat.recovered`. The full event list is in
[architecture section 14](architecture.md#14-observability-and-configuration).

## Metrics: `GET /metrics`

The API serves the Prometheus text format (version 0.0.4) at `/metrics`, outside `/api`: the
dashboard's proxy (and nginx in Phase 11) forwards `/api` only, so browsers never reach it, and
it is not in the OpenAPI document. It has no authentication: never expose the API port itself.
The response is always 200; a source that could not be read shows as
`sitescope_source_up{source} 0` and its part is left out.

### Collected from multiple processes without double counting

Two kinds of series, so that nothing is counted twice:

1. **State, counted at the source.** Every scrape counts jobs, URLs and search documents in
   PostgreSQL, in one read-only snapshot under `SITESCOPE_ANALYSIS_TIMEOUT_SECONDS`. These are
   totals over every process's work: no process reports them, so none can be missed or added
   twice.
2. **Process metrics, one series per process.** Each process keeps its own counters and
   histograms (`app/observability/metrics.py`) and publishes a snapshot in its heartbeat. The
   endpoint renders every live process's series with the labels `role` and `process` (its
   unique name, which changes when it restarts). Nothing is summed before exposure; sum in the
   query, after the rate:

   ```promql
   sum by (role) (rate(sitescope_tasks_handled_total[5m]))
   histogram_quantile(0.95, sum by (le) (rate(sitescope_queue_delay_seconds_bucket{role="crawler"}[5m])))
   ```

   A restarted process starts new series at zero; a crashed one's series end when its heartbeat
   expires. Summing counters across processes before the rate would make an exiting process
   look like a counter reset, and the rate would count its total again.

Any API replica returns the same view, since all of it comes from PostgreSQL and Redis.

### State series (from PostgreSQL and Redis)

| Series | Meaning |
|---|---|
| `sitescope_source_up{source="postgres"\|"redis"}` | 1 if the source was read for this scrape |
| `sitescope_crawl_jobs{status}` | crawl jobs by status |
| `sitescope_crawl_urls{state}` | admitted URLs (crawl tasks) by state: pending, queued, in_progress, fetched, failed, skipped, cancelled |
| `sitescope_crawl_urls_waiting_for_retry` | pending URLs that failed an attempt and wait for their retry |
| `sitescope_crawl_leases_expired` | in progress, lease expired: waiting for a takeover |
| `sitescope_crawl_oldest_queued_seconds` | how long the oldest queued task has waited (0 if none) |
| `sitescope_search_documents{status}` | search documents by status: pending, queued, indexed, failed |
| `sitescope_search_documents_active` | queued documents an indexer is working on |
| `sitescope_index_oldest_pending_seconds`, `_oldest_queued_seconds` | the oldest waiting documents (0 if none) |
| `sitescope_index_jobs_awaiting_completion` | finished jobs whose indexing is not complete |
| `sitescope_stream_entries{stream}`, `_undelivered`, `_pending`, `_consumers`, `_dead_letters` | the crawl and index streams (Redis, approximate) |
| `sitescope_metrics_snapshot_errors` | process metric families that could not be rendered in this scrape (a damaged or incompatible snapshot); absent when there are none |

### Process series (labels `role`, `process`)

| Series | Type | Processes | Meaning |
|---|---|---|---|
| `sitescope_process_up` | gauge | all | 1 per live heartbeat |
| `sitescope_process_heartbeat_age_seconds`, `_uptime_seconds` | gauge | all | |
| `sitescope_process_in_flight_tasks` | gauge | workers, indexers | tasks running at the last heartbeat |
| `sitescope_process_last_progress_seconds` | gauge | all | since the last finished task, or the last pass that did work (a job's failed step is not work); since the start if none |
| `sitescope_process_last_pass_seconds` | gauge | scheduler, index dispatcher | since the last finished pass (since the start if none): grows while a pass hangs, though the heartbeat stays fresh |
| `sitescope_process_failing` | gauge | all | 1 while backing off after errors, or when a job's step failed in the last pass |
| `sitescope_tasks_handled_total{reason,disposition}` | counter | workers, indexers | messages handled |
| `sitescope_task_duration_seconds{disposition}` | histogram | workers, indexers | one message, start to settlement |
| `sitescope_queue_delay_seconds` | histogram | workers, indexers | last publish to claim (crawl) or to first attempt (index), PostgreSQL clock; a republish by the reconciliation starts it again, so time lost with a message shows in `sitescope_pass_items_total{item="republished"}` instead |
| `sitescope_db_operation_seconds{operation}` | histogram | workers, indexers | one transaction: `claim`, `inspect`, `record`, `recover`, `quarantine`, `cancel`, `start_attempt`, `index_document`, `fail_exhausted`, `note_error` |
| `sitescope_crawl_phase_seconds` | histogram | crawlers | robots.txt, politeness wait, request and parsing of one claimed URL |
| `sitescope_politeness_wait_seconds` | histogram | crawlers | waiting for politeness slots, per task |
| `sitescope_crawl_results_total{state,outcome}` | counter | crawlers | attempt results (retried attempts included) |
| `sitescope_index_results_total{outcome}` | counter | indexers | attempt results |
| `sitescope_recoveries_total{kind}` | counter | workers, indexers | `lease_takeover`, `exhausted_url_failed`, `poison_message_quarantined`, `index_lease_takeover`, `exhausted_document_failed` |
| `sitescope_messages_reclaimed_total` | counter | workers, indexers | messages taken over with `XAUTOCLAIM` |
| `sitescope_worker_errors_total{source}` | counter | workers, indexers | errors that paused the consumer (`stream`, `task`) |
| `sitescope_settle_failures_total` | counter | workers, indexers | acknowledgments that failed (the message stays pending) |
| `sitescope_pass_duration_seconds{result}` | histogram | scheduler, index dispatcher | one pass (`ok`, `failed`) |
| `sitescope_pass_items_total{item}` | counter | scheduler, index dispatcher | `activated`, `advanced`, `finished`, `dispatched`, `republished`, `cancelled`, `failed_jobs`, `completed_jobs` |
| `sitescope_http_request_duration_seconds{method,route,status_class}` | histogram | API | by route template (`unmatched` if none), method (`other` for unusual ones) and status class |

Labels come from fixed sets: never a URL, a path, a job ID or a query. Each family caps its series
(64 by default, 256 for the request histogram); beyond it, new label values are dropped and a
`metrics.series_limit` warning is logged once.

### Cost

A scrape runs one snapshot that counts `crawl_jobs`, `crawl_urls` and `search_documents` (like the
dashboard's overview: linear in the deployment's data), a few Redis reads, and one hash read per
live process. Measured costs are in [performance.md](performance.md). Scrape every 15 to 60
seconds.

## Heartbeats

Every long-running process (API, scheduler, crawler workers, index dispatcher, indexers) writes a
heartbeat every `SITESCOPE_HEARTBEAT_INTERVAL_SECONDS` (default 10; `app/observability/
heartbeat.py`). The API publishes one only as the server application (`uvicorn app.main:app`),
not in tests.

- `<prefix>:process:<name>`: a hash, `payload` (JSON: role, version, interval, uptime, state,
  metrics) and `seen_ms` (Redis `TIME`), expiring after three intervals.
- `<prefix>:processes`: the names, scored by their last heartbeat; entries older than an hour are
  removed by the writers.

State fields: workers and indexers report `in_flight`, `concurrency`, `handled` (by reason),
`last_progress_seconds_ago`, `last_recovered_seconds_ago`, `paused_seconds`, `stream_failures`
and `task_failures`; the scheduler and the index dispatcher `passes`, `failed_passes`,
`consecutive_failures`, `failed_jobs_last_pass` (jobs whose step failed in the last pass, which
itself succeeded: one broken job fails every pass), `last_pass_seconds_ago` (a pass that hangs
leaves the heartbeat fresh, since another task writes it), `last_progress_seconds_ago` (the last
pass that did work) and `last_recovered_seconds_ago`.

A process that crashes, freezes, or cannot reach Redis disappears within three intervals; a
graceful stop removes its heartbeat at once. A worker whose task slots are all busy keeps its
heartbeat: it is written by a task of its own, not by the read loop. Heartbeats decide nothing:
no claim, lease, acknowledgment or dispatch reads them.

## The status command

```powershell
.\.venv\Scripts\python.exe -m app.scheduler.status [--jobs 20] [--dead-letters 10] [--stall-seconds 120]
```

Read-only. Prints one JSON document: `database` (jobs, URL states, leases), `stream`, `indexing`,
`index_stream` (each stream with its newest dead letters), `processes` (every live heartbeat) and
`assessment`: for `api`, `postgres`, `redis`, `crawl_progress`, `crawler_workers`, `indexing`,
`repeated_failures` and `recovery`, a status (`ok`, `warning`, `critical`, `unknown`) and its
reason. The rules (`services/diagnostics.py: assess`):

- running jobs without a live scheduler or crawler worker: `critical`;
- a scheduler (or index dispatcher) that finished no pass for `--stall-seconds` (counted from its
  start if it never did): `warning`; `critical` if every one is in that state while work waits;
- a queued task older than `--stall-seconds`, or expired leases: `warning`;
- a worker with tasks in flight that finished none for `--stall-seconds` (counted from its start
  if it never did): `warning`;
- waiting documents without a live indexer (or pending ones without an index dispatcher):
  `critical`; the oldest waiting longer than `--stall-seconds`: `warning`;
- a process backing off after errors, or a scheduler whose last pass had a failed job:
  `warning` (repeated failures, not recovered);
- a source that could not be read makes its answers `unknown`, never `ok`: with Redis down and
  work waiting, crawl progress and indexing are `unknown` unless PostgreSQL alone shows a
  problem (URLs that cannot be published stay `pending`, which no rule counts).

`sitescope_process_last_progress_seconds`, `_last_pass_seconds` and `_failing` follow the same
rules (`ProcessView`), so the metrics and the assessment agree.

The exit status is 1 if PostgreSQL or Redis could not be read.

## Health probes

- `GET /health`, `GET /api/v1/health/live`: liveness; no dependency is checked.
- `GET /api/v1/health/ready`: PostgreSQL (`SELECT 1`) and Redis (`PING`), each under
  `SITESCOPE_READINESS_TIMEOUT_SECONDS`; 503 with the same body when either fails.
- Containers (Phase 11, `deploy/compose.yaml`):
  - The API is checked with `GET /health`. The worker roles (scheduler, crawler, index
    dispatcher, indexer) use `python -m app.observability.alive <role>`, which exits 0 while
    Redis holds a live heartbeat of that role from the container's own host name.
  - A worker that froze, lost Redis for three heartbeat intervals, or stopped turns unhealthy.
  - Docker reports the result in `docker compose ps`; Compose uses it only to order startup.
    Like every heartbeat reader, it decides nothing in SiteScope (ADR-0021).

## Limitations

- No Prometheus server, Grafana or alerting is part of the project; the endpoint is ready for
  any Prometheus to scrape.
- Process counters live in memory: they restart with their process. Totals over a deployment's
  life come from the PostgreSQL state series.
- The state counts read whole tables; a much larger deployment would need counters maintained by
  the writers (ADR-0021).
- Stream statistics are approximate (Redis); progress is authoritative only from PostgreSQL.
- Heartbeat ages use the Redis clock; process state ages come from each process's monotonic
  clock, as of its last heartbeat.
