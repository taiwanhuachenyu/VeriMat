"""Version identity and forward-only migrations for non-control runtime databases."""
from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.events import canonical_json
from src.core.portability import extended_path
from src.operations.migrations import MIGRATION_TABLE_SQL, MigrationError


@dataclass(frozen=True)
class DatabaseSpec:
    kind: str
    application_id: int
    migrations: dict[int, tuple[str, ...]]
    legacy_anchor_tables: frozenset[str]

    @property
    def latest_version(self) -> int:
        versions = sorted(self.migrations)
        if versions != list(range(1, max(versions) + 1)):
            raise MigrationError(f"{self.kind} migration definitions are not contiguous")
        return max(versions)


ARTIFACT_SPEC = DatabaseSpec(
    kind="artifact", application_id=0x474F4101,
    legacy_anchor_tables=frozenset({"artifacts"}),
    migrations={1: (
        """CREATE TABLE IF NOT EXISTS artifacts (
            tenant_id TEXT NOT NULL, job_id TEXT NOT NULL, logical_key TEXT NOT NULL,
            content_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            media_type TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(tenant_id, job_id, logical_key)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_artifact_content
            ON artifacts(tenant_id, content_sha256)""",
    )},
)

POLICY_SPEC = DatabaseSpec(
    kind="policy", application_id=0x474F4102,
    legacy_anchor_tables=frozenset({"strategies"}),
    migrations={1: (
        """CREATE TABLE IF NOT EXISTS strategies (
            strategy_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
            strategy_key TEXT NOT NULL, kind TEXT NOT NULL, pattern TEXT NOT NULL,
            source_job_id TEXT NOT NULL, source_task_family TEXT NOT NULL,
            status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(tenant_id, strategy_key)
        )""",
        """CREATE TABLE IF NOT EXISTS applications (
            application_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(strategy_id),
            tenant_id TEXT NOT NULL, target_job_id TEXT NOT NULL,
            target_task_family TEXT NOT NULL, rendered_query_sha256 TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(tenant_id, idempotency_key)
        )""",
        """CREATE TABLE IF NOT EXISTS outcomes (
            application_id TEXT PRIMARY KEY REFERENCES applications(application_id),
            evaluator_kind TEXT NOT NULL, success INTEGER NOT NULL,
            false_gap_avoided INTEGER NOT NULL, valid_finding_delta REAL NOT NULL,
            calls INTEGER NOT NULL, tokens INTEGER NOT NULL,
            evidence_ref TEXT NOT NULL, recorded_at REAL NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_applications_strategy
            ON applications(strategy_id)""",
        """CREATE INDEX IF NOT EXISTS idx_strategies_tenant_status
            ON strategies(tenant_id, status)""",
        """CREATE TABLE IF NOT EXISTS policy_outbox (
            event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, event_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, payload TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, created_at REAL NOT NULL, dispatched_at REAL,
            UNIQUE(tenant_id, idempotency_key)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_policy_outbox_pending
            ON policy_outbox(tenant_id, dispatched_at, created_at)""",
        """CREATE TABLE IF NOT EXISTS sequence_interventions (
            tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, method_id TEXT NOT NULL,
            sequence_index INTEGER NOT NULL, challenge_id TEXT NOT NULL,
            order_sha256 TEXT NOT NULL, memory_mode TEXT NOT NULL,
            strategy_ids TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(tenant_id, run_id, method_id, sequence_index),
            UNIQUE(tenant_id, run_id, method_id, challenge_id)
        )""",
    )},
)

RECONCILIATION_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS operation_reconciliations (
        reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_kind TEXT NOT NULL, operation_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, prior_status TEXT NOT NULL,
        action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
        evidence_receipt_sha256 TEXT NOT NULL, reconciled_at REAL NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS operation_reconciliations_operation
        ON operation_reconciliations(operation_kind, operation_id, reconciliation_id)""",
)

CIRCUIT_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS circuit_breakers (
        circuit_id TEXT PRIMARY KEY, state TEXT NOT NULL,
        consecutive_failures INTEGER NOT NULL, opened_at REAL,
        probe_operation_id TEXT, probe_started_at REAL, updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS circuit_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, circuit_id TEXT NOT NULL,
        operation_id TEXT NOT NULL, event_type TEXT NOT NULL, prior_state TEXT NOT NULL,
        next_state TEXT NOT NULL, reason_code TEXT NOT NULL, occurred_at REAL NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS circuit_events_circuit
        ON circuit_events(circuit_id, event_id)""",
)

MODEL_OPERATION_SPEC = DatabaseSpec(
    kind="model_operation", application_id=0x474F4103,
    legacy_anchor_tables=frozenset({"model_operations"}),
    migrations={1: (
        """CREATE TABLE IF NOT EXISTS model_operations (
            operation_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
            status TEXT NOT NULL, response_text TEXT, input_tokens INTEGER,
            output_tokens INTEGER, request_id TEXT, created_at REAL NOT NULL,
            completed_at REAL
        )""",
        *RECONCILIATION_STATEMENTS, *CIRCUIT_STATEMENTS,
    )},
)

