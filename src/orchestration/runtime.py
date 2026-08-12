"""Integration boundary joining job control, the event ledger, and CEDG validation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from src.core.events import EventEnvelope
from src.evidence.graph import DecisionGraph, GraphInvariantError
from src.evidence.ledger import EventLedger
from src.learning.audit import PolicyAuditBridge, PolicyFlushReport
from src.learning.policy_store import PolicyStore

from .job_store import Job, JobStatus, JobStore, Stage


@dataclass(frozen=True)
class FinalizationReport:
    ok: bool
    job: Job
    ledger_events: int
    ledger_head: str
    graph_metrics: dict[str, int]
    issues: tuple[str, ...]


class ExecutionContext:
    """One leased execution attempt with durable scientific and operational state."""

    def __init__(
        self, *, store: JobStore, job: Job, worker_id: str, ledger: EventLedger,
    ):
        self.store = store
        self.job = job
        self.worker_id = worker_id
        self.ledger = ledger

    @classmethod
    def start(
        cls,
        *,
        store: JobStore,
        ledger_root: str | Path,
        tenant_id: str,
        idempotency_key: str,
        task: str,
        worker_id: str,
        lease_seconds: float,
        max_calls: int,
        max_tokens: int,
        max_cost_microunits: int,
    ) -> "ExecutionContext":
        job = store.create_job(
            tenant_id=tenant_id, idempotency_key=idempotency_key, task=task,
            max_calls=max_calls, max_tokens=max_tokens,
            max_cost_microunits=max_cost_microunits,
        )
        job = store.acquire_lease(
            job.job_id, worker_id=worker_id, lease_seconds=lease_seconds
        )
        ledger = EventLedger(Path(ledger_root) / tenant_id / job.job_id / "events.jsonl")
        return cls(store=store, job=job, worker_id=worker_id, ledger=ledger)

    def emit(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ):
        return self.ledger.append(
            tenant_id=self.job.tenant_id,
            job_id=self.job.job_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def checkpoint(
        self, *, stage: Stage, checkpoint_key: str, payload: dict[str, Any],
    ) -> Job:
        receipt = self.ledger.verify()
        if not receipt.ok:
            raise GraphInvariantError("cannot checkpoint an invalid event ledger: " + receipt.error)
        self.job = self.store.save_checkpoint(
            self.job.job_id,
            worker_id=self.worker_id,
            stage=stage,
            checkpoint_key=checkpoint_key,
            payload={**payload, "ledger_head": receipt.head_hash,
                     "ledger_event_count": receipt.event_count},
        )
        return self.job

    def charge(
        self, *, charge_key: str, provider: str, calls: int = 0,
        tokens: int = 0, cost_microunits: int = 0,
    ) -> Job:
        self.job = self.store.charge(
            self.job.job_id, charge_key=charge_key, provider=provider,
            calls=calls, tokens=tokens, cost_microunits=cost_microunits,
        )
        return self.job

    def sync_policy_audit(
        self, *, policy_store: PolicyStore, policy_ledger: EventLedger,
        bridge: PolicyAuditBridge | None = None,
    ) -> PolicyFlushReport:
        """Flush tenant policy events and bind their verified head into this job ledger."""
        dispatcher = bridge or PolicyAuditBridge()
        report = dispatcher.flush(
            store=policy_store, ledger=policy_ledger, tenant_id=self.job.tenant_id,
        )
        if report.ledger_events:
            self.emit(
                aggregate_type="policy_audit", aggregate_id="policy-memory",
                event_type="policy.audit_linked",
                payload={
                    "policy_ledger_head": report.ledger_head,
                    "policy_ledger_events": report.ledger_events,
                    "newly_dispatched": report.dispatched,
                },
                idempotency_key=f"policy-audit:{report.ledger_head}",
            )
        return report

    def finalize(self) -> FinalizationReport:
        def validate(events: Iterable[EventEnvelope]):
            graph = DecisionGraph.project(events)
            return graph.metrics(), graph.validate_for_publication()

        return self.finalize_with_validator(
            validator_name="cedg-publication-v1", validator=validate,
        )

    def finalize_with_validator(
        self, *, validator_name: str,
        validator: Callable[[Iterable[EventEnvelope]], tuple[dict[str, int], list[str]]],
    ) -> FinalizationReport:
        """Close a job through an explicit deterministic evidence validator.

        The default ``finalize`` remains the CEDG publication gate. This method exists for
        non-CEDG experimental controls whose treatment definition forbids using that graph while
        still requiring a named, replayable validator and the same operational success gate.
        """
        if not validator_name.strip():
            raise ValueError("validator_name is required")
        receipt = self.ledger.verify()
        issues: list[str] = []
        metrics: dict[str, int] = {}
        if not receipt.ok:
            issues.append("ledger: " + receipt.error)
        else:
            try:
                metrics, validation_issues = validator(self.ledger.events())
                issues.extend(validation_issues)
            except (GraphInvariantError, ValueError) as exc:
                issues.append("validator: " + str(exc))
        if issues:
            self.job = self.store.transition(
                self.job.job_id, target=JobStatus.FAILED,
                error_code="EVIDENCE_INVARIANT_FAILED",
            )
            return FinalizationReport(
                ok=False, job=self.job, ledger_events=receipt.event_count,
                ledger_head=receipt.head_hash, graph_metrics=metrics,
                issues=tuple(issues),
            )
        self.checkpoint(
            stage=Stage.VALIDATE,
            checkpoint_key="evidence-finalization",
            payload={
                "ok": True, "validator_name": validator_name,
                "validation_metrics": metrics,
            },
        )
        self.job = self.store.transition(
            self.job.job_id, target=JobStatus.VALIDATING, release_lease=False
        )
        self.job = self.store.transition(
            self.job.job_id, target=JobStatus.SUCCEEDED
        )
        return FinalizationReport(
            ok=True, job=self.job, ledger_events=receipt.event_count,
            ledger_head=receipt.head_hash, graph_metrics=metrics, issues=(),
        )
