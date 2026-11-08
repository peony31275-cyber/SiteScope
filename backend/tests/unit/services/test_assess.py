"""The status command's health assessment (``services.diagnostics.assess``, Phase 10)."""

from typing import Any

from app.observability.heartbeat import Heartbeat
from app.services.diagnostics import (
    CRITICAL,
    OK,
    UNKNOWN,
    WARNING,
    CrawlOverview,
    IndexOverview,
    ProcessView,
    assess,
)
from app.services.search_index import IndexingOverview


def crawl(
    *, running: int = 1, oldest_queued: float | None = 1.0, expired: int = 0
) -> CrawlOverview:
    return CrawlOverview(
        jobs_by_status={"running": running, "completed": 3},
        active_jobs=[],
        live_leases_by_owner={},
        expired_leases=expired,
        oldest_queued_seconds=oldest_queued,
    )


def index(
    *, pending: int = 0, queued: int = 0, oldest_pending: float | None = None
) -> IndexOverview:
    return IndexOverview(
        documents=IndexingOverview(
            pending=pending,
            queued=queued,
            indexed=10,
            failed=0,
            active=0,
            oldest_pending_seconds=oldest_pending,
            oldest_queued_seconds=None,
            jobs_awaiting_completion=0,
        ),
        live_leases_by_indexer={},
    )


def process(
    role: str,
    name: str | None = None,
    *,
    age: float = 1.0,
    uptime: float | None = 3600.0,
    **state: Any,
) -> ProcessView:
    return ProcessView(
        role=role,
        name=name or f"{role}-h-1-aa",
        heartbeat_age_seconds=age,
        state=state,
        uptime_seconds=uptime,
    )


HEALTHY = [
    process("api"),
    process(
        "scheduler",
        passes=500,
        last_pass_seconds_ago=0.2,
        last_progress_seconds_ago=0.2,
        consecutive_failures=0,
        failed_jobs_last_pass=0,
    ),
    process("crawler", in_flight=2, last_progress_seconds_ago=0.5),
    process("index_dispatcher", passes=500, last_pass_seconds_ago=0.1, last_progress_seconds_ago=1),
    process("indexer", in_flight=0, last_progress_seconds_ago=3.0),
]


def without(*roles: str) -> list[ProcessView]:
    return [item for item in HEALTHY if item.role not in roles]


def test_a_healthy_system_is_ok_everywhere() -> None:
    findings = assess(crawl=crawl(), index=index(), redis_ok=True, processes=HEALTHY)

    assert {name: finding.status for name, finding in findings.items()} == {
        "api": OK,
        "postgres": OK,
        "redis": OK,
        "crawl_progress": OK,
        "crawler_workers": OK,
        "indexing": OK,
        "repeated_failures": OK,
        "recovery": OK,
    }


def test_running_jobs_without_a_crawler_or_a_scheduler_are_critical() -> None:
    findings = assess(
        crawl=crawl(), index=index(), redis_ok=True, processes=without("crawler", "scheduler")
    )

    assert findings["crawl_progress"].status == CRITICAL
    assert "no scheduler" in findings["crawl_progress"].detail
    assert "no crawler worker" in findings["crawl_progress"].detail
    assert findings["crawler_workers"].status == WARNING


def test_old_queued_tasks_and_expired_leases_are_warnings() -> None:
    findings = assess(
        crawl=crawl(oldest_queued=600.0, expired=2),
        index=index(),
        redis_ok=True,
        processes=HEALTHY,
        stall_seconds=120,
    )

    assert findings["crawl_progress"].status == WARNING
    assert "600 s" in findings["crawl_progress"].detail
    assert "2 expired lease(s)" in findings["crawl_progress"].detail


