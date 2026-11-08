"""``GET /metrics``: SiteScope's state and processes in the Prometheus text format (Phase 10).

Internal (ADR-0021). The API serves it next to ``/health``, outside ``/api``: the dashboard's
proxy (Vite, and nginx in a deployment) forwards ``/api`` only, so a browser never reaches it,
and it is not part of the OpenAPI document. It needs no authentication on a private network;
a deployment must not expose the API port itself.

What it reports (``app.services.system_metrics``):

* ``sitescope_source_up{source}``: whether PostgreSQL and Redis could be read for this scrape.
* State, counted from PostgreSQL in one snapshot (the authoritative counts, never added up from
  processes): ``sitescope_crawl_jobs{status}``, ``sitescope_crawl_urls{state}``,
  ``sitescope_crawl_urls_waiting_for_retry``, ``sitescope_crawl_leases_expired``,
  ``sitescope_crawl_oldest_queued_seconds``, ``sitescope_search_documents{status}``,
  ``sitescope_search_documents_active``, ``sitescope_index_oldest_pending_seconds``,
  ``sitescope_index_oldest_queued_seconds``, ``sitescope_index_jobs_awaiting_completion``.
* Streams, from Redis: ``sitescope_stream_entries``, ``_undelivered``, ``_pending``,
  ``_consumers``, ``_dead_letters`` (label ``stream``: ``crawl`` or ``index``).
* Processes, from their heartbeats (labels ``role``, ``process``): ``sitescope_process_up``,
  ``_heartbeat_age_seconds``, ``_uptime_seconds``, ``_in_flight_tasks``,
  ``_last_progress_seconds``, ``_last_pass_seconds`` (pass loops), ``_failing`` (the rules of
  ``services.diagnostics.ProcessView``, as in the status assessment); then every family of each
  process's own metrics. Each process is its own series: sum them in the query, after
  ``rate()``.
"""

import math
import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Response

from app.api.dependencies import DatabaseDep, RedisDep, SettingsDep
from app.observability.exposition import CONTENT_TYPE, Exposition
from app.observability.heartbeat import Heartbeat
from app.services.diagnostics import ProcessView
from app.services.system_metrics import SystemMetrics, collect_system_metrics

router = APIRouter()

_ROLE = re.compile(r"[a-z_]{1,32}")


