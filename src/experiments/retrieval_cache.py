"""Content-addressed retrieval cache shared by every method in a run.

Verification fairness requires that identical queries return identical evidence to every
method; rate limits require that identical queries are not paid for twice.  One cache over the
retrieval client gives both, and doubles as the run's unified retrieval snapshot: the cache file
IS the evidence retrieval record, keyed by the canonical request.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from src.core.events import canonical_json


class CachedThrottledSciverse:
    """Transparent cache + pacing proxy over one SciverseClient.

    Cache hits never touch the network; misses are paced and stored.  The cache key is the
    canonical request, so the year-window separation between discovery-time verification and
    validation-time oracle search is preserved automatically.
    """

    def __init__(self, inner: Any, *, cache_path: str | Path,
                 min_interval: float = 3.0, wait_on_429: float = 75.0, max_429: int = 12):
        self._inner = inner
        self._cache_path = Path(cache_path)
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._min_interval = min_interval
        self._wait_on_429 = wait_on_429
        self._max_429 = max_429
        self._last = 0.0
        self._store: dict[str, Any] = {}
        if self._cache_path.exists():
            for line in self._cache_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                self._store[row["key"]] = row["payload"]
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(name: str, args: tuple, kwargs: dict) -> str:
        body = canonical_json({"call": name, "args": args, "kwargs": kwargs})
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _pacing(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self._min_interval:
            time.sleep(self._min_interval - gap)
        self._last = time.monotonic()

    def _append(self, key: str, payload: Any) -> None:
        from src.core import portability
        with self._cache_path.open("a", encoding="utf-8") as handle:
            portability.lock_exclusive(handle)
            try:
                handle.write(canonical_json({"key": key, "payload": payload}) + "\n")
                handle.flush()
            finally:
                portability.unlock(handle)

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            key = self._key(name, args, kwargs)
            if key in self._store:
                self.hits += 1
                return json.loads(json.dumps(self._store[key]))  # detach from cache
            for attempt in range(self._max_429 + 1):
                self._pacing()
                try:
                    value = attribute(*args, **kwargs)
                    break
                except Exception as exc:
                    if "429" not in str(exc) or attempt == self._max_429:
                        raise
                    wait = self._wait_on_429 * (attempt + 1)
                    print(f"[retrieval] 429, waiting {wait:.0f}s ...", flush=True)
                    time.sleep(wait)
            self.misses += 1
            try:
                self._append(key, value)
                self._store[key] = value
            except (TypeError, ValueError):
                pass  # non-JSON-serialisable payloads bypass the cache
            return value
        return call
