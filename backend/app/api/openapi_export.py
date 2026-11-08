r"""Write the API's OpenAPI document as JSON: the source of the dashboard's types (Phase 9).

Run from backend\ (writes the snapshot the frontend generates its types from)::

    .\.venv\Scripts\python.exe -m app.api.openapi_export ..\frontend\src\lib\api\openapi.json

Then regenerate the types in frontend\ with ``npm run api:types``. The unit test
``tests/unit/api/test_openapi_snapshot.py`` fails while the committed snapshot differs from the
application's document. The document does not depend on settings; defaults are used, and no
service is contacted.
"""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.main import create_app


def openapi_document() -> dict[str, Any]:
    """The application's OpenAPI document (independent of backend/.env)."""
    document: dict[str, Any] = create_app(Settings(_env_file=None)).openapi()
    return document


def render(document: dict[str, Any]) -> str:
    """Stable JSON text: two-space indentation, non-ASCII kept, a final newline."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.api.openapi_export",
        description="Write the API's OpenAPI document as JSON.",
    )
    parser.add_argument("path", type=Path, help="the file to write (overwritten)")
    args = parser.parse_args(argv)
    # Bytes, so the file has LF line endings on Windows too (.gitattributes stores LF).
    args.path.write_bytes(render(openapi_document()).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