def _number(value: object) -> float | None:
    """A finite number from a heartbeat (written by another process, maybe another version)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _process_gauges(exposition: Exposition, heartbeat: Heartbeat) -> None:
    role = heartbeat.role if _ROLE.fullmatch(heartbeat.role) else "unknown"
    labels = {"role": role, "process": heartbeat.name}
    state: Mapping[str, Any] = heartbeat.state
    exposition.gauge(
        "sitescope_process_up", "1 for each process whose heartbeat is live.", 1, **labels
    )
    exposition.gauge(
        "sitescope_process_heartbeat_age_seconds",
        "Seconds since the process's last heartbeat (Redis clock).",
        round(heartbeat.age_seconds, 3),
        **labels,
    )
    if (uptime := _number(heartbeat.payload.get("uptime_seconds"))) is not None:
        exposition.gauge(
            "sitescope_process_uptime_seconds",
            "Seconds since the process started, as of its last heartbeat.",
            uptime,
            **labels,
        )
    if (in_flight := _number(state.get("in_flight"))) is not None:
        exposition.gauge(
            "sitescope_process_in_flight_tasks",
            "Tasks a worker or indexer was running at its last heartbeat.",
            in_flight,
            **labels,
        )
    view = ProcessView.from_heartbeat(heartbeat)  # the same rules as the status assessment
    if (progress := view.progress_age_seconds) is not None:
        exposition.gauge(
            "sitescope_process_last_progress_seconds",
            "Seconds since the process last finished a task (workers, indexers) or did work "
            "in a pass (scheduler, index dispatcher); since it started if it never did.",
            round(progress, 3),
            **labels,
        )
    if (passed := view.pass_age_seconds) is not None:
        exposition.gauge(
            "sitescope_process_last_pass_seconds",
            "Scheduler and index dispatcher: seconds since a pass last finished (since the "
            "start if none has); it grows while a pass hangs.",
            round(passed, 3),
            **labels,
        )
    exposition.gauge(
        "sitescope_process_failing",
        "1 while the process is backing off after errors (an outage), or a job's step failed "
        "in its last pass; else 0.",
        1 if view.failing else 0,
        **labels,
    )


def render_metrics(collected: SystemMetrics) -> str:
    """The exposition text (see the module docstring)."""
    exposition = Exposition()
    up_help = "1 if the source could be read for this scrape, else 0."
    exposition.gauge(
        "sitescope_source_up", up_help, 0 if collected.state is None else 1, source="postgres"
    )
    exposition.gauge(
        "sitescope_source_up", up_help, 0 if collected.redis_error else 1, source="redis"
    )
    state = collected.state
    if state is not None:
        for status, count in state.jobs_by_status.items():
            exposition.gauge(
                "sitescope_crawl_jobs", "Crawl jobs by status.", count, status=status.value
            )
        for url_state, count in state.urls_by_state.items():
            exposition.gauge(
                "sitescope_crawl_urls",
                "Admitted URLs (crawl tasks) of every job by state.",
                count,
                state=url_state.value,
            )
        exposition.gauge(
            "sitescope_crawl_urls_waiting_for_retry",
            "Pending URLs that failed an attempt and wait for their retry.",
            state.urls_waiting_for_retry,
        )
        exposition.gauge(
            "sitescope_crawl_leases_expired",
            "URLs in progress whose lease expired: waiting for a takeover.",
            state.expired_leases,
        )
        exposition.gauge(
            "sitescope_crawl_oldest_queued_seconds",
            "How long the oldest queued (dispatched, unclaimed) task has waited; 0 if none.",
            round(state.oldest_queued_seconds or 0.0, 3),
        )
        documents = state.documents
        for document_status in ("pending", "queued", "indexed", "failed"):
            exposition.gauge(
                "sitescope_search_documents",
                "Search documents of every job by status.",
                getattr(documents, document_status),
                status=document_status,
            )
        exposition.gauge(
            "sitescope_search_documents_active",
            "Queued documents an indexer is working on (live lease).",
            documents.active,
        )
        exposition.gauge(
            "sitescope_index_oldest_pending_seconds",
            "How long the oldest pending document has waited for dispatch; 0 if none.",
            round(documents.oldest_pending_seconds or 0.0, 3),
        )
        exposition.gauge(
            "sitescope_index_oldest_queued_seconds",
            "Time since the oldest queued document was published; 0 if none.",
            round(documents.oldest_queued_seconds or 0.0, 3),
        )
        exposition.gauge(
            "sitescope_index_jobs_awaiting_completion",
            "Finished jobs whose indexing is not complete yet.",
            documents.jobs_awaiting_completion,
        )
    for stream in collected.streams:
        if not stream.exists:
            continue
        exposition.gauge(
            "sitescope_stream_entries",
            "Entries in a task stream.",
            stream.length,
            stream=stream.name,
        )
        if stream.undelivered is not None:
            exposition.gauge(
                "sitescope_stream_undelivered",
                "Stream entries not yet delivered to any consumer (the group's lag).",
                stream.undelivered,
                stream=stream.name,
            )
        exposition.gauge(
            "sitescope_stream_pending",
            "Entries delivered and not yet acknowledged.",
            stream.pending,
            stream=stream.name,
        )
        exposition.gauge(
            "sitescope_stream_consumers",
            "Consumers of the stream's group, live or not (removed after an idle hour).",
            stream.consumers,
            stream=stream.name,
        )
        exposition.gauge(
            "sitescope_stream_dead_letters",
            "Entries in the stream's dead-letter stream (capped).",
            stream.dead_letters,
            stream=stream.name,
        )
    for heartbeat in collected.heartbeats:
        _process_gauges(exposition, heartbeat)
    for heartbeat in collected.heartbeats:
        metrics = heartbeat.payload.get("metrics")
        if isinstance(metrics, list):
            role = heartbeat.role if _ROLE.fullmatch(heartbeat.role) else "unknown"
            exposition.add_snapshot(metrics, role=role, process=heartbeat.name)
    if exposition.skipped:
        exposition.gauge(
            "sitescope_metrics_snapshot_errors",
            "Metric families of process snapshots that could not be rendered in this scrape.",
            exposition.skipped,
        )
    return exposition.render()


@router.get("/metrics", include_in_schema=False)
async def get_metrics(settings: SettingsDep, database: DatabaseDep, redis: RedisDep) -> Response:
    """Prometheus text exposition (see the module docstring). Always 200: a source that could
    not be read is reported by ``sitescope_source_up``."""
    collected = await collect_system_metrics(
        database,
        redis,
        key_prefix=settings.redis_key_prefix,
        timeout_seconds=settings.analysis_timeout_seconds,
    )
    return Response(
        content=render_metrics(collected),
        media_type=CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )
