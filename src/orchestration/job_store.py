"""SQLite control-plane store for recoverable, idempotent research jobs."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from src.core.events import EventValidationError, validate_durable_payload
from src.core.portability import extended_path
from src.operations.migrations import (
    assert_control_compatibility, migrate_control_database, verify_control_database,
)


class JobStoreError(RuntimeError):
    pass


class IdempotencyConflict(JobStoreError):
    pass


class IllegalTransition(JobStoreError):
    pass


class LeaseConflict(JobStoreError):
    pass


class BudgetExceeded(JobStoreError):
    pass


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Stage(str, Enum):
    PLAN = "PLAN"
    RETRIEVE = "RETRIEVE"
    READ = "READ"
    PROPOSE = "PROPOSE"
    CHALLENGE = "CHALLENGE"
    DECIDE = "DECIDE"
    SYNTHESIZE = "SYNTHESIZE"
    VALIDATE = "VALIDATE"


STAGE_ORDER = {stage: index for index, stage in enumerate(Stage)}
TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
ALLOWED_STATUS = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {
        JobStatus.VALIDATING, JobStatus.RETRY_WAIT, JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.RETRY_WAIT: {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.VALIDATING: {
        JobStatus.SUCCEEDED, JobStatus.RETRY_WAIT, JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    lease_owner TEXT,
    lease_expires_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_calls INTEGER NOT NULL,
    max_tokens INTEGER NOT NULL,
    max_cost_microunits INTEGER NOT NULL,
    used_calls INTEGER NOT NULL DEFAULT 0,
    used_tokens INTEGER NOT NULL DEFAULT 0,
    used_cost_microunits INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(tenant_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    checkpoint_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(job_id, checkpoint_key)
);
CREATE TABLE IF NOT EXISTS usage_ledger (
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    charge_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    calls INTEGER NOT NULL,
    tokens INTEGER NOT NULL,
    cost_microunits INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(job_id, charge_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_runnable
    ON jobs(status, lease_expires_at, created_at);
"""


@dataclass(frozen=True)
class Job:
    job_id: str
    tenant_id: str
    idempotency_key: str
    task: str
    status: JobStatus
    stage: Stage
    version: int
    lease_owner: str | None
    lease_expires_at: float | None
    attempts: int
    max_calls: int
    max_tokens: int
    max_cost_microunits: int
    used_calls: int
    used_tokens: int
    used_cost_microunits: int
    last_error_code: str | None
    created_at: float
    updated_at: float


