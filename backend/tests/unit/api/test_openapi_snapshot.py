r"""The dashboard's API types come from the current OpenAPI document (Phase 9).

The frontend generates its TypeScript types (``frontend/src/lib/api/schema.d.ts``) from a
committed snapshot of the OpenAPI document (``frontend/src/lib/api/openapi.json``). This test
fails while that snapshot differs from what the application serves, so an API change cannot
leave the dashboard's types behind unnoticed. To update both, from backend\::

    .\.venv\Scripts\python.exe -m app.api.openapi_export ..\frontend\src\lib\api\openapi.json

and then ``npm run api:types`` in frontend\.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.openapi_export import main, openapi_document, render

SNAPSHOT = Path(__file__).resolve().parents[4] / "frontend" / "src" / "lib" / "api" / "openapi.json"


def test_the_dashboard_snapshot_is_the_served_document(client: TestClient) -> None:
    served = client.get("/openapi.json").json()

    assert json.loads(SNAPSHOT.read_text(encoding="utf-8")) == served, (
        "frontend/src/lib/api/openapi.json is out of date: run "
        "`python -m app.api.openapi_export ../frontend/src/lib/api/openapi.json` in backend/ "
        "and `npm run api:types` in frontend/"
    )


def test_the_export_writes_the_document_with_lf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"

    assert main([str(target)]) == 0

    written = target.read_bytes()
    assert b"\r\n" not in written
    assert written.endswith(b"}\n")
    assert json.loads(written) == openapi_document()
    assert written.decode("utf-8") == render(openapi_document())
