import sqlite3

import pytest

from src.operations.migrations import (
    CONTROL_APPLICATION_ID, CONTROL_MIGRATIONS, MigrationError,
    assert_control_compatibility,
    migrate_control_database, migration_checksum, schema_status,
    verify_control_database,
)
from src.orchestration.job_store import JobStore


def test_blank_database_migrates_transactionally_and_records_checksum(tmp_path):
    path = tmp_path / "jobs.db"
    status = migrate_control_database(path)
    assert status["ready"] and status["current_version"] == 1
    assert status["application_id"] == CONTROL_APPLICATION_ID
    assert status["history"][0]["checksum_sha256"] == migration_checksum(1)
    assert verify_control_database(path)["integrity_verified"]
    with JobStore(path) as store:
        store.create_job(
            tenant_id="tenant", idempotency_key="key", task="task",
            max_calls=1, max_tokens=1, max_cost_microunits=1,
        )


def test_unknown_future_version_is_refused(tmp_path):
    path = tmp_path / "jobs.db"
    migrate_control_database(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=999")
    connection.close()
    with pytest.raises(MigrationError, match="newer"):
        schema_status(path)
    with pytest.raises(MigrationError):
        JobStore(path)


def test_migration_checksum_tampering_is_refused(tmp_path):
    path = tmp_path / "jobs.db"
    migrate_control_database(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE schema_migrations SET checksum_sha256=? WHERE version=1", ("0" * 64,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(MigrationError, match="checksum mismatch"):
        verify_control_database(path)


def test_exact_legacy_schema_requires_explicit_adoption(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    for statement in CONTROL_MIGRATIONS[1]:
        connection.execute(statement)
    connection.commit()
    connection.close()
    assert schema_status(path)["legacy_adoption_required"]
    with pytest.raises(MigrationError, match="explicit"):
        migrate_control_database(path)
    adopted = migrate_control_database(path, allow_legacy_adoption=True)
    assert adopted["ready"] and adopted["history"][0]["version"] == 1


def test_structurally_ambiguous_legacy_schema_is_not_adopted(tmp_path):
    path = tmp_path / "ambiguous.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(MigrationError, match="unexpected columns"):
        schema_status(path)


def test_request_path_compatibility_guard_rejects_hot_swapped_version(tmp_path):
    path = tmp_path / "jobs.db"
    migrate_control_database(path)
    connection = sqlite3.connect(path)
    assert_control_compatibility(connection)
    connection.execute("PRAGMA user_version=2")
    with pytest.raises(MigrationError, match="changed"):
        assert_control_compatibility(connection)
    connection.close()
