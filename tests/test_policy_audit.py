from src.evidence.ledger import EventLedger
from src.learning.audit import PolicyAuditBridge
from src.learning.policy_store import PolicyStore
from src.orchestration.job_store import JobStore
from src.orchestration.runtime import ExecutionContext


def _propose(store, *, source_job="source-job"):
    return store.propose_strategy(
        tenant_id="tenant", kind="boundary_probe", pattern="{material} stability limit",
        source_job_id=source_job, source_task_family="electrolytes",
    )


def test_policy_outbox_flush_is_crash_safe_and_idempotent(tmp_path):
    store = PolicyStore(tmp_path / "policy.db")
    _propose(store)
    ledger = EventLedger(tmp_path / "policy-events.jsonl")
    pending = store.pending_events(tenant_id="tenant")
    assert len(pending) == 1

    # Simulate a crash after durable append but before acknowledging the outbox row.
    event = pending[0]
    ledger.append(
        tenant_id="tenant", job_id="policy-memory",
        aggregate_type=event.aggregate_type, aggregate_id=event.aggregate_id,
        event_type=event.event_type, payload=event.payload,
        idempotency_key=event.idempotency_key,
    )
    report = PolicyAuditBridge().flush(
        store=store, ledger=ledger, tenant_id="tenant",
    )
    assert report.dispatched == 1
    assert report.ledger_events == 1
    assert store.audit_snapshot(tenant_id="tenant")["undispatched_events"] == 0
    assert PolicyAuditBridge().flush(
        store=store, ledger=ledger, tenant_id="tenant",
    ).dispatched == 0


def test_execution_context_binds_policy_audit_head(tmp_path):
    jobs = JobStore(tmp_path / "jobs.db")
    context = ExecutionContext.start(
        store=jobs, ledger_root=tmp_path / "job-ledgers", tenant_id="tenant",
        idempotency_key="request", task="task", worker_id="worker",
        lease_seconds=60, max_calls=10, max_tokens=100, max_cost_microunits=10,
    )
    policies = PolicyStore(tmp_path / "policy.db")
    _propose(policies, source_job=context.job.job_id)
    policy_ledger = EventLedger(tmp_path / "policy-ledger.jsonl")
    report = context.sync_policy_audit(
        policy_store=policies, policy_ledger=policy_ledger,
    )
    assert report.dispatched == 1
    link = list(context.ledger.events())[-1]
    assert link.event_type == "policy.audit_linked"
    assert link.payload["policy_ledger_head"] == policy_ledger.verify().head_hash