def test_a_busy_worker_without_progress_is_reported_and_an_idle_one_is_not() -> None:
    stuck = process("crawler", "crawler-h-2-bb", in_flight=4, last_progress_seconds_ago=500.0)
    idle = process("crawler", "crawler-h-3-cc", in_flight=0, last_progress_seconds_ago=900.0)

    findings = assess(
        crawl=crawl(), index=index(), redis_ok=True, processes=[*HEALTHY, stuck, idle]
    )

    assert findings["crawler_workers"].status == WARNING
    assert "crawler-h-2-bb" in findings["crawler_workers"].detail
    assert "crawler-h-3-cc" not in findings["crawler_workers"].detail


def test_a_busy_worker_that_never_finished_a_task_is_measured_from_its_start() -> None:
    """Phase 10 review: with no progress recorded, the age used to be unknown and the worker
    was never reported. Its uptime is the age now."""
    stuck = process("crawler", "crawler-h-2-bb", uptime=500.0, in_flight=4)
    starting = process("crawler", "crawler-h-3-cc", uptime=30.0, in_flight=4)

    findings = assess(
        crawl=crawl(), index=index(), redis_ok=True, processes=[*HEALTHY, stuck, starting]
    )

    assert findings["crawler_workers"].status == WARNING
    assert "crawler-h-2-bb" in findings["crawler_workers"].detail
    assert "crawler-h-3-cc" not in findings["crawler_workers"].detail


def test_a_scheduler_whose_pass_hangs_is_reported_though_its_heartbeat_is_fresh() -> None:
    """Phase 10 review: the heartbeat is written by its own task, so a pass that waits for a
    lock keeps it fresh; the time since the last finished pass shows it."""
    hung = process("scheduler", "scheduler-h-2-bb", passes=40, last_pass_seconds_ago=300.0)

    alone = assess(
        crawl=crawl(), index=index(), redis_ok=True, processes=[*without("scheduler"), hung]
    )
    with_another = assess(crawl=crawl(), index=index(), redis_ok=True, processes=[*HEALTHY, hung])

    assert alone["crawl_progress"].status == CRITICAL
    assert "no scheduler pass finished for 120 s: scheduler-h-2-bb" in (
        alone["crawl_progress"].detail
    )
    assert with_another["crawl_progress"].status == WARNING


def test_a_scheduler_that_never_finished_a_pass_is_measured_from_its_start() -> None:
    stuck = process("scheduler", "scheduler-h-2-bb", uptime=400.0, passes=0)
    starting = process("scheduler", "scheduler-h-3-cc", uptime=5.0, passes=0)

    findings = assess(
        crawl=crawl(), index=index(), redis_ok=True, processes=[*without("scheduler"), starting]
    )
    stalled = assess(
        crawl=crawl(), index=index(), redis_ok=True, processes=[*without("scheduler"), stuck]
    )

    assert findings["crawl_progress"].status == OK
    assert stalled["crawl_progress"].status == CRITICAL


def test_an_index_dispatcher_whose_pass_hangs_is_reported() -> None:
    hung = process(
        "index_dispatcher", "index-dispatcher-h-2-bb", passes=9, last_pass_seconds_ago=900
    )

    findings = assess(
        crawl=crawl(),
        index=index(pending=20, oldest_pending=30.0),
        redis_ok=True,
        processes=[*without("index_dispatcher"), hung],
    )

    assert findings["indexing"].status == CRITICAL
    assert "index-dispatcher-h-2-bb" in findings["indexing"].detail


def test_an_indexing_backlog_without_indexers_is_critical() -> None:
    findings = assess(
        crawl=crawl(),
        index=index(pending=50, oldest_pending=400.0),
        redis_ok=True,
        processes=without("indexer"),
    )

    assert findings["indexing"].status == CRITICAL
    assert "no indexer" in findings["indexing"].detail
    assert "400 s" in findings["indexing"].detail


