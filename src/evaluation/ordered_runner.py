"""Ordered memory-treatment runner with immutable interventions and delayed external credit."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from src.core.events import canonical_json
from src.core.portability import extended_path
from src.evidence.ledger import EventLedger
from src.learning.audit import PolicyAuditBridge
from src.learning.policy_store import PolicyStore, StrategyScore
from src.orchestration.artifacts import ArtifactError, ArtifactStore
from src.orchestration.job_store import JobStore

from .baseline_runner import (
    BaselineBackend,
    BaselineContractError,
    BaselineTaskRunner,
    BlindTask,
    DecisionOutput,
    MethodSpec,
    QueryPlan,
)

MEMORY_MARKER = "[RECALLED_FALSIFICATION_STRATEGY]"


@dataclass(frozen=True)
class CreditOutcome:
    evaluator_kind: str
    success: bool
    false_gap_avoided: bool
    valid_finding_delta: float
    evidence_ref: str


class ExternalCreditEvaluator(Protocol):
    """Post-execution evaluator; it is never passed to the task backend."""

    def evaluate(
        self, *, challenge_id: str, prediction: dict[str, Any],
    ) -> CreditOutcome: ...


class MemoryInjectedBackend:
    """Deterministically inject bounded strategy text into counter queries only."""

    def __init__(self, backend: BaselineBackend, strategies: list[StrategyScore]):
        self.backend = backend
        self.strategies = tuple(strategies)
        self.provider_id = backend.provider_id
        self.plan_call_reservation = int(getattr(backend, "plan_call_reservation", 0))
        self.decision_call_reservation = int(getattr(
            backend, "decision_call_reservation", 0,
        ))

    def plan_queries(
        self, *, task: BlindTask, intent: str, operation_id: str,
    ) -> QueryPlan:
        plan = self.backend.plan_queries(
            task=task, intent=intent, operation_id=operation_id,
        )
        plan.validate()
        if intent != "counterevidence" or not self.strategies:
            return plan
        memory = "\n" + MEMORY_MARKER + "\n" + "\n".join(
            f"- {strategy.kind}: {strategy.pattern}" for strategy in self.strategies
        )
        queries = tuple(query + memory for query in plan.queries)
        injected = QueryPlan(queries=queries, usage=plan.usage)
        injected.validate()
        return injected

    def decide(
        self, *, task: BlindTask, method: MethodSpec,
        support_passages, counter_passages, operation_id: str,
    ) -> DecisionOutput:
        return self.backend.decide(
            task=task, method=method,
            support_passages=support_passages,
            counter_passages=counter_passages,
            operation_id=operation_id,
        )


class OrderedBenchmarkRunner:
    """Run a fixed task order while keeping current-task gold outside execution.

    Uncredited replay may use recent cross-family candidates but never records outcomes. Delayed
    credit uses at most one strategy per task, making the external outcome attributable; candidates
    are explored deterministically until they qualify for active recall.
    """

    def __init__(
        self, *, store: JobStore, artifacts: ArtifactStore, ledger_root: str | Path,
        policy_store: PolicyStore, policy_ledger: EventLedger,
        backend: BaselineBackend, retriever, worker_id: str,
        tenant_id: str = "benchmark", exploration_period: int = 3,
    ):
        if exploration_period < 1:
            raise ValueError("exploration_period must be positive")
        self.store = store
        self.artifacts = artifacts
        self.ledger_root = extended_path(ledger_root)
        self.policy_store = policy_store
        self.policy_ledger = policy_ledger
        self.backend = backend
        self.retriever = retriever
        self.worker_id = worker_id
        self.tenant_id = tenant_id
        self.exploration_period = exploration_period

    def _select(
        self, *, method: MethodSpec, task: BlindTask, sequence_index: int,
    ) -> list[StrategyScore]:
        if method.memory == "uncredited_replay":
            return self.policy_store.recall_uncredited(
                tenant_id=self.tenant_id,
                target_task_family=task.task_family,
                limit=2,
            )
        active = self.policy_store.recall_active(
            tenant_id=self.tenant_id,
            target_task_family=task.task_family,
            limit=1,
        )
        candidate = self.policy_store.recall_candidate_for_evaluation(
            tenant_id=self.tenant_id,
            target_task_family=task.task_family,
        )
        should_explore = candidate is not None and (
            not active or sequence_index % self.exploration_period == 0
        )
        if should_explore:
            return [candidate]
        return active[:1]

    @staticmethod
    def _job_id(prediction: dict[str, Any]) -> str:
        parts = Path(str(prediction["ledger_relpath"])).parts
        if len(parts) != 3 or parts[-1] != "events.jsonl":
            raise BaselineContractError("unexpected benchmark ledger path")
        return parts[-2]

    def _artifact(self, *, job_id: str, logical_key: str) -> dict[str, Any] | None:
        try:
            ref = self.artifacts.get_ref(
                tenant_id=self.tenant_id, job_id=job_id, logical_key=logical_key,
            )
        except ArtifactError:
            return None
        return self.artifacts.read_json(ref)

    def _bind_policy_receipt(
        self, *, prediction: dict[str, Any], challenge_id: str, run_id: str,
    ) -> dict[str, Any]:
        report = PolicyAuditBridge(
            audit_job_id=f"policy:{run_id}"
        ).flush(
            store=self.policy_store, ledger=self.policy_ledger,
            tenant_id=self.tenant_id,
        )
        ledger_path = self.ledger_root / prediction["ledger_relpath"]
        job_ledger = EventLedger(ledger_path)
        links = [
            event for event in job_ledger.events()
            if event.event_type == "policy.audit_linked"
        ]
        if len(links) > 1:
            raise BaselineContractError("task ledger has multiple policy audit links")
        if not links and report.ledger_events:
            job_ledger.append(
                tenant_id=self.tenant_id,
                job_id=self._job_id(prediction),
                aggregate_type="policy_audit",
                aggregate_id="policy-memory",
                event_type="policy.audit_linked",
                payload={
                    "policy_ledger_head": report.ledger_head,
                    "policy_ledger_events": report.ledger_events,
                    "newly_dispatched": report.dispatched,
                    "challenge_id": challenge_id,
                },
                idempotency_key=f"policy-link:{run_id}:{challenge_id}",
            )
        receipt = job_ledger.verify()
        if not receipt.ok:
            raise BaselineContractError("ordered task ledger failed after policy link")
        return {
            **prediction,
            "ledger_head": receipt.head_hash,
            "ledger_event_count": receipt.event_count,
        }

    def run(
        self, *, task_values: list[dict[str, Any]], method: MethodSpec, run_id: str,
        max_calls: int, max_tokens: int,
        outcome_evaluator: ExternalCreditEvaluator | None = None,
        min_evaluations: int = 3, activation_lower_bound: float = 0.2,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if method.memory not in {"uncredited_replay", "delayed_external_credit"}:
            raise BaselineContractError("ordered runner requires a memory treatment")
        if not method.cedg or not method.external_counter_retrieval:
            raise BaselineContractError("memory treatments require CEDG and counter retrieval")
        if (method.memory == "delayed_external_credit") != (outcome_evaluator is not None):
            raise BaselineContractError(
                "external evaluator is required only for delayed-credit treatment"
            )
        if not run_id.strip() or not task_values:
            raise BaselineContractError("ordered run id and tasks are required")
        tasks = [BlindTask.from_dict(value) for value in task_values]
        identifiers = [task.challenge_id for task in tasks]
        if len(identifiers) != len(set(identifiers)):
            raise BaselineContractError("ordered task list contains duplicate challenge ids")
        order_sha256 = hashlib.sha256(canonical_json(identifiers).encode()).hexdigest()
        predictions: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []

        for sequence_index, (task_value, task) in enumerate(zip(task_values, tasks)):
            persisted = self.policy_store.sequence_intervention(
                tenant_id=self.tenant_id, run_id=run_id,
                method_id=method.method_id, sequence_index=sequence_index,
            )
            proposed = persisted if persisted is not None else self._select(
                method=method, task=task, sequence_index=sequence_index,
            )
            selected = self.policy_store.bind_sequence_intervention(
                tenant_id=self.tenant_id, run_id=run_id, method_id=method.method_id,
                sequence_index=sequence_index, challenge_id=task.challenge_id,
                order_sha256=order_sha256, memory_mode=method.memory,
                strategy_ids=[item.strategy_id for item in proposed],
            )
            runner = BaselineTaskRunner(
                store=self.store, artifacts=self.artifacts,
                ledger_root=self.ledger_root,
                backend=MemoryInjectedBackend(self.backend, selected),
                retriever=self.retriever,
                worker_id=self.worker_id,
                tenant_id=self.tenant_id,
            )
            prediction, worker_report = runner.run_task(
                task_value=task_value,
                method=replace(method, memory="none"),
                run_id=run_id,
                max_calls=max_calls,
                max_tokens=max_tokens,
            )
            job_id = self._job_id(prediction)
            plan = self._artifact(job_id=job_id, logical_key="baseline:plan")
            applied = bool(
                selected and plan and any(
                    MEMORY_MARKER in query for query in plan.get("counter_queries", [])
                )
            )
            applications: list[str] = []
            if applied:
                rendered_query = "\n\n".join(plan["counter_queries"])
                for strategy in selected:
                    applications.append(self.policy_store.record_application(
                        tenant_id=self.tenant_id,
                        strategy_id=strategy.strategy_id,
                        target_job_id=job_id,
                        target_task_family=task.task_family,
                        rendered_query=rendered_query,
                        idempotency_key=(
                            f"{run_id}:{method.method_id}:{sequence_index}:"
                            f"{strategy.strategy_id}:application"
                        ),
                    ))

            if method.memory == "delayed_external_credit" and applications:
                pending_applications = [
                    application_id for application_id in applications
                    if not self.policy_store.application_has_outcome(
                        tenant_id=self.tenant_id, application_id=application_id,
                    )
                ]
                outcome = None
                if pending_applications:
                    outcome = outcome_evaluator.evaluate(
                        challenge_id=task.challenge_id, prediction=prediction,
                    )
                for application_id in pending_applications:
                    self.policy_store.record_outcome(
                        tenant_id=self.tenant_id,
                        application_id=application_id,
                        evaluator_kind=outcome.evaluator_kind,
                        success=outcome.success,
                        false_gap_avoided=outcome.false_gap_avoided,
                        valid_finding_delta=outcome.valid_finding_delta,
                        calls=prediction["calls"], tokens=prediction["tokens"],
                        evidence_ref=outcome.evidence_ref,
                    )
                self.policy_store.refresh_statuses(
                    tenant_id=self.tenant_id,
                    min_evaluations=min_evaluations,
                    activation_lower_bound=activation_lower_bound,
                )

            decision = self._artifact(job_id=job_id, logical_key="baseline:decision")
            proposed_ids: list[str] = []
            if prediction["status"] == "completed" and decision is not None:
                for candidate in decision.get("strategy_candidates", []):
                    proposed_ids.append(self.policy_store.propose_strategy(
                        tenant_id=self.tenant_id,
                        kind=candidate["kind"], pattern=candidate["pattern"],
                        source_job_id=job_id,
                        source_task_family=task.task_family,
                    ))

            prediction = self._bind_policy_receipt(
                prediction=prediction, challenge_id=task.challenge_id, run_id=run_id,
            )
            predictions.append(prediction)
            steps.append({
                "sequence_index": sequence_index,
                "challenge_id": task.challenge_id,
                "task_family": task.task_family,
                "strategy_ids": [item.strategy_id for item in selected],
                "strategy_applied": applied,
                "application_ids": applications,
                "proposed_strategy_ids": proposed_ids,
                "worker_status": worker_report.status,
                "prediction_status": prediction["status"],
            })

        policy_receipt = self.policy_ledger.verify()
        report = {
            "schema_version": 1,
            "run_id": run_id,
            "method_id": method.method_id,
            "memory_mode": method.memory,
            "task_order_sha256": order_sha256,
            "tasks": len(tasks),
            "policy_ledger_head": policy_receipt.head_hash,
            "policy_ledger_events": policy_receipt.event_count,
            "policy_snapshot": self.policy_store.audit_snapshot(
                tenant_id=self.tenant_id,
            ),
            "steps": steps,
        }
        return predictions, report
