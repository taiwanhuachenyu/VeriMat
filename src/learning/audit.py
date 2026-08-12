"""Transactional-outbox bridge from policy memory to the immutable event ledger."""
from __future__ import annotations

from dataclasses import dataclass

from src.evidence.ledger import EventLedger, LedgerIntegrityError

from .policy_store import PolicyStore


@dataclass(frozen=True)
class PolicyFlushReport:
    dispatched: int
    ledger_events: int
    ledger_head: str


class PolicyAuditBridge:
    """Reliably dispatch policy mutations without a cross-database transaction.

    Policy mutations and their outbox rows commit in one SQLite transaction. Dispatch is
    at-least-once; the destination ledger's idempotency keys reduce that to exactly one
    durable event. A crash after append and before acknowledgement is therefore safe.
    """

    def __init__(self, *, audit_job_id: str = "policy-memory"):
        if not audit_job_id.strip():
            raise ValueError("audit_job_id is required")
        self.audit_job_id = audit_job_id

    def flush(
        self, *, store: PolicyStore, ledger: EventLedger, tenant_id: str,
        batch_size: int = 100,
    ) -> PolicyFlushReport:
        if not tenant_id.strip() or batch_size < 1:
            raise ValueError("tenant_id and positive batch_size are required")
        dispatched = 0
        while True:
            pending = store.pending_events(tenant_id=tenant_id, limit=batch_size)
            if not pending:
                break
            for event in pending:
                ledger.append(
                    tenant_id=tenant_id,
                    job_id=self.audit_job_id,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    event_type=event.event_type,
                    payload=event.payload,
                    idempotency_key=event.idempotency_key,
                )
                store.mark_dispatched(tenant_id=tenant_id, event_id=event.event_id)
                dispatched += 1
            if len(pending) < batch_size:
                break
        receipt = ledger.verify()
        if not receipt.ok:
            raise LedgerIntegrityError("invalid policy audit ledger: " + receipt.error)
        return PolicyFlushReport(
            dispatched=dispatched, ledger_events=receipt.event_count,
            ledger_head=receipt.head_hash,
        )
