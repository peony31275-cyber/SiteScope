import re

import pytest
from fastapi.testclient import TestClient

from tests.support import LogReader

GENERATED_REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def test_generates_request_id_when_header_is_missing(client: TestClient) -> None:
    response = client.get("/health")

    assert GENERATED_REQUEST_ID.fullmatch(response.headers["X-Request-ID"])


def test_reuses_safe_client_request_id(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-42.a_B"})

    assert response.headers["X-Request-ID"] == "trace-42.a_B"


# Raw header bytes as a client could send them (the client library rejects non-ASCII str values).
@pytest.mark.parametrize("unsafe", [b"", b"has space", b"semi;colon", b"x" * 129, b"caf\xe9"])
def test_replaces_unsafe_client_request_id(client: TestClient, unsafe: bytes) -> None:
    response = client.get("/health", headers={b"X-Request-ID": unsafe})

    assert GENERATED_REQUEST_ID.fullmatch(response.headers["X-Request-ID"])


def test_writes_one_access_log_line_per_request(client: TestClient, read_logs: LogReader) -> None:
    read_logs()  # discard startup output

    response = client.get("/health", headers={"X-Request-ID": "trace-1"})

    entries = [entry for entry in read_logs() if entry.get("event") == "http.request"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["request_id"] == response.headers["X-Request-ID"] == "trace-1"
    assert entry["method"] == "GET"
    assert entry["path"] == "/health"
    assert entry["status_code"] == 200
    assert entry["duration_ms"] >= 0
    assert entry["service"] == "sitescope-api"


def test_access_log_omits_query_string(client: TestClient, read_logs: LogReader) -> None:
    read_logs()

    client.get("/health", params={"token": "secret-value"})

    # Only server-side entries matter; the test client logs its own request URL.
    server_entries = [entry for entry in read_logs() if entry["logger"].startswith("app.")]
    assert server_entries
    assert "secret-value" not in str(server_entries)
