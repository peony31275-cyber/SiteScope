"""The indexer's acknowledgment rules and configuration (app/workers/indexer.py), without services.

``judge_unstarted`` decides what happens to a message whose attempt could not start. The rule it
enforces (ADR-0018): acknowledge only when no work is left that the message stands for, and give
up on a document only through attempts counted in PostgreSQL, never through delivery counts.
Processes, Redis and PostgreSQL are exercised in tests/integration and tests/fault.
"""

import itertools
import uuid

import pytest

from app.core.config import Settings
from app.db.models import IndexStatus
from app.queue.messages import IndexTask
from app.services.search_index import DocumentStatus
from app.workers.consumer import Disposition, Handled
from app.workers.indexer import IndexerConfig, Retry, judge_unstarted, pool_capacity

JOB = uuid.uuid4()
PAGE = uuid.uuid4()
TASK = IndexTask(JOB, PAGE, 2)


def status(
    state: IndexStatus,
    *,
    attempts: int = 1,
    live: bool = False,
    job_id: uuid.UUID = JOB,
    content_version: int = 2,
) -> DocumentStatus:
    return DocumentStatus(
        page_id=PAGE,
        job_id=job_id,
        status=state,
        attempt_count=attempts,
        lease_owner="indexer-other" if live else None,
        lease_live=live,
        content_version=content_version,
        indexed_content_version=None,
    )


def judge(value: DocumentStatus | None, task: IndexTask = TASK) -> Handled | Retry:
    return judge_unstarted(value, task, max_attempts=3)


def test_an_unknown_document_is_dead_lettered() -> None:
    assert judge(None) == Handled(Disposition.DEAD_LETTER, "unknown_document")


def test_a_document_of_another_job_is_dead_lettered() -> None:
    assert judge(status(IndexStatus.QUEUED, job_id=uuid.uuid4())) == Handled(
        Disposition.DEAD_LETTER, "job_mismatch"
    )


def test_a_version_the_page_never_had_is_dead_lettered() -> None:
    assert judge(status(IndexStatus.QUEUED, content_version=1)) == Handled(
        Disposition.DEAD_LETTER, "version_ahead"
    )


@pytest.mark.parametrize(
    ("state", "reason"),
    [(IndexStatus.INDEXED, "already_indexed"), (IndexStatus.FAILED, "already_failed")],
)
def test_a_finished_document_is_acknowledged(state: IndexStatus, reason: str) -> None:
    """A duplicate, or a redelivery after the commit whose XACK was lost."""
    assert judge(status(state)) == Handled(Disposition.ACK, reason)


def test_a_pending_document_means_its_dispatch_rolled_back_or_it_was_reset() -> None:
    assert judge(status(IndexStatus.PENDING)) == Handled(Disposition.ACK, "not_dispatched")


def test_an_older_version_of_a_queued_document_is_started_like_the_current_one() -> None:
    """An attempt indexes the page's current content whatever version the message names, so an
    older message is never dropped as outdated: the current version may have no message of its
    own (a dispatch that raced a content change published the older one; independent review)."""
    old = IndexTask(JOB, PAGE, 1)

    assert judge(status(IndexStatus.QUEUED), old) is Retry.START
    assert judge(status(IndexStatus.QUEUED, live=True), old) == Handled(Disposition.KEEP, "leased")
    assert judge(status(IndexStatus.QUEUED, attempts=3), old) is Retry.FAIL
    assert judge(status(IndexStatus.INDEXED), old) == Handled(Disposition.ACK, "already_indexed")


def test_a_live_attempt_of_another_indexer_keeps_the_message_pending() -> None:
    assert judge(status(IndexStatus.QUEUED, live=True)) == Handled(Disposition.KEEP, "leased")
    assert judge(status(IndexStatus.QUEUED, live=True, attempts=3)) == Handled(
        Disposition.KEEP, "leased"
    )


def test_a_queued_document_with_attempts_left_is_started_again() -> None:
    assert judge(status(IndexStatus.QUEUED, attempts=0)) is Retry.START
    assert judge(status(IndexStatus.QUEUED, attempts=2)) is Retry.START


def test_a_queued_document_without_attempts_or_a_live_attempt_is_failed() -> None:
    assert judge(status(IndexStatus.QUEUED, attempts=3)) is Retry.FAIL
    assert judge(status(IndexStatus.QUEUED, attempts=7)) is Retry.FAIL


def test_no_rule_acknowledges_a_queued_document_that_may_still_need_its_message() -> None:
    """Exhaustive over the queued states and the versions a valid message can name."""
    for attempts, live, version in itertools.product(range(5), (False, True), (1, 2)):
        verdict = judge(
            status(IndexStatus.QUEUED, attempts=attempts, live=live), IndexTask(JOB, PAGE, version)
        )
        assert not (isinstance(verdict, Handled) and verdict.disposition is Disposition.ACK)


def test_indexer_config_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        index_concurrency=6,
        index_max_attempts=4,
        index_max_body_chars=50_000,
        index_task_deadline_seconds=10,
        index_reclaim_min_idle_seconds=30,
        index_reconcile_after_seconds=60,
    )

    config = IndexerConfig.from_settings(settings)
    narrowed = IndexerConfig.from_settings(settings, concurrency=1)

    assert (config.concurrency, config.max_attempts, config.max_body_chars) == (6, 4, 50_000)
    assert (config.task_deadline, config.reclaim_min_idle) == (10, 30)
    assert config.lease_seconds == 20  # between the deadline and the reclaim idle time
    assert config.read_block_ms == settings.stream_read_block_ms
    assert narrowed.concurrency == 1


def test_the_default_timing_keeps_the_lease_between_deadline_and_reclaim() -> None:
    settings = Settings(_env_file=None)
    config = IndexerConfig.from_settings(settings)

    assert config.task_deadline + 1 <= config.lease_seconds < config.reclaim_min_idle
    assert config.task_deadline < settings.db_command_timeout_seconds


def test_an_indexer_never_runs_more_tasks_than_its_connection_pool_holds() -> None:
    """A task that waited for a connection after its attempt was counted would use up an
    attempt without indexing (independent review): concurrency is capped by the pool."""
    settings = Settings(_env_file=None)  # pool 5 + overflow 5
    larger = Settings(_env_file=None, db_pool_size=15, db_max_overflow=5)

    assert pool_capacity(settings) == 10
    assert IndexerConfig.from_settings(settings, concurrency=10).concurrency == 10
    assert IndexerConfig.from_settings(larger, concurrency=20).concurrency == 20
    with pytest.raises(ValueError, match="one per database connection"):
        IndexerConfig.from_settings(settings, concurrency=11)


def test_every_indexer_config_gets_its_own_name() -> None:
    first, second = IndexerConfig(), IndexerConfig()

    assert first.name != second.name
    assert first.name.startswith("indexer-")


@pytest.mark.parametrize(
    "values",
    [
        {"concurrency": 0},
        {"task_deadline": 0},
        {"task_deadline": 60, "reclaim_min_idle": 60},  # the deadline must end first
        {"task_deadline": 61, "reclaim_min_idle": 60},
        {"max_attempts": 0},
        {"max_body_chars": 0},
    ],
)
def test_indexer_config_rejects_unsafe_values(values: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"must be"):
        IndexerConfig(**values)  # type: ignore[arg-type]  # test values
