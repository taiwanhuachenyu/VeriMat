"""Hard token budget enforced at the transport boundary.

A preregistered comparison is only fair if no method can silently overspend.  The wrapper
delegates everything to the inner transport and, before each call, refuses to proceed once the
cumulative token usage recorded by the inner transport's operation store crosses the cap.  Usage
is read from the same SQLite table the transport commits to, so the accounting survives a crash.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from src.evaluation.model_backend import ModelResponse, StructuredModelTransport


class BudgetExhausted(RuntimeError):
    """The method's preregistered token budget is spent; further calls are refused."""


class BudgetedTransport(StructuredModelTransport):
    """Wrap one transport with a cumulative input+output token ceiling."""

    def __init__(self, inner: StructuredModelTransport, *, max_tokens: int):
        if max_tokens < 1000:
            raise ValueError("a token budget under 1000 cannot run even one call")
        self.inner = inner
        self.max_tokens = max_tokens

    def _spent(self) -> int:
        db_path = getattr(self.inner, "conn", None)
        if db_path is None:
            return 0
        conn: sqlite3.Connection = self.inner.conn  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM model_operations"
            " WHERE status='COMPLETED'",
        ).fetchone()
        return int(row[0]) if row else 0

    def usage(self) -> dict[str, int]:
        return {"spent_tokens": self._spent(), "max_tokens": self.max_tokens}

    def complete(self, **kwargs: Any) -> ModelResponse:
        if self._spent() >= self.max_tokens:
            raise BudgetExhausted(
                f"token budget {self.max_tokens} exhausted; no further model calls are permitted"
            )
        response = self.inner.complete(**kwargs)
        if self._spent() > self.max_tokens:
            raise BudgetExhausted(
                f"the last call crossed the token budget {self.max_tokens}; "
                "its response is discarded and the overrun is recorded"
            )
        return response

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()
