"""Shared staged runner for blind, treatment-isolated V2 benchmark methods."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from src.evidence.graph import ClaimState, EvidenceRelation
from src.evidence.ledger import EventLedger
from src.orchestration.artifacts import ArtifactRef, ArtifactStore
from src.orchestration.job_store import Job, JobStatus, JobStore, Stage
from src.orchestration.runtime import ExecutionContext, FinalizationReport
from src.orchestration.worker import (
    ChargeSpec, EventSpec, ExecutionWorker, StageInvocation, StageResult, WorkerReport,
)

from .blinding import TASK_FIELDS
from .challenge import DECISIONS, prediction_commitment

STRATEGY_KINDS = {"counter_query_template", "boundary_probe", "precedent_probe"}


class BaselineContractError(ValueError):
    pass


@dataclass(frozen=True)
class MethodSpec:
    method_id: str
    support_retrieval: bool
    external_counter_retrieval: bool
    cedg: bool
    memory: str
    decision_mode: str

    @classmethod
    def load(cls, path: str | Path, method_id: str) -> tuple["MethodSpec", dict[str, int]]:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if value.get("schema_version") != 1 or not isinstance(value.get("methods"), list):
            raise BaselineContractError("invalid method registry")
        matches = [item for item in value["methods"] if item.get("method_id") == method_id]
        if len(matches) != 1:
            raise BaselineContractError(f"method {method_id!r} is missing or duplicated")
        item = matches[0]
        required = {
            "method_id", "support_retrieval", "external_counter_retrieval", "cedg", "memory",
            "decision_mode",
        }
        if set(item) != required or any(
            not isinstance(item[field], bool)
            for field in ("support_retrieval", "external_counter_retrieval", "cedg")
        ):
            raise BaselineContractError("method definition has invalid fields or types")
        if item["memory"] not in {"none", "uncredited_replay", "delayed_external_credit"}:
            raise BaselineContractError("unsupported memory mode")
        if item["decision_mode"] not in {"direct", "self_critic", "verifier"}:
            raise BaselineContractError("unsupported decision mode")
        if item["decision_mode"] == "self_critic" and item["external_counter_retrieval"]:
            raise BaselineContractError("self-critic control must not receive counter retrieval")
        if item["decision_mode"] == "verifier" and not item["external_counter_retrieval"]:
            raise BaselineContractError("verifier decision mode requires counter retrieval")
        budget = value.get("budget") or {}
        if any(
            isinstance(budget.get(field), bool) or not isinstance(budget.get(field), int)
            or budget[field] < 0
            for field in ("max_calls", "max_tokens")
        ):
            raise BaselineContractError("method registry has invalid budget")
        return cls(**item), {
            "max_calls": budget["max_calls"], "max_tokens": budget["max_tokens"],
        }


@dataclass(frozen=True)
class BlindTask:
    schema_version: int
    challenge_id: str
    benchmark_track: str
    split: str
    task_family: str
    prompt: str
    cutoff_date: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BlindTask":
        if set(value) != set(TASK_FIELDS):
            raise BaselineContractError("runner accepts only the exact blind-task contract")
        task = cls(**value)
        if task.schema_version != 1 or not task.challenge_id.strip() or not task.prompt.strip():
            raise BaselineContractError("invalid blind task")
        return task


@dataclass(frozen=True)
class Usage:
    calls: int
    tokens: int

    def validate(self) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (self.calls, self.tokens)):
            raise BaselineContractError("usage must contain non-negative integers")


@dataclass(frozen=True)
class QueryPlan:
    queries: tuple[str, ...]
    usage: Usage

    def validate(self) -> None:
        self.usage.validate()
        if not 1 <= len(self.queries) <= 8 or any(
            not query.strip() or len(query) > 1000 for query in self.queries
        ):
            raise BaselineContractError("query plan requires 1-8 bounded queries")


@dataclass(frozen=True)
class RetrievedPassage:
    passage_id: str
    query_id: str
    doc_id: str
    text: str
    locator: dict[str, int]
    content_sha256: str
    publication_date: str

    def validate(self) -> None:
        if not all((self.passage_id.strip(), self.query_id.strip(), self.doc_id.strip(), self.text.strip())):
            raise BaselineContractError("retrieved passage has empty identity or text")
        if set(self.locator) not in ({"offset"}, {"page_no"}):
            raise BaselineContractError("passage locator must contain exactly offset or page_no")
        position = next(iter(self.locator.values()))
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise BaselineContractError("passage locator is invalid")
        actual = hashlib.sha256(self.text.encode()).hexdigest()
        if self.content_sha256 != actual:
            raise BaselineContractError("passage content hash mismatch")


@dataclass(frozen=True)
class RetrievalResult:
    passages: tuple[RetrievedPassage, ...]
    usage: Usage

    def validate(self) -> None:
        self.usage.validate()
        identifiers: set[str] = set()
        for passage in self.passages:
            passage.validate()
            if passage.passage_id in identifiers:
                raise BaselineContractError("duplicate passage_id in retrieval result")
            identifiers.add(passage.passage_id)


@dataclass(frozen=True)
class EvidenceSelection:
    passage_id: str
    relation: str


@dataclass(frozen=True)
class StrategyCandidate:
    kind: str
    pattern: str

    def validate(self) -> None:
        if self.kind not in STRATEGY_KINDS:
            raise BaselineContractError("decision proposed an unsupported strategy kind")
        if not self.pattern.strip() or len(self.pattern) > 240:
            raise BaselineContractError("strategy pattern must contain 1-240 characters")


@dataclass(frozen=True)
class DecisionOutput:
    decision: str
    counterevidence_probability: float
    evidence: tuple[EvidenceSelection, ...]
    reason: str
    boundary: str
    usage: Usage
    strategy_candidates: tuple[StrategyCandidate, ...] = ()

    def validate(self, available_passage_ids: set[str]) -> None:
        self.usage.validate()
        if self.decision not in DECISIONS:
            raise BaselineContractError("unsupported decision")
        probability = self.counterevidence_probability
        if not isinstance(probability, (int, float)) or isinstance(probability, bool):
            raise BaselineContractError("counterevidence probability must be numeric")
        if not math.isfinite(float(probability)) or not 0 <= float(probability) <= 1:
            raise BaselineContractError("counterevidence probability must be in [0,1]")
        if not self.reason.strip():
            raise BaselineContractError("decision reason is required")
        seen: set[str] = set()
        for selection in self.evidence:
            if selection.passage_id not in available_passage_ids:
                raise BaselineContractError("decision cites a passage not exposed to the backend")
            if selection.relation not in {relation.value for relation in EvidenceRelation}:
                raise BaselineContractError("decision uses an unsupported evidence relation")
            if selection.passage_id in seen:
                raise BaselineContractError("decision cites a passage more than once")
            seen.add(selection.passage_id)
        if self.decision in {"SURVIVED", "NARROWED"} and not self.boundary.strip():
            raise BaselineContractError("surviving or narrowed decision requires a boundary")
        if len(self.strategy_candidates) > 3:
            raise BaselineContractError("a decision may propose at most three strategies")
        normalized: set[tuple[str, str]] = set()
        for candidate in self.strategy_candidates:
            candidate.validate()
            identity = (candidate.kind, " ".join(candidate.pattern.lower().split()))
            if identity in normalized:
                raise BaselineContractError("decision proposed a duplicate strategy")
            normalized.add(identity)


class BaselineBackend(Protocol):
    provider_id: str

    def plan_queries(
        self, *, task: BlindTask, intent: str, operation_id: str,
    ) -> QueryPlan: ...

    def decide(
        self, *, task: BlindTask, method: MethodSpec,
        support_passages: tuple[RetrievedPassage, ...],
        counter_passages: tuple[RetrievedPassage, ...],
        operation_id: str,
    ) -> DecisionOutput: ...


class BenchmarkRetriever(Protocol):
    provider_id: str

    def search(
        self, *, query_id: str, query: str, intent: str, cutoff_date: str,
        operation_id: str, reserve_call: Callable[[str], None],
    ) -> RetrievalResult: ...


def _passage_dict(passage: RetrievedPassage) -> dict[str, Any]:
    return {
        "passage_id": passage.passage_id, "query_id": passage.query_id,
        "doc_id": passage.doc_id, "text": passage.text, "locator": passage.locator,
        "content_sha256": passage.content_sha256,
        "publication_date": passage.publication_date,
    }


def _passage_from_dict(value: dict[str, Any]) -> RetrievedPassage:
    passage = RetrievedPassage(**value)
    passage.validate()
    return passage


class BaselineTaskRunner:
    """Run one blind task through the shared durable worker without exposing benchmark gold."""

    def __init__(
        self, *, store: JobStore, artifacts: ArtifactStore, ledger_root: str | Path,
        backend: BaselineBackend, retriever: BenchmarkRetriever, worker_id: str,
        tenant_id: str = "benchmark", lease_seconds: float = 120,
    ):
        self.store = store
        self.artifacts = artifacts
        self.ledger_root = Path(ledger_root)
        self.backend = backend
        self.retriever = retriever
        self.worker_id = worker_id
        self.tenant_id = tenant_id
        self.lease_seconds = lease_seconds

    def _put(self, job: Job, key: str, value: dict[str, Any]) -> ArtifactRef:
        return self.artifacts.put_json(
            tenant_id=job.tenant_id, job_id=job.job_id, logical_key=key, value=value,
        )

    def _get(self, job: Job, key: str) -> dict[str, Any]:
        ref = self.artifacts.get_ref(
            tenant_id=job.tenant_id, job_id=job.job_id, logical_key=key,
        )
        return self.artifacts.read_json(ref)

    @staticmethod
    def _charge(provider: str, operation_id: str, usage: Usage) -> ChargeSpec:
        return ChargeSpec(
            charge_key=operation_id, provider=provider,
            calls=usage.calls, tokens=usage.tokens,
        )

    def _reserve_backend_calls(
        self, *, job: Job, provider: str, operation_id: str, calls: int,
    ) -> None:
        if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
            raise BaselineContractError("backend call reservation must be non-negative")
        if calls:
            self.store.charge(
                job.job_id, charge_key=f"{operation_id}:call-reservation",
                provider=provider, calls=calls,
            )

    @staticmethod
    def _post_reservation_charge(
        *, provider: str, operation_id: str, usage: Usage, reserved_calls: int,
    ) -> ChargeSpec:
        if usage.calls < reserved_calls:
            raise BaselineContractError(
                "backend reported fewer calls than were reserved before execution"
            )
        return ChargeSpec(
            charge_key=f"{operation_id}:reported-usage", provider=provider,
            calls=usage.calls - reserved_calls, tokens=usage.tokens,
        )

    def _plan_handler(
        self, *, task: BlindTask, method: MethodSpec,
    ):
        def handler(invocation: StageInvocation) -> StageResult:
            support_operation = invocation.operation_id + ":support"
            support_reserved = int(getattr(self.backend, "plan_call_reservation", 0))
            self._reserve_backend_calls(
                job=invocation.job, provider=self.backend.provider_id,
                operation_id=support_operation, calls=support_reserved,
            )
            support = self.backend.plan_queries(
                task=task, intent="support",
                operation_id=support_operation,
            )
            support.validate()
            counter = None
            charges = [self._post_reservation_charge(
                provider=self.backend.provider_id, operation_id=support_operation,
                usage=support.usage, reserved_calls=support_reserved,
            )]
            if method.external_counter_retrieval:
                counter_operation = invocation.operation_id + ":counter"
                counter_reserved = int(getattr(
                    self.backend, "plan_call_reservation", 0,
                ))
                self._reserve_backend_calls(
                    job=invocation.job, provider=self.backend.provider_id,
                    operation_id=counter_operation, calls=counter_reserved,
                )
                counter = self.backend.plan_queries(
                    task=task, intent="counterevidence",
                    operation_id=counter_operation,
                )
                counter.validate()
                charges.append(self._post_reservation_charge(
                    provider=self.backend.provider_id, operation_id=counter_operation,
                    usage=counter.usage, reserved_calls=counter_reserved,
                ))
            ref = self._put(invocation.job, "baseline:plan", {
                "support_queries": list(support.queries),
                "counter_queries": list(counter.queries) if counter else [],
                "external_counter_retrieval": method.external_counter_retrieval,
            })
            events = ()
            if method.cedg:
                events = (EventSpec(
                    aggregate_type="claim", aggregate_id=task.challenge_id,
                    event_type="claim.proposed",
                    payload={
                        "claim_id": task.challenge_id, "text": task.prompt,
                        "scope": f"{task.task_family} through {task.cutoff_date}",
                    },
                    idempotency_key=f"{invocation.operation_id}:claim",
                ),)
            return StageResult(
                events=events, charges=tuple(charges),
                checkpoint={"artifact": ref.checkpoint_value()},
            )

        return handler

    def _retrieve_queries(
        self, *, invocation: StageInvocation, task: BlindTask, queries: list[str],
        intent: str,
    ) -> tuple[list[RetrievedPassage], list[ChargeSpec], list[EventSpec]]:
        passages: list[RetrievedPassage] = []
        charges: list[ChargeSpec] = []
        events: list[EventSpec] = []
        passage_ids: set[str] = set()
        for index, query in enumerate(queries):
            operation_id = f"{invocation.operation_id}:{intent}:{index}"
            query_id = f"{intent}-{index}"
            reserved_calls = 0

            def reserve_call(suboperation: str) -> None:
                nonlocal reserved_calls
                if not suboperation.strip() or len(suboperation) > 300:
                    raise BaselineContractError("retrieval suboperation id is invalid")
                self.store.charge(
                    invocation.job.job_id,
                    charge_key=f"{operation_id}:{suboperation}:call-reservation",
                    provider=self.retriever.provider_id, calls=1,
                )
                reserved_calls += 1

            result = self.retriever.search(
                query_id=query_id, query=query, intent=intent,
                cutoff_date=task.cutoff_date,
                operation_id=operation_id,
                reserve_call=reserve_call,
            )
            result.validate()
            events.append(EventSpec(
                aggregate_type="query", aggregate_id=query_id,
                event_type="query.executed",
                payload={
                    "query_id": query_id, "claim_id": task.challenge_id, "text": query,
                    "intent": intent, "n_hits": len(result.passages),
                },
                idempotency_key=f"{invocation.operation_id}:query:{index}",
            ))
            for passage in result.passages:
                if passage.query_id != query_id:
                    raise BaselineContractError(
                        "retriever passage query_id does not match assigned query"
                    )
                if passage.passage_id in passage_ids:
                    raise BaselineContractError("passage_id duplicated across query results")
                passage_ids.add(passage.passage_id)
                passages.append(passage)
                events.append(EventSpec(
                    aggregate_type="passage", aggregate_id=passage.passage_id,
                    event_type="passage.observed",
                    payload={
                        "passage_id": passage.passage_id, "doc_id": passage.doc_id,
                        "query_id": passage.query_id, **passage.locator,
                        "content_sha256": passage.content_sha256,
                    },
                    idempotency_key=(
                        f"{invocation.operation_id}:passage:{passage.passage_id}"
                    ),
                ))
            charges.append(self._post_reservation_charge(
                provider=self.retriever.provider_id, operation_id=operation_id,
                usage=result.usage, reserved_calls=reserved_calls,
            ))
        return passages, charges, events

    def _retrieval_handler(self, *, task: BlindTask, intent: str, artifact_key: str):
        def handler(invocation: StageInvocation) -> StageResult:
            plan = self._get(invocation.job, "baseline:plan")
            plan_key = "counter_queries" if intent == "counterevidence" else "support_queries"
            queries = list(plan[plan_key])
            passages, charges, events = self._retrieve_queries(
                invocation=invocation, task=task, queries=queries, intent=intent,
            )
            ref = self._put(invocation.job, artifact_key, {
                "intent": intent, "passages": [_passage_dict(item) for item in passages],
            })
            return StageResult(
                events=tuple(events), charges=tuple(charges),
                checkpoint={"artifact": ref.checkpoint_value()},
            )

        return handler

    def _read_passages(self, job: Job, key: str) -> tuple[RetrievedPassage, ...]:
        try:
            value = self._get(job, key)
        except Exception:
            if key == "baseline:counter":
                return ()
            raise
        return tuple(_passage_from_dict(item) for item in value["passages"])

    def _decide_handler(self, *, task: BlindTask, method: MethodSpec):
        def handler(invocation: StageInvocation) -> StageResult:
            support = self._read_passages(invocation.job, "baseline:support")
            counter = self._read_passages(invocation.job, "baseline:counter")
            if not method.external_counter_retrieval and counter:
                raise BaselineContractError("control method received external counterevidence")
            reserved = int(getattr(self.backend, "decision_call_reservation", 0))
            self._reserve_backend_calls(
                job=invocation.job, provider=self.backend.provider_id,
                operation_id=invocation.operation_id, calls=reserved,
            )
            decision = self.backend.decide(
                task=task, method=method, support_passages=support,
                counter_passages=counter, operation_id=invocation.operation_id,
            )
            available = {item.passage_id for item in support + counter}
            decision.validate(available)
            decision_value = {
                "decision": decision.decision,
                "counterevidence_probability": decision.counterevidence_probability,
                "evidence": [selection.__dict__ for selection in decision.evidence],
                "reason": decision.reason, "boundary": decision.boundary,
                "strategy_candidates": [
                    candidate.__dict__ for candidate in decision.strategy_candidates
                ],
            }
            ref = self._put(invocation.job, "baseline:decision", decision_value)
            events: list[EventSpec] = [EventSpec(
                aggregate_type="benchmark", aggregate_id=task.challenge_id,
                event_type="benchmark.decision_recorded",
                payload={
                    "challenge_id": task.challenge_id, "method_id": method.method_id,
                    **{key: value for key, value in decision_value.items()
                       if key != "strategy_candidates"},
                    "strategy_candidate_hashes": [
                        hashlib.sha256(
                            f"{candidate.kind}|{candidate.pattern}".encode()
                        ).hexdigest()
                        for candidate in decision.strategy_candidates
                    ],
                },
                idempotency_key=f"{invocation.operation_id}:decision",
            )]
            if method.cedg:
                for index, selection in enumerate(decision.evidence):
                    events.append(EventSpec(
                        aggregate_type="claim", aggregate_id=task.challenge_id,
                        event_type="evidence.linked",
                        payload={
                            "claim_id": task.challenge_id,
                            "passage_id": selection.passage_id,
                            "relation": selection.relation,
                        },
                        idempotency_key=f"{invocation.operation_id}:edge:{index}",
                    ))
                relations = {item.relation for item in decision.evidence}
                if decision.decision == ClaimState.SURVIVED.value:
                    if EvidenceRelation.SUPPORTS.value not in relations:
                        raise BaselineContractError("SURVIVED CEDG decision lacks support")
                    events.append(self._transition(
                        invocation, task, "SUPPORTED", decision.reason, suffix="supported",
                    ))
                if method.external_counter_retrieval:
                    events.append(self._transition(
                        invocation, task, "CHALLENGED", "counter-query executed",
                        suffix="challenged",
                    ))
                events.append(self._transition(
                    invocation, task, decision.decision, decision.reason,
                    boundary=decision.boundary, suffix="terminal",
                ))
            return StageResult(
                events=tuple(events),
                charges=(self._post_reservation_charge(
                    provider=self.backend.provider_id,
                    operation_id=invocation.operation_id,
                    usage=decision.usage, reserved_calls=reserved,
                ),),
                checkpoint={"artifact": ref.checkpoint_value()},
            )

        return handler

    @staticmethod
    def _transition(
        invocation: StageInvocation, task: BlindTask, state: str, reason: str,
        *, suffix: str, boundary: str = "",
    ) -> EventSpec:
        payload: dict[str, Any] = {
            "claim_id": task.challenge_id, "to_state": state, "reason": reason,
        }
        if boundary:
            payload["boundary"] = boundary
        return EventSpec(
            aggregate_type="claim", aggregate_id=task.challenge_id,
            event_type="claim.transitioned", payload=payload,
            idempotency_key=f"{invocation.operation_id}:transition:{suffix}",
        )

    def _read_handler(self):
        def handler(invocation: StageInvocation) -> StageResult:
            ref = self.artifacts.get_ref(
                tenant_id=invocation.job.tenant_id, job_id=invocation.job.job_id,
                logical_key="baseline:support",
            )
            value = self.artifacts.read_json(ref)
            for item in value["passages"]:
                _passage_from_dict(item)
            return StageResult(checkpoint={
                "verified_artifact": ref.checkpoint_value(),
                "passages": len(value["passages"]),
            })

        return handler

    def _propose_handler(self, *, task: BlindTask):
        def handler(invocation: StageInvocation) -> StageResult:
            claim_hash = hashlib.sha256(task.prompt.encode()).hexdigest()
            return StageResult(checkpoint={
                "challenge_id": task.challenge_id,
                "claim_sha256": claim_hash,
                "gold_visible": False,
            })

        return handler

    def _challenge_handler(self, *, task: BlindTask, method: MethodSpec):
        if method.external_counter_retrieval:
            return self._retrieval_handler(
                task=task, intent="counterevidence", artifact_key="baseline:counter",
            )

        def handler(invocation: StageInvocation) -> StageResult:
            ref = self._put(invocation.job, "baseline:counter", {
                "intent": "counterevidence", "passages": [],
            })
            return StageResult(checkpoint={
                "artifact": ref.checkpoint_value(),
                "external_counter_retrieval": False,
            })

        return handler

    def _synthesize_handler(
        self, *, task: BlindTask, method: MethodSpec, run_id: str,
    ):
        def handler(invocation: StageInvocation) -> StageResult:
            decision = self._get(invocation.job, "baseline:decision")
            support = self._read_passages(invocation.job, "baseline:support")
            counter = self._read_passages(invocation.job, "baseline:counter")
            passage_index = {item.passage_id: item for item in support + counter}
            selected = []
            for item in decision["evidence"]:
                passage = passage_index[item["passage_id"]]
                selected.append({
                    "doc_id": passage.doc_id, "relation": item["relation"],
                    "locator": passage.locator,
                    "content_sha256": passage.content_sha256,
                })
            prediction = {
                "schema_version": 1,
                "challenge_id": task.challenge_id,
                "run_id": run_id,
                "method_id": method.method_id,
                "status": "completed",
                "predicted_decision": decision["decision"],
                "counterevidence_probability": decision["counterevidence_probability"],
                "evidence": selected,
                "calls": invocation.job.used_calls,
                "tokens": invocation.job.used_tokens,
            }
            commitment = prediction_commitment(prediction)
            ref = self._put(invocation.job, "baseline:prediction", {
                **prediction, "prediction_commitment_sha256": commitment,
            })
            return StageResult(
                events=(EventSpec(
                    aggregate_type="benchmark_prediction",
                    aggregate_id=task.challenge_id,
                    # This is a candidate, not the scored outcome. The final outcome is
                    # committed only after the named validator succeeds or the job fails.
                    event_type="benchmark.prediction_candidate_recorded",
                    payload={
                        "challenge_id": task.challenge_id, "run_id": run_id,
                        "method_id": method.method_id,
                        "prediction_commitment_sha256": commitment,
                    },
                    idempotency_key=f"{invocation.operation_id}:commitment",
                ),),
                checkpoint={"artifact": ref.checkpoint_value()},
            )

        return handler

    @staticmethod
    def _control_finalizer(context: ExecutionContext) -> FinalizationReport:
        def validator(events):
            values = list(events)
            decisions = [event for event in values
                         if event.event_type == "benchmark.decision_recorded"]
            commitments = [event for event in values
                           if event.event_type == "benchmark.prediction_candidate_recorded"]
            issues = []
            if len(decisions) != 1:
                issues.append(f"expected one benchmark decision, found {len(decisions)}")
            if len(commitments) != 1:
                issues.append(
                    f"expected one prediction candidate, found {len(commitments)}"
                )
            return {
                "benchmark_decisions": len(decisions),
                "prediction_candidates": len(commitments),
            }, issues

        return context.finalize_with_validator(
            validator_name="non-cedg-benchmark-control-v1", validator=validator,
        )

    def run_task(
        self, *, task_value: dict[str, Any], method: MethodSpec, run_id: str,
        max_calls: int, max_tokens: int, max_attempts: int = 3,
    ) -> tuple[dict[str, Any], WorkerReport]:
        task = BlindTask.from_dict(task_value)
        if method.memory != "none":
            raise BaselineContractError(
                "memory treatments require the ordered multi-task runner"
            )
        if not method.support_retrieval:
            raise BaselineContractError("V2 baseline contract requires support retrieval")
        if not run_id.strip() or min(max_calls, max_tokens) < 0:
            raise BaselineContractError("run id and non-negative budgets are required")
        job = self.store.create_job(
            tenant_id=self.tenant_id,
            idempotency_key=f"{run_id}:{method.method_id}:{task.challenge_id}",
            task=task.prompt, max_calls=max_calls, max_tokens=max_tokens,
            max_cost_microunits=0,
        )
        handlers = {
            Stage.PLAN: self._plan_handler(task=task, method=method),
            Stage.RETRIEVE: self._retrieval_handler(
                task=task, intent="support", artifact_key="baseline:support",
            ),
            Stage.READ: self._read_handler(),
            Stage.PROPOSE: self._propose_handler(task=task),
            Stage.CHALLENGE: self._challenge_handler(task=task, method=method),
            Stage.DECIDE: self._decide_handler(task=task, method=method),
            Stage.SYNTHESIZE: self._synthesize_handler(
                task=task, method=method, run_id=run_id,
            ),
        }
        worker = ExecutionWorker(
            store=self.store, ledger_root=self.ledger_root, worker_id=self.worker_id,
            handlers=handlers, lease_seconds=self.lease_seconds,
            max_attempts=max_attempts,
            finalizer=None if method.cedg else self._control_finalizer,
            job_id=job.job_id,
        )
        report = worker.run_once()
        # RETRY_WAIT is durable state, not a completed invocation. The task runner owns the
        # bounded resume loop so a one-shot experiment driver cannot accidentally drop retries.
        while report.status == JobStatus.RETRY_WAIT.value:
            report = worker.run_once()
        current = self.store.get(job.job_id)
        ledger_relpath = f"{current.tenant_id}/{current.job_id}/events.jsonl"
        ledger = EventLedger(self.ledger_root / ledger_relpath)
        if current.status == JobStatus.SUCCEEDED:
            prediction = self._get(current, "baseline:prediction")
            commitment = prediction.pop("prediction_commitment_sha256")
            outcome = "completed"
        elif current.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
            prediction = {
                "schema_version": 1,
                "challenge_id": task.challenge_id,
                "run_id": run_id,
                "method_id": method.method_id,
                "status": "failed",
                "predicted_decision": ClaimState.UNRESOLVED.value,
                "counterevidence_probability": 0.5,
                "evidence": [],
                "calls": current.used_calls,
                "tokens": current.used_tokens,
            }
            commitment = prediction_commitment(prediction)
            self._put(current, "baseline:failure_prediction", {
                **prediction,
                "prediction_commitment_sha256": commitment,
                "failure_code": report.error_code or current.last_error_code or "CANCELLED",
            })
            outcome = "failed"
        else:
            raise BaselineContractError(
                f"baseline task stopped in nonterminal state {current.status.value}"
            )
        ledger.append(
            tenant_id=current.tenant_id,
            job_id=current.job_id,
            aggregate_type="benchmark_prediction",
            aggregate_id=task.challenge_id,
            event_type="benchmark.prediction_committed",
            payload={
                "challenge_id": task.challenge_id,
                "run_id": run_id,
                "method_id": method.method_id,
                "status": outcome,
                "prediction_commitment_sha256": commitment,
            },
            idempotency_key=(
                f"outcome:{run_id}:{method.method_id}:{task.challenge_id}:{outcome}"
            ),
        )
        receipt = ledger.verify()
        if not receipt.ok:
            raise BaselineContractError("terminal task has an invalid ledger")
        completed = {
            **prediction,
            "ledger_head": receipt.head_hash,
            "ledger_event_count": receipt.event_count,
            "ledger_relpath": ledger_relpath,
            "prediction_commitment_sha256": commitment,
        }
        if prediction_commitment(completed) != commitment:
            raise BaselineContractError("stored prediction commitment is inconsistent")
        return completed, report
