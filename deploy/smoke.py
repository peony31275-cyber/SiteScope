"""Smoke checks of a deployed SiteScope stack through its public entry (Phase 11).

    python3 deploy/smoke.py http://127.0.0.1:8080 [--read-only] [--demo]

Standard library only, so any Python 3.10+ runs it (a CI runner, the server itself). It sends
reads, plus write attempts that a read-only deployment must refuse (--read-only). It changes
nothing on any deployment. Checks:

* the dashboard is served with its security headers;
* the API answers on the same origin, and is ready (PostgreSQL and Redis reachable);
* the internal endpoints (/metrics, /docs, /openapi.json) are not served;
* with --read-only: creating and cancelling jobs is refused (403 writes_disabled);
* with --demo: the seeded demo jobs completed and indexed, search finds pages, and the broken
  link is reported (app/scheduler/admin.py, seed-demo).

Exit status 0 when every check passes, 1 otherwise; one line per check.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

failures = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    global failures
    failures += 0 if ok else 1
    sys.stdout.write(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}\n")


def request(
    base: str, method: str, path: str, body: Any = None
) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)  # noqa: S310 - http(s), checked
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310 - http(s) only
            return (
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


def get_json(base: str, path: str) -> Any:
    status, _, body = request(base, "GET", path)
    if status != 200:
        raise RuntimeError(f"GET {path}: HTTP {status}")
    return json.loads(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke checks of a deployed SiteScope stack.")
    parser.add_argument("base_url", help="the public entry, e.g. http://127.0.0.1:8080")
    parser.add_argument("--read-only", action="store_true", help="writes must be refused")
    parser.add_argument("--demo", action="store_true", help="the demo jobs must be seeded")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        parser.error("the base URL must be http(s)")

    status, headers, body = request(base, "GET", "/")
    check("dashboard served", status == 200 and b'<div id="root">' in body, status)
    csp = headers.get("content-security-policy", "")
    check("strict CSP", "default-src 'self'" in csp and "unsafe" not in csp, csp[:60])
    check("nosniff", headers.get("x-content-type-options") == "nosniff")
    check("no framing", "frame-ancestors 'none'" in csp)

    ready = get_json(base, "/api/v1/health/ready")
    check("API ready (PostgreSQL, Redis)", ready.get("status") == "ready", ready.get("checks"))
    policy = get_json(base, "/api/v1/crawl-policy")
    check("crawl policy", bool(policy.get("approved_hosts")), policy.get("approved_hosts"))

    for path in ("/metrics", "/docs", "/openapi.json", "/api/../metrics"):
        status, _, body = request(base, "GET", path)
        check(f"{path} not served", status == 404 and b"# HELP" not in body, status)

    if args.read_only:
        check(
            "write access disabled",
            policy.get("write_access") == "disabled",
            policy.get("write_access"),
        )
        job = {"seed_url": "http://demo-site:8080/", "max_depth": 1, "max_pages": 5}
        for method, path, payload in (
            ("POST", "/api/v1/crawls", job),
            ("POST", f"/api/v1/crawls/{uuid.uuid4()}/cancel", None),
        ):
            status, _, body = request(base, method, path, payload)
            code = json.loads(body).get("code") if body.startswith(b"{") else None
            check(
                f"{method} {path.split('/')[-1] or path} refused", status == 403, f"{status} {code}"
            )

    if args.demo:
        jobs = get_json(base, "/api/v1/crawls?limit=100")["items"]
        settled = [job for job in jobs if job["status"] == "completed"]
        check("demo jobs completed", len(settled) >= 4, f"{len(settled)} of {len(jobs)}")
        full = [job for job in settled if job["max_pages"] == 100]
        if full:
            job_id = full[0]["id"]
            detail = get_json(base, f"/api/v1/crawls/{job_id}")
            check(
                "full crawl indexed",
                detail["indexing"]["status"] == "indexed",
                detail["indexing"]["status"],
            )
            found = get_json(base, f"/api/v1/search?job_id={job_id}&q=crawler")
            check("search finds pages", found.get("total", 0) > 0, found.get("total"))
            broken = get_json(base, f"/api/v1/crawls/{job_id}/broken-links")
            check("broken link reported", broken.get("total") == 1, broken.get("total"))
        else:
            check("full demo crawl present", False, "no completed job with a page limit of 100")

    sys.stdout.write(f"{'all checks passed' if failures == 0 else f'{failures} check(s) failed'}\n")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
