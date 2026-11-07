"""The crawl retry policy: which failures are retried, after how long, and when they are final."""

import pytest

from app.core.backoff import full_jitter, jitter_ceiling
from app.core.config import Settings
from app.crawler.errors import RETRYABLE_KINDS, FailureKind
from app.db.models import UrlState
from app.services.crawl_state import UrlResult
from app.workers.crawler import WorkerConfig
from app.workers.local_crawler import DriverConfig
from app.workers.retry import (
    INFRASTRUCTURE_ERROR,
    RetryDecision,
    RetryPolicy,
    RetryVerdict,
)
from app.workers.runtime import DEADLINE_EXCEEDED

POLICY = RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=60.0, retry_after_max=300.0)


def failed(
    outcome: str, *, status: int | None = None, retry_after: float | None = None
) -> UrlResult:
    return UrlResult(
        UrlState.FAILED, outcome, http_status=status, error="test", retry_after=retry_after
    )


def ceiling(low: float, high: float) -> float:
    """A ``uniform`` that always picks the longest wait."""
    return high


# --- Classification ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    sorted({kind.value for kind in RETRYABLE_KINDS} | {DEADLINE_EXCEEDED, INFRASTRUCTURE_ERROR}),
)
def test_transient_failures_are_retried(outcome: str) -> None:
    assert POLICY.retryable(failed(outcome))
    assert POLICY.decide(failed(outcome), attempt=1).verdict is RetryVerdict.RETRY


def test_the_retryable_kinds_are_the_transient_ones() -> None:
    assert {kind.value for kind in RETRYABLE_KINDS} == {
        "dns_error",
        "connection_error",
        "connect_timeout",
        "read_timeout",
        "deadline_exceeded",
        "truncated",
        "rate_limited",
    }


@pytest.mark.parametrize(
    "outcome",
    [
        kind.value
        for kind in FailureKind
        if kind not in RETRYABLE_KINDS and kind is not FailureKind.HTTP_ERROR
    ]
    + ["robots_disallowed", "robots_unreachable", "out_of_scope", "parse_error", "noindex"],
)
def test_permanent_failures_are_never_retried(outcome: str) -> None:
    """Invalid URLs, scope and SSRF refusals, robots.txt, TLS, redirects, content problems."""
    assert not POLICY.retryable(failed(outcome))
    assert POLICY.decide(failed(outcome), attempt=1) == RetryDecision(RetryVerdict.PERMANENT)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_http_statuses_are_retried(status: int) -> None:
    assert POLICY.decide(failed("http_error", status=status), attempt=1).verdict is (
        RetryVerdict.RETRY
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 418, 451, 501, 505, None])
def test_other_http_statuses_are_final(status: int | None) -> None:
    assert POLICY.decide(failed("http_error", status=status), attempt=1).verdict is (
        RetryVerdict.PERMANENT
    )


def test_the_retried_statuses_are_configurable() -> None:
    policy = RetryPolicy(http_statuses=frozenset({503}))
    assert policy.retryable(failed("http_error", status=503))
    assert not policy.retryable(failed("http_error", status=500))


@pytest.mark.parametrize("state", [UrlState.FETCHED, UrlState.SKIPPED])
def test_success_and_skips_are_never_retried(state: UrlState) -> None:
    result = UrlResult(state, "html_stored" if state is UrlState.FETCHED else "robots_disallowed")
    assert POLICY.decide(result, attempt=1).verdict is RetryVerdict.PERMANENT


# --- Attempts --------------------------------------------------------------------------------


def test_the_last_attempt_is_final() -> None:
    result = failed("read_timeout")
    assert POLICY.decide(result, attempt=2).verdict is RetryVerdict.RETRY
    assert POLICY.decide(result, attempt=3) == RetryDecision(RetryVerdict.EXHAUSTED)
    assert POLICY.decide(result, attempt=4).verdict is RetryVerdict.EXHAUSTED


def test_one_attempt_means_no_retry() -> None:
    policy = RetryPolicy(max_attempts=1)
    assert policy.decide(failed("read_timeout"), attempt=1).verdict is RetryVerdict.EXHAUSTED


