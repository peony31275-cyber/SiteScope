r"""Fetch and parse one URL with the safe fetcher, for manual checks (no database, no Redis).

Run from backend\ (settings come from the environment and backend\.env, as for every process):

    .\.venv\Scripts\python.exe -m app.crawler.cli https://docs.example.com/
    .\.venv\Scripts\python.exe -m app.crawler.cli http://localhost:8080/ --max-redirects 0

The URL must be an approved crawl target (``SITESCOPE_CRAWL_ALLOWED_HOSTS``); a local server
also needs ``SITESCOPE_FETCH_INTERNAL_ORIGINS``. Prints one JSON object. The exit status is 0
when the fetch succeeds and 1 when it fails.
"""

import argparse
import asyncio
import dataclasses
import json
import sys
from collections.abc import Sequence
from typing import Any

from app.core.config import Settings, get_settings
from app.crawler.errors import FetchError
from app.crawler.fetcher import build_fetcher
from app.crawler.html_parser import ParseLimits, parse_html


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.crawler.cli",
        description="Fetch one approved URL with the safe fetcher and parse it.",
    )
    parser.add_argument("url", help="an absolute http(s) URL on an approved crawl target")
    parser.add_argument(
        "--max-redirects",
        type=int,
        default=None,
        help="redirects to follow (default: SITESCOPE_FETCH_MAX_REDIRECTS; 0 returns them)",
    )
    parser.add_argument("--links", type=int, default=10, help="links to list (default 10)")
    args = parser.parse_args(argv)
    report, ok = asyncio.run(
        run(
            settings or get_settings(), args.url, max_redirects=args.max_redirects, links=args.links
        )
    )
    sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0 if ok else 1


async def run(
    settings: Settings, url: str, *, max_redirects: int | None = None, links: int = 10
) -> tuple[dict[str, Any], bool]:
    """Fetch and parse ``url``; return a JSON-ready report and whether the fetch succeeded."""
    fetcher = build_fetcher(settings)
    try:
        result = await fetcher.fetch(url, max_redirects=max_redirects)
    except FetchError as error:
        return {
            "ok": False,
            "failure": error.kind.value,
            "retryable": error.retryable,
            "http_status": error.http_status,
            "message": error.message,
            "url": error.url,
            "redirects": [dataclasses.asdict(hop) for hop in error.redirects],
        }, False

    report: dict[str, Any] = {
        "ok": True,
        "requested_url": result.requested_url,
        "url": result.url,
        "status_code": result.status_code,
        "remote_address": result.remote_address,
        "content_type": result.content_type,
        "charset": result.charset,
        "body_bytes": len(result.body),
        "wire_bytes": result.wire_bytes,
        "elapsed_ms": round(result.elapsed_seconds * 1000, 1),
        "redirects": [dataclasses.asdict(hop) for hop in result.redirects],
        "redirect_location": result.redirect_location,
    }
    if result.redirect_location is None:
        # Parsing is CPU-bound: keep it off the event loop, as a worker should.
        page = await asyncio.to_thread(
            parse_html,
            result.body,
            url=result.url,
            declared_charset=result.charset,
            limits=ParseLimits.from_settings(settings),
        )
        report["page"] = {
            "title": page.title,
            "description": page.description,
            "language": page.language,
            "canonical_url": page.canonical_url,
            "robots": sorted(page.robots),
            "encoding": page.encoding,
            "encoding_source": page.encoding_source,
            "headings": page.headings.splitlines()[:10],
            "text_preview": page.text[:300],
            "links": [link.url for link in page.links[:links]],
            "stats": dataclasses.asdict(page.stats),
        }
    return report, True


if __name__ == "__main__":
    raise SystemExit(main())
