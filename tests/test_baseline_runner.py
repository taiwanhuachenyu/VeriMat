import hashlib

from src.evaluation.baseline_runner import (
    BaselineTaskRunner, DecisionOutput, EvidenceSelection, MethodSpec, QueryPlan,
    RetrievalResult, RetrievedPassage, Usage,
)
from src.orchestration.artifacts import ArtifactStore
from src.orchestration.job_store import JobStore


def _task(identifier="positive"):
    return {
        "schema_version": 1,
        "challenge_id": identifier,
        "benchmark_track": "known_answer",
        "split": "development",
        "task_family": "fixture-family",
        "prompt": f"Assess fixture claim {identifier}",
        "cutoff_date": "2020-01-01",
    }


def _method(identifier, *, counter, cedg):
    return MethodSpec(
        method_id=identifier, support_retrieval=True,
        external_counter_retrieval=counter, cedg=cedg, memory="none",
        decision_mode="verifier" if counter else "direct",
    )


class FixtureBackend:
    provider_id = "fixture-model"

    def __init__(self):
        self.plans = []
        self.decisions = []

    def plan_queries(self, *, task, intent, operation_id):
        self.plans.append((task.challenge_id, intent, operation_id))
        return QueryPlan((f"{intent} {task.challenge_id}",), Usage(calls=1, tokens=10))

    def decide(
        self, *, task, method, support_passages, counter_passages, operation_id,
    ):
        self.decisions.append({
            "task": task, "method": method,
            "support": support_passages, "counter": counter_passages,
        })
        if counter_passages:
            return DecisionOutput(
                decision="REFUTED", counterevidence_probability=0.9,
                evidence=(EvidenceSelection(
                    counter_passages[0].passage_id, "PRECEDENT_FOR",
                ),),
                reason="fixture counterevidence", boundary="", usage=Usage(1, 20),
            )
        return DecisionOutput(
            decision="SURVIVED", counterevidence_probability=0.1,
            evidence=(EvidenceSelection(
                support_passages[0].passage_id, "SUPPORTS",
            ),),
            reason="fixture support", boundary="fixture corpus through cutoff",
            usage=Usage(1, 20),
        )


class FixtureRetriever:
    provider_id = "fixture-retrieval"

    def __init__(self):
        self.searches = []

    def search(
        self, *, query_id, query, intent, cutoff_date, operation_id,
        reserve_call=lambda _suboperation: None,
    ):
        self.searches.append((intent, query, cutoff_date, operation_id))
        identifier = query.rsplit(" ", 1)[-1]
        text = identifier
        passage = RetrievedPassage(
            passage_id=f"{intent}-passage", query_id=query_id,
            doc_id=f"doc-{identifier}", text=text, locator={"offset": 0},
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            publication_date="2019-01-01",
        )
        return RetrievalResult((passage,), Usage(calls=1, tokens=0))


def _runner(tmp_path, backend=None, retriever=None):
    return BaselineTaskRunner(
        store=JobStore(tmp_path / "jobs.db"),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        ledger_root=tmp_path / "ledgers",
        backend=backend or FixtureBackend(),
        retriever=retriever or FixtureRetriever(), worker_id="worker",
    )


def test_same_budget_rag_never_receives_external_counter_passages(tmp_path):
    backend, retriever = FixtureBackend(), FixtureRetriever()
    runner = _runner(tmp_path, backend, retriever)
    prediction, report = runner.run_task(
        task_value=_task(), method=_method("same_budget_rag", counter=False, cedg=False),
        run_id="run", max_calls=24, max_tokens=30000,
    )
    assert report.status == "SUCCEEDED"
    assert [intent for _, intent, _ in backend.plans] == ["support"]
    assert [row[0] for row in retriever.searches] == ["support"]
    assert backend.decisions[0]["counter"] == ()
    assert prediction["predicted_decision"] == "SURVIVED"


def test_retrieval_verifier_gets_separate_counter_query_and_control_validator(tmp_path):
    backend, retriever = FixtureBackend(), FixtureRetriever()
    runner = _runner(tmp_path, backend, retriever)
    prediction, report = runner.run_task(
        task_value=_task(),
        method=_method("retrieval_verifier", counter=True, cedg=False),
        run_id="run", max_calls=24, max_tokens=30000,
    )
    assert [intent for _, intent, _ in backend.plans] == ["support", "counterevidence"]
    assert [row[0] for row in retriever.searches] == ["support", "counterevidence"]
    assert prediction["predicted_decision"] == "REFUTED"
    assert report.finalization.graph_metrics == {
        "benchmark_decisions": 1, "prediction_candidates": 1,
    }


def test_cedg_baseline_replays_direct_refutation_graph(tmp_path):
    runner = _runner(tmp_path)
    prediction, report = runner.run_task(
        task_value=_task(), method=_method("cedg_no_memory", counter=True, cedg=True),
        run_id="run", max_calls=24, max_tokens=30000,
    )
    assert prediction["predicted_decision"] == "REFUTED"
    assert report.finalization.ok
    assert report.finalization.graph_metrics["claims_refuted"] == 1
    assert len(prediction["ledger_head"]) == 64


def test_runner_replays_completed_job_without_duplicate_effects(tmp_path):
    backend, retriever = FixtureBackend(), FixtureRetriever()
    runner = _runner(tmp_path, backend, retriever)
    kwargs = dict(
        task_value=_task(), method=_method("retrieval_verifier", counter=True, cedg=False),
        run_id="run", max_calls=24, max_tokens=30000,
    )
    first, _ = runner.run_task(**kwargs)
    second, report = runner.run_task(**kwargs)
    assert second == first
    assert not report.claimed
    assert len(retriever.searches) == 2


class FailingBackend(FixtureBackend):
    def decide(
        self, *, task, method, support_passages, counter_passages, operation_id,
    ):
        self.decisions.append(operation_id)
        raise RuntimeError("fixture backend failure")


def test_failed_task_is_retained_as_committed_unresolved_row(tmp_path):
    backend = FailingBackend()
    runner = _runner(tmp_path, backend=backend)
    prediction, report = runner.run_task(
        task_value=_task(),
        method=_method("retrieval_verifier", counter=True, cedg=False),
        run_id="failed-run", max_calls=24, max_tokens=30000, max_attempts=3,
    )
    assert report.status == "FAILED"
    assert len(backend.decisions) == 3
    assert prediction["status"] == "failed"
    assert prediction["predicted_decision"] == "UNRESOLVED"
    assert prediction["counterevidence_probability"] == 0.5
    assert prediction["evidence"] == []
    assert len(prediction["ledger_head"]) == 64

    replay, replay_report = runner.run_task(
        task_value=_task(),
        method=_method("retrieval_verifier", counter=True, cedg=False),
        run_id="failed-run", max_calls=24, max_tokens=30000, max_attempts=3,
    )
    assert replay == prediction
    assert not replay_report.claimed
    assert len(backend.decisions) == 3


class PrechargedPlanningFailure(FixtureBackend):
    plan_call_reservation = 1

    def plan_queries(self, *, task, intent, operation_id):
        raise RuntimeError("indeterminate provider call")


def test_failed_external_operation_remains_precharged_once_across_retries(tmp_path):
    runner = _runner(tmp_path, backend=PrechargedPlanningFailure())
    prediction, report = runner.run_task(
        task_value=_task(),
        method=_method("same_budget_rag", counter=False, cedg=False),
        run_id="precharged-failure", max_calls=24, max_tokens=30000,
        max_attempts=3,
    )
    assert report.status == "FAILED"
    assert prediction["status"] == "failed"
    assert prediction["calls"] == 1
