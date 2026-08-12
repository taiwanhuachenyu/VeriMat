import hashlib

from src.orchestration.job_store import JobStatus, JobStore
from src.orchestration.runtime import ExecutionContext


def _emit_valid_survivor(context):
    events = [
        ("claim", "c1", "claim.proposed",
         {"claim_id": "c1", "text": "bounded claim", "scope": "LLZO"}),
        ("query", "qs", "query.executed",
         {"query_id": "qs", "claim_id": "c1", "text": "support", "intent": "support",
          "n_hits": 1}),
        ("passage", "ps", "passage.observed",
         {"passage_id": "ps", "doc_id": "doc", "query_id": "qs", "offset": 0,
          "content_sha256": hashlib.sha256(b"passage").hexdigest()}),
        ("claim", "c1", "evidence.linked",
         {"claim_id": "c1", "passage_id": "ps", "relation": "SUPPORTS"}),
        ("claim", "c1", "claim.transitioned",
         {"claim_id": "c1", "to_state": "SUPPORTED", "reason": "passage entails claim"}),
        ("query", "qc", "query.executed",
         {"query_id": "qc", "claim_id": "c1", "text": "counter", "intent": "counterevidence",
          "n_hits": 0}),
        ("claim", "c1", "claim.transitioned",
         {"claim_id": "c1", "to_state": "CHALLENGED", "reason": "counter query executed"}),
        ("claim", "c1", "claim.transitioned",
         {"claim_id": "c1", "to_state": "SURVIVED", "reason": "no precedent located",
          "boundary": "indexed LLZO literature through cutoff"}),
    ]
    for index, (aggregate_type, aggregate_id, event_type, payload) in enumerate(events):
        context.emit(
            aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            event_type=event_type, payload=payload, idempotency_key=f"event-{index}",
        )


def test_execution_context_finishes_only_from_valid_ledger_and_graph(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    context = ExecutionContext.start(
        store=store, ledger_root=tmp_path / "ledgers", tenant_id="tenant",
        idempotency_key="request", task="LLZO", worker_id="worker", lease_seconds=60,
        max_calls=10, max_tokens=1000, max_cost_microunits=100,
    )
    _emit_valid_survivor(context)
    result = context.finalize()
    assert result.ok
    assert result.job.status == JobStatus.SUCCEEDED
    assert result.graph_metrics["claims_survived"] == 1
    assert store.checkpoints(result.job.job_id)[0]["payload"]["ledger_head"]


def test_execution_context_fails_nonterminal_claim(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    context = ExecutionContext.start(
        store=store, ledger_root=tmp_path / "ledgers", tenant_id="tenant",
        idempotency_key="request", task="LLZO", worker_id="worker", lease_seconds=60,
        max_calls=10, max_tokens=1000, max_cost_microunits=100,
    )
    context.emit(
        aggregate_type="claim", aggregate_id="c1", event_type="claim.proposed",
        payload={"claim_id": "c1", "text": "unfinished", "scope": "LLZO"},
        idempotency_key="claim",
    )
    result = context.finalize()
    assert not result.ok
    assert result.job.status == JobStatus.FAILED
    assert "non-terminal state" in result.issues[0]


def test_execution_context_fails_empty_scientific_graph(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    context = ExecutionContext.start(
        store=store, ledger_root=tmp_path / "ledgers", tenant_id="tenant",
        idempotency_key="request", task="LLZO", worker_id="worker", lease_seconds=60,
        max_calls=10, max_tokens=1000, max_cost_microunits=100,
    )
    result = context.finalize()
    assert not result.ok
    assert result.issues == ("graph contains no claims",)


def test_explicit_non_cedg_control_uses_named_replayable_validator(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    context = ExecutionContext.start(
        store=store, ledger_root=tmp_path / "ledgers", tenant_id="tenant",
        idempotency_key="control", task="control", worker_id="worker", lease_seconds=60,
        max_calls=10, max_tokens=1000, max_cost_microunits=100,
    )
    context.emit(
        aggregate_type="benchmark", aggregate_id="decision",
        event_type="benchmark.decision_recorded",
        payload={"challenge_id": "challenge", "decision": "REFUTED"},
        idempotency_key="decision",
    )

    def validator(events):
        decisions = [event for event in events
                     if event.event_type == "benchmark.decision_recorded"]
        return {"decisions": len(decisions)}, ([] if len(decisions) == 1 else ["bad count"])

    result = context.finalize_with_validator(
        validator_name="benchmark-control-v1", validator=validator,
    )
    assert result.ok
    checkpoint = store.checkpoints(result.job.job_id)[0]["payload"]
    assert checkpoint["validator_name"] == "benchmark-control-v1"
