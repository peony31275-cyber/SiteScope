"""Exponential backoff with full jitter, for loops that retry transient infrastructure errors.

The n-th consecutive failure waits a uniformly random time in ``[0, min(cap, base * 2**(n-1))]``
(AWS Architecture Blog, "Exponential Backoff And Jitter"). Crawl retries are not done this way:
they live in PostgreSQL (Phase 6).
"""

import random
from dataclasses import dataclass

_RANDOM = random.SystemRandom()


@dataclass
class Backoff:
    base: float = 0.5
    cap: float = 10.0
    failures: int = 0  # consecutive failures so far

    def next_delay(self) -> float:
        """Record a failure and return how long to wait before the next attempt."""
        self.failures += 1
        return _RANDOM.uniform(0.0, self.ceiling())

    def ceiling(self) -> float:
        """The longest wait after the failures recorded so far."""
        if self.failures == 0:
            return 0.0
        return min(self.cap, self.base * 2.0 ** min(self.failures - 1, 30))

    def reset(self) -> bool:
        """Record a success; True if it ended a series of failures."""
        recovered = self.failures > 0
        self.failures = 0
        return recovered
