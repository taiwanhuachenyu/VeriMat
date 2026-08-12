"""Offline VeriMat Pareto-MCTS example with no model or network calls."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.discovery import EvidenceBoundHypothesis, Expansion, ObjectiveVector, ParetoMCTS


def hypothesis(identifier: str, boundary: str) -> EvidenceBoundHypothesis:
    return EvidenceBoundHypothesis(
        hypothesis_id=identifier,
        claim=f"bounded materials relation {identifier}",
        boundary=boundary,
        support_evidence=("doi:10.example/verimat@120",),
        counter_queries=("direct precedent contradictory trend failure boundary",),
        physical_checks=("dimensional consistency", "declared operating conditions"),
    )


ROOT = hypothesis("root", "room temperature; declared protocol")
CANDIDATES = {
    "root": (
        Expansion("increase evidence", hypothesis("evidence-rich", "room temperature"), 0.65),
        Expansion("increase validation", hypothesis("database-rich", "ambient pressure"), 0.35),
    )
}
SCORES = {
    "root": ObjectiveVector(0.1, 0.1, 0.1, 0.1, 0.2, 0.5, 0.8),
    "evidence-rich": ObjectiveVector(0.7, 1.0, 0.9, 0.3, 0.9, 0.9, 0.8),
    "database-rich": ObjectiveVector(0.8, 0.7, 0.7, 1.0, 0.8, 0.9, 0.7),
}


def main() -> None:
    search = ParetoMCTS(
        expander=lambda item: CANDIDATES.get(item.hypothesis_id, ()),
        evaluator=lambda item: SCORES[item.hypothesis_id],
        max_depth=2,
    )
    report = search.search(ROOT, iterations=20)
    print("Pareto archive:")
    for result in report.pareto_archive:
        print(f"- {result.hypothesis.hypothesis_id}: {result.objectives.as_dict()}")


if __name__ == "__main__":
    main()
