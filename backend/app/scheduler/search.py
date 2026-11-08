r"""Search one crawl job's index from the command line (Phase 7; read-only).

Run from backend\ (settings come from the environment and backend\.env)::

    .\.venv\Scripts\python.exe -m app.scheduler.search <job-id> "query" [--limit N] [--offset N]

Prints one JSON document (``app/services/search.py``): the normalized query, whether it had a
term to look up, the number of matches, one page of results (URL, title, rank, a snippet with
the highlighted ranges, when the page was fetched and indexed) and the job's indexing progress.
Only pages whose index reflects their current content are returned. It reads PostgreSQL only,
in a read-only transaction. The exit status is 0 on success, 1 if the job does not exist, the
query or the paging is invalid, the search timed out, or PostgreSQL is unreachable.

The output is ASCII JSON: page text is untrusted, and escaping keeps control characters out of
the terminal. The HTTP search API is Phase 8.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import DomainError, InvalidInputError
from app.core.process import error_type, int_in_range
from app.db.session import Database
from app.indexing.queries import DEFAULT_LIMIT, MAX_LIMIT, MAX_OFFSET
from app.services.search import SearchResults, search_job


def render(results: SearchResults) -> dict[str, Any]:
    """The JSON document of a search."""
    indexing = results.indexing
    return {
        "job_id": str(results.job_id),
        "query": results.query,
        "searchable": results.searchable,
        "total": results.total,
        "limit": results.limit,
        "offset": results.offset,
        "results": [
            {
                "page_id": str(hit.page_id),
                "url": hit.url,
                "title": hit.title,
                "rank": round(hit.rank, 6),
                "snippet": hit.snippet.text,
                "highlights": [list(span) for span in hit.snippet.highlights],
                "fetched_at": hit.fetched_at.isoformat(),
                "indexed_at": hit.indexed_at.isoformat(),
                "content_version": hit.content_version,
            }
            for hit in results.hits
        ],
        "indexing": {
            "status": indexing.state.value,
            **{key: value for key, value in asdict(indexing).items() if key != "completed_at"},
            "completed_at": (indexing.completed_at.isoformat() if indexing.completed_at else None),
        },
    }


async def run_search(
    settings: Settings, job_id: uuid.UUID, query: str, *, limit: int, offset: int
) -> dict[str, Any]:
    """Search, and return the JSON document (or an ``error`` document)."""
    database = Database(settings, application_name="sitescope-search")
    try:
        async with database.session() as session:
            results = await search_job(
                session,
                job_id,
                query,
                limit=limit,
                offset=offset,
                timeout_seconds=settings.search_timeout_seconds,
            )
    except InvalidInputError as exc:
        return {"error": {"code": exc.code, "violations": exc.problem_extensions()["errors"]}}
    except DomainError as exc:
        return {"error": {"code": exc.code, "detail": exc.detail}}
    except Exception as exc:
        return {"error": {"code": "unavailable", "error_type": error_type(exc)}}
    finally:
        await database.dispose()
    return render(results)


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scheduler.search",
        description="Search one crawl job's indexed pages and print the results as JSON"
        " (read-only).",
    )
    parser.add_argument("job_id", type=uuid.UUID, help="the crawl job's ID")
    parser.add_argument("query", help='the query, e.g. "async queue" or \'"exact phrase" -word\'')
    parser.add_argument(
        "--limit",
        type=int_in_range(1, MAX_LIMIT),
        default=DEFAULT_LIMIT,
        help=f"results per page, 1 to {MAX_LIMIT} (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--offset",
        type=int_in_range(0, MAX_OFFSET),
        default=0,
        help=f"results to skip, 0 to {MAX_OFFSET} (default: 0)",
    )
    args = parser.parse_args(argv)
    document = asyncio.run(
        run_search(
            settings or get_settings(),
            args.job_id,
            args.query,
            limit=args.limit,
            offset=args.offset,
        )
    )
    sys.stdout.write(json.dumps(document, indent=2) + "\n")
    return 1 if "error" in document else 0


if __name__ == "__main__":
    raise SystemExit(main())
