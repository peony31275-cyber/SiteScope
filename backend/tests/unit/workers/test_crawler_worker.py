"""The crawler worker's acknowledgment rules (app/workers/crawler.py), without services.

``judge_unclaimed`` decides what happens to a message whose URL could not be claimed. The rule
it enforces (ADR-0016): acknowledge only when no work is left that the message stands for.
Processes, Redis and PostgreSQL are exercised in tests/integration and tests/fault.
"""

import uuid

import pytest

from app.core.config import Settings
from app.db.models import JobStatus, UrlState
from app.queue.messages import CrawlTask
from app.services.crawl_state import UrlStatus
from app.workers.crawler import Disposition, Handled, Retry, WorkerConfig, judge_unclaimed

JOB = uuid.uuid4()
URL = uuid.uuid4()
TASK = CrawlTask(JOB, URL)


def status(
    state: UrlState,
    *,
    job_status: JobStatus = JobStatus.RUNNING,
    attempts: int = 1,
    live: bool = False,
    job_id: uuid.UUID = JOB,
) -> UrlStatus:
    return UrlStatus(
        url_id=URL,
        job_id=job_id,
        job_status=job_status,
        state=state,
        attempt_count=attempts,
        lease_owner="crawler-other" if state is UrlState.IN_PROGRESS else None,
        lease_live=live,
    )


def judge(value: UrlStatus | None) -> Handled | Retry:
    return judge_unclaimed(value, TASK, max_attempts=3)


def test_an_unknown_url_is_dead_lettered() -> None:
    assert judge(None) == Handled(Disposition.DEAD_LETTER, "unknown_url")


def test_a_url_of_another_job_is_dead_lettered() -> None:
    assert judge(status(UrlState.QUEUED, job_id=uuid.uuid4())) == Handled(
        Disposition.DEAD_LETTER, "job_mismatch"
    )


@pytest.mark.parametrize(
    "state", [UrlState.FETCHED, UrlState.SKIPPED, UrlState.FAILED, UrlState.CANCELLED]
)
def test_a_finished_url_is_acknowledged(state: UrlState) -> None:
    """Scenario C: the result was committed but the ack was lost; the redelivery acks."""
    assert judge(status(state)) == Handled(Disposition.ACK, "already_finished")


@pytest.mark.parametrize(
    "job_status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLING, JobStatus.QUEUED]
)
def test_a_url_of_a_job_that_is_not_running_is_acknowledged(job_status: JobStatus) -> None:
    assert judge(status(UrlState.QUEUED, job_status=job_status)) == Handled(
        Disposition.ACK, "job_not_running"
    )


def test_a_pending_url_means_its_dispatch_rolled_back() -> None:
    """The claim waited for any dispatch in flight; still pending: the publish was orphaned."""
    assert judge(status(UrlState.PENDING, attempts=0)) == Handled(Disposition.ACK, "not_dispatched")


def test_a_queued_url_is_claimed_again() -> None:
    assert judge(status(UrlState.QUEUED, attempts=0)) is Retry.CLAIM


def test_a_live_lease_of_another_worker_keeps_the_message_pending() -> None:
    """Never ack: if the holder dies, this pending message is what recovers the URL."""
    assert judge(status(UrlState.IN_PROGRESS, live=True)) == Handled(Disposition.KEEP, "leased")


def test_an_expired_lease_with_attempts_left_is_taken_over() -> None:
    assert judge(status(UrlState.IN_PROGRESS, attempts=2, live=False)) is Retry.CLAIM


def test_an_expired_lease_after_the_last_attempt_is_recovered() -> None:
    assert judge(status(UrlState.IN_PROGRESS, attempts=3, live=False)) is Retry.RECOVER


def test_no_rule_acknowledges_an_unfinished_url_of_a_running_job_that_was_dispatched() -> None:
    """The safety property behind the table: for queued or in-progress URLs of a running job,
    the message is never acknowledged without a recorded outcome."""
    for state in (UrlState.QUEUED, UrlState.IN_PROGRESS):
        for attempts in range(5):
            for live in (False, True):
                verdict = judge(status(state, attempts=attempts, live=live))
                assert not (
                    isinstance(verdict, Handled) and verdict.disposition is Disposition.ACK
                ), (state, attempts, live)


def test_worker_config_from_settings() -> None:
    settings = Settings(_env_file=None)

    config = WorkerConfig.from_settings(settings, concurrency=7)

    assert config.concurrency == 7
    assert config.lease_seconds == 90
    assert config.task_deadline == 80  # the lease minus the margin
    assert config.reclaim_min_idle == 120
    assert config.name.startswith("crawler-")
    assert WorkerConfig.from_settings(settings).concurrency == settings.crawl_concurrency


def test_every_worker_config_gets_its_own_name() -> None:
    assert WorkerConfig().name != WorkerConfig().name


@pytest.mark.parametrize(
    "values",
    [
        {"concurrency": 0},
        {"task_deadline": 90.0, "lease_seconds": 90.0},
        {"task_deadline": 0.0},
        {"reclaim_min_idle": 89.0, "lease_seconds": 90.0},
    ],
)
def test_worker_config_rejects_unsafe_timing(values: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must"):
        WorkerConfig(**values)  # type: ignore[arg-type]  # parametrized keyword values
