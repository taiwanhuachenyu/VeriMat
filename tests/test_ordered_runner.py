import hashlib

from src.evaluation.baseline_runner import (
    DecisionOutput,
    EvidenceSelection,
    MethodSpec,
    QueryPlan,
    RetrievalResult,
    RetrievedPassage,
    StrategyCandidate,
    Usage,
)
from src.evaluation.ordered_runner import (
    MEMORY_MARKER,
    CreditOutcome,
    OrderedBenchmarkRunner,
)
from src.evidence.ledger import EventLedger
from src.learning.policy_store import PolicyStore
from src.orchestration.artifacts import ArtifactStore
from src.orchestration.job_store import JobStore


def _tasks(count=5):
    return [{
        "schema_version": 1,
        "challenge_id": f"challenge-{index}",
        "benchmark_track": "known_answer",
        "split": "development",
        "task_family": f"family-{index}",
        "prompt": f"Assess fixture claim {index}",
        "cutoff_date": "2020-01-01",
    } for index in range(count)]


def _method(identifier, memory):
    return MethodSpec(
        method_id=identifier, support_retrieval=True,
        external_counter_retrieval=True, cedg=True, memory=memory,
        decision_mode="verifier",
    )


class StrategyBackend:
    provider_id = "fixture-model"

    def plan_queries(self, *, task, intent, operation_id):
        del operation_id
        return QueryPlan((f"{task.challenge_id} {intent}",), Usage(0, 0))

    def decide(
        self, *, task, method, support_passages, counter_passages, operation_id,
    ):
        del task, method, support_passages, operation_id
        return DecisionOutput(
            decision="REFUTED", counterevidence_probability=0.9,
            evidence=(EvidenceSelection(
                counter_passages[0].passage_id, "PRECEDENT_FOR",
            ),),
            reason="fixture precedent", boundary="", usage=Usage(0, 0),
            strategy_candidates=(StrategyCandidate(
                kind="precedent_probe",
                pattern="search direct precedent under the same operating conditions",
            ),),
        )


class RecordingRetriever:
    provider_id = "fixture-retrieval"

    def __init__(self):
        self.queries = []

    def search(
        self, *, query_id, query, intent, cutoff_date, operation_id,
        reserve_call=lambda _suboperation: None,
    ):
        del cutoff_date, operation_id
        self.queries.append((intent, query))
        content = query
        passage = RetrievedPassage(
            passage_id=f"{query_id}-{hashlib.sha256(query.encode()).hexdigest()[:12]}",
            query_id=query_id, doc_id=f"doc-{query_id}", text=content,
            locator={"offset": 0},
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            publication_date="2019-01-01",
        )
        return RetrievalResult((passage,), Usage(1, 0))


class PassingEvaluator:
    def __init__(self):
        self.calls = []

    def evaluate(self, *, challenge_id, prediction):
        self.calls.append((challenge_id, prediction["status"]))
        return CreditOutcome(
            evaluator_kind="known_answer", success=True,
            false_gap_avoided=True, valid_finding_delta=0.0,
            evidence_ref=f"sealed-fixture:{challenge_id}",
        )


def _runner(tmp_path, retriever):
    return OrderedBenchmarkRunner(
        store=JobStore(tmp_path / "jobs.db"),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        ledger_root=tmp_path / "ledgers",
        policy_store=PolicyStore(tmp_path / "policy.db"),
        policy_ledger=EventLedger(tmp_path / "policy-ledger.jsonl"),
        backend=StrategyBackend(), retriever=retriever,
        worker_id="ordered-worker", tenant_id="ordered-tenant",
    )


def test_uncredited_replay_uses_memory_but_never_creates_credit(tmp_path):
    retriever = RecordingRetriever()
    runner = _runner(tmp_path, retriever)
    predictions, report = runner.run(
        task_values=_tasks(3),
        method=_method("cedg_uncredited_replay", "uncredited_replay"),
        run_id="uncredited-run", max_calls=24, max_tokens=30000,
    )
    counter_queries = [query for intent, query in retriever.queries
                       if intent == "counterevidence"]
    assert MEMORY_MARKER not in counter_queries[0]
    assert all(MEMORY_MARKER in query for query in counter_queries[1:])
    assert all(row["status"] == "completed" for row in predictions)
    assert report["policy_snapshot"]["credited_outcomes"] == 0
    assert report["policy_snapshot"]["sequence_interventions"] == 3


def test_delayed_credit_promotes_after_cross_family_outcomes_and_replays_exactly(tmp_path):
    retriever, evaluator = RecordingRetriever(), PassingEvaluator()
    runner = _runner(tmp_path, retriever)
    kwargs = dict(
        task_values=_tasks(5),
        method=_method("cedg_delayed_credit", "delayed_external_credit"),
        run_id="credited-run", max_calls=24, max_tokens=30000,
        outcome_evaluator=evaluator,
    )
    predictions, report = runner.run(**kwargs)
    assert len(predictions) == 5
    assert len(evaluator.calls) == 4
    assert report["policy_snapshot"]["credited_outcomes"] == 4
    assert report["policy_snapshot"]["strategies"][0]["status"] == "ACTIVE"
    assert report["steps"][0]["strategy_ids"] == []
    assert all(step["strategy_applied"] for step in report["steps"][1:])

    replay_predictions, replay_report = runner.run(**kwargs)
    assert replay_predictions == predictions
    assert replay_report["policy_ledger_head"] == report["policy_ledger_head"]
    assert len(evaluator.calls) == 4  # already credited applications are not re-evaluated
