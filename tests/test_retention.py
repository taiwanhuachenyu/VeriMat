import json
import sqlite3

import pytest

from src.evidence.ledger import EventLedger
from src.operations.retention import (
    RetentionError, build_retention_plan, execute_retention_plan,
)
from src.orchestration.artifacts import ArtifactStore
from src.orchestration.job_store import JobStatus, JobStore


def _fixture(tmp_path):
    jobs_path = tmp_path / "jobs.db"
    artifacts_root = tmp_path / "artifacts"
    ledgers_root = tmp_path / "ledgers"
    with JobStore(jobs_path) as jobs:
        old = jobs.create_job(
            tenant_id="tenant-a", idempotency_key="old", task="private old task",
            max_calls=0, max_tokens=0, max_cost_microunits=0, job_id="old-job",
        )
        jobs.transition(old.job_id, target=JobStatus.CANCELLED)
        survivor = jobs.create_job(
            tenant_id="tenant-a", idempotency_key="new", task="private new task",
            max_calls=0, max_tokens=0, max_cost_microunits=0, job_id="new-job",
        )
        jobs.transition(survivor.job_id, target=JobStatus.CANCELLED)
        foreign = jobs.create_job(
            tenant_id="tenant-b", idempotency_key="foreign", task="private foreign task",
            max_calls=0, max_tokens=0, max_cost_microunits=0, job_id="foreign-job",
        )
        jobs.transition(foreign.job_id, target=JobStatus.CANCELLED)
        jobs.conn.execute("UPDATE jobs SET updated_at=10 WHERE job_id='old-job'")
        jobs.conn.execute("UPDATE jobs SET updated_at=90 WHERE job_id!='old-job'")

    for tenant, job in (
        ("tenant-a", "old-job"), ("tenant-a", "new-job"),
        ("tenant-b", "foreign-job"),
    ):
        EventLedger(ledgers_root / tenant / job / "events.jsonl").append(
            tenant_id=tenant, job_id=job, aggregate_type="job", aggregate_id=job,
            event_type="CREATED", payload={"state": "fixture"},
            idempotency_key=f"create:{job}",
        )
    with ArtifactStore(artifacts_root) as artifacts:
        artifacts.put_bytes(
            tenant_id="tenant-a", job_id="old-job", logical_key="shared",
            content=b"shared", media_type="text/plain",
        )
        artifacts.put_bytes(
            tenant_id="tenant-a", job_id="new-job", logical_key="shared",
            content=b"shared", media_type="text/plain",
        )
        artifacts.put_bytes(
            tenant_id="tenant-a", job_id="old-job", logical_key="exclusive",
            content=b"exclusive", media_type="text/plain",
        )
        artifacts.put_bytes(
            tenant_id="tenant-b", job_id="foreign-job", logical_key="foreign",
            content=b"foreign", media_type="text/plain",
        )
    return jobs_path, artifacts_root, ledgers_root


def test_retention_plan_is_tenant_scoped_content_free_and_shared_blob_aware(tmp_path):
    jobs, artifacts, ledgers = _fixture(tmp_path)
    plan = build_retention_plan(
        job_database=jobs, artifact_root=artifacts, ledger_root=ledgers,
        tenant_id="tenant-a", cutoff_epoch=50, now_epoch=100,
    )
    assert [target["job_id"] for target in plan["targets"]] == ["old-job"]
    assert len(plan["targets"][0]["artifact_bindings"]) == 2
    assert len(plan["deletable_blobs"]) == 1
    rendered = json.dumps(plan)
    assert "private old task" not in rendered
    assert "idempotency" not in rendered
    assert "tenant-b" not in rendered


