# SVG markup strings stay whole on one line each, where splitting them would hide the markup.
# ruff: noqa: E501
r"""SVG charts of the Phase 10 benchmark results, computed from the raw result files only.

Every chart shows medians of repeated runs with a whisker from the smallest to the largest run,
and a value label on each bar (the docs repeat the numbers in tables). Colors: a validated
three-hue categorical palette for light surfaces (blue, orange, aqua; aqua is below 3:1 against
the surface, hence the labels). A chart whose result file is missing is skipped.

Run from the repository root or backend\::

    .\.venv\Scripts\python.exe ..\benchmarks\charts.py
"""

import csv
import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parent / "results"
OUT = RESULTS / "charts"

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")
FONT = "system-ui, -apple-system, Segoe UI, sans-serif"


@dataclass
class Series:
    name: str
    values: list[list[float]]  # per category, the runs


def _load(name: str) -> dict[str, Any] | None:
    path = RESULTS / name
    if not path.is_file():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _fmt(value: float) -> str:
    """Rounded half up, as the docs' tables round (68.55 is 68.6, not the float's 68.5)."""
    places = Decimal("1") if value >= 100 else Decimal("0.1")
    return str(Decimal(str(value)).quantize(places, rounding=ROUND_HALF_UP))


def _nice_max(value: float) -> float:
    """The smallest of 1, 2, 2.5, 5 times a power of ten that reaches ``value``."""
    for magnitude in (0.1, 1, 10, 100, 1000, 10000, 100000):
        for step in (1, 2, 2.5, 5):
            if step * magnitude >= value:
                return step * magnitude
    return value


def _lines(label: str, width: float) -> list[str]:
    """``label`` on one line, or two (split at the space nearest its middle) if it is wider
    than ``width`` at the axis font (about 7 px per character)."""
    if len(label) * 7 <= width or " " not in label:
        return [label]
    middle = len(label) / 2
    split = min(
        (index for index, char in enumerate(label) if char == " "), key=lambda i: abs(i - middle)
    )
    return [label[:split], label[split + 1 :]]


def _bar_path(x: float, y: float, width: float, height: float, radius: float = 4) -> str:
    """A bar anchored at its base, with only its data end rounded."""
    radius = min(radius, width / 2, height)
    return (
        f"M{x:.2f},{y + height:.2f} V{y + radius:.2f} Q{x:.2f},{y:.2f} {x + radius:.2f},{y:.2f}"
        f" H{x + width - radius:.2f} Q{x + width:.2f},{y:.2f} {x + width:.2f},{y + radius:.2f}"
        f" V{y + height:.2f} Z"
    )


