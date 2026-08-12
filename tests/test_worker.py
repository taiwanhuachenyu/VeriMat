import hashlib

import pytest

from src.orchestration.job_store import JobStatus, JobStore, JobStoreError, Stage
from src.orchestration.worker import (
    ChargeSpec, EventSpec, ExecutionWorker, RetryableStageError, StageResult,
)


def _create(store, *, key="request", max_calls=10):
    return store.create_job(
        tenant_id="tenant", idempotency_key=key, task="task", max_calls=max_calls,
        max_tokens=1000, max_cost_microunits=100,
    )


def _graph_events():
    digest = hashlib.sha256(b"passage").hexdigest()
    values = [
        ("claim", "c1", "claim.proposed", {"claim_id": "c1", "text": "claim", "scope": "scope"}),
        ("query", "q1", "query.executed", {"query_id": "q1", "claim_id": "c1", "text": "support", "intent": "support", "n_hits": 1}),
        ("passage", "p1", "passage.observed", {"passage_id": "p1", "doc_id": "doc", "query_id": "q1", "offset": 0, "content_sha256": digest}),
        ("claim", "c1", "evidence.linked", {"claim_id": "c1", "passage_id": "p1", "relation": "SUPPORTS"}),
        ("claim", "c1", "claim.transitioned", {"claim_id": "c1", "to_state": "SUPPORTED", "reason": "supported"}),
        ("query", "q2", "query.executed", {"query_id": "q2", "claim_id": "c1", "text": "counter", "intent": "counterevidence", "n_hits": 0}),
        ("claim", "c1", "claim.transitioned", {"claim_id": "c1", "to_state": "CHALLENGED", "reason": "challenged"}),
        ("claim", "c1", "claim.transitioned", {"claim_id": "c1", "to_state": "SURVIVED", "reason": "bounded", "boundary": "cutoff"}),
    ]
    return tuple(
        EventSpec(
            aggregate_type=kind, aggregate_id=identifier, event_type=event_type,
            payload=payload, idempotency_key=f"graph-{index}",
        )
        for index, (kind, identifier, event_type, payload) in enumerate(values)
    )


def _handlers(callback=None):
    handlers = {}
    for stage in Stage:
        if stage == Stage.VALIDATE:
            continue

        def handler(invocation, current=stage):
            if callback:
                callback(invocation)
            return StageResult(
                events=_graph_events() if current == Stage.SYNTHESIZE else (),
                checkpoint={"stage": current.value},
            )

        handlers[stage] = handler
    return handlers


def test_worker_completes_valid_graph_and_checkpoints_every_stage(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store)
    report = ExecutionWorker(
        store=store, ledger_root=tmp_path / "ledgers", worker_id="worker",
        handlers=_handlers(),
    ).run_once()
    assert report.job_id == job.job_id
    assert report.status == JobStatus.SUCCEEDED.value
    assert report.finalization and report.finalization.ok
    assert len(store.checkpoints(job.job_id)) == len(Stage)


def test_retry_resumes_after_checkpoint_with_stable_operation_id(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store)
    calls = []
    failed = False

    def callback(invocation):
        nonlocal failed
        calls.append((invocation.stage, invocation.operation_id))
        if invocation.stage == Stage.RETRIEVE and not failed:
            failed = True
            raise RetryableStageError("temporary")

    worker = ExecutionWorker(
        store=store, ledger_root=tmp_path / "ledgers", worker_id="worker",
        handlers=_handlers(callback), max_attempts=3,
    )
    assert worker.run_once().status == JobStatus.RETRY_WAIT.value
    assert worker.run_once().status == JobStatus.SUCCEEDED.value
    assert [stage for stage, _ in calls].count(Stage.PLAN) == 1
    retrieve_ids = [operation for stage, operation in calls if stage == Stage.RETRIEVE]
    assert retrieve_ids == [f"{job.job_id}:RETRIEVE:v1"] * 2


def test_budget_failure_is_terminal_and_charge_does_not_overshoot(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store, max_calls=1)
    handlers = _handlers()
    handlers[Stage.RETRIEVE] = lambda _invocation: StageResult(
        charges=(ChargeSpec(charge_key="too-much", provider="fixture", calls=2),)
    )
    report = ExecutionWorker(
        store=store, ledger_root=tmp_path / "ledgers", worker_id="worker",
        handlers=handlers,
    ).run_once()
    assert report.status == JobStatus.FAILED.value
    assert report.error_code == "BUDGET_EXCEEDED"
    assert store.get(job.job_id).used_calls == 0


def test_worker_observes_cancellation_before_committing_stage(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store)
    handlers = _handlers()

    def cancel(_invocation):
        store.transition(job.job_id, target=JobStatus.CANCELLED)
        return StageResult(checkpoint={"must_not": "commit"})

    handlers[Stage.PLAN] = cancel
    report = ExecutionWorker(
        store=store, ledger_root=tmp_path / "ledgers", worker_id="worker",
        handlers=handlers,
    ).run_once()
    assert report.status == JobStatus.CANCELLED.value
    assert store.checkpoints(job.job_id) == []


def test_checkpoint_rejects_structured_secrets(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _create(store)
    store.acquire_lease(job.job_id, worker_id="worker", lease_seconds=60)
    with pytest.raises(JobStoreError, match="sensitive fields"):
        store.save_checkpoint(
            job.job_id, worker_id="worker", stage=Stage.PLAN,
            checkpoint_key="secret", payload={"nested": {"api_token": "no"}},
        )
