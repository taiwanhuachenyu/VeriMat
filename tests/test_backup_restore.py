import json
import os

import pytest

from src.evidence.ledger import EventLedger
from src.operations.backup import BackupError, create_backup, restore_backup, verify_backup
from src.orchestration.artifacts import ArtifactStore
from src.orchestration.job_store import JobStore


def _runtime(tmp_path):
    job_database = tmp_path / "runtime" / "control" / "jobs.db"
    ledger_root = tmp_path / "runtime" / "ledgers"
    artifact_root = tmp_path / "runtime" / "artifacts"
    with JobStore(job_database) as jobs:
        job = jobs.create_job(
            tenant_id="tenant-a", idempotency_key="request-1", task="private task",
            max_calls=4, max_tokens=100, max_cost_microunits=20,
        )
        jobs.charge(
            job.job_id, charge_key="call-1", provider="provider",
            calls=1, tokens=7, cost_microunits=2,
        )
    ledger = EventLedger(ledger_root / "tenant-a" / job.job_id / "events.jsonl")
    ledger.append(
        tenant_id="tenant-a", job_id=job.job_id, aggregate_type="job",
        aggregate_id=job.job_id, event_type="job.created", payload={"version": 1},
        idempotency_key="created",
    )
    with ArtifactStore(artifact_root) as artifacts:
        ref = artifacts.put_json(
            tenant_id="tenant-a", job_id=job.job_id, logical_key="result",
            value={"decision": "UNRESOLVED"},
        )
    return job_database, ledger_root, artifact_root, job, ref


def test_verified_backup_restores_database_ledgers_and_artifacts(tmp_path):
    job_database, ledgers, artifacts, job, ref = _runtime(tmp_path)
    backup = tmp_path / "backup"
    manifest = create_backup(
        job_database=job_database, ledger_root=ledgers,
        artifact_root=artifacts, output=backup,
    )
    assert manifest["summary"] == {
        "control_databases": 1, "artifact_databases": 1,
        "artifact_blobs": 1, "event_ledgers": 1, "ledger_events": 1,
    }
    assert verify_backup(backup)["verified"]

    restored = tmp_path / "restored"
    receipt = restore_backup(backup=backup, target_root=restored)
    assert receipt["backup_id"] == manifest["backup_id"]
    with JobStore(restored / "control" / "jobs.db") as jobs:
        assert jobs.get(job.job_id, tenant_id="tenant-a").used_tokens == 7
    assert EventLedger(
        restored / "ledgers" / "tenant-a" / job.job_id / "events.jsonl"
    ).verify().ok
    with ArtifactStore(restored / "artifacts") as store:
        assert store.read_json(ref) == {"decision": "UNRESOLVED"}
    assert json.loads((restored / "restore_receipt.json").read_text())["verified"]


def test_the_whole_cycle_works_where_windows_would_refuse_the_path(tmp_path):
    """Guards the fix independently of how long the runner's temporary directory happens to be."""
    deep = tmp_path.joinpath(*("nested" + "d" * 60,) * 4)
    assert len(str(deep)) > 260
    # The tree is deliberately not created here: every store has to build its own root
    # from a path this long, which is the step that failed before the conversion.
    job_database, ledgers, artifacts, job, ref = _runtime(deep)
    backup = deep / "backup"
    manifest = create_backup(
        job_database=job_database, ledger_root=ledgers,
        artifact_root=artifacts, output=backup,
    )
    recorded = [entry["path"] for entry in manifest["files"]]
    assert recorded and not any("?" in path or "\\" in path for path in recorded), (
        "the long-path prefix must never reach a recorded manifest path"
    )
    assert verify_backup(backup)["verified"]
    restored = deep / "restored"
    restore_backup(backup=backup, target_root=restored)
    with ArtifactStore(restored / "artifacts") as store:
        assert store.read_json(ref) == {"decision": "UNRESOLVED"}
    with JobStore(restored / "control" / "jobs.db") as jobs:
        assert jobs.get(job.job_id, tenant_id="tenant-a").used_tokens == 7


def test_backup_tampering_and_overwrite_are_rejected(tmp_path):
    job_database, ledgers, artifacts, *_ = _runtime(tmp_path)
    backup = tmp_path / "backup"
    create_backup(
        job_database=job_database, ledger_root=ledgers,
        artifact_root=artifacts, output=backup,
    )
    ledger = next((backup / "ledgers").rglob("*.jsonl"))
    ledger.write_text(ledger.read_text() + "{}\n", encoding="utf-8")
    with pytest.raises(BackupError, match="hash or size mismatch"):
        verify_backup(backup)
    with pytest.raises(BackupError, match="already exists"):
        create_backup(
            job_database=job_database, ledger_root=ledgers,
            artifact_root=artifacts, output=backup,
        )


def test_backup_rejects_symlinked_sources(tmp_path):
    job_database, ledgers, artifacts, *_ = _runtime(tmp_path)
    os.symlink("/tmp", ledgers / "unsafe-link")
    with pytest.raises(BackupError, match="symbolic links"):
        create_backup(
            job_database=job_database, ledger_root=ledgers,
            artifact_root=artifacts, output=tmp_path / "backup",
        )
