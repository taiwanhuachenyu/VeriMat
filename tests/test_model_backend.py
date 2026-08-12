import hashlib
import json

import pytest

from src.evaluation.baseline_runner import (
    BaselineContractError, BlindTask, MethodSpec, RetrievedPassage,
)
from src.evaluation.model_backend import (
    ModelResponse, ProviderProvenance, StructuredModelBackend,
)


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, *, operation_id, system, user, response_schema):
        self.calls.append({
            "operation_id": operation_id, "system": system,
            "user": json.loads(user), "schema": response_schema,
        })
        return self.responses.pop(0)


def _response(value, *, tokens=(10, 5)):
    text = value if isinstance(value, str) else json.dumps(value)
    return ModelResponse(
        text=text, input_tokens=tokens[0], output_tokens=tokens[1],
        request_id="request",
    )


def _task():
    return BlindTask.from_dict({
        "schema_version": 1, "challenge_id": "challenge",
        "benchmark_track": "known_answer", "split": "development",
        "task_family": "family", "prompt": "Assess a claim",
        "cutoff_date": "2020-01-01",
    })


def _method(*, cedg=True, mode="verifier"):
    return MethodSpec(
        method_id="method", support_retrieval=True,
        external_counter_retrieval=mode == "verifier",
        cedg=cedg, memory="none", decision_mode=mode,
    )


def _passage(identifier, query_id):
    text = f"passage {identifier}"
    return RetrievedPassage(
        passage_id=identifier, query_id=query_id, doc_id=f"doc-{identifier}",
        text=text, locator={"offset": 0},
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        publication_date="2019-01-01",
    )


def _backend(transport):
    return StructuredModelBackend(
        transport=transport,
        provenance=ProviderProvenance(
            route_id="volcengine-plan", request_alias="ark-code-latest",
            operator_declared_backend="claude-opus-4-8",
        ),
    )


def test_planner_receives_only_blind_task_and_uses_reported_usage():
    transport = QueueTransport([_response({"queries": ["query one"]})])
    plan = _backend(transport).plan_queries(
        task=_task(), intent="counterevidence", operation_id="operation",
    )
    assert plan.queries == ("query one",)
    assert plan.usage.calls == 1 and plan.usage.tokens == 15
    task_payload = transport.calls[0]["user"]["task"]
    assert set(task_payload) == {
        "schema_version", "challenge_id", "benchmark_track", "split",
        "task_family", "prompt", "cutoff_date",
    }
    assert "expected_decision" not in transport.calls[0]["user"]


def test_verifier_decision_is_strict_and_cites_only_exposed_passages():
    value = {
        "decision": "REFUTED", "counterevidence_probability": 0.9,
        "evidence": [{"passage_id": "counter", "relation": "PRECEDENT_FOR"}],
        "reason": "direct precedent", "boundary": "",
        "strategy_candidates": [{
            "kind": "precedent_probe", "pattern": "search direct precedent",
        }],
    }
    transport = QueueTransport([_response(value)])
    output = _backend(transport).decide(
        task=_task(), method=_method(),
        support_passages=(_passage("support", "support-0"),),
        counter_passages=(_passage("counter", "counterevidence-0"),),
        operation_id="decision-operation",
    )
    assert output.decision == "REFUTED"
    assert output.usage.tokens == 15
    assert transport.calls[0]["user"]["decision_mode"] == "verifier"


def test_backend_rejects_fences_extra_fields_and_unexposed_citations():
    fenced = QueueTransport([_response('```json\n{"queries":["q"]}\n```')])
    with pytest.raises(BaselineContractError, match="without a code fence"):
        _backend(fenced).plan_queries(
            task=_task(), intent="support", operation_id="operation",
        )

    bad = {
        "decision": "REFUTED", "counterevidence_probability": 0.9,
        "evidence": [{"passage_id": "hidden", "relation": "PRECEDENT_FOR"}],
        "reason": "reason", "boundary": "", "strategy_candidates": [],
    }
    with pytest.raises(BaselineContractError, match="not exposed"):
        _backend(QueueTransport([_response(bad)])).decide(
            task=_task(), method=_method(), support_passages=(),
            counter_passages=(_passage("counter", "counterevidence-0"),),
            operation_id="operation",
        )


def test_provider_manifest_separates_alias_from_unattested_backend_identity():
    provenance = _backend(QueueTransport([])).provenance.manifest()
    assert provenance["request_alias"] == "ark-code-latest"
    assert provenance["operator_declared_backend"] == "claude-opus-4-8"
    assert not provenance["backend_independently_attested"]