class JobStore:
    def __init__(self, path: str | Path, *, full_schema_verification: bool = True):
        self.path = str(extended_path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(self.path).exists():
            migrate_control_database(self.path)
        elif full_schema_verification:
            verify_control_database(self.path)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA journal_mode=WAL")
        assert_control_compatibility(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        value = dict(row)
        value["status"] = JobStatus(value["status"])
        value["stage"] = Stage(value["stage"])
        return Job(**value)

    def get(self, job_id: str, *, tenant_id: str | None = None) -> Job:
        if tenant_id is None:
            row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE job_id=? AND tenant_id=?", (job_id, tenant_id)
            ).fetchone()
        if row is None:
            raise JobStoreError(f"job {job_id!r} not found")
        return self._job(row)

    def create_job(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        task: str,
        max_calls: int,
        max_tokens: int,
        max_cost_microunits: int,
        job_id: str | None = None,
    ) -> Job:
        if not tenant_id.strip() or not idempotency_key.strip() or not task.strip():
            raise JobStoreError("tenant_id, idempotency_key, and task are required")
        if min(max_calls, max_tokens, max_cost_microunits) < 0:
            raise JobStoreError("budgets must be non-negative")
        now, identifier = time.time(), job_id or str(uuid.uuid4())
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                "SELECT * FROM jobs WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["task"] == task
                    and existing["max_calls"] == max_calls
                    and existing["max_tokens"] == max_tokens
                    and existing["max_cost_microunits"] == max_cost_microunits
                )
                if not same:
                    raise IdempotencyConflict(
                        "idempotency key already exists with different task or budgets"
                    )
                self.conn.execute("COMMIT")
                return self._job(existing)
            self.conn.execute(
                """INSERT INTO jobs
                   (job_id,tenant_id,idempotency_key,task,status,stage,max_calls,
                    max_tokens,max_cost_microunits,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, tenant_id, idempotency_key, task, JobStatus.QUEUED.value,
                 Stage.PLAN.value, max_calls, max_tokens, max_cost_microunits, now, now),
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return self.get(identifier, tenant_id=tenant_id)

    def acquire_lease(
        self, job_id: str, *, worker_id: str, lease_seconds: float,
        now: float | None = None,
    ) -> Job:
        if not worker_id.strip() or lease_seconds <= 0:
            raise JobStoreError("worker_id and positive lease_seconds are required")
        current_time = time.time() if now is None else now
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobStoreError(f"job {job_id!r} not found")
            status = JobStatus(row["status"])
            if status in TERMINAL or status == JobStatus.VALIDATING:
                raise LeaseConflict(f"job in non-runnable status {status.value}")
            owner, expiry = row["lease_owner"], row["lease_expires_at"]
            if owner and owner != worker_id and expiry is not None and expiry > current_time:
                raise LeaseConflict(f"job leased by {owner!r} until {expiry}")
            if status in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}:
                status = JobStatus.RUNNING
            self.conn.execute(
                """UPDATE jobs SET status=?,lease_owner=?,lease_expires_at=?,
                   attempts=attempts+1,version=version+1,updated_at=? WHERE job_id=?""",
                (status.value, worker_id, current_time + lease_seconds, current_time, job_id),
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return self.get(job_id)

    def heartbeat(
        self, job_id: str, *, worker_id: str, lease_seconds: float,
        now: float | None = None,
    ) -> Job:
        current_time = time.time() if now is None else now
        cursor = self.conn.execute(
            """UPDATE jobs SET lease_expires_at=?,updated_at=?,version=version+1
               WHERE job_id=? AND lease_owner=? AND status=?
               AND lease_expires_at>=?""",
            (current_time + lease_seconds, current_time, job_id, worker_id,
             JobStatus.RUNNING.value, current_time),
        )
        if cursor.rowcount != 1:
            raise LeaseConflict("heartbeat rejected: lease missing, expired, or owned elsewhere")
        return self.get(job_id)

    def save_checkpoint(
        self, job_id: str, *, worker_id: str, stage: Stage,
        checkpoint_key: str, payload: dict[str, Any], now: float | None = None,
    ) -> Job:
        current_time = time.time() if now is None else now
        try:
            validate_durable_payload(payload, prefix="checkpoint")
            rendered = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (EventValidationError, TypeError, ValueError) as exc:
            raise JobStoreError(f"checkpoint payload is invalid: {exc}") from exc
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobStoreError(f"job {job_id!r} not found")
            if row["status"] != JobStatus.RUNNING.value or row["lease_owner"] != worker_id:
                raise LeaseConflict("checkpoint requires the active job lease")
            if row["lease_expires_at"] is None or row["lease_expires_at"] < current_time:
                raise LeaseConflict("checkpoint rejected because lease expired")
            current_stage = Stage(row["stage"])
            if STAGE_ORDER[stage] < STAGE_ORDER[current_stage]:
                raise IllegalTransition(
                    f"stage cannot move backwards: {current_stage.value} -> {stage.value}"
                )
            existing = self.conn.execute(
                "SELECT payload,stage FROM checkpoints WHERE job_id=? AND checkpoint_key=?",
                (job_id, checkpoint_key),
            ).fetchone()
            if existing is not None:
                if existing["payload"] != rendered or existing["stage"] != stage.value:
                    raise IdempotencyConflict("checkpoint key reused with different content")
            else:
                self.conn.execute(
                    "INSERT INTO checkpoints VALUES (?,?,?,?,?)",
                    (job_id, stage.value, checkpoint_key, rendered, current_time),
                )
            self.conn.execute(
                "UPDATE jobs SET stage=?,version=version+1,updated_at=? WHERE job_id=?",
                (stage.value, current_time, job_id),
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return self.get(job_id)

    def charge(
        self, job_id: str, *, charge_key: str, provider: str,
        calls: int = 0, tokens: int = 0, cost_microunits: int = 0,
        now: float | None = None,
    ) -> Job:
        if min(calls, tokens, cost_microunits) < 0:
            raise JobStoreError("usage charges must be non-negative")
        current_time = time.time() if now is None else now
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobStoreError(f"job {job_id!r} not found")
            existing = self.conn.execute(
                "SELECT * FROM usage_ledger WHERE job_id=? AND charge_key=?",
                (job_id, charge_key),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["provider"] == provider and existing["calls"] == calls
                    and existing["tokens"] == tokens
                    and existing["cost_microunits"] == cost_microunits
                )
                if not same:
                    raise IdempotencyConflict("charge key reused with different usage")
                self.conn.execute("COMMIT")
                return self._job(row)
            projected = {
                "calls": row["used_calls"] + calls,
                "tokens": row["used_tokens"] + tokens,
                "cost": row["used_cost_microunits"] + cost_microunits,
            }
            exceeded = []
            if projected["calls"] > row["max_calls"]:
                exceeded.append("calls")
            if projected["tokens"] > row["max_tokens"]:
                exceeded.append("tokens")
            if projected["cost"] > row["max_cost_microunits"]:
                exceeded.append("cost")
            if exceeded:
                raise BudgetExceeded("budget exceeded: " + ", ".join(exceeded))
            self.conn.execute(
                "INSERT INTO usage_ledger VALUES (?,?,?,?,?,?,?)",
                (job_id, charge_key, provider, calls, tokens, cost_microunits, current_time),
            )
            self.conn.execute(
                """UPDATE jobs SET used_calls=?,used_tokens=?,used_cost_microunits=?,
                   version=version+1,updated_at=? WHERE job_id=?""",
                (projected["calls"], projected["tokens"], projected["cost"],
                 current_time, job_id),
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return self.get(job_id)

    def transition(
        self, job_id: str, *, target: JobStatus, expected_version: int | None = None,
        error_code: str | None = None, release_lease: bool = True,
    ) -> Job:
        now = time.time()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobStoreError(f"job {job_id!r} not found")
            current = JobStatus(row["status"])
            if expected_version is not None and row["version"] != expected_version:
                raise IllegalTransition(
                    f"optimistic version mismatch: expected {expected_version}, got {row['version']}"
                )
            if target not in ALLOWED_STATUS.get(current, set()):
                raise IllegalTransition(f"illegal status transition {current.value} -> {target.value}")
            if target == JobStatus.SUCCEEDED and Stage(row["stage"]) != Stage.VALIDATE:
                raise IllegalTransition("SUCCEEDED requires a committed VALIDATE checkpoint")
            owner = None if release_lease else row["lease_owner"]
            expiry = None if release_lease else row["lease_expires_at"]
            self.conn.execute(
                """UPDATE jobs SET status=?,lease_owner=?,lease_expires_at=?,last_error_code=?,
                   version=version+1,updated_at=? WHERE job_id=?""",
                (target.value, owner, expiry, error_code, now, job_id),
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return self.get(job_id)

    def checkpoints(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM checkpoints WHERE job_id=? ORDER BY created_at,checkpoint_key",
            (job_id,),
        ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in rows
        ]

    def runnable_job_ids(self, *, limit: int = 100, now: float | None = None) -> list[str]:
        """Return candidates only; callers must still acquire a lease to win a race."""
        if limit < 1:
            return []
        current_time = time.time() if now is None else now
        rows = self.conn.execute(
            """SELECT job_id FROM jobs
               WHERE status IN (?,?)
                  OR (status=? AND lease_expires_at IS NOT NULL AND lease_expires_at<=?)
               ORDER BY created_at,job_id LIMIT ?""",
            (JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value,
             JobStatus.RUNNING.value, current_time, limit),
        ).fetchall()
        return [str(row["job_id"]) for row in rows]

    def readiness_check(self) -> dict[str, Any]:
        """Verify database readability and required schema without exposing stored values."""
        integrity = self.conn.execute("PRAGMA quick_check(1)").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise JobStoreError("database quick check failed")
        required = {"jobs", "checkpoints", "usage_ledger"}
        present = {
            str(row[0]) for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not required <= present:
            raise JobStoreError("database schema is incomplete")
        journal = str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if journal != "wal":
            raise JobStoreError("database journal mode is not WAL")
        return {"ready": True, "storage": "sqlite", "journal_mode": "wal"}

    def operational_snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        """Return fixed-cardinality aggregate telemetry; never expose row identities or text."""
        current_time = time.time() if now is None else now
        jobs_by_status = {status.value: 0 for status in JobStatus}
        for row in self.conn.execute(
            "SELECT status,COUNT(*) AS count FROM jobs GROUP BY status"
        ).fetchall():
            if row["status"] not in jobs_by_status:
                raise JobStoreError("database contains an unknown job status")
            jobs_by_status[str(row["status"])] = int(row["count"])
        jobs_by_stage = {stage.value: 0 for stage in Stage}
        for row in self.conn.execute(
            "SELECT stage,COUNT(*) AS count FROM jobs GROUP BY stage"
        ).fetchall():
            if row["stage"] not in jobs_by_stage:
                raise JobStoreError("database contains an unknown job stage")
            jobs_by_stage[str(row["stage"])] = int(row["count"])
        totals = self.conn.execute(
            """SELECT COUNT(*) AS jobs_total,COALESCE(SUM(used_calls),0) AS used_calls,
                      COALESCE(SUM(used_tokens),0) AS used_tokens,
                      COALESCE(SUM(used_cost_microunits),0) AS used_cost_microunits
               FROM jobs"""
        ).fetchone()
        active_leases = int(self.conn.execute(
            """SELECT COUNT(*) FROM jobs WHERE status=? AND lease_expires_at>?""",
            (JobStatus.RUNNING.value, current_time),
        ).fetchone()[0])
        expired_leases = int(self.conn.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE status=? AND lease_expires_at IS NOT NULL AND lease_expires_at<=?""",
            (JobStatus.RUNNING.value, current_time),
        ).fetchone()[0])
        oldest = self.conn.execute(
            """SELECT MIN(created_at) FROM jobs
               WHERE status IN (?,?) OR (
                   status=? AND lease_expires_at IS NOT NULL AND lease_expires_at<=?
               )""",
            (
                JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value,
                JobStatus.RUNNING.value, current_time,
            ),
        ).fetchone()[0]
        return {
            "schema_version": 1,
            "jobs_by_status": jobs_by_status,
            "jobs_by_stage": jobs_by_stage,
            "jobs_total": int(totals["jobs_total"]),
            "active_leases": active_leases,
            "expired_leases": expired_leases,
            "oldest_runnable_age_seconds": (
                max(0.0, current_time - float(oldest)) if oldest is not None else 0.0
            ),
            "used_calls": int(totals["used_calls"]),
            "used_tokens": int(totals["used_tokens"]),
            "used_cost_microunits": int(totals["used_cost_microunits"]),
            "checkpoint_records": int(self.conn.execute(
                "SELECT COUNT(*) FROM checkpoints"
            ).fetchone()[0]),
            "usage_records": int(self.conn.execute(
                "SELECT COUNT(*) FROM usage_ledger"
            ).fetchone()[0]),
        }
