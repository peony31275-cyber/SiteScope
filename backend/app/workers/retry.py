"""The crawl retry policy: which failed attempts are tried again, and when (ADR-0017).

A task's result is *retryable* if trying the same URL later may succeed:

* a failure kind in ``app.crawler.errors.RETRYABLE_KINDS``: temporary DNS failures, connection
  failures, connect and read timeouts, the fetch deadline, truncated responses, and
  ``rate_limited`` (no politeness slot in time; nothing was sent);
* ``deadline_exceeded`` from the task deadline (``runtime.crawl_within``), and
  ``infrastructure_error`` (Redis failed during the task: the politeness gate or the robots.txt
  cache);
* ``http_error`` with a status in ``SITESCOPE_RETRY_HTTP_STATUSES`` (429, 500, 502, 503 and 504
  by default).

Everything else is permanent: invalid URLs, scope and SSRF refusals, robots.txt decisions, TLS
errors, redirect problems, other HTTP statuses such as 404 and 410, unsupported or oversized
content, parse errors.

A retryable result is retried while attempts remain (``attempt < SITESCOPE_CRAWL_MAX_ATTEMPTS``;
claims count attempts, so crashed attempts count too). The delay is full jitter,
``uniform(0, min(max_delay, base * 2^(attempt - 1)))``, and at least a valid ``Retry-After``.
A ``Retry-After`` above ``SITESCOPE_RETRY_AFTER_MAX_SECONDS`` is not retried at all: the server
asked for more patience than the crawl gives one URL (its BFS level waits for it).

The decision only computes a delay. PostgreSQL holds the retry itself (``pending`` with
``next_attempt_at``, ``crawl_state.record_result``): no in-memory timer, so a retry survives
restarts, and any worker may run it.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

from app.core.backoff import full_jitter
from app.core.config import Settings
from app.crawler.errors import RETRYABLE_KINDS, FailureKind
from app.services.crawl_state import UrlResult

INFRASTRUCTURE_ERROR: Final = "infrastructure_error"
RETRYABLE_OUTCOMES: Final = frozenset(
    {kind.value for kind in RETRYABLE_KINDS} | {INFRASTRUCTURE_ERROR}
)
_RANDOM = random.SystemRandom()


class RetryVerdict(StrEnum):
    RETRY = "retry"
    PERMANENT = "permanent"  # not a retryable failure (or not a failure at all)
    EXHAUSTED = "exhausted"  # retryable, but this was the last allowed attempt
    RETRY_AFTER_TOO_LONG = "retry_after_too_long"  # the server asked for more than the cap


@dataclass(frozen=True, slots=True)
class RetryDecision:
    verdict: RetryVerdict
    delay: float | None = None  # seconds until the next attempt, for RETRY only


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 2.0
    max_delay: float = 60.0
    retry_after_max: float = 300.0
    http_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if not 0 < self.base_delay <= self.max_delay:
            raise ValueError("the base delay must be positive and at most the maximum delay")
        if self.retry_after_max < 0:
            raise ValueError("retry_after_max must not be negative")

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            max_attempts=settings.crawl_max_attempts,
            base_delay=settings.retry_base_delay_seconds,
            max_delay=settings.retry_max_delay_seconds,
            retry_after_max=settings.retry_after_max_seconds,
            http_statuses=frozenset(settings.retry_http_statuses),
        )

    def retryable(self, result: UrlResult) -> bool:
        if result.outcome == FailureKind.HTTP_ERROR.value:
            return result.http_status in self.http_statuses
        return result.outcome in RETRYABLE_OUTCOMES

    def decide(
        self,
        result: UrlResult,
        *,
        attempt: int,
        uniform: Callable[[float, float], float] = _RANDOM.uniform,
    ) -> RetryDecision:
        """Whether attempt number ``attempt`` (1-based) of this URL is retried, and after how
        long. ``uniform`` is injected by tests."""
        if not self.retryable(result):
            return RetryDecision(RetryVerdict.PERMANENT)
        if attempt >= self.max_attempts:
            return RetryDecision(RetryVerdict.EXHAUSTED)
        if result.retry_after is not None and result.retry_after > self.retry_after_max:
            return RetryDecision(RetryVerdict.RETRY_AFTER_TOO_LONG)
        delay = full_jitter(attempt, base=self.base_delay, cap=self.max_delay, uniform=uniform)
        if result.retry_after is not None:
            delay = max(delay, result.retry_after)
        return RetryDecision(RetryVerdict.RETRY, delay)
