"""Search document rules (app/indexing/documents.py) and the derived indexing state of a job."""

from datetime import UTC, datetime

import pytest

from app.indexing import documents
from app.services.search_index import IndexingProgress, IndexingState, IndexOutcome, IndexResult

COMPLETED = datetime(2026, 11, 7, tzinfo=UTC)


def test_fields_have_distinct_weights_from_title_down_to_body() -> None:
    weights = [
        documents.TITLE_WEIGHT,
        documents.DESCRIPTION_WEIGHT,
        documents.HEADINGS_WEIGHT,
        documents.BODY_WEIGHT,
    ]

    assert weights == ["A", "B", "C", "D"]  # ts_rank: 1.0, 0.4, 0.2, 0.1
    assert documents.TEXT_SEARCH_CONFIG == "english"


@pytest.mark.parametrize(("limit", "fallback"), [(100_000, 25_000), (1000, 250), (3, 1), (1, 1)])
def test_the_fallback_body_is_a_quarter_of_the_limit(limit: int, fallback: int) -> None:
    assert documents.fallback_body_chars(limit) == fallback


@pytest.mark.parametrize(
    ("progress", "state"),
    [
        (IndexingProgress(), IndexingState.PENDING),
        (IndexingProgress(pending=3), IndexingState.PENDING),
        (IndexingProgress(pending=2, queued=1), IndexingState.RUNNING),
        (IndexingProgress(pending=1, indexed=1), IndexingState.RUNNING),
        (IndexingProgress(indexed=4), IndexingState.RUNNING),  # the job may still add pages
        (IndexingProgress(failed=1), IndexingState.RUNNING),
        (IndexingProgress(indexed=4, completed_at=COMPLETED), IndexingState.INDEXED),
        (IndexingProgress(completed_at=COMPLETED), IndexingState.INDEXED),  # nothing to index
        (IndexingProgress(indexed=3, failed=1, completed_at=COMPLETED), IndexingState.FAILED),
    ],
)
def test_a_jobs_indexing_state_is_complete_only_once_marked(
    progress: IndexingProgress, state: IndexingState
) -> None:
    assert progress.state is state


def test_documents_counts_every_status() -> None:
    assert IndexingProgress(pending=1, queued=2, indexed=3, failed=4, active=2).documents == 10


@pytest.mark.parametrize(
    ("body_chars", "indexed_body_chars", "truncated"),
    [(10, 10, False), (150_000, 100_000, True), (None, None, False), (5, None, False)],
)
def test_a_result_is_truncated_when_less_of_the_body_was_indexed(
    body_chars: int | None, indexed_body_chars: int | None, truncated: bool
) -> None:
    result = IndexResult(
        IndexOutcome.INDEXED, body_chars=body_chars, indexed_body_chars=indexed_body_chars
    )

    assert result.truncated is truncated