def column_chart(
    path: Path,
    *,
    title: str,
    subtitle: str,
    categories: Sequence[str],
    axis_label: str,
    unit: str,
    series: Sequence[Series],
) -> None:
    """Grouped columns: medians with min-max whiskers and value labels."""
    width, height = 720, 420
    left, right, top, bottom = 64, 24, 96, 72  # room for two-line category labels
    plot_w, plot_h = width - left - right, height - top - bottom
    medians = [
        [statistics.median(runs) if runs else 0.0 for runs in item.values] for item in series
    ]
    highest = max((max(runs) for item in series for runs in item.values if runs), default=1.0)
    y_max = _nice_max(highest * 1.12)
    group_w = plot_w / len(categories)
    bar_w = min(56.0, (group_w * 0.72) / len(series))
    gap = 2.0
    parts: list[str] = []
    for tick in range(6):
        value = y_max * tick / 5
        y = top + plot_h - plot_h * tick / 5
        parts.append(
            f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="{INK_2}">{_fmt(value)}</text>'
        )
    for index, category in enumerate(categories):
        center = left + group_w * (index + 0.5)
        total = len(series) * bar_w + (len(series) - 1) * gap
        start = center - total / 2
        for number, item in enumerate(series):
            runs = item.values[index]
            if not runs:
                continue
            median = medians[number][index]
            x = start + number * (bar_w + gap)
            bar_h = plot_h * median / y_max
            y = top + plot_h - bar_h
            color = SERIES[number % len(SERIES)]
            parts.append(
                f'<path d="{_bar_path(x, y, bar_w, bar_h)}" fill="{color}">'
                f"<title>{escape(item.name)}, {escape(category)}: median {_fmt(median)} {unit}"
                f" (runs: {', '.join(_fmt(run) for run in runs)})</title></path>"
            )
            low = top + plot_h - plot_h * min(runs) / y_max
            high = top + plot_h - plot_h * max(runs) / y_max
            mid = x + bar_w / 2
            parts.append(
                f'<path d="M{mid:.2f},{low:.2f} V{high:.2f} M{mid - 4:.2f},{high:.2f} H{mid + 4:.2f}'
                f' M{mid - 4:.2f},{low:.2f} H{mid + 4:.2f}" stroke="{INK}" stroke-width="1.2" fill="none"/>'
            )
            # Value labels where they fit; narrow bars label their first series only (the
            # others are in each bar's tooltip and in the docs' tables).
            if bar_w >= 26 or number == 0:
                parts.append(
                    f'<text x="{mid:.2f}" y="{high - 6:.2f}" text-anchor="middle" font-size="11"'
                    f' fill="{INK}">{_fmt(median)}</text>'
                )
        for line, text in enumerate(_lines(category, group_w - 6)):
            parts.append(
                f'<text x="{center:.1f}" y="{top + plot_h + 20 + 14 * line}" text-anchor="middle"'
                f' font-size="12" fill="{INK_2}">{escape(text)}</text>'
            )
    parts.append(
        f'<line x1="{left}" x2="{width - right}" y1="{top + plot_h}" y2="{top + plot_h}" stroke="{INK_2}" stroke-width="1"/>'
    )
    parts.append(
        f'<text x="{left + plot_w / 2:.1f}" y="{height - 14}" text-anchor="middle" font-size="12"'
        f' fill="{INK_2}">{escape(axis_label)}</text>'
    )
    parts.append(
        f'<text x="16" y="{top + plot_h / 2:.1f}" transform="rotate(-90 16 {top + plot_h / 2:.1f})"'
        f' text-anchor="middle" font-size="12" fill="{INK_2}">{escape(unit)}</text>'
    )
    legend_x = left
    for number, item in enumerate(series):
        color = SERIES[number % len(SERIES)]
        parts.append(f'<rect x="{legend_x}" y="64" width="12" height="12" rx="2" fill="{color}"/>')
        parts.append(
            f'<text x="{legend_x + 18}" y="74" font-size="12" fill="{INK}">{escape(item.name)}</text>'
        )
        legend_x += 26 + 7.2 * len(item.name)
    description = "; ".join(
        f"{item.name}: "
        + ", ".join(
            f"{category} {_fmt(medians[number][index])}"
            for index, category in enumerate(categories)
            if item.values[index]
        )
        for number, item in enumerate(series)
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}"'
        f' height="{height}" role="img" aria-labelledby="t d" font-family="{FONT}">'
        f'<title id="t">{escape(title)}</title><desc id="d">{escape(subtitle)}. {escape(description)}</desc>'
        f'<rect width="{width}" height="{height}" fill="{SURFACE}"/>'
        f'<text x="{left}" y="30" font-size="16" font-weight="600" fill="{INK}">{escape(title)}</text>'
        f'<text x="{left}" y="50" font-size="12" fill="{INK_2}">{escape(subtitle)}</text>'
        + "".join(parts)
        + "</svg>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8", newline="\n")


def crawl_series(
    data: dict[str, Any], workers: Sequence[int], *, variant: str | None = None
) -> list[list[float]]:
    runs = data["runs"]
    return [
        [
            run["html_pages_per_second"]
            for run in runs
            if run["workers"] == count and (variant is None or run["variant"] == variant)
        ]
        for count in workers
    ]


def crawl_chart(experiment: str, title: str) -> None:
    workers = (1, 3, 5)
    series: list[Series] = []
    baseline = _load(f"phase10-crawl-{experiment}-baseline.json")
    if baseline is not None:
        series.append(Series("Before (100 tasks per job)", crawl_series(baseline, workers)))
    final = _load(f"phase10-crawl-{experiment}-ab.json")
    if final is not None:
        series.append(
            Series("Before, same session", crawl_series(final, workers, variant="baseline"))
        )
        series.append(
            Series("After (2 tasks per host slot)", crawl_series(final, workers, variant="current"))
        )
    if not series:
        return
    column_chart(
        OUT / f"crawl-{experiment}.svg",
        title=title,
        subtitle="HTML pages stored per second; median of 3 runs, whiskers from the slowest to the fastest",
        categories=[f"{count} worker{'s' if count > 1 else ''}" for count in workers],
        axis_label="crawler worker processes (4 tasks each)",
        unit="pages/s",
        series=series,
    )


