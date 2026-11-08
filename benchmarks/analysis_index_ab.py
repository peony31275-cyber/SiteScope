r"""A/B of the two indexes of migration 0005 on the analysis queries (Phase 8).

Builds the schema of revision 0004 (before migration 0005) in the disposable benchmark database,
loads the 10,000-page job of ``analysis.py`` once, and measures every analysis case (``--repeat``
calls each, p50 reported) in four variants, in this order:

1. ``0004``: the indexes of revision 0004;
2. ``A``: plus ``pages (job_id, normalized_url)``;
3. ``B``: instead, ``page_links (job_id, target_normalized_url) INCLUDE (is_internal)`` in place
   of ``ix_page_links_job_id_target``;
4. ``0004 again``: the indexes of revision 0004, to see the drift between runs.

``VACUUM (ANALYZE)`` runs after each change. Isolation as in ``analysis.py``: the disposable
database ``<SITESCOPE_POSTGRES_DB>_test_bench``, created and dropped here. Run from backend\:

    .\.venv\Scripts\python.exe ..\benchmarks\analysis_index_ab.py `
        --output ..\benchmarks\results\phase8-index-ab.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis as bench
from sqlalchemy import text

from app.core.config import Settings
from app.db.session import Database
from tests.integration import helpers

OLD_LINKS_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_page_links_job_id_target"
    " ON page_links (job_id, target_normalized_url)"
)
VARIANTS: dict[str, list[str]] = {
    "0004": [],
    "A": ["CREATE INDEX ix_ab_pages_job_url ON pages (job_id, normalized_url)"],
    "B": [
        "DROP INDEX ix_ab_pages_job_url",
        "CREATE INDEX ix_ab_links_covering ON page_links (job_id, target_normalized_url)"
        " INCLUDE (is_internal)",
        "DROP INDEX ix_page_links_job_id_target",
    ],
    "0004 again": [OLD_LINKS_INDEX, "DROP INDEX ix_ab_links_covering"],
}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pages", type=int, default=10_000)
    parser.add_argument("--links", type=int, default=50)
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    base = Settings()
    settings = base.model_copy(update={"postgres_db": f"{base.postgres_db}_test_bench"})
    helpers.require_disposable(settings.postgres_db)
    await helpers.recreate_database(settings)
    await helpers.upgrade(settings, "0004")
    database = Database(settings, application_name="sitescope-benchmark-index-ab")
    report: dict[str, Any] = {
        "command": " ".join(sys.argv),
        "environment": bench.environment(),
        "pages": args.pages,
        "links_per_page": args.links,
        "repeat": args.repeat,
        "variants": {},
    }
    try:
        job_id = await bench.store(database, args.pages, args.links)
        report["rows"] = await bench.counts(database, job_id)
        for name, statements in VARIANTS.items():
            async with database.engine.connect() as connection:
                autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
                for statement in statements:
                    await autocommit.execute(text(statement))
                await autocommit.execute(text("VACUUM (ANALYZE)"))
            results = await bench.measure(database, job_id, args.repeat)
            report["variants"][name] = {case: result["p50"] for case, result in results.items()}
            print(json.dumps({"variant": name, "p50": report["variants"][name]}), flush=True)  # noqa: T201
    finally:
        await database.dispose()
        await helpers.drop_database(settings)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    asyncio.run(main())
