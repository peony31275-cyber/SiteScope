"""The crawl task message contract (app/queue/messages.py): identifiers only, versioned."""

import uuid

import pytest

from app.queue.messages import MESSAGE_VERSION, CrawlTask, MalformedMessageError

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
