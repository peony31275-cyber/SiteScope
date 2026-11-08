"""The process-local metrics registry (Phase 10)."""

import math

import pytest

from app.observability.metrics import MetricError, Registry


def test_a_counter_counts_per_label_set_and_snapshots_sorted_series() -> None:
    registry = Registry()
    handled = registry.counter("sitescope_test_total", "Test counter.", ("reason",))

    handled.inc(reason="completed")
    handled.inc(2, reason="completed")
    handled.inc(reason="fenced")

    (family,) = registry.snapshot()
    assert family == {
        "name": "sitescope_test_total",
        "type": "counter",
        "help": "Test counter.",
        "labels": ["reason"],
        "series": [[["completed"], 3.0], [["fenced"], 1.0]],
    }


def test_histogram_buckets_include_their_upper_bound_and_snapshot_cumulative_counts() -> None:
    registry = Registry()
    durations = registry.histogram("sitescope_test_seconds", "Test.", buckets=(0.1, 1.0))

    for value in (0.05, 0.1, 0.5, 1.0, 7.0):
        durations.observe(value)

    (family,) = registry.snapshot()
    assert family["buckets"] == [0.1, 1.0]
    ((labels, cumulative, total, count),) = family["series"]
    assert labels == []
    # le=0.1: 0.05 and 0.1; le=1: and 0.5 and 1.0; +Inf: and 7.0.
    assert cumulative == [2, 4, 5]
    assert total == pytest.approx(8.65)
    assert count == 5


def test_a_histogram_ignores_values_that_are_not_finite() -> None:
    registry = Registry()
    durations = registry.histogram("sitescope_test_seconds", "Test.", buckets=(1.0,))

    durations.observe(math.nan)
    durations.observe(math.inf)

    assert registry.snapshot()[0]["series"] == []


def test_declaring_a_family_twice_returns_it_and_a_conflict_is_an_error() -> None:
    registry = Registry()
    first = registry.counter("sitescope_test_total", "Test.", ("reason",))

    assert registry.counter("sitescope_test_total", "Test.", ("reason",)) is first
    with pytest.raises(MetricError):
        registry.counter("sitescope_test_total", "Test.", ("other",))
    with pytest.raises(MetricError):
        registry.histogram("sitescope_test_total", "Test.", ("reason",))


@pytest.mark.parametrize(
    ("name", "labels"),
    [
        ("bad name", ()),
        ("sitescope_ok", ("bad-label",)),
        ("sitescope_ok", ("__reserved",)),
        ("sitescope_ok", ("role",)),  # added by the exposition
        ("sitescope_ok", ("process",)),
        ("sitescope_ok", ("a", "a")),
    ],
)
def test_names_and_labels_are_validated(name: str, labels: tuple[str, ...]) -> None:
    with pytest.raises(MetricError):
        Registry().counter(name, "Test.", labels)


def test_histogram_buckets_must_be_finite_and_strictly_increasing() -> None:
    registry = Registry()
    for buckets in ((), (1.0, 0.5), (1.0, 1.0), (1.0, math.inf)):
        with pytest.raises(MetricError):
            registry.histogram(f"sitescope_test_{len(buckets)}_seconds", "Test.", buckets=buckets)


def test_label_values_must_match_the_declared_labels() -> None:
    counter = Registry().counter("sitescope_test_total", "Test.", ("reason",))

    with pytest.raises(MetricError):
        counter.inc()
    with pytest.raises(MetricError):
        counter.inc(reason="a", extra="b")


def test_a_counter_never_goes_down() -> None:
    counter = Registry().counter("sitescope_test_total", "Test.")

    with pytest.raises(MetricError):
        counter.inc(-1)
    with pytest.raises(MetricError):
        counter.inc(math.nan)


def test_a_family_drops_series_beyond_its_limit_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = Registry()
    counter = registry.counter("sitescope_test_total", "Test.", ("value",), max_series=2)

    for value in ("a", "b", "c", "d"):
        counter.inc(value=value)
    counter.inc(value="a")  # an existing series still counts

    series = registry.snapshot()[0]["series"]
    assert series == [[["a"], 2.0], [["b"], 1.0]]
    limit_records = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "metrics.series_limit"
    ]
    assert len(limit_records) == 1
