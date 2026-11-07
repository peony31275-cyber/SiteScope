"""Print a Markdown table of a benchmark result file: every run, and the median per setup.

python benchmarks/summarize.py benchmarks/results/phase5-distributed.json
"""

# ruff: noqa: T201 - printing the table is this script's job

import json
import statistics
import sys
from pathlib import Path
from typing import Any


def main(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    runs: list[dict[str, Any]] = data["runs"]
    print("| Hosts | Workers (x4 tasks) | Pages | Runs: seconds | Runs: pages/s | Median pages/s |")
    print("|---|---|---|---|---|---|")
    setups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for run in runs:
        setups.setdefault((run["hosts"], run["workers"]), []).append(run)
    for (hosts, workers), group in sorted(setups.items()):
        seconds = ", ".join(f"{run['elapsed_seconds']:.1f}" for run in group)
        rates = [float(run["pages_per_second"]) for run in group]
        rate_text = ", ".join(f"{rate:.1f}" for rate in rates)
        pages = group[0]["page_requests"]
        print(
            f"| {hosts} | {workers} | {pages} | {seconds} | {rate_text} "
            f"| {statistics.median(rates):.1f} |"
        )
    print()
    print("Pages recorded per worker (each run):")
    print()
    for run in runs:
        shares = sorted(run["recorded_per_worker"].values(), reverse=True)
        print(
            f"- {run['hosts']} host(s), {run['workers']} worker(s), run {run['repeat']}: {shares}"
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
