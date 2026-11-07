"""Exponential backoff with full jitter.

The n-th consecutive failure waits a uniformly random time in ``[0, min(cap, base * 2**(n-1))]``
(AWS Architecture Blog, "Exponential Backoff And Jitter"): spread out, so that many clients that
failed together do not retry together.

* ``Backoff`` counts failures itself: loops that retry transient infrastructure errors.
* ``full_jitter`` takes the attempt number: crawl retries, whose attempts PostgreSQL counts
  (``app/workers/retry.py``).
"""

import random
from collections.abc import Callable
from dataclasses import dataclass

_RANDOM = random.SystemRandom()


def jitter_ceiling(attempt: int, *, base: float, cap: float) -> float:
    """The longest wait after ``attempt`` failures (``attempt`` >= 1)."""
    if attempt < 1:
        return 0.0
    return min(cap, base * 2.0 ** min(attempt - 1, 60))


def full_jitter(
    attempt: int,
    *,
    base: float,
    cap: float,
    uniform: Callable[[float, float], float] = _RANDOM.uniform,
) -> float:
    """A random wait in ``[0, jitter_ceiling(attempt)]`` (``uniform`` is injected by tests)."""
    return uniform(0.0, jitter_ceiling(attempt, base=base, cap=cap))


@dataclass
class Backoff:
    base: float = 0.5
    cap: float = 10.0
    failures: int = 0  # consecutive failures so far

    def next_delay(self) -> float:
        """Record a failure and return how long to wait before the next attempt."""
        self.failures += 1
        return full_jitter(self.failures, base=self.base, cap=self.cap)

    def ceiling(self) -> float:
        """The longest wait after the failures recorded so far."""
        return jitter_ceiling(self.failures, base=self.base, cap=self.cap)

    def reset(self) -> bool:
        """Record a success; True if it ended a series of failures."""
        recovered = self.failures > 0
        self.failures = 0
        return recovered