def crawl_sizes_chart() -> None:
    before = _load("phase10-crawl-sizes-baseline.json")
    after = _load("phase10-crawl-sizes.json")
    if before is None or after is None:
        return
    sizes = sorted({run["pages_total"] for run in after["runs"]})

    def rates(data: dict[str, Any]) -> list[list[float]]:
        return [
            [run["html_pages_per_second"] for run in data["runs"] if run["pages_total"] == size]
            for size in sizes
        ]

    column_chart(
        OUT / "crawl-sizes.svg",
        title="Crawl throughput by job size, five hosts, 5 workers",
        subtitle="HTML pages stored per second; median of 3 runs, whiskers min to max; two sessions",
        categories=[f"{size:,} pages" for size in sizes],
        axis_label="pages over five hosts (one job per host)",
        unit="pages/s",
        series=[Series("Before Phase 10", rates(before)), Series("After", rates(after))],
    )


def graph_zoom_chart(before_file: str, after_file: str, name: str, job: str) -> None:
    """The share of zoom frames that took longer than 25 ms (two frame intervals or more), per
    load, before and after the labels layer."""
    before = _load(before_file)
    after = _load(after_file)
    if before is None or after is None:
        return
    sizes = sorted(
        {(load["requested"]["nodes"], load["requested"]["edges"]) for load in before["loads"]}
    )

    def shares(data: dict[str, Any]) -> list[list[float]]:
        return [
            [
                100
                * sum(1 for frame in load["zoomFramesMs"] if frame > 25)
                / max(1, len(load["zoomFramesMs"]))
                for load in data["loads"]
                if (load["requested"]["nodes"], load["requested"]["edges"]) == size
            ]
            for size in sizes
        ]

    column_chart(
        OUT / name,
        title=f"Link graph, zooming in: slow frames, {job}",
        subtitle="frames over 25 ms in 25 wheel steps; one session, alternating; median of 3 loads, min to max",
        categories=[f"{nodes:,} nodes / {edges:,} links" for nodes, edges in sizes],
        axis_label="requested graph limits (headless Edge, production build)",
        unit="% of frames",
        series=[
            Series("Before (every label)", shares(before)),
            Series("After (labels in view)", shares(after)),
        ],
    )


def indexing_chart() -> None:
    data = _load("phase10-indexing.json")
    if data is None:
        return
    counts = sorted({run["indexers"] for run in data["runs"]})
    modes = sorted({run["gin_fastupdate"] for run in data["runs"]})
    series = [
        Series(
            f"GIN fastupdate {mode}",
            [
                [
                    run["pages_per_second"]
                    for run in data["runs"]
                    if run["indexers"] == count and run["gin_fastupdate"] == mode
                ]
                for count in counts
            ],
        )
        for mode in modes
    ]
    column_chart(
        OUT / "indexing.svg",
        title="Indexing throughput by indexer processes",
        subtitle="2,000 pages of 1,000 words; median of 3 interleaved runs, whiskers min to max",
        categories=[f"{count} indexer{'s' if count > 1 else ''}" for count in counts],
        axis_label="indexer processes (4 tasks each)",
        unit="pages/s",
        series=series,
    )


def search_chart() -> None:
    data = _load("phase10-search.json")
    if data is None:
        return
    largest = max(item["documents"] for item in data["service"])
    rows = [item for item in data["service"] if item["documents"] == largest and item["searchable"]]
    categories = [item["query_kind"].replace("_", " ") for item in rows]
    column_chart(
        OUT / "search-latency.svg",
        title=f"Search latency in the service, {largest:,} documents",
        subtitle="milliseconds per search (10 results with snippets); 200 calls per query",
        categories=categories,
        axis_label="query",
        unit="ms",
        series=[
            Series("p50", [[item["ms"]["p50"]] for item in rows]),
            Series("p95", [[item["ms"]["p95"]] for item in rows]),
            Series("p99", [[item["ms"]["p99"]] if item["ms"].get("p99") else [] for item in rows]),
        ],
    )


def recovery_chart() -> None:
    data = _load("phase10-recovery.json")
    if data is None:
        return
    keys = {
        "worker_crash": "recovery_seconds",
        "indexer_crash": "recovery_seconds",
        "redis_restart": "resume_after_return_seconds",
        "redis_data_loss": "resume_after_return_seconds",
        "postgres_restart": "resume_after_return_seconds",
        "cancellation": "cancel_to_cancelled_seconds",
    }
    names = [name for name in keys if any(run["scenario"] == name for run in data["runs"])]
    column_chart(
        OUT / "recovery.svg",
        title="Time to recover",
        subtitle="seconds: a crash until its tasks are taken over; an outage's end until the next result; a cancellation until settled",
        categories=[name.replace("_", " ") for name in names],
        axis_label="scenario (short process-test timings: 7 s leases, 5 s index reclaim)",
        unit="seconds",
        series=[
            Series(
                "median of 3 runs",
                [
                    [
                        run[keys[name]]
                        for run in data["runs"]
                        if run["scenario"] == name and run.get(keys[name]) is not None
                    ]
                    for name in names
                ],
            )
        ],
    )


