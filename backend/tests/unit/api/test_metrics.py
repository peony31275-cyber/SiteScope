"""GET /metrics rendering, its place outside the OpenAPI document, and the request histogram
(Phase 10). The endpoint against real PostgreSQL and Redis: tests/integration/test_metrics.py."""

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.metrics import render_metrics
from app.db.models import JobStatus, UrlState
from app.observability.heartbeat import Heartbeat
from app.observability.metrics import REGISTRY, Registry
from app.services.search_index import IndexingOverview
from app.services.system_metrics import StreamStats, SystemMetrics, SystemState


def state() -> SystemState:
    return SystemState(
        jobs_by_status=dict.fromkeys(JobStatus, 0) | {JobStatus.RUNNING: 2},
        urls_by_state=dict.fromkeys(UrlState, 0) | {UrlState.PENDING: 7, UrlState.FETCHED: 40},
        urls_waiting_for_retry=3,
        expired_leases=1,
        oldest_queued_seconds=12.5,
        documents=IndexingOverview(
            pending=4,
            queued=2,
            indexed=30,
            failed=1,
            active=2,
            oldest_pending_seconds=8.0,
            oldest_queued_seconds=None,
            jobs_awaiting_completion=1,
        ),
        counted_at=datetime(2026, 11, 8, tzinfo=UTC),
    )


def heartbeat(role: str, name: str, **state: Any) -> Heartbeat:
    registry = Registry()
    registry.counter("sitescope_tasks_handled_total", "Handled.", ("reason",)).inc(
        5, reason="completed"
    )
    return Heartbeat(
        name=name,
        role=role,
        age_seconds=1.5,
        payload={
            "role": role,
            "uptime_seconds": 60.0,
            "state": state,
            "metrics": registry.snapshot(),
        },
    )


def lines_of(collected: SystemMetrics) -> list[str]:
    return render_metrics(collected).splitlines()


def test_state_streams_and_processes_are_rendered() -> None:
    collected = SystemMetrics(
        state=state(),
        streams=[
            StreamStats("crawl", True, length=50, undelivered=10, pending=4, consumers=2),
            StreamStats("index", False),
        ],
        heartbeats=[
            heartbeat("crawler", "crawler-h-1-aa", in_flight=3, last_progress_seconds_ago=2.0),
            heartbeat(
                "scheduler",
                "scheduler-h-2-bb",
                passes=10,
                last_pass_seconds_ago=0.25,
                last_progress_seconds_ago=0.5,
                failed_jobs_last_pass=0,
            ),
            heartbeat("scheduler", "scheduler-h-3-cc", passes=10, failed_jobs_last_pass=1),
        ],
    )

    lines = lines_of(collected)

    assert 'sitescope_source_up{source="postgres"} 1' in lines
    assert 'sitescope_source_up{source="redis"} 1' in lines
    assert 'sitescope_crawl_jobs{status="running"} 2' in lines
    assert 'sitescope_crawl_jobs{status="queued"} 0' in lines
    assert 'sitescope_crawl_urls{state="pending"} 7' in lines
    assert "sitescope_crawl_urls_waiting_for_retry 3" in lines
    assert "sitescope_crawl_leases_expired 1" in lines
    assert "sitescope_crawl_oldest_queued_seconds 12.5" in lines
    assert 'sitescope_search_documents{status="indexed"} 30' in lines
    assert "sitescope_index_oldest_queued_seconds 0" in lines  # none queued
    assert 'sitescope_stream_undelivered{stream="crawl"} 10' in lines
    assert not any('stream="index"' in line for line in lines)  # the stream does not exist
    crawler = 'role="crawler",process="crawler-h-1-aa"'
    assert f"sitescope_process_up{{{crawler}}} 1" in lines
    assert f"sitescope_process_in_flight_tasks{{{crawler}}} 3" in lines
    # Progress age = the state's age plus the heartbeat's own age (1.5 s).
    assert f"sitescope_process_last_progress_seconds{{{crawler}}} 3.5" in lines
    assert f"sitescope_process_failing{{{crawler}}} 0" in lines
    assert not any(  # workers have no passes
        line.startswith(f"sitescope_process_last_pass_seconds{{{crawler}") for line in lines
    )
    scheduler = 'role="scheduler",process="scheduler-h-2-bb"'
    assert f"sitescope_process_last_progress_seconds{{{scheduler}}} 2" in lines
    assert f"sitescope_process_last_pass_seconds{{{scheduler}}} 1.75" in lines
    assert f"sitescope_process_failing{{{scheduler}}} 0" in lines
    # A job whose step failed in the last pass: failing, though the pass itself succeeded;
    # it never progressed and never finished a pass: both ages count from its start (60 s).
    broken = 'role="scheduler",process="scheduler-h-3-cc"'
    assert f"sitescope_process_failing{{{broken}}} 1" in lines
    assert f"sitescope_process_last_progress_seconds{{{broken}}} 61.5" in lines
    assert f"sitescope_process_last_pass_seconds{{{broken}}} 61.5" in lines
    assert f'sitescope_tasks_handled_total{{{crawler},reason="completed"}} 5' in lines


def test_a_source_that_failed_is_reported_and_its_part_left_out() -> None:
    lines = lines_of(SystemMetrics(state_error="OSError", redis_error="ConnectionError"))

    assert 'sitescope_source_up{source="postgres"} 0' in lines
    assert 'sitescope_source_up{source="redis"} 0' in lines
    assert not any(line.startswith("sitescope_crawl_") for line in lines)


def test_a_failing_process_and_an_unknown_role_are_rendered_safely() -> None:
    collected = SystemMetrics(
        heartbeats=[heartbeat("Not A Role!", "odd-name", stream_failures=2, in_flight="many")]
    )

    lines = lines_of(collected)

    labels = 'role="unknown",process="odd-name"'
    assert f"sitescope_process_failing{{{labels}}} 1" in lines
    assert not any(line.startswith("sitescope_process_in_flight_tasks") for line in lines)


def test_metrics_is_not_part_of_the_openapi_document(app: FastAPI) -> None:
    assert "/metrics" not in app.openapi()["paths"]


def _requests(route: str, method: str, status_class: str) -> int:
    for family in REGISTRY.snapshot():
        if family["name"] == "sitescope_http_request_duration_seconds":
            for labels, _cumulative, _total, count in family["series"]:
                if labels == [method, route, status_class]:
                    return int(count)
    return 0


def test_requests_are_timed_by_route_template_never_by_raw_path(client: TestClient) -> None:
    health_before = _requests("/health", "GET", "2xx")
    unmatched_before = _requests("unmatched", "GET", "4xx")
    other_before = _requests("unmatched", "other", "4xx")

    client.get("/health")
    client.get("/no/such/path/12345")
    client.request("PROPFIND", "/another/unknown/path")

    assert _requests("/health", "GET", "2xx") == health_before + 1
    assert _requests("unmatched", "GET", "4xx") == unmatched_before + 1
    assert _requests("unmatched", "other", "4xx") == other_before + 1
    routes = {
        tuple(labels)
        for family in REGISTRY.snapshot()
        if family["name"] == "sitescope_http_request_duration_seconds"
        for labels, *_ in family["series"]
    }
    assert not any("12345" in route for _method, route, _status in routes)
