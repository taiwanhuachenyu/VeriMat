import sqlite3

import pytest

from src.operations.migrations import MigrationError
from src.operations.runtime_migrations import (
    ARTIFACT_SPEC, CIRCUIT_SPEC, MODEL_OPERATION_SPEC, POLICY_SPEC,
    RETRIEVAL_OPERATION_SPEC, DatabaseSpec, assert_runtime_compatibility,
    migrate_runtime_database, runtime_schema_status, schema_script,
    verify_runtime_database,
)
from src.orchestration.artifacts import ArtifactStore


RUNTIME_SPECS = (
    ARTIFACT_SPEC, POLICY_SPEC, MODEL_OPERATION_SPEC,
    RETRIEVAL_OPERATION_SPEC, CIRCUIT_SPEC,
)


@pytest.mark.parametrize("spec", RUNTIME_SPECS, ids=lambda spec: spec.kind)
def test_blank_runtime_database_migrates_with_identity_history_and_integrity(tmp_path, spec):
    path = tmp_path / f"{spec.kind}.db"
    status = migrate_runtime_database(path, spec)
    assert status["ready"] and status["integrity_verified"]
    assert status["database_kind"] == spec.kind
    assert status["application_id"] == spec.application_id
    assert status["current_version"] == status["latest_version"] == 1
    assert [row["version"] for row in status["history"]] == [1]


def test_wrong_kind_future_version_checksum_name_and_structure_are_refused(tmp_path):
    path = tmp_path / "artifact.db"
    migrate_runtime_database(path, ARTIFACT_SPEC)
    with pytest.raises(MigrationError, match="identity"):
        verify_runtime_database(path, POLICY_SPEC)

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_migrations SET checksum_sha256=?", ("0" * 64,))
    with pytest.raises(MigrationError, match="checksum"):
        verify_runtime_database(path, ARTIFACT_SPEC)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum_sha256=(SELECT checksum_sha256 FROM schema_migrations), name='changed'"
        )
    # Restore through a fresh database for each independent corruption class.
    name_path = tmp_path / "name.db"
    migrate_runtime_database(name_path, ARTIFACT_SPEC)
    with sqlite3.connect(name_path) as connection:
        connection.execute("UPDATE schema_migrations SET name='changed'")
    with pytest.raises(MigrationError, match="checksum mismatch"):
        verify_runtime_database(name_path, ARTIFACT_SPEC)

    future_path = tmp_path / "future.db"
    migrate_runtime_database(future_path, ARTIFACT_SPEC)
    with sqlite3.connect(future_path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(MigrationError, match="newer"):
        verify_runtime_database(future_path, ARTIFACT_SPEC)

    structure_path = tmp_path / "structure.db"
    migrate_runtime_database(structure_path, ARTIFACT_SPEC)
    with sqlite3.connect(structure_path) as connection:
        connection.execute("ALTER TABLE artifacts ADD COLUMN untracked TEXT")
    with pytest.raises(MigrationError, match="structure differs"):
        verify_runtime_database(structure_path, ARTIFACT_SPEC)


def test_legacy_adoption_is_explicit_and_preserves_anchor_data(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    path = root / "artifacts.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(schema_script(ARTIFACT_SPEC))
        connection.execute(
            "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)",
            ("tenant", "job", "key", "a" * 64, 4, "text/plain", 1.0),
        )
    status = runtime_schema_status(path, ARTIFACT_SPEC)
    assert status["legacy_adoption_required"] and not status["ready"]
    with pytest.raises(MigrationError, match="explicit adoption"):
        ArtifactStore(root)
    migrate_runtime_database(path, ARTIFACT_SPEC, allow_legacy_adoption=True)
    with ArtifactStore(root) as store:
        assert store.get_ref(
            tenant_id="tenant", job_id="job", logical_key="key"
        ).size_bytes == 4


def test_legacy_adoption_rejects_unknown_columns_and_business_tables(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(schema_script(ARTIFACT_SPEC))
        connection.execute("ALTER TABLE artifacts ADD COLUMN surprise TEXT")
    with pytest.raises(MigrationError, match="unexpected columns"):
        migrate_runtime_database(path, ARTIFACT_SPEC, allow_legacy_adoption=True)

    other = tmp_path / "other.db"
    with sqlite3.connect(other) as connection:
        connection.executescript(schema_script(ARTIFACT_SPEC))
        connection.execute("CREATE TABLE foreign_business_data(value TEXT)")
    with pytest.raises(MigrationError, match="unexpected business table"):
        migrate_runtime_database(other, ARTIFACT_SPEC, allow_legacy_adoption=True)


def test_forward_migration_preserves_rows_and_request_guard_detects_identity_change(tmp_path):
    v1 = DatabaseSpec(
        kind="fixture", application_id=0x474F4190,
        legacy_anchor_tables=frozenset({"items"}),
        migrations={1: ("CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY)",)},
    )
    v2 = DatabaseSpec(
        kind="fixture", application_id=v1.application_id,
        legacy_anchor_tables=v1.legacy_anchor_tables,
        migrations={
            1: v1.migrations[1],
            2: ("ALTER TABLE items ADD COLUMN label TEXT NOT NULL DEFAULT ''",),
        },
    )
    path = tmp_path / "forward.db"
    migrate_runtime_database(path, v1)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO items(id) VALUES ('preserved')")
    status = migrate_runtime_database(path, v2)
    assert status["current_version"] == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id,label FROM items").fetchone() == (
            "preserved", "",
        )
        connection.execute("PRAGMA application_id=0")
        with pytest.raises(MigrationError, match="changed after startup"):
            assert_runtime_compatibility(connection, v2)


def test_runtime_database_symlink_is_refused(tmp_path):
    real = tmp_path / "real.db"
    migrate_runtime_database(real, CIRCUIT_SPEC)
    link = tmp_path / "link.db"
    link.symlink_to(real)
    with pytest.raises(MigrationError, match="symbolic link"):
        verify_runtime_database(link, CIRCUIT_SPEC)