def test_retention_requires_exact_ack_and_refuses_stale_state_without_mutation(tmp_path):
    jobs, artifacts, ledgers = _fixture(tmp_path)
    plan = build_retention_plan(
        job_database=jobs, artifact_root=artifacts, ledger_root=ledgers,
        tenant_id="tenant-a", cutoff_epoch=50, now_epoch=100,
    )
    with pytest.raises(RetentionError, match="acknowledgement"):
        execute_retention_plan(
            plan=plan, acknowledged_sha256="0" * 64, job_database=jobs,
            artifact_root=artifacts, ledger_root=ledgers,
            maintenance_root=tmp_path / "maintenance", audit_log=tmp_path / "audit.jsonl",
        )
    assert (ledgers / "tenant-a" / "old-job" / "events.jsonl").is_file()

    with sqlite3.connect(artifacts / "artifacts.db") as connection:
        connection.execute(
            """INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)""",
            ("tenant-a", "old-job", "late", "f" * 64, 1, "text/plain", 11),
        )
    with pytest.raises(RetentionError, match="bindings changed"):
        execute_retention_plan(
            plan=plan, acknowledged_sha256=plan["plan_sha256"], job_database=jobs,
            artifact_root=artifacts, ledger_root=ledgers,
            maintenance_root=tmp_path / "maintenance", audit_log=tmp_path / "audit.jsonl",
        )
    assert (ledgers / "tenant-a" / "old-job" / "events.jsonl").is_file()
    with sqlite3.connect(jobs) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_id='old-job'"
        ).fetchone()[0] == 1


def test_execute_retention_deletes_exact_target_and_preserves_shared_and_foreign_data(tmp_path):
    jobs, artifacts, ledgers = _fixture(tmp_path)
    plan = build_retention_plan(
        job_database=jobs, artifact_root=artifacts, ledger_root=ledgers,
        tenant_id="tenant-a", cutoff_epoch=50, now_epoch=100,
    )
    receipt = execute_retention_plan(
        plan=plan, acknowledged_sha256=plan["plan_sha256"], job_database=jobs,
        artifact_root=artifacts, ledger_root=ledgers,
        maintenance_root=tmp_path / "maintenance", audit_log=tmp_path / "audit.jsonl",
    )
    assert receipt["deleted_jobs"] == receipt["deleted_ledgers"] == 1
    assert receipt["deleted_artifact_bindings"] == 2
    assert receipt["deleted_exclusive_blobs"] == 1
    assert receipt["secure_delete_enabled"]
    assert receipt["wal_checkpoint_complete"]
    assert receipt["external_api_calls"] == 0

    with sqlite3.connect(jobs) as connection:
        assert connection.execute("SELECT job_id FROM jobs ORDER BY job_id").fetchall() == [
            ("foreign-job",), ("new-job",),
        ]
    with ArtifactStore(artifacts) as store:
        shared = store.get_ref(
            tenant_id="tenant-a", job_id="new-job", logical_key="shared",
        )
        assert store.read_bytes(shared) == b"shared"
        foreign = store.get_ref(
            tenant_id="tenant-b", job_id="foreign-job", logical_key="foreign",
        )
        assert store.read_bytes(foreign) == b"foreign"
    assert not (ledgers / "tenant-a" / "old-job" / "events.jsonl").exists()
    assert (ledgers / "tenant-a" / "new-job" / "events.jsonl").is_file()
    assert (ledgers / "tenant-b" / "foreign-job" / "events.jsonl").is_file()
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "tenant-a" not in audit and "old-job" not in audit and "private" not in audit
    audit_events = [json.loads(line)["event_type"] for line in audit.splitlines()]
    assert audit_events == ["RETENTION_PREPARED", "RETENTION_COMPLETED"]


def test_retention_plan_ignores_nonterminal_old_jobs(tmp_path):
    jobs, artifacts, ledgers = _fixture(tmp_path)
    with sqlite3.connect(jobs) as connection:
        connection.execute(
            """INSERT INTO jobs
               (job_id,tenant_id,idempotency_key,task,status,stage,max_calls,max_tokens,
                max_cost_microunits,created_at,updated_at)
               VALUES ('active','tenant-a','active','active task','RUNNING','PLAN',0,0,0,1,1)"""
        )
    plan = build_retention_plan(
        job_database=jobs, artifact_root=artifacts, ledger_root=ledgers,
        tenant_id="tenant-a", cutoff_epoch=50, now_epoch=100,
    )
    assert [target["job_id"] for target in plan["targets"]] == ["old-job"]