def test_failing_processes_are_repeated_failures_not_yet_recovered() -> None:
    failing = process("crawler", "crawler-h-9-zz", in_flight=0, stream_failures=3)

    findings = assess(crawl=crawl(), index=index(), redis_ok=True, processes=[*HEALTHY, failing])

    assert findings["repeated_failures"].status == WARNING
    assert "crawler-h-9-zz" in findings["repeated_failures"].detail
    assert findings["recovery"].status == WARNING


def test_a_job_failing_in_every_pass_is_a_repeated_failure() -> None:
    """Phase 10 review: the pass succeeds (its other jobs go on), so the loop does not back
    off; the job whose step failed is in the heartbeat instead."""
    broken = process(
        "scheduler",
        "scheduler-h-2-bb",
        passes=90,
        last_pass_seconds_ago=0.1,
        last_progress_seconds_ago=0.1,
        consecutive_failures=0,
        failed_jobs_last_pass=1,
    )

    findings = assess(
        crawl=crawl(), index=index(), redis_ok=True, processes=[*without("scheduler"), broken]
    )

    assert findings["repeated_failures"].status == WARNING
    assert "scheduler-h-2-bb" in findings["repeated_failures"].detail
    assert findings["recovery"].status == WARNING


def test_a_recent_recovery_is_reported() -> None:
    recovered = process("indexer", "indexer-h-4-dd", age=2.0, last_recovered_seconds_ago=30.0)

    findings = assess(crawl=crawl(), index=index(), redis_ok=True, processes=[*HEALTHY, recovered])

    assert findings["recovery"].status == OK
    assert "indexer-h-4-dd (32 s ago)" in findings["recovery"].detail


def test_unreachable_sources_make_their_answers_unknown_never_healthy() -> None:
    findings = assess(crawl=None, index=None, redis_ok=False, processes=[])

    assert findings["postgres"].status == CRITICAL
    assert findings["redis"].status == CRITICAL
    assert findings["api"].status == UNKNOWN
    assert findings["crawl_progress"].status == UNKNOWN
    assert findings["crawler_workers"].status == UNKNOWN
    assert findings["indexing"].status == UNKNOWN
    assert findings["repeated_failures"].status == UNKNOWN
    assert findings["recovery"].status == UNKNOWN


def test_redis_down_with_postgres_up_never_reports_moving_work_as_ok() -> None:
    """Phase 10 review: PostgreSQL alone cannot show that work moves (URLs that cannot be
    published stay pending, which no rule counts); a problem it does show is still reported."""
    down = assess(
        crawl=crawl(), index=index(pending=5, oldest_pending=2.0), redis_ok=False, processes=[]
    )
    known_problem = assess(crawl=crawl(expired=3), index=index(), redis_ok=False, processes=[])
    idle = assess(crawl=crawl(running=0), index=index(), redis_ok=False, processes=[])

    assert down["redis"].status == CRITICAL
    assert down["crawl_progress"].status == UNKNOWN
    assert "Redis could not be read" in down["crawl_progress"].detail
    assert down["indexing"].status == UNKNOWN
    assert known_problem["crawl_progress"].status == WARNING
    assert "3 expired lease(s)" in known_problem["crawl_progress"].detail
    # Nothing to move: no job running or queued, no document waiting.
    assert idle["crawl_progress"].status == OK
    assert idle["indexing"].status == OK


def test_the_view_reads_a_heartbeat_and_ignores_values_of_the_wrong_type() -> None:
    heartbeat = Heartbeat(
        name="scheduler-h-1-aa",
        role="scheduler",
        age_seconds=2.0,
        payload={
            "uptime_seconds": 50.0,
            "state": {
                "passes": 3,
                "last_pass_seconds_ago": "soon",
                "last_progress_seconds_ago": True,
                "failed_jobs_last_pass": 2,
            },
        },
    )

    view = ProcessView.from_heartbeat(heartbeat)

    assert view.uptime_seconds == 50.0
    assert view.pass_age_seconds == 52.0  # not a number: measured from the start
    assert view.progress_age_seconds == 52.0
    assert view.failing
