"""Structured logging: one JSON object per line on stdout, for every SiteScope process.

Request-scoped context lives in context variables, so a log call made anywhere while a request
is being handled carries that request's ``request_id`` automatically.

Conventions for callers:

* use a module-level ``logger = logging.getLogger(__name__)``;
* pass a stable, dotted event name and structured fields through ``extra``, for example
  ``logger.info("Job created", extra={"event": "job.created", "job_id": job_id})``;
* never log secrets or page bodies.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# The ID of the HTTP request being handled in the current context, if any.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_HANDLER_NAME = "sitescope-json"

# Attributes present on every LogRecord. Anything else on a record was passed via ``extra``.
_STANDARD_RECORD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))
) | {"message", "asctime"}

# Uvicorn attaches an ANSI-coloured copy of some messages, which is noise in JSON output.
_DROPPED_ATTRIBUTES = frozenset({"color_message"})


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, Any] = {
            "timestamp": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id

        # Fields passed via ``extra``. They never overwrite the core fields above.
        for key, value in vars(record).items():
            if key in _STANDARD_RECORD_ATTRIBUTES or key in _DROPPED_ATTRIBUTES:
                continue
            payload.setdefault(key, value)

        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


class _StdoutHandler(logging.Handler):
    """Write each record to the *current* ``sys.stdout``.

    ``logging.StreamHandler`` keeps a reference to the stream it was created with, which breaks
    when the stream is replaced later (pytest's output capturing does this).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


def configure_logging(level: str, *, service: str) -> None:
    """Send all log records, including Uvicorn's, through one JSON handler on stdout.

    Safe to call more than once: it replaces only the handler it installed itself, so handlers
    added by other tools (for example pytest's log capture) are left alone.
    """
    handler = _StdoutHandler()
    handler.name = _HANDLER_NAME
    handler.setFormatter(JsonFormatter(service=service))

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.name == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn installs plain-text handlers before it imports the application. Route its
    # server messages through the root JSON handler instead.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # RequestContextMiddleware writes one structured line per request, so Uvicorn's own access
    # log would only duplicate it. Keep warnings and errors from that logger, drop the rest.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = True
    access_logger.setLevel(logging.WARNING)
