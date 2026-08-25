import hashlib

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


def _branching_tree(*, breadth: int, depth: int):
    """A deterministic tree whose node ids encode their own path, so traces are readable."""

    def children(item: EvidenceBoundHypothesis) -> list[Expansion]:
        level = 0 if item.hypothesis_id == "root" else item.hypothesis_id.count("-") + 1
        if level >= depth:
            return []
        parent = "" if item.hypothesis_id == "root" else item.hypothesis_id + "-"
        return [
            Expansion(f"refine {parent}{index}", hypothesis(f"{parent}{index}"), 1.0 / (index + 1))
            for index in range(breadth)
        ]

    def score(item: EvidenceBoundHypothesis) -> ObjectiveVector:
        digest = hashlib.sha256(item.hypothesis_id.encode()).digest()
        return ObjectiveVector(*(byte / 255 for byte in digest[:7]))

    return children, score


def test_progressive_widening_defers_expansion_until_a_node_is_evaluated():
    """The root is unvisited on iteration 0, so it must be scored before it is widened."""
    children, score = _branching_tree(breadth=3, depth=3)
    search = ParetoMCTS(expander=children, evaluator=score, max_depth=3)
    trace = search.search(hypothesis("root"), iterations=6).trace

    assert [entry["event"] for entry in trace[:3]] == ["evaluation", "expansion", "evaluation"]
    assert trace[0]["hypothesis_id"] == "root"
    assert trace[0]["path"] == []
    assert trace[1]["hypothesis_id"] == "root"
    assert trace[2]["path"] == ["refine 0"]


def test_no_node_is_expanded_before_its_own_evaluation():
    children, score = _branching_tree(breadth=3, depth=4)
    search = ParetoMCTS(expander=children, evaluator=score, max_depth=4)
    trace = search.search(hypothesis("root"), iterations=80).trace

    evaluated: set[str] = set()
    for entry in trace:
        if entry["event"] == "expansion":
            assert entry["hypothesis_id"] in evaluated, (
                f"{entry['hypothesis_id']} was widened before it was ever scored"
            )
        else:
            evaluated.add(entry["hypothesis_id"])


def test_each_node_is_expanded_at_most_once():
    children, score = _branching_tree(breadth=3, depth=4)
    search = ParetoMCTS(expander=children, evaluator=score, max_depth=4)
    trace = search.search(hypothesis("root"), iterations=80).trace

    expanded = [entry["hypothesis_id"] for entry in trace if entry["event"] == "expansion"]
    assert len(expanded) == len(set(expanded))


def test_search_descends_past_depth_two_and_records_consistent_paths():
    children, score = _branching_tree(breadth=2, depth=4)
    search = ParetoMCTS(expander=children, evaluator=score, max_depth=4)
    report = search.search(hypothesis("root"), iterations=120)

    evaluations = [entry for entry in report.trace if entry["event"] == "evaluation"]
    depths = {len(entry["path"]) for entry in evaluations}
    assert max(depths) == 4, f"search never reached max_depth, saw depths {sorted(depths)}"
    assert depths == {0, 1, 2, 3, 4}

    for entry in evaluations:
        # The action names encode the node id, so the path must spell out the node reached.
        expected = "-".join(action.split()[-1].split("-")[-1] for action in entry["path"])
        assert entry["hypothesis_id"] == (expected or "root")


def test_deep_search_is_deterministic_in_trace_and_archive():
    children, score = _branching_tree(breadth=3, depth=3)
    first = ParetoMCTS(expander=children, evaluator=score, max_depth=3).search(
        hypothesis("root"), iterations=60
    )
    second = ParetoMCTS(expander=children, evaluator=score, max_depth=3).search(
        hypothesis("root"), iterations=60
    )
    assert first.trace == second.trace
    assert [item.hypothesis.hypothesis_id for item in first.pareto_archive] == [
        item.hypothesis.hypothesis_id for item in second.pareto_archive
    ]
    assert first.evaluated == second.evaluated == 60


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
