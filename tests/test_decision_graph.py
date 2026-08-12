import hashlib

import pytest

from src.core.events import EventEnvelope
from src.evidence.graph import ClaimState, DecisionGraph, GraphInvariantError


def _event(seq, event_type, payload):
    return EventEnvelope.build(
        sequence=seq, event_id=f"event-{seq}", tenant_id="tenant", job_id="job",
        aggregate_type=event_type.split(".")[0],
        aggregate_id=payload.get("claim_id", payload.get("query_id", "aggregate")),
        event_type=event_type, payload=payload, idempotency_key=f"key-{seq}",
        previous_hash="" if seq == 1 else "0" * 64,
    )


def _supported_graph():
    graph = DecisionGraph()
    graph.apply(_event(1, "claim.proposed", {
        "claim_id": "c1", "text": "A bounded materials claim", "scope": "LLZO",
    }))
    graph.apply(_event(2, "query.executed", {
        "query_id": "q-support", "claim_id": "c1", "text": "support query",
        "intent": "support", "n_hits": 1,
    }))
    graph.apply(_event(3, "passage.observed", {
        "passage_id": "p-support", "doc_id": "doc-support", "query_id": "q-support",
        "offset": 10, "content_sha256": hashlib.sha256(b"support").hexdigest(),
    }))
    graph.apply(_event(4, "evidence.linked", {
        "claim_id": "c1", "passage_id": "p-support", "relation": "SUPPORTS",
    }))
    graph.apply(_event(5, "claim.transitioned", {
        "claim_id": "c1", "to_state": "SUPPORTED", "reason": "support located",
    }))
    return graph


def test_claim_cannot_be_supported_without_supporting_evidence():
    graph = DecisionGraph()
    graph.apply(_event(1, "claim.proposed", {
        "claim_id": "c1", "text": "claim", "scope": "scope",
    }))
    with pytest.raises(GraphInvariantError, match="SUPPORTS edge"):
        graph.apply(_event(2, "claim.transitioned", {
            "claim_id": "c1", "to_state": "SUPPORTED", "reason": "model said so",
        }))


def test_surviving_claim_requires_challenge_and_boundary():
    graph = _supported_graph()
    with pytest.raises(GraphInvariantError, match="counterevidence query"):
        graph.apply(_event(6, "claim.transitioned", {
            "claim_id": "c1", "to_state": "CHALLENGED", "reason": "not executed",
        }))
    graph.apply(_event(7, "query.executed", {
        "query_id": "q-counter", "claim_id": "c1", "text": "counter query",
        "intent": "counterevidence", "n_hits": 0,
    }))
    graph.apply(_event(8, "claim.transitioned", {
        "claim_id": "c1", "to_state": "CHALLENGED", "reason": "query executed",
    }))
    with pytest.raises(GraphInvariantError, match="boundary"):
        graph.apply(_event(9, "claim.transitioned", {
            "claim_id": "c1", "to_state": "SURVIVED", "reason": "no precedent found",
        }))
    graph.apply(_event(10, "claim.transitioned", {
        "claim_id": "c1", "to_state": "SURVIVED", "reason": "no precedent found",
        "boundary": "indexed LLZO literature through the cutoff date",
    }))
    assert graph.claims["c1"].state == ClaimState.SURVIVED
    assert graph.validate_for_publication() == []


def test_refutation_requires_direct_relation():
    graph = _supported_graph()
    graph.apply(_event(6, "query.executed", {
        "query_id": "q-counter", "claim_id": "c1", "text": "counter query",
        "intent": "counterevidence", "n_hits": 0,
    }))
    graph.apply(_event(7, "claim.transitioned", {
        "claim_id": "c1", "to_state": "CHALLENGED", "reason": "query executed",
    }))
    with pytest.raises(GraphInvariantError, match="CONTRADICTS or PRECEDENT_FOR"):
        graph.apply(_event(8, "claim.transitioned", {
            "claim_id": "c1", "to_state": "REFUTED", "reason": "unsupported assertion",
        }))


def test_known_answer_claim_can_be_refuted_without_invented_support():
    graph = DecisionGraph()
    graph.apply(_event(1, "claim.proposed", {
        "claim_id": "c1", "text": "assertion under test", "scope": "benchmark",
    }))
    graph.apply(_event(2, "query.executed", {
        "query_id": "q-counter", "claim_id": "c1", "text": "counterexample",
        "intent": "counterevidence", "n_hits": 1,
    }))
    graph.apply(_event(3, "passage.observed", {
        "passage_id": "p-counter", "doc_id": "doc", "query_id": "q-counter",
        "offset": 0, "content_sha256": hashlib.sha256(b"counterexample").hexdigest(),
    }))
    graph.apply(_event(4, "evidence.linked", {
        "claim_id": "c1", "passage_id": "p-counter", "relation": "CONTRADICTS",
    }))
    graph.apply(_event(5, "claim.transitioned", {
        "claim_id": "c1", "to_state": "CHALLENGED", "reason": "query executed",
    }))
    graph.apply(_event(6, "claim.transitioned", {
        "claim_id": "c1", "to_state": "REFUTED", "reason": "direct evidence",
    }))
    assert graph.claims["c1"].state == ClaimState.REFUTED


def test_passage_requires_locator_and_content_hash():
    graph = DecisionGraph()
    graph.apply(_event(1, "claim.proposed", {
        "claim_id": "c1", "text": "claim", "scope": "scope",
    }))
    graph.apply(_event(2, "query.executed", {
        "query_id": "q1", "claim_id": "c1", "text": "support",
        "intent": "support", "n_hits": 1,
    }))
    with pytest.raises(GraphInvariantError, match="offset or page_no"):
        graph.apply(_event(3, "passage.observed", {
            "passage_id": "p1", "doc_id": "doc", "query_id": "q1",
            "content_sha256": hashlib.sha256(b"text").hexdigest(),
        }))


def test_counter_relation_cannot_be_relabelled_from_support_retrieval():
    graph = DecisionGraph()
    graph.apply(_event(1, "claim.proposed", {
        "claim_id": "c1", "text": "claim", "scope": "scope",
    }))
    graph.apply(_event(2, "query.executed", {
        "query_id": "q1", "claim_id": "c1", "text": "support",
        "intent": "support", "n_hits": 1,
    }))
    graph.apply(_event(3, "passage.observed", {
        "passage_id": "p1", "doc_id": "doc", "query_id": "q1", "offset": 0,
        "content_sha256": hashlib.sha256(b"passage").hexdigest(),
    }))
    with pytest.raises(GraphInvariantError, match="counterevidence query"):
        graph.apply(_event(4, "evidence.linked", {
            "claim_id": "c1", "passage_id": "p1", "relation": "CONTRADICTS",
        }))
