"""The overview endpoint's response (Phase 9): every status and state reported, totals that add
up. The counts themselves are tested against PostgreSQL in tests/integration/test_overview.py."""

from datetime import UTC, datetime

from app.api.v1.overview import JobStatusCounts, OverviewResponse
from app.db.models import IndexStatus, JobStatus, UrlState
from app.services.overview import SystemOverview


def test_every_job_status_is_reported() -> None:
    assert set(JobStatusCounts.model_fields) == {status.value for status in JobStatus}


def test_the_response_reports_every_count_and_its_total() -> None:
    overview = SystemOverview(
        jobs_by_status={status: index for index, status in enumerate(JobStatus)},
        urls_by_state={state: 10 + index for index, state in enumerate(UrlState)},
        pages_stored=7,
        documents_by_status={
            IndexStatus.PENDING: 1,
            IndexStatus.QUEUED: 2,
            IndexStatus.INDEXED: 3,
            IndexStatus.FAILED: 4,
        },
        documents_active=2,
        counted_at=datetime(2026, 11, 7, 10, 15, 5, tzinfo=UTC),
    )

    body = OverviewResponse.from_overview(overview).model_dump(mode="json")

    assert body["jobs"] == {
        "total": 0 + 1 + 2 + 3 + 4 + 5,
        "by_status": {status.value: index for index, status in enumerate(JobStatus)},
    }
    assert body["urls"]["total"] == sum(10 + index for index in range(len(UrlState)))
    assert body["urls"]["by_state"] == {
        state.value: 10 + index for index, state in enumerate(UrlState)
    }
    assert body["pages_stored"] == 7
    assert body["documents"] == {
        "total": 10,
        "by_status": {"pending": 1, "queued": 2, "indexed": 3, "failed": 4},
        "active": 2,
    }
    assert body["counted_at"] == "2026-11-07T10:15:05Z"