def graph_chart() -> None:
    # The same-session runs of the code before the labels layer (it does not change drawing).
    jobs = [
        ("a job of 5,000 pages", _load("phase10-dashboard-graph-ab-before.json")),
        (
            "a job of 2,000 pages (denser graph)",
            _load("phase10-dashboard-graph-dense-ab-before.json"),
        ),
    ]
    present = [(name, data) for name, data in jobs if data is not None]
    if not present:
        return
    sizes = sorted(
        {
            (load["requested"]["nodes"], load["requested"]["edges"])
            for _, data in present
            for load in data["loads"]
        }
    )
    column_chart(
        OUT / "dashboard-graph.svg",
        title="Link graph: navigation until every node is drawn",
        subtitle="milliseconds, headless Edge, production build; median of the loads, whiskers min to max",
        categories=[f"{nodes:,} nodes / {edges:,} links" for nodes, edges in sizes],
        axis_label="requested graph limits",
        unit="ms",
        series=[
            Series(
                name,
                [
                    [
                        load["drawnMs"]
                        for load in data["loads"]
                        if (load["requested"]["nodes"], load["requested"]["edges"]) == size
                    ]
                    for size in sizes
                ],
            )
            for name, data in present
        ],
    )


def _write_csv(name: str, header: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    path = RESULTS / name
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _spread(values: Sequence[float]) -> tuple[float, float, float]:
    return (round(statistics.median(values), 2), round(min(values), 2), round(max(values), 2))


def csv_summaries() -> None:
    """One CSV per benchmark, computed from its raw result file(s)."""
    rows: list[list[object]] = []
    for name in sorted(RESULTS.glob("phase10-crawl-*.json")):
        data = json.loads(name.read_text(encoding="utf-8"))
        groups: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
        for run in data.get("runs", []):
            key = (run["variant"], run["pages_total"], run["workers"])
            groups.setdefault(key, []).append(run)
        for (variant, pages, workers), runs in sorted(groups.items()):
            rate = _spread([run["html_pages_per_second"] for run in runs])
            seconds = _spread([run["elapsed_seconds"] for run in runs])
            rows.append(
                [
                    name.name,
                    variant,
                    pages,
                    workers,
                    len(runs),
                    *rate,
                    seconds[0],
                    statistics.median(run["url_latency_ms"]["p50"] for run in runs),
                    statistics.median(run["url_latency_ms"]["p95"] for run in runs),
                    max(run["levels"]["gap_ms"].get("max") or 0 for run in runs),
                    all(all(run["checks"].values()) for run in runs),
                ]
            )
    if rows:
        _write_csv(
            "phase10-crawl-summary.csv",
            [
                "file",
                "variant",
                "pages",
                "workers",
                "runs",
                "pages_per_s_median",
                "pages_per_s_min",
                "pages_per_s_max",
                "seconds_median",
                "url_latency_p50_ms",
                "url_latency_p95_ms",
                "largest_level_gap_ms",
                "all_checks_passed",
            ],
            rows,
        )
    indexing = _load("phase10-indexing.json")
    if indexing is not None:
        _write_csv(
            "phase10-indexing-summary.csv",
            [
                "variant",
                "gin_fastupdate",
                "indexers",
                "pages_per_s_median",
                "min",
                "max",
                "seconds_median",
                "latency_p50_s_median",
            ],
            [
                [
                    item["variant"],
                    item["gin_fastupdate"],
                    item["indexers"],
                    item["pages_per_second"]["median"],
                    item["pages_per_second"]["min"],
                    item["pages_per_second"]["max"],
                    item["seconds"]["median"],
                    item["latency_p50_seconds"]["median"],
                ]
                for item in indexing["summary"]
            ],
        )
    search = _load("phase10-search.json")
    if search is not None:
        _write_csv(
            "phase10-search-summary.csv",
            ["where", "documents", "query_kind", "matches", "p50_ms", "p95_ms", "p99_ms", "max_ms"],
            [
                [
                    "service",
                    item["documents"],
                    item["query_kind"],
                    item["matches"],
                    item["ms"].get("p50"),
                    item["ms"].get("p95"),
                    item["ms"].get("p99"),
                    item["ms"].get("max"),
                ]
                for item in search["service"]
            ]
            + [
                [
                    "api",
                    item["documents"],
                    item["query_kind"],
                    "",
                    item["ms"].get("p50"),
                    item["ms"].get("p95"),
                    item["ms"].get("p99"),
                    item["ms"].get("max"),
                ]
                for item in search["api_sequential"]
            ],
        )
    recovery = _load("phase10-recovery.json")
    if recovery is not None:
        _write_csv(
            "phase10-recovery-summary.csv",
            ["scenario", "measure", "median_s", "min_s", "max_s", "trials"],
            [
                [scenario, measure, values["median"], values["min"], values["max"], values["n"]]
                for scenario, entry in recovery["summary"].items()
                for measure, values in entry.items()
                if isinstance(values, dict) and "median" in values
            ],
        )
    plans: list[list[object]] = []
    for path in sorted(RESULTS.glob("phase10-admission-plans*.json")) + sorted(
        RESULTS.glob("phase10-analysis-reads*.json")
    ):
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data.get("results", []):
            plans.append(
                [
                    path.name,
                    "level admission",
                    item["statistics"],
                    f"nested loops {item['nested_loops']}",
                    item["ms_median"],
                    "statement timeout" if item["timed_out_after_ms"] else "",
                ]
            )
        for stats, reads in data.get("reads", {}).items():
            for read, measured in reads.items():
                plans.append(
                    [
                        path.name,
                        read,
                        stats,
                        "as the service plans it",
                        measured["ms_median"],
                        measured["error"] or "",
                    ]
                )
    if plans:
        _write_csv(
            "phase10-plans-summary.csv",
            ["file", "statement", "statistics", "planning", "ms_median", "stopped_by"],
            plans,
        )
    graph_rows: list[list[object]] = []
    for path in sorted(RESULTS.glob("phase10-dashboard-graph*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data["summary"]:
            slowest_zoom = [
                max(load["zoomFramesMs"], default=0.0)
                for load in data["loads"]
                if load["requested"]["nodes"] == item["requested_nodes"]
            ]
            graph_rows.append(
                [
                    path.name,
                    item["requested_nodes"],
                    item["requested_edges"],
                    item["drawn_nodes"],
                    item["drawn_edges"],
                    item["drawn_ms"].get("p50"),
                    item["drawn_ms"].get("max"),
                    item["long_task_total_ms"].get("p50"),
                    item["pan_frame_ms"].get("p50"),
                    item["pan_frame_ms"].get("p99"),
                    item["zoom_frame_ms"].get("p50"),
                    item["zoom_frame_ms"].get("p95"),
                    round(statistics.median(slowest_zoom), 1) if slowest_zoom else "",
                    item["heap_mb"].get("p50"),
                    item["dom_elements"],
                ]
            )
    if graph_rows:
        _write_csv(
            "phase10-dashboard-graph-summary.csv",
            [
                "file",
                "nodes",
                "edges",
                "drawn_nodes",
                "drawn_edges",
                "drawn_ms_p50",
                "drawn_ms_max",
                "long_task_total_ms_p50",
                "pan_frame_ms_p50",
                "pan_frame_ms_p99",
                "zoom_frame_ms_p50",
                "zoom_frame_ms_p95",
                "slowest_zoom_frame_ms_median",
                "heap_mb_p50",
                "dom_elements",
            ],
            graph_rows,
        )


def main() -> None:
    csv_summaries()
    crawl_chart("multi-host", "Crawl throughput, five hosts (one job each)")
    crawl_chart("single-host", "Crawl throughput, one host (one job)")
    crawl_sizes_chart()
    indexing_chart()
    search_chart()
    recovery_chart()
    graph_chart()
    # Before and after from one session, alternating (the earlier "before" files ran hours apart
    # from the "after" ones, on a machine in another state).
    graph_zoom_chart(
        "phase10-dashboard-graph-dense-ab-before.json",
        "phase10-dashboard-graph-dense-ab-after.json",
        "dashboard-graph-zoom-dense.svg",
        "a job of 2,000 pages, 10 links each",
    )
    graph_zoom_chart(
        "phase10-dashboard-graph-ab-before.json",
        "phase10-dashboard-graph-ab-after.json",
        "dashboard-graph-zoom.svg",
        "a job of 5,000 pages, 10 links each",
    )
    for chart in sorted(OUT.glob("*.svg")):
        print(chart.relative_to(RESULTS.parent))  # noqa: T201 - the script's output


if __name__ == "__main__":
    main()