RETRIEVAL_OPERATION_SPEC = DatabaseSpec(
    kind="retrieval_operation", application_id=0x474F4104,
    legacy_anchor_tables=frozenset({"retrieval_operations"}),
    migrations={1: (
        """CREATE TABLE IF NOT EXISTS retrieval_operations (
            operation_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
            status TEXT NOT NULL, response_json TEXT, created_at REAL NOT NULL,
            completed_at REAL
        )""",
        *RECONCILIATION_STATEMENTS, *CIRCUIT_STATEMENTS,
    )},
)

CIRCUIT_SPEC = DatabaseSpec(
    kind="circuit", application_id=0x474F4105,
    legacy_anchor_tables=frozenset({"circuit_breakers"}),
    migrations={1: CIRCUIT_STATEMENTS},
)

SPECS = {
    spec.kind: spec for spec in (
        ARTIFACT_SPEC, POLICY_SPEC, MODEL_OPERATION_SPEC,
        RETRIEVAL_OPERATION_SPEC, CIRCUIT_SPEC,
    )
}


def schema_script(spec: DatabaseSpec) -> str:
    """Compatibility rendering for tools that need the authoritative latest DDL."""
    return ";\n".join(
        statement.strip() for version in range(1, spec.latest_version + 1)
        for statement in spec.migrations[version]
    ) + ";\n"


def migration_checksum(spec: DatabaseSpec, version: int) -> str:
    return hashlib.sha256(canonical_json({
        "database": spec.kind, "version": version,
        "statements": spec.migrations[version],
    }).encode()).hexdigest()


def migration_name(spec: DatabaseSpec, version: int) -> str:
    return f"initial_{spec.kind}_schema" if version == 1 else f"{spec.kind}_v{version}"


