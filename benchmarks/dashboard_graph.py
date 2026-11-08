r"""The dashboard's link graph at the API's limits, in the installed Edge (Phase 10).

One completed job is built in a disposable database with the Phase 8 generator (``analysis.py``:
``--pages`` stored pages, ``--links`` links each, plus 404s, redirects, failures and never-admitted
targets), the real API serves it, and the dashboard's production build (``vite build``, ``vite
preview`` with its ``/api`` proxy) shows its link graph. Playwright (``frontend/e2e/
graph-performance.spec.ts``, opt-in) loads the Link Explorer at 300/1,500, 1,000/5,000 and
2,000/10,000 nodes/links, ``--repeat`` times each in a fresh browser context, and measures the
response, the time until every node is drawn, long tasks, heap and DOM size, and frame intervals
during a scripted pan and zoom. Raw measurements are written to ``--output``.

Run from backend\ with PostgreSQL and Redis up and ``npm ci`` done in frontend\::

    .\.venv\Scripts\python.exe ..\benchmarks\dashboard_graph.py `
        --output ..\benchmarks\results\phase10-dashboard-graph.json
"""

import argparse
import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from analysis import store as store_job
from benchlib import (
    VariantCluster,
    bench_settings,
    delete_keys,
    distribution,
    environment,
    fresh_database,
    parse_variants,
    process_environment,
    write_json,
)

from app.db.session import Database
from tests.dashboard_stack import FRONTEND_DIR, frontend_process, node, wait_until_serving
from tests.integration import helpers


def summarize(loads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for load in loads:
        key = (load["requested"]["nodes"], load["requested"]["edges"])
        groups.setdefault(key, []).append(load)
    summary = []
    for (nodes, edges), members in sorted(groups.items()):
        long_tasks = [sum(load["longTasksMs"]) for load in members]
        summary.append(
            {
                "requested_nodes": nodes,
                "requested_edges": edges,
                "drawn_nodes": members[0]["drawn"]["nodes"],
                "drawn_edges": members[0]["drawn"]["edges"],
                "loads": len(members),
                "response_end_ms": distribution(
                    [load["response"]["responseEndMs"] for load in members]
                ),
                "drawn_ms": distribution([load["drawnMs"] for load in members]),
                "long_task_total_ms": distribution(long_tasks),
                "longest_task_ms": max(
                    (max(load["longTasksMs"], default=0.0) for load in members), default=0.0
                ),
                "heap_mb": distribution(
                    [load["heapBytes"] / 2**20 for load in members if load["heapBytes"]]
                ),
                "dom_elements": members[0]["domElements"],
                "pan_frame_ms": distribution(
                    [frame for load in members for frame in load["panFramesMs"]]
                ),
                "zoom_frame_ms": distribution(
                    [frame for load in members for frame in load["zoomFramesMs"]]
                ),
            }
        )
    return summary


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pages", type=int, default=5000, help="stored pages of the job")
    parser.add_argument("--links", type=int, default=10, help="links per page")
    parser.add_argument("--repeat", type=int, default=3, help="loads per graph size")
    parser.add_argument("--variant", action="append", help="name=<backend dir> of the API")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-build", action="store_true", help="serve the existing build")
    args = parser.parse_args()
    [variant] = parse_variants(args.variant)
    settings = bench_settings()
    await fresh_database(settings)
    database = Database(settings, application_name="sitescope-benchmark")
    log_dir = Path(tempfile.mkdtemp(prefix="sitescope-graph-"))
    prefix = f"bench-{uuid.uuid4().hex[:10]}"
    cluster = VariantCluster(
        process_environment(
            settings, prefix=prefix, hosts=["127.0.0.1"], ports=[80], extra={"LOG_LEVEL": "WARNING"}
        ),
        log_dir,
        backend_dir=variant.backend_dir,
    )
    raw_path = log_dir / "graph-performance.json"
    try:
        env_info = await environment(settings, database, [variant])
        job_id = await store_job(database, args.pages, args.links)
        api_port = helpers.unused_port()
        api = cluster.api(api_port)
        api_url = f"http://127.0.0.1:{api_port}"
        await wait_until_serving(f"{api_url}/health", within=60, process=api)
        frontend_env = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().startswith(("SITESCOPE_", "VITE_"))
        }
        frontend_env.update(
            {
                "SITESCOPE_API_URL": api_url,
                "VITE_API_BASE_URL": "/api/v1",
                "VITE_POLL_INTERVAL_MS": "3000",
            }
        )
        vite = ["node_modules/vite/bin/vite.js"]
        if not args.no_build:
            builder = frontend_process(cluster, "frontend-build", [*vite, "build"], frontend_env)
            if await asyncio.to_thread(builder.popen.wait) != 0:
                raise RuntimeError(f"the frontend build failed:\n{builder.tail()}")
        preview_port = helpers.unused_port()
        preview = frontend_process(
            cluster,
            "frontend",
            [*vite, "preview", "--host", "127.0.0.1", "--port", str(preview_port), "--strictPort"],
            frontend_env,
        )
        dashboard = f"http://127.0.0.1:{preview_port}"
        await wait_until_serving(f"{dashboard}/", within=60, process=preview)
        playwright_env = dict(os.environ)
        playwright_env.update(
            {
                "E2E_BASE_URL": dashboard,
                "E2E_GRAPH_PERF": "1",
                "E2E_GRAPH_JOB": str(job_id),
                "E2E_GRAPH_OUTPUT": str(raw_path),
                "E2E_GRAPH_REPEAT": str(args.repeat),
            }
        )
        process = await asyncio.create_subprocess_exec(
            node(),
            "node_modules/@playwright/test/cli.js",
            "test",
            "e2e/graph-performance.spec.ts",
            cwd=FRONTEND_DIR,
            env=playwright_env,
        )
        if await process.wait() != 0:
            raise RuntimeError("the Playwright run failed")
        loads = json.loads(raw_path.read_text(encoding="utf-8"))
    finally:
        for process_ in cluster.processes:
            if process_.name.startswith("frontend") and process_.alive:
                process_.popen.terminate()
        cluster.kill_all()
        await delete_keys(settings, prefix)  # the API's heartbeat and the process index
        await database.dispose()
        await helpers.drop_database(settings)
    write_json(
        args.output,
        {
            "benchmark": "dashboard_graph",
            "environment": env_info,
            "configuration": {
                "job_pages": args.pages,
                "links_per_page": args.links,
                "loads_per_size": args.repeat,
                "browser": "installed Microsoft Edge, headless (Playwright channel msedge)",
                "viewport": "1440 x 900",
                "frontend": "production build (vite build), served by vite preview",
            },
            "summary": summarize(loads),
            "loads": loads,
        },
    )


if __name__ == "__main__":
    asyncio.run(main())
