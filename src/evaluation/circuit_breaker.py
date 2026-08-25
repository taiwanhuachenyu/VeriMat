"""Persistent closed/open/half-open circuit breaker for external benchmark calls."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable

from src.core.portability import extended_path
from src.operations.runtime_migrations import (
    CIRCUIT_SPEC, DatabaseSpec, assert_runtime_compatibility, prepare_runtime_database,
    schema_script,
)

from .baseline_runner import BaselineContractError

CIRCUIT_SCHEMA = schema_script(CIRCUIT_SPEC)


class CircuitOpenError(BaselineContractError):
    """The provider circuit is open and no external request may be transmitted."""


class PersistentCircuitBreaker:
    """Coordinate a single half-open probe across processes using SQLite transactions."""

    def __init__(
        self, *, database: str | Path, circuit_id: str, failure_threshold: int = 3,
        recovery_timeout_seconds: float = 60.0, probe_timeout_seconds: float = 900.0,
        clock: Callable[[], float] = time.time,
        database_spec: DatabaseSpec = CIRCUIT_SPEC,
    ):
        if not circuit_id.strip() or len(circuit_id) > 500:
            raise ValueError("circuit_id is invalid")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if recovery_timeout_seconds <= 0 or probe_timeout_seconds <= 0:
            raise ValueError("circuit timeouts must be positive")
        self.circuit_id = circuit_id
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.probe_timeout_seconds = probe_timeout_seconds
        self.clock = clock
        path = extended_path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        prepare_runtime_database(path, database_spec)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA journal_mode=WAL")
        assert_runtime_compatibility(self.conn, database_spec)
        now = self.clock()
        self.conn.execute(
            """INSERT OR IGNORE INTO circuit_breakers(
                   circuit_id,state,consecutive_failures,opened_at,probe_operation_id,
                   probe_started_at,updated_at
               ) VALUES (?,'CLOSED',0,NULL,NULL,NULL,?)""",
            (self.circuit_id, now),
        )

    def close(self) -> None:
        self.conn.close()

    def _row(self) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM circuit_breakers WHERE circuit_id=?", (self.circuit_id,),
        ).fetchone()
        if row is None:
            raise BaselineContractError("circuit state disappeared")
        return row

    def _event(
        self, *, operation_id: str, event_type: str, prior_state: str,
        next_state: str, reason_code: str, now: float,
    ) -> None:
        self.conn.execute(
            """INSERT INTO circuit_events(
                   circuit_id,operation_id,event_type,prior_state,next_state,
                   reason_code,occurred_at
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                self.circuit_id, operation_id, event_type, prior_state, next_state,
                reason_code, now,
            ),
        )

    @staticmethod
    def _operation(operation_id: str) -> None:
        if not operation_id.strip() or len(operation_id) > 500:
            raise BaselineContractError("circuit operation id is invalid")

    def before_call(self, *, operation_id: str) -> None:
        """Admit a closed-circuit request or atomically lease one half-open probe."""
        self._operation(operation_id)
        now = self.clock()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self._row()
            state = str(row["state"])
            if state == "CLOSED":
                self.conn.execute("COMMIT")
                return
            if state == "HALF_OPEN":
                started = float(row["probe_started_at"] or 0)
                if now - started >= self.probe_timeout_seconds:
                    self.conn.execute(
                        """UPDATE circuit_breakers SET state='OPEN',opened_at=?,
                                  probe_operation_id=NULL,probe_started_at=NULL,updated_at=?
                           WHERE circuit_id=? AND state='HALF_OPEN'""",
                        (now, now, self.circuit_id),
                    )
                    self._event(
                        operation_id=operation_id, event_type="probe_expired",
                        prior_state="HALF_OPEN", next_state="OPEN",
                        reason_code="probe_lease_expired", now=now,
                    )
                    self.conn.execute("COMMIT")
                    raise CircuitOpenError(
                        "half-open probe lease expired; circuit reopened for a new cooldown"
                    )
                self.conn.execute("COMMIT")
                raise CircuitOpenError("circuit is HALF_OPEN with another probe in flight")
            if state != "OPEN":
                raise BaselineContractError(f"invalid circuit state {state!r}")
            opened_at = float(row["opened_at"] or 0)
            if now - opened_at < self.recovery_timeout_seconds:
                self.conn.execute("COMMIT")
                raise CircuitOpenError("circuit is OPEN; recovery cooldown has not elapsed")
            cursor = self.conn.execute(
                """UPDATE circuit_breakers SET state='HALF_OPEN',probe_operation_id=?,
                          probe_started_at=?,updated_at=?
                   WHERE circuit_id=? AND state='OPEN'""",
                (operation_id, now, now, self.circuit_id),
            )
            if cursor.rowcount != 1:
                raise CircuitOpenError("half-open probe was acquired concurrently")
            self._event(
                operation_id=operation_id, event_type="probe_admitted",
                prior_state="OPEN", next_state="HALF_OPEN",
                reason_code="recovery_timeout_elapsed", now=now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def record_success(self, *, operation_id: str) -> None:
        self._operation(operation_id)
        now = self.clock()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self._row()
            state = str(row["state"])
            if state == "HALF_OPEN" and row["probe_operation_id"] != operation_id:
                raise BaselineContractError("success does not belong to the half-open probe")
            if state == "OPEN":
                raise BaselineContractError("cannot record success while circuit is open")
            self.conn.execute(
                """UPDATE circuit_breakers SET state='CLOSED',consecutive_failures=0,
                          opened_at=NULL,probe_operation_id=NULL,probe_started_at=NULL,updated_at=?
                   WHERE circuit_id=?""",
                (now, self.circuit_id),
            )
            if state != "CLOSED" or int(row["consecutive_failures"]) != 0:
                self._event(
                    operation_id=operation_id, event_type="call_succeeded",
                    prior_state=state, next_state="CLOSED", reason_code="provider_success",
                    now=now,
                )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def record_failure(self, *, operation_id: str, reason_code: str) -> None:
        self._operation(operation_id)
        if not reason_code.strip() or len(reason_code) > 120:
            raise BaselineContractError("circuit failure reason code is invalid")
        now = self.clock()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self._row()
            state = str(row["state"])
            if state == "OPEN":
                self.conn.execute("COMMIT")
                return
            if state == "HALF_OPEN" and row["probe_operation_id"] != operation_id:
                raise BaselineContractError("failure does not belong to the half-open probe")
            failures = int(row["consecutive_failures"]) + 1
            next_state = (
                "OPEN" if state == "HALF_OPEN" or failures >= self.failure_threshold
                else "CLOSED"
            )
            opened_at = now if next_state == "OPEN" else None
            self.conn.execute(
                """UPDATE circuit_breakers SET state=?,consecutive_failures=?,opened_at=?,
                          probe_operation_id=NULL,probe_started_at=NULL,updated_at=?
                   WHERE circuit_id=?""",
                (next_state, failures, opened_at, now, self.circuit_id),
            )
            self._event(
                operation_id=operation_id, event_type="call_failed",
                prior_state=state, next_state=next_state,
                reason_code=reason_code, now=now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def snapshot(self) -> dict[str, object]:
        row = self._row()
        return {
            "circuit_id": self.circuit_id,
            "state": str(row["state"]),
            "consecutive_failures": int(row["consecutive_failures"]),
            "opened_at": row["opened_at"],
            "probe_operation_id": row["probe_operation_id"],
            "probe_started_at": row["probe_started_at"],
            "updated_at": float(row["updated_at"]),
            "failure_threshold": self.failure_threshold,
            "recovery_timeout_seconds": self.recovery_timeout_seconds,
            "probe_timeout_seconds": self.probe_timeout_seconds,
        }

    def events(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.conn.execute(
            """SELECT event_id,circuit_id,operation_id,event_type,prior_state,next_state,
                      reason_code,occurred_at
               FROM circuit_events WHERE circuit_id=? ORDER BY event_id""",
            (self.circuit_id,),
        ).fetchall()]