def _connect(path: str | Path, *, must_exist: bool = False) -> sqlite3.Connection:
    target = extended_path(path)
    if target.is_symlink():
        raise MigrationError(f"database path must not be a symbolic link: {target}")
    if must_exist and not target.is_file():
        raise MigrationError(f"database does not exist or is unsafe: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _business_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%'
                 AND name!='schema_migrations'"""
        ).fetchall()
    }


def _history(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "schema_migrations" not in tables:
        return []
    return [dict(row) for row in connection.execute(
        "SELECT version,name,checksum_sha256,applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()]


def _validate_migration_table(connection: sqlite3.Connection) -> None:
    columns = [tuple(row) for row in connection.execute(
        'PRAGMA table_info("schema_migrations")'
    ).fetchall()]
    expected = [
        (0, "version", "INTEGER", 0, None, 1),
        (1, "name", "TEXT", 1, None, 0),
        (2, "checksum_sha256", "TEXT", 1, None, 0),
        (3, "applied_at", "REAL", 1, None, 0),
    ]
    if columns != expected:
        raise MigrationError("schema_migrations table structure is invalid")


def _signature(connection: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for table in sorted(_business_tables(connection)):
        columns = [tuple(row) for row in connection.execute(
            f'PRAGMA table_info("{table}")'
        ).fetchall()]
        foreign_keys = [tuple(row) for row in connection.execute(
            f'PRAGMA foreign_key_list("{table}")'
        ).fetchall()]
        indexes = []
        for index_row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
            index_name = str(index_row[1])
            indexes.append({
                "name": index_name,
                "unique": int(index_row[2]),
                "origin": str(index_row[3]),
                "partial": int(index_row[4]),
                "columns": [tuple(item) for item in connection.execute(
                    f'PRAGMA index_info("{index_name}")'
                ).fetchall()],
            })
        result[table] = {
            "columns": columns, "foreign_keys": foreign_keys,
            "indexes": sorted(indexes, key=lambda value: value["name"]),
        }
    return result


def _expected_signature(spec: DatabaseSpec, version: int) -> dict[str, Any]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        for current in range(1, version + 1):
            for statement in spec.migrations[current]:
                connection.execute(statement)
        return _signature(connection)
    finally:
        connection.close()


def _verify_structure(connection: sqlite3.Connection, spec: DatabaseSpec, version: int) -> None:
    actual = _signature(connection)
    expected = _expected_signature(spec, version)
    if actual != expected:
        raise MigrationError(
            f"{spec.kind} database structure differs from its versioned migration definition"
        )


def runtime_schema_status(
    path: str | Path, spec: DatabaseSpec, *, must_exist: bool = True,
) -> dict[str, Any]:
    connection = _connect(path, must_exist=must_exist)
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables, history = _business_tables(connection), _history(connection)
        latest = spec.latest_version
        if application_id not in {0, spec.application_id}:
            raise MigrationError(
                f"database identity does not match expected kind {spec.kind}"
            )
        if user_version > latest:
            raise MigrationError(
                f"{spec.kind} database version {user_version} is newer than supported {latest}"
            )
        if history:
            _validate_migration_table(connection)
            versions = [int(row["version"]) for row in history]
            if versions != list(range(1, user_version + 1)):
                raise MigrationError("migration history is non-contiguous or disagrees with version")
            for row in history:
                version = int(row["version"])
                if (
                    version not in spec.migrations
                    or row["checksum_sha256"] != migration_checksum(spec, version)
                    or row["name"] != migration_name(spec, version)
                    or not math.isfinite(float(row["applied_at"]))
                    or float(row["applied_at"]) <= 0
                ):
                    raise MigrationError(f"migration checksum mismatch at version {version}")
        elif user_version:
            raise MigrationError("versioned database has no migration history")
        blank = not tables and user_version == 0 and application_id == 0
        legacy = bool(tables & spec.legacy_anchor_tables) and user_version == 0
        if tables and not legacy and user_version == 0:
            raise MigrationError(f"unversioned database is not an adoptable {spec.kind} schema")
        if legacy:
            expected = _expected_signature(spec, latest)
            for table in tables:
                if table not in expected:
                    raise MigrationError("legacy database contains an unexpected business table")
                if _signature(connection)[table]["columns"] != expected[table]["columns"]:
                    raise MigrationError(f"legacy table {table!r} has unexpected columns")
        if user_version:
            _verify_structure(connection, spec, user_version)
        pending = list(range(user_version + 1, latest + 1))
        return {
            "schema_version": 1, "database_kind": spec.kind,
            "application_id": application_id, "current_version": user_version,
            "latest_version": latest, "blank": blank,
            "legacy_adoption_required": legacy,
            "pending_versions": pending if not legacy else [],
            "history": history,
            "ready": (
                application_id == spec.application_id and user_version == latest
                and len(history) == latest
            ),
        }
    finally:
        connection.close()


def migrate_runtime_database(
    path: str | Path, spec: DatabaseSpec, *, allow_legacy_adoption: bool = False,
) -> dict[str, Any]:
    status = runtime_schema_status(path, spec, must_exist=False)
    if status["ready"]:
        return verify_runtime_database(path, spec)
    if status["legacy_adoption_required"] and not allow_legacy_adoption:
        raise MigrationError(
            f"legacy {spec.kind} schema requires explicit adoption after backup"
        )
    versioned_pending = (
        status["application_id"] == spec.application_id
        and 0 < status["current_version"] < spec.latest_version
    )
    if not status["blank"] and not status["legacy_adoption_required"] and not versioned_pending:
        raise MigrationError(f"{spec.kind} database is not safely migratable")
    connection = _connect(path)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute(MIGRATION_TABLE_SQL)
        # All V1 statements are idempotent, allowing explicit adoption to add ancillary indexes
        # and tables while preserving anchor-table data.
        first_version = (
            1 if status["blank"] or status["legacy_adoption_required"]
            else int(status["current_version"]) + 1
        )
        for version in range(first_version, spec.latest_version + 1):
            for statement in spec.migrations[version]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                (version, migration_name(spec, version),
                 migration_checksum(spec, version), time.time()),
            )
            connection.execute(f"PRAGMA user_version={version}")
        connection.execute(f"PRAGMA application_id={spec.application_id}")
        _verify_structure(connection, spec, spec.latest_version)
        connection.execute("COMMIT")
        connection.execute("PRAGMA journal_mode=WAL")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return verify_runtime_database(path, spec)


def verify_runtime_database(path: str | Path, spec: DatabaseSpec) -> dict[str, Any]:
    status = runtime_schema_status(path, spec)
    if not status["ready"]:
        raise MigrationError(f"{spec.kind} database is not at the supported schema version")
    connection = _connect(path, must_exist=True)
    try:
        quick = connection.execute("PRAGMA quick_check(1)").fetchone()
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick is None or str(quick[0]).lower() != "ok" or foreign:
            raise MigrationError(f"{spec.kind} database integrity check failed")
    finally:
        connection.close()
    return {**status, "integrity_verified": True, "foreign_keys_verified": True}


def prepare_runtime_database(path: str | Path, spec: DatabaseSpec) -> dict[str, Any]:
    """Create a blank database, or fully verify an existing versioned database."""
    status = runtime_schema_status(path, spec, must_exist=False)
    if status["blank"]:
        return migrate_runtime_database(path, spec)
    if status["legacy_adoption_required"]:
        raise MigrationError(
            f"legacy {spec.kind} schema requires explicit adoption after backup"
        )
    return verify_runtime_database(path, spec)


def assert_runtime_compatibility(connection: sqlite3.Connection, spec: DatabaseSpec) -> None:
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id != spec.application_id or version != spec.latest_version:
        raise MigrationError(f"{spec.kind} database identity or version changed after startup")
