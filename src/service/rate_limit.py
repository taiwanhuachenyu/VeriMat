"""Bounded-principal token-bucket admission for the single-node control plane."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class PrincipalRateLimiter:
    """One bucket per configured principal; no IP, job, task, or arbitrary label state."""

    def __init__(
        self, *, requests_per_minute: int = 600, burst: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ):
        if requests_per_minute < 1 or burst < 1:
            raise ValueError("rate and burst must be positive")
        self.requests_per_minute = requests_per_minute
        self.burst = burst
        self.clock = clock
        self._rate_per_second = requests_per_minute / 60.0
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def allow(self, *, tenant_id: str, principal_id: str) -> tuple[bool, int]:
        key = (tenant_id, principal_id)
        now = self.clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.burst), updated_at=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(
                float(self.burst), bucket.tokens + elapsed * self._rate_per_second,
            )
            bucket.updated_at = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0
            retry_after = max(1, math.ceil((1.0 - bucket.tokens) / self._rate_per_second))
            return False, retry_after

    def configured_bucket_count(self) -> int:
        with self._lock:
            return len(self._buckets)
