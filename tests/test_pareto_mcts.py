from src.discovery.pareto_mcts import (
    EvidenceBoundHypothesis,
    Expansion,
    ObjectiveVector,
    ParetoMCTS,
    SearchResult,
    default_acceptance_gate,
    dominates,
    pareto_front,
)


def hypothesis(name: str, *, complete: bool = True) -> EvidenceBoundHypothesis:
    return EvidenceBoundHypothesis(
        hypothesis_id=name,
        claim=f"bounded claim {name}",
        boundary="room temperature; declared protocol" if complete else "",
        support_evidence=("doi:10.example/test@120",),
        counter_queries=("failure boundary precedent",),
        physical_checks=("dimensional consistency",),
    )


def vector(*values: float) -> ObjectiveVector:
    return ObjectiveVector(*values)


def test_objective_vector_rejects_out_of_range_values():
    try:
        vector(1.1, 1, 1, 1, 1, 1, 1)
    except ValueError as exc:
        assert "predictive_utility" in str(exc)
    else:
        raise AssertionError("out-of-range objective was accepted")


def test_pareto_dominance_and_exact_front():
    strong = SearchResult(hypothesis("strong"), vector(0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9), (), 0)
    weak = SearchResult(hypothesis("weak"), vector(0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4), (), 1)
    tradeoff = SearchResult(hypothesis("tradeoff"), vector(1.0, 0.5, 1.0, 0.5, 1.0, 0.5, 1.0), (), 2)
    assert dominates(strong.objectives, weak.objectives)
    assert not dominates(strong.objectives, tradeoff.objectives)
    assert [x.hypothesis.hypothesis_id for x in pareto_front([weak, tradeoff, strong])] == ["strong", "tradeoff"]


def test_gate_keeps_acceptance_outside_the_proposal_model():
    proposed_without_boundary = hypothesis("plausible-language-only", complete=False)
    accepted, reason = default_acceptance_gate(proposed_without_boundary, vector(1, 1, 1, 1, 1, 1, 1))
    assert not accepted
    assert reason == "missing_boundary"


def test_search_is_deterministic_and_preserves_tradeoffs():
    root = hypothesis("root")
    options = {
        "root": [
            Expansion("improve evidence", hypothesis("evidence"), 0.7),
            Expansion("improve validation", hypothesis("validation"), 0.3),
            Expansion("invalid shortcut", hypothesis("invalid", complete=False), 1.0),
        ]
    }
    scores = {
        "root": vector(0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1),
        "evidence": vector(0.7, 1.0, 0.9, 0.3, 0.9, 0.9, 0.8),
        "validation": vector(0.8, 0.7, 0.7, 1.0, 0.8, 0.9, 0.7),
        "invalid": vector(1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
    }

    def expand(item):
        return options.get(item.hypothesis_id, [])

    search = ParetoMCTS(expander=expand, evaluator=lambda item: scores[item.hypothesis_id], max_depth=2)
    report1 = search.search(root, iterations=20)
    report2 = search.search(root, iterations=20)
    ids = {item.hypothesis.hypothesis_id for item in report1.pareto_archive}
    assert ids == {"evidence", "validation"}
    assert report1.rejected_by_gate > 0
    assert report1.trace == report2.trace


def test_invalid_evidence_locator_is_rejected():
    item = EvidenceBoundHypothesis(
        hypothesis_id="bad-locator",
        claim="claim",
        boundary="boundary",
        support_evidence=("doi-only",),
        counter_queries=("counter",),
        physical_checks=("physics",),
    )
    accepted, reason = default_acceptance_gate(item, vector(1, 1, 1, 1, 1, 1, 1))
    assert not accepted
    assert reason == "invalid_evidence_locator"