# --- Delays ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("attempt", "longest"), [(1, 2.0), (2, 4.0), (3, 8.0), (6, 60.0)])
def test_the_delay_is_full_jitter_up_to_the_cap(attempt: int, longest: float) -> None:
    policy = RetryPolicy(max_attempts=10, base_delay=2.0, max_delay=60.0)
    decision = policy.decide(failed("connect_timeout"), attempt=attempt, uniform=ceiling)
    assert decision == RetryDecision(RetryVerdict.RETRY, longest)
    shortest = policy.decide(failed("connect_timeout"), attempt=attempt, uniform=lambda a, b: a)
    assert shortest.delay == 0.0


def test_real_jitter_stays_within_its_bounds() -> None:
    for attempt in (1, 2):  # attempt 3 of 3 is final: no delay
        for _ in range(200):
            delay = POLICY.decide(failed("dns_error"), attempt=attempt).delay
            assert delay is not None
            assert 0.0 <= delay <= jitter_ceiling(attempt, base=2.0, cap=60.0)


def test_retry_after_is_a_lower_bound() -> None:
    result = failed("http_error", status=503, retry_after=45.0)
    assert POLICY.decide(result, attempt=1, uniform=ceiling).delay == 45.0  # jitter says 2 s
    long_jitter = RetryPolicy(max_attempts=10, base_delay=50.0, max_delay=60.0)
    assert long_jitter.decide(result, attempt=1, uniform=ceiling).delay == 50.0


def test_a_retry_after_beyond_the_cap_is_not_waited_for() -> None:
    result = failed("http_error", status=429, retry_after=301.0)
    assert POLICY.decide(result, attempt=1) == RetryDecision(RetryVerdict.RETRY_AFTER_TOO_LONG)
    at_cap = failed("http_error", status=429, retry_after=300.0)
    assert POLICY.decide(at_cap, attempt=1, uniform=ceiling).delay == 300.0


def test_jitter_ceilings_never_overflow() -> None:
    assert jitter_ceiling(0, base=2.0, cap=60.0) == 0.0
    assert jitter_ceiling(1, base=2.0, cap=60.0) == 2.0
    assert jitter_ceiling(10_000, base=2.0, cap=60.0) == 60.0
    assert full_jitter(10_000, base=2.0, cap=60.0, uniform=ceiling) == 60.0


# --- Configuration -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        {"max_attempts": 0},
        {"base_delay": 0.0},
        {"base_delay": 61.0, "max_delay": 60.0},
        {"retry_after_max": -1.0},
    ],
)
def test_invalid_policies_are_rejected(values: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must"):
        RetryPolicy(**values)  # type: ignore[arg-type]  # parametrized keyword values


def test_the_policy_comes_from_the_settings() -> None:
    settings = Settings(
        _env_file=None,
        crawl_max_attempts=5,
        retry_base_delay_seconds=1.5,
        retry_max_delay_seconds=30.0,
        retry_after_max_seconds=120.0,
        retry_http_statuses=(503, 429),
    )
    assert RetryPolicy.from_settings(settings) == RetryPolicy(
        max_attempts=5,
        base_delay=1.5,
        max_delay=30.0,
        retry_after_max=120.0,
        http_statuses=frozenset({429, 503}),
    )
    assert WorkerConfig.from_settings(settings).retry_policy == RetryPolicy.from_settings(settings)
    assert DriverConfig.from_settings(settings).retry_policy == RetryPolicy.from_settings(settings)


def test_processes_default_to_a_policy_with_their_own_attempts() -> None:
    assert WorkerConfig(max_attempts=4).retry_policy == RetryPolicy(max_attempts=4)
    assert DriverConfig(max_attempts=2).retry_policy == RetryPolicy(max_attempts=2)


@pytest.mark.parametrize("config", [WorkerConfig, DriverConfig])
def test_a_policy_with_other_attempts_is_rejected(
    config: type[WorkerConfig | DriverConfig],
) -> None:
    with pytest.raises(ValueError, match="same max_attempts"):
        config(max_attempts=3, retry=RetryPolicy(max_attempts=5))
