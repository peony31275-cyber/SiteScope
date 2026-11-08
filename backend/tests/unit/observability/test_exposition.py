"""The Prometheus text exposition (Phase 10)."""

import math
from typing import Any

from app.observability.exposition import Exposition, format_number
from app.observability.metrics import Registry


def test_gauges_render_help_and_type_once_per_family() -> None:
    exposition = Exposition()
    exposition.gauge("sitescope_up", "Is it up.", 1, source="postgres")
    exposition.gauge("sitescope_up", "Is it up.", 0, source="redis")

    assert exposition.render() == (
        "# HELP sitescope_up Is it up.\n"
        "# TYPE sitescope_up gauge\n"
        'sitescope_up{source="postgres"} 1\n'
        'sitescope_up{source="redis"} 0\n'
    )


def test_label_values_and_help_text_are_escaped() -> None:
    exposition = Exposition()
    exposition.gauge("sitescope_x", 'Back\\slash\nnewline "quoted".', 1, value='a"b\\c\nd')

    rendered = exposition.render()

    assert '# HELP sitescope_x Back\\\\slash\\nnewline "quoted".\n' in rendered
    assert 'sitescope_x{value="a\\"b\\\\c\\nd"} 1\n' in rendered


def test_numbers_are_rendered_as_prometheus_reads_them() -> None:
    assert format_number(3.0) == "3"
    assert format_number(0.25) == "0.25"
    assert format_number(math.inf) == "+Inf"
    assert format_number(-math.inf) == "-Inf"
    assert format_number(math.nan) == "NaN"


def test_a_process_snapshot_gets_role_and_process_labels_and_cumulative_buckets() -> None:
    registry = Registry()
    registry.counter("sitescope_tasks_handled_total", "Handled.", ("reason",)).inc(
        3, reason="completed"
    )
    histogram = registry.histogram("sitescope_task_duration_seconds", "Durations.", buckets=(1.0,))
    histogram.observe(0.5)
    histogram.observe(2.0)
    exposition = Exposition()

    exposition.add_snapshot(registry.snapshot(), role="crawler", process="crawler-h-1-ab")

    lines = exposition.render().splitlines()
    labels = 'role="crawler",process="crawler-h-1-ab"'
    assert "# TYPE sitescope_tasks_handled_total counter" in lines
    assert f'sitescope_tasks_handled_total{{{labels},reason="completed"}} 3' in lines
    assert "# TYPE sitescope_task_duration_seconds histogram" in lines
    assert f'sitescope_task_duration_seconds_bucket{{{labels},le="1"}} 1' in lines
    assert f'sitescope_task_duration_seconds_bucket{{{labels},le="+Inf"}} 2' in lines
    assert f"sitescope_task_duration_seconds_sum{{{labels}}} 2.5" in lines
    assert f"sitescope_task_duration_seconds_count{{{labels}}} 2" in lines


def test_two_processes_share_one_family_block() -> None:
    registry = Registry()
    registry.counter("sitescope_tasks_handled_total", "Handled.").inc()
    exposition = Exposition()

    exposition.add_snapshot(registry.snapshot(), role="crawler", process="crawler-a")
    exposition.add_snapshot(registry.snapshot(), role="crawler", process="crawler-b")

    rendered = exposition.render()
    assert rendered.count("# TYPE sitescope_tasks_handled_total counter") == 1
    assert 'process="crawler-a"' in rendered
    assert 'process="crawler-b"' in rendered


def test_invalid_snapshot_families_are_skipped_not_rendered() -> None:
    good = {"name": "sitescope_ok_total", "type": "counter", "help": "", "labels": [], "series": []}
    histogram = {"name": "sitescope_h", "type": "histogram", "help": "", "labels": []}
    # Whatever Redis may hold: written by another version of the code, or damaged.
    invalid: list[Any] = [
        {**good, "name": "bad name"},
        {**good, "type": "summary"},
        {**good, "labels": ["bad-label"]},
        {**good, "labels": ["role"]},
        {**good, "labels": ["reason"], "series": [[["a", "b"], 1]]},
        {**histogram, "buckets": [1.0], "series": [[[], [1], 1.0, 1]]},  # a count is missing
        {"name": "missing_fields"},
        "not a mapping",
    ]
    exposition = Exposition()

    exposition.add_snapshot([*invalid, good], role="crawler", process="p")

    assert exposition.skipped == len(invalid)
    assert exposition.render().count("# TYPE") == 1


def test_a_snapshot_family_whose_type_conflicts_is_skipped() -> None:
    exposition = Exposition()
    exposition.gauge("sitescope_x", "A gauge.", 1)

    exposition.add_snapshot(
        [{"name": "sitescope_x", "type": "counter", "help": "", "labels": [], "series": [[[], 1]]}],
        role="crawler",
        process="p",
    )

    assert exposition.skipped == 1
    assert "process=" not in exposition.render()
