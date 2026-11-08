"""The task message contracts (app/queue/messages.py): identifiers only, versioned."""

import uuid

import pytest

from app.queue.messages import MESSAGE_VERSION, CrawlTask, IndexTask, MalformedMessageError

JOB = uuid.UUID("0190f5f5-0000-7000-8000-000000000001")
URL = uuid.UUID("0190f5f5-0000-7000-8000-000000000002")


def test_encodes_only_the_version_and_identifiers() -> None:
    assert CrawlTask(JOB, URL).encode() == {
        "v": MESSAGE_VERSION,
        "job_id": str(JOB),
        "url_id": str(URL),
    }


def test_round_trip() -> None:
    task = CrawlTask(JOB, URL)

    assert CrawlTask.decode(task.encode()) == task


def test_ignores_unknown_extra_fields() -> None:
    """A newer producer may add fields; a consumer of the same version ignores them."""
    fields = {**CrawlTask(JOB, URL).encode(), "trace": "abc"}

    assert CrawlTask.decode(fields) == CrawlTask(JOB, URL)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"v": "2", "job_id": str(JOB), "url_id": str(URL)},  # unknown version
        {"job_id": str(JOB), "url_id": str(URL)},  # no version
        {"v": MESSAGE_VERSION, "url_id": str(URL)},  # no job
        {"v": MESSAGE_VERSION, "job_id": str(JOB)},  # no URL
        {"v": MESSAGE_VERSION, "job_id": "not-a-uuid", "url_id": str(URL)},
        {"v": MESSAGE_VERSION, "job_id": str(JOB), "url_id": "https://example.com/"},
        {"v": MESSAGE_VERSION, "job_id": "", "url_id": str(URL)},
    ],
)
def test_rejects_malformed_messages(fields: dict[str, str]) -> None:
    with pytest.raises(MalformedMessageError):
        CrawlTask.decode(fields)


# --- Index tasks (Phase 7) -----------------------------------------------------------------------

PAGE = uuid.UUID("0190f5f5-0000-7000-8000-000000000003")


def test_an_index_task_encodes_the_version_identifiers_and_content_version() -> None:
    assert IndexTask(JOB, PAGE, 3).encode() == {
        "v": MESSAGE_VERSION,
        "job_id": str(JOB),
        "page_id": str(PAGE),
        "content_version": "3",
    }
    assert IndexTask.CONTRACT_FIELDS == ("v", "job_id", "page_id", "content_version")


def test_an_index_task_round_trips_and_ignores_extra_fields() -> None:
    task = IndexTask(JOB, PAGE, 2_147_483_647)

    assert IndexTask.decode(task.encode()) == task
    assert IndexTask.decode({**task.encode(), "trace": "abc"}) == task


def test_an_index_task_names_its_identifiers_for_log_context() -> None:
    assert IndexTask(JOB, PAGE, 1).identifiers() == {
        "job_id": str(JOB),
        "page_id": str(PAGE),
        "content_version": "1",
    }


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"v": "2", "job_id": str(JOB), "page_id": str(PAGE), "content_version": "1"},
        {"job_id": str(JOB), "page_id": str(PAGE), "content_version": "1"},  # no version
        {"v": MESSAGE_VERSION, "page_id": str(PAGE), "content_version": "1"},  # no job
        {"v": MESSAGE_VERSION, "job_id": str(JOB), "content_version": "1"},  # no page
        {"v": MESSAGE_VERSION, "job_id": str(JOB), "page_id": str(PAGE)},  # no content version
        {"v": MESSAGE_VERSION, "job_id": "x", "page_id": str(PAGE), "content_version": "1"},
        {"v": MESSAGE_VERSION, "job_id": str(JOB), "page_id": "", "content_version": "1"},
        *(
            {"v": MESSAGE_VERSION, "job_id": str(JOB), "page_id": str(PAGE), "content_version": v}
            for v in (
                *("0", "-1", "", "1.5", "abc", " 1", "1e3", "\u0663"),
                *("2147483648", "99999999999"),  # beyond PostgreSQL's integer
            )
        ),
    ],
)
def test_malformed_index_tasks_are_rejected(fields: dict[str, str]) -> None:
    with pytest.raises(MalformedMessageError):
        IndexTask.decode(fields)
