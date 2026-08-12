import json

import pytest

from src.orchestration.job_store import (
    BudgetExceeded,
    IdempotencyConflict,
    IllegalTransition,
    JobStatus,
    JobStore,
    JobStoreError,
    LeaseConflict,
    Stage,
)


def _create(store, *, key="request-1", task="research LLZO"):
    return store.create_job(
        tenant_id="tenant-a", idempotency_key=key, task=task,
        max_calls=10, max_tokens=1000, max_cost_microunits=500,
    )


def test_create_job_is_idempotent_and_tenant_scoped(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    first = _create(store)
    replay = _create(store)
    assert replay.job_id == first.job_id
    with pytest.raises(IdempotencyConflict):
        _create(store, task="different task")
    with pytest.raises(JobStoreError):
        store.get(first.job_id, tenant_id="tenant-b")


def test_lease_prevents_concurrent_workers_and_allows_expiry_recovery(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store)
    leased = store.acquire_lease(job.job_id, worker_id="worker-1", lease_seconds=10, now=100)
    assert leased.status == JobStatus.RUNNING
    with pytest.raises(LeaseConflict):
        store.acquire_lease(job.job_id, worker_id="worker-2", lease_seconds=10, now=105)
    recovered = store.acquire_lease(
        job.job_id, worker_id="worker-2", lease_seconds=10, now=111
    )
    assert recovered.lease_owner == "worker-2"
    assert recovered.attempts == 2


def test_checkpoint_is_idempotent_and_cannot_move_backwards(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store)
    store.acquire_lease(job.job_id, worker_id="worker", lease_seconds=100, now=10)
    store.save_checkpoint(
        job.job_id, worker_id="worker", stage=Stage.RETRIEVE,
        checkpoint_key="retrieval-1", payload={"documents": 3}, now=20,
    )
    store.save_checkpoint(
        job.job_id, worker_id="worker", stage=Stage.RETRIEVE,
        checkpoint_key="retrieval-1", payload={"documents": 3}, now=21,
    )
    assert len(store.checkpoints(job.job_id)) == 1
    with pytest.raises(IdempotencyConflict):
        store.save_checkpoint(
            job.job_id, worker_id="worker", stage=Stage.RETRIEVE,
            checkpoint_key="retrieval-1", payload={"documents": 4}, now=22,
        )
    with pytest.raises(IllegalTransition):
        store.save_checkpoint(
            job.job_id, worker_id="worker", stage=Stage.PLAN,
            checkpoint_key="late-plan", payload={}, now=23,
        )


def test_usage_ledger_is_idempotent_and_enforces_hard_budget(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store)
    charged = store.charge(
        job.job_id, charge_key="provider-call-1", provider="provider",
        calls=2, tokens=400, cost_microunits=100,
    )
    replay = store.charge(
        job.job_id, charge_key="provider-call-1", provider="provider",
        calls=2, tokens=400, cost_microunits=100,
    )
    assert replay.used_calls == charged.used_calls == 2
    with pytest.raises(BudgetExceeded, match="tokens"):
        store.charge(
            job.job_id, charge_key="provider-call-2", provider="provider",
            calls=1, tokens=700, cost_microunits=1,
        )
    assert store.get(job.job_id).used_tokens == 400


def test_success_requires_validate_checkpoint_and_legal_status_path(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store)
    leased = store.acquire_lease(job.job_id, worker_id="worker", lease_seconds=100, now=10)
    with pytest.raises(IllegalTransition):
        store.transition(job.job_id, target=JobStatus.SUCCEEDED)
    store.save_checkpoint(
        job.job_id, worker_id="worker", stage=Stage.VALIDATE,
        checkpoint_key="validation", payload={"ok": True}, now=20,
    )
    validating = store.transition(
        job.job_id, target=JobStatus.VALIDATING, expected_version=store.get(job.job_id).version,
        release_lease=False,
    )
    succeeded = store.transition(
        job.job_id, target=JobStatus.SUCCEEDED, expected_version=validating.version,
    )
    assert succeeded.status == JobStatus.SUCCEEDED
    with pytest.raises(IllegalTransition):
        store.transition(job.job_id, target=JobStatus.RUNNING)


def test_readiness_and_operational_snapshot_are_aggregate_only(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    queued = _create(store, key="secret-key", task="secret task")
    running = _create(store, key="running", task="another secret")
    store.acquire_lease(
        running.job_id, worker_id="worker-secret", lease_seconds=10, now=100,
    )
    store.charge(
        queued.job_id, charge_key="charge", provider="private-provider",
        calls=2, tokens=12, cost_microunits=7, now=100,
    )
    assert store.readiness_check() == {
        "ready": True, "storage": "sqlite", "journal_mode": "wal",
    }
    snapshot = store.operational_snapshot(now=111)
    assert snapshot["jobs_total"] == 2
    assert snapshot["jobs_by_status"]["QUEUED"] == 1
    assert snapshot["jobs_by_status"]["RUNNING"] == 1
    assert snapshot["expired_leases"] == 1 and snapshot["active_leases"] == 0
    assert snapshot["used_calls"] == 2 and snapshot["used_tokens"] == 12
    rendered = json.dumps(snapshot)
    assert "secret" not in rendered and "private-provider" not in rendered
