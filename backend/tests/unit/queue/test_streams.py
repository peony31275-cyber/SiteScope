"""Pure parts of the stream adapter (app/queue/streams.py): entry IDs, the trim point, decoding.

The adapter's Redis commands are tested against real Redis in
tests/integration/test_streams.py.
"""

import uuid

import pytest

from app.queue.messages import CrawlTask
from app.queue.streams import Delivery, parse_entry_id, safe_trim_point


def test_parse_entry_id() -> None:
    assert parse_entry_id("1700000000000-5") == (1_700_000_000_000, 5)
    assert parse_entry_id("12") == (12, 0)


def test_entry_ids_compare_numerically_not_as_text() -> None:
    assert parse_entry_id("9-0") < parse_entry_id("10-0")
    assert parse_entry_id("10-9") < parse_entry_id("10-10")


def test_trim_point_is_the_time_threshold_when_everything_older_is_acknowledged() -> None:
    point = safe_trim_point(older_than_ms=1000, oldest_pending=None, last_delivered=["5000-0"])

    assert point == "1000-0"


def test_trim_point_never_passes_the_oldest_pending_entry() -> None:
    """A pending entry is owed to a consumer (or to XAUTOCLAIM): it is never trimmed."""
    point = safe_trim_point(older_than_ms=9000, oldest_pending="700-3", last_delivered=["8000-0"])

    assert point == "700-3"  # XTRIM MINID keeps entries >= the point


def test_trim_point_never_passes_an_undelivered_entry() -> None:
    """Entries after a group's last-delivered ID have not been read yet: never trimmed. The
    last-delivered entry itself was delivered (acknowledged, or pending: capped above)."""
    point = safe_trim_point(
        older_than_ms=9000, oldest_pending=None, last_delivered=["8000-0", "300-1"]
    )

    assert point == "300-2"


def test_trim_point_with_a_fresh_group_trims_nothing() -> None:
    """A group created at "0" has delivered nothing: every entry (IDs start above 0-0) is
    kept."""
    assert safe_trim_point(older_than_ms=9000, oldest_pending=None, last_delivered=["0-0"]) == (
        "0-1"
    )


def test_delivery_decodes_a_valid_task() -> None:
    task = CrawlTask(uuid.uuid4(), uuid.uuid4())

    delivery = Delivery.of("1-0", task.encode())

    assert delivery.task == task
    assert delivery.error is None


@pytest.mark.parametrize("fields", [None, {}, {"v": "1", "job_id": "x", "url_id": "y"}])
def test_delivery_keeps_the_reason_a_message_is_malformed(
    fields: dict[str, str] | None,
) -> None:
    delivery = Delivery.of("1-0", fields)

    assert delivery.task is None
    assert delivery.error
