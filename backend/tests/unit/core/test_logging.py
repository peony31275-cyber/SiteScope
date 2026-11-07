import json
import logging
import sys
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from app.core.logging import JsonFormatter, configure_logging, request_id_var


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo global logging changes made by a test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def make_record(
    message: str = "hello", level: int = logging.INFO, **extra: Any
) -> logging.LogRecord:
    record = logging.LogRecord("sitescope.test", level, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def format_record(record: logging.LogRecord, service: str = "test-service") -> dict[str, Any]:
    output = JsonFormatter(service=service).format(record)
    assert "\n" not in output, "each record must be a single line"
    parsed: dict[str, Any] = json.loads(output)
    return parsed


def test_formats_core_fields_and_extras() -> None:
    payload = format_record(make_record(event="unit.test", job_id=42))

    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "sitescope.test"
    assert payload["service"] == "test-service"
    assert payload["event"] == "unit.test"
    assert payload["job_id"] == 42


def test_timestamp_is_utc_iso8601() -> None:
    timestamp = format_record(make_record())["timestamp"]

    assert timestamp.endswith("Z")
    assert datetime.fromisoformat(timestamp).utcoffset() is not None


def test_includes_request_id_from_context() -> None:
    token = request_id_var.set("req-123")
    try:
        payload = format_record(make_record())
    finally:
        request_id_var.reset(token)

    assert payload["request_id"] == "req-123"


def test_omits_request_id_outside_a_request() -> None:
    assert "request_id" not in format_record(make_record())


def test_includes_exception_traceback() -> None:
    record = make_record(level=logging.ERROR)
    try:
        raise ValueError("boom")
    except ValueError:
        record.exc_info = sys.exc_info()

    assert "ValueError: boom" in format_record(record)["exception"]


def test_extras_cannot_overwrite_core_fields() -> None:
    payload = format_record(make_record(service="spoofed", timestamp="spoofed"), service="real")

    assert payload["service"] == "real"
    assert payload["timestamp"] != "spoofed"


def test_unserializable_extras_are_stringified() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "opaque-value"

    assert format_record(make_record(thing=Opaque()))["thing"] == "opaque-value"


def test_drops_uvicorn_color_message() -> None:
    assert "color_message" not in format_record(make_record(color_message="\x1b[1mhi\x1b[0m"))


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO", service="first")
    configure_logging("DEBUG", service="second")

    root = logging.getLogger()
    ours = [handler for handler in root.handlers if handler.name == "sitescope-json"]
    assert len(ours) == 1
    assert root.level == logging.DEBUG


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_routes_uvicorn_through_root() -> None:
    configure_logging("INFO", service="api")

    for name in ("uvicorn", "uvicorn.error"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    # The request middleware is the access log, so Uvicorn's INFO access lines are dropped.
    assert not logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)


@pytest.mark.usefixtures("restore_logging")
def test_configured_logging_writes_json_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", service="api")

    logging.getLogger("sitescope.test").info("written", extra={"event": "unit.stdout"})

    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(line)["event"] == "unit.stdout"


def test_http_client_loggers_never_log_request_urls() -> None:
    """httpx2 logs full URLs at INFO and httpcore2 at DEBUG; query strings may hold tokens."""
    configure_logging("DEBUG", service="api")

    for name in ("httpx2", "httpcore2"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
