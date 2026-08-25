"""Audited, forward-only schema migrations for trusted runtime databases."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.core.events import canonical_json
from src.core.portability import extended_path

CONTROL_APPLICATION_ID = 0x474F4149  # ASCII "GOAI"
CONTROL_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE jobs (
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
        )""",
        """CREATE TABLE checkpoints (
            job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
            stage TEXT NOT NULL,
            checkpoint_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY(job_id, checkpoint_key)
        )""",
        """CREATE TABLE usage_ledger (
            job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
            charge_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            calls INTEGER NOT NULL,
            tokens INTEGER NOT NULL,
            cost_microunits INTEGER NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY(job_id, charge_key)
        )""",
        """CREATE INDEX idx_jobs_runnable
            ON jobs(status, lease_expires_at, created_at)""",
    ),
}
REQUIRED_CONTROL_COLUMNS = {
    "jobs": {
        "job_id", "tenant_id", "idempotency_key", "task", "status", "stage", "version",
        "lease_owner", "lease_expires_at", "attempts", "max_calls", "max_tokens",
        "max_cost_microunits", "used_calls", "used_tokens", "used_cost_microunits",
        "last_error_code", "created_at", "updated_at",
    },
    "checkpoints": {"job_id", "stage", "checkpoint_key", "payload", "created_at"},
    "usage_ledger": {
        "job_id", "charge_key", "provider", "calls", "tokens", "cost_microunits",
        "created_at",
    },
}
MIGRATION_TABLE_SQL = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    applied_at REAL NOT NULL
)"""


class MigrationError(RuntimeError):
    """Raised when schema identity, history, or compatibility cannot be proven."""


def migration_checksum(version: int) -> str:
    statements = CONTROL_MIGRATIONS[version]
    return hashlib.sha256(canonical_json({
        "database": "control", "version": version, "statements": statements,
    }).encode()).hexdigest()


def _connect(path: str | Path, *, must_exist: bool = False) -> sqlite3.Connection:
    target = extended_path(path)
    if must_exist and not target.is_file():
        raise MigrationError(f"database does not exist: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _validate_structure(connection: sqlite3.Connection) -> None:
    present = _tables(connection)
    for table, required in REQUIRED_CONTROL_COLUMNS.items():
        if table not in present:
            raise MigrationError(f"required control table is missing: {table}")
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if columns != required:
            raise MigrationError(
                f"control table {table!r} has unexpected columns; refusing automatic adoption"
            )


def _history(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    if "schema_migrations" not in _tables(connection):
        return []
    return [dict(row) for row in connection.execute(
        "SELECT version,name,checksum_sha256,applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()]


def schema_status(path: str | Path, *, must_exist: bool = True) -> dict[str, Any]:
    connection = _connect(path, must_exist=must_exist)
    try:
        latest = max(CONTROL_MIGRATIONS)
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = _tables(connection)
        history = _history(connection)
        if application_id not in {0, CONTROL_APPLICATION_ID}:
            raise MigrationError("database application_id belongs to another application")
        if user_version > latest:
            raise MigrationError(
                f"database schema version {user_version} is newer than supported {latest}"
            )
        if history:
            expected_versions = list(range(1, user_version + 1))
            if [int(row["version"]) for row in history] != expected_versions:
                raise MigrationError("migration history is non-contiguous or disagrees with user_version")
            for row in history:
                version = int(row["version"])
                if version not in CONTROL_MIGRATIONS or row["checksum_sha256"] != migration_checksum(version):
                    raise MigrationError(f"migration checksum mismatch at version {version}")
        elif user_version != 0:
            raise MigrationError("versioned database has no migration history")
        legacy = bool(tables & set(REQUIRED_CONTROL_COLUMNS)) and user_version == 0
        if legacy:
            _validate_structure(connection)
        if user_version:
            _validate_structure(connection)
        pending = list(range(user_version + 1, latest + 1)) if not legacy else []
        return {
            "schema_version": 1, "database_kind": "control",
            "application_id": application_id, "current_version": user_version,
            "latest_version": latest, "legacy_adoption_required": legacy,
            "pending_versions": pending,
            "history": history,
            "ready": user_version == latest and application_id == CONTROL_APPLICATION_ID,
        }
    finally:
        connection.close()


def migrate_control_database(
    path: str | Path, *, allow_legacy_adoption: bool = False,
) -> dict[str, Any]:
    """Apply all pending migrations, or explicitly adopt a structurally exact legacy V1 DB."""
    connection = _connect(path)
    try:
        latest = max(CONTROL_MIGRATIONS)
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = _tables(connection)
        if application_id not in {0, CONTROL_APPLICATION_ID}:
            raise MigrationError("database application_id belongs to another application")
        if user_version > latest:
            raise MigrationError("database schema is newer than this runtime")
        legacy = bool(tables & set(REQUIRED_CONTROL_COLUMNS)) and user_version == 0
        if legacy and not allow_legacy_adoption:
            raise MigrationError(
                "legacy control schema requires explicit allow_legacy_adoption after backup"
            )
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute(MIGRATION_TABLE_SQL)
        if legacy:
            _validate_structure(connection)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                (1, "initial_control_schema", migration_checksum(1), time.time()),
            )
            connection.execute(f"PRAGMA application_id={CONTROL_APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
        else:
            for version in range(user_version + 1, latest + 1):
                for statement in CONTROL_MIGRATIONS[version]:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                    (
                        version, "initial_control_schema" if version == 1 else f"control_v{version}",
                        migration_checksum(version), time.time(),
                    ),
                )
                connection.execute(f"PRAGMA user_version={version}")
            connection.execute(f"PRAGMA application_id={CONTROL_APPLICATION_ID}")
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    status = schema_status(path)
    if not status["ready"]:
        raise MigrationError("control database is not ready after migration")
    return status


def verify_control_database(path: str | Path) -> dict[str, Any]:
    status = schema_status(path)
    if not status["ready"]:
        raise MigrationError("control database has unapplied or unadopted schema changes")
    connection = _connect(path, must_exist=True)
    try:
        quick = connection.execute("PRAGMA quick_check(1)").fetchone()
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick is None or str(quick[0]).lower() != "ok" or foreign:
            raise MigrationError("control database integrity or foreign-key check failed")
    finally:
        connection.close()
    return {**status, "integrity_verified": True, "foreign_keys_verified": True}


def assert_control_compatibility(connection: sqlite3.Connection) -> None:
    """Constant-time request-path guard after a process-level full startup verification."""
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id != CONTROL_APPLICATION_ID or user_version != max(CONTROL_MIGRATIONS):
        raise MigrationError(
            "control database identity or schema version changed after startup verification"
        )
