"""Counterevidence-aware Pareto Monte Carlo tree search.

The language model, if one is attached, is only an ``expander``: it may propose
hypotheses and priors.  Acceptance is controlled by an external evidence gate,
an objective evaluator, and a non-dominated archive.  This keeps "I proposed a
plausible sentence" separate from "the system accepted a discovery".

All objectives are normalized to [0, 1] and maximized.  Search uses a fixed bank
of scalarizations for deterministic Pareto exploration while the returned
archive is computed using exact Pareto dominance, not the scalarized scores.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence


OBJECTIVE_NAMES = (
    "predictive_utility",
    "evidence_sufficiency",
    "counterevidence_survival",
    "database_validation",
    "falsifiability",
    "physical_validity",
    "simplicity",
)


@dataclass(frozen=True)
class EvidenceBoundHypothesis:
    """A hypothesis that declares its evidence and falsification boundary."""

    hypothesis_id: str
    claim: str
    boundary: str
    support_evidence: tuple[str, ...] = ()
    counter_queries: tuple[str, ...] = ()
    physical_checks: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict, compare=False, hash=False)

    def canonical_sha256(self) -> str:
        payload = {
            "hypothesis_id": self.hypothesis_id,
            "claim": self.claim,
            "boundary": self.boundary,
            "support_evidence": list(self.support_evidence),
            "counter_queries": list(self.counter_queries),
            "physical_checks": list(self.physical_checks),
            "metadata": dict(self.metadata),
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ObjectiveVector:
    predictive_utility: float
    evidence_sufficiency: float
    counterevidence_survival: float
    database_validation: float
    falsifiability: float
    physical_validity: float
    simplicity: float

    def __post_init__(self) -> None:
        for name, value in zip(OBJECTIVE_NAMES, self.values):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1], got {value!r}")

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(getattr(self, name) for name in OBJECTIVE_NAMES)

    def as_dict(self) -> dict[str, float]:
        return dict(zip(OBJECTIVE_NAMES, self.values))


@dataclass(frozen=True)
class Expansion:
    action: str
    hypothesis: EvidenceBoundHypothesis
    prior: float

    def __post_init__(self) -> None:
        if not self.action.strip():
            raise ValueError("expansion action must be non-empty")
        if not math.isfinite(self.prior) or self.prior < 0:
            raise ValueError("expansion prior must be finite and non-negative")


@dataclass(frozen=True)
class SearchResult:
    hypothesis: EvidenceBoundHypothesis
    objectives: ObjectiveVector
    path: tuple[str, ...]
    iteration: int


@dataclass(frozen=True)
class SearchReport:
    pareto_archive: tuple[SearchResult, ...]
    evaluated: int
    rejected_by_gate: int
    rejected_by_plausibility: int
    pruned: int
    trace: tuple[dict[str, object], ...]


def dominates(left: ObjectiveVector, right: ObjectiveVector, *, eps: float = 1e-12) -> bool:
    """Return whether ``left`` Pareto-dominates ``right``."""

    weakly_better = all(a + eps >= b for a, b in zip(left.values, right.values))
    strictly_better = any(a > b + eps for a, b in zip(left.values, right.values))
    return weakly_better and strictly_better


def pareto_front(results: Iterable[SearchResult]) -> tuple[SearchResult, ...]:
    """Return a stable, de-duplicated exact non-dominated archive."""

    best_by_id: dict[str, SearchResult] = {}
    for result in results:
        previous = best_by_id.get(result.hypothesis.hypothesis_id)
        if previous is None or dominates(result.objectives, previous.objectives):
            best_by_id[result.hypothesis.hypothesis_id] = result
        elif not dominates(previous.objectives, result.objectives):
            # Deterministic tie-break for incomparable revisions with the same ID.
            if result.objectives.values > previous.objectives.values:
                best_by_id[result.hypothesis.hypothesis_id] = result
    candidates = list(best_by_id.values())
    front = [
        item
        for item in candidates
        if not any(dominates(other.objectives, item.objectives) for other in candidates if other is not item)
    ]
    return tuple(sorted(front, key=lambda item: (item.hypothesis.hypothesis_id, item.iteration)))


def default_acceptance_gate(hypothesis: EvidenceBoundHypothesis, objectives: ObjectiveVector) -> tuple[bool, str]:
    """Hard gate that the proposal model cannot override."""

    if not hypothesis.hypothesis_id.strip():
        return False, "missing_hypothesis_id"
    if not hypothesis.claim.strip():
        return False, "missing_claim"
    if not hypothesis.boundary.strip():
        return False, "missing_boundary"
    if not hypothesis.support_evidence:
        return False, "missing_support_evidence"
    if not all("@" in locator and locator.split("@", 1)[1].strip() for locator in hypothesis.support_evidence):
        return False, "invalid_evidence_locator"
    if not hypothesis.counter_queries:
        return False, "counterevidence_search_not_declared"
    if not hypothesis.physical_checks:
        return False, "physical_checks_not_declared"
    if objectives.evidence_sufficiency <= 0:
        return False, "zero_evidence_sufficiency"
    if objectives.physical_validity <= 0:
        return False, "zero_physical_validity"
    return True, "accepted"


@dataclass
class _Node:
    hypothesis: EvidenceBoundHypothesis
    action: str
    prior: float
    parent: "_Node | None" = None
    depth: int = 0
    visits: int = 0
    value_sum: list[float] = field(default_factory=lambda: [0.0] * len(OBJECTIVE_NAMES))
    children: list["_Node"] = field(default_factory=list)
    expanded: bool = False

    @property
    def mean_values(self) -> tuple[float, ...]:
        if not self.visits:
            return (0.0,) * len(OBJECTIVE_NAMES)
        return tuple(value / self.visits for value in self.value_sum)


class ParetoMCTS:
    """Deterministic multi-objective MCTS with an external evidence gate."""

    _WEIGHT_BANK: tuple[tuple[float, ...], ...] = (
        (1, 1, 1, 1, 1, 1, 1),
        (2, 1, 2, 1, 2, 2, 1),
        (1, 2, 1, 2, 1, 2, 1),
        (2, 2, 2, 0.5, 1, 1, 0.5),
        (0.5, 1, 1, 2, 2, 1, 2),
    )

    def __init__(
        self,
        *,
        expander: Callable[[EvidenceBoundHypothesis], Sequence[Expansion]],
        evaluator: Callable[[EvidenceBoundHypothesis], ObjectiveVector],
        gate: Callable[[EvidenceBoundHypothesis, ObjectiveVector], tuple[bool, str]] = default_acceptance_gate,
        plausibility: Callable[[EvidenceBoundHypothesis, ObjectiveVector], tuple[bool, str]] | None = None,
        prune: Callable[[Expansion], tuple[bool, str]] | None = None,
        exploration: float = 1.25,
        max_depth: int = 4,
    ) -> None:
        if exploration < 0:
            raise ValueError("exploration must be non-negative")
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        self.expander = expander
        self.evaluator = evaluator
        self.gate = gate
        self.plausibility = plausibility or (lambda _hypothesis, _objectives: (True, "accepted"))
        self.prune = prune or (lambda _proposal: (True, "accepted"))
        self.exploration = exploration
        self.max_depth = max_depth

    def search(self, root: EvidenceBoundHypothesis, *, iterations: int) -> SearchReport:
        if iterations < 1:
            raise ValueError("iterations must be at least 1")
        root_node = _Node(root, action="ROOT", prior=1.0)
        archive_candidates: list[SearchResult] = []
        trace: list[dict[str, object]] = []
        rejected = 0
        evaluated = 0
        rejected_plausibility = 0

        for iteration in range(iterations):
            weights = self._normalized_weights(self._WEIGHT_BANK[iteration % len(self._WEIGHT_BANK)])
            node = root_node
            path = [root_node]
            while node.expanded and node.children:
                node = self._select_child(node, weights)
                path.append(node)
                if node.visits == 0:
                    break

            # Progressive widening: a leaf is evaluated on the visit that discovers it and is
            # expanded only on a later visit, so action priors are never spent on a node whose
            # own objectives are still unknown.
            if node.visits > 0 and not node.expanded and node.depth < self.max_depth:
                self._expand(node, trace, iteration)
                if node.children:
                    node = self._select_child(node, weights)
                    path.append(node)

            objectives = self.evaluator(node.hypothesis)
            evaluated += 1
            plausible, plausibility_reason = self.plausibility(node.hypothesis, objectives)
            if plausible:
                accepted, reason = self.gate(node.hypothesis, objectives)
            else:
                accepted = False
                reason = f"plausibility:{plausibility_reason}"
                rejected_plausibility += 1
            reward = objectives.values if accepted else (0.0,) * len(OBJECTIVE_NAMES)
            if accepted:
                archive_candidates.append(
                    SearchResult(
                        hypothesis=node.hypothesis,
                        objectives=objectives,
                        path=tuple(item.action for item in path[1:]),
                        iteration=iteration,
                    )
                )
            else:
                rejected += 1
            self._backpropagate(path, reward)
            trace.append(
                {
                    "event": "evaluation",
                    "iteration": iteration,
                    "hypothesis_id": node.hypothesis.hypothesis_id,
                    "hypothesis_sha256": node.hypothesis.canonical_sha256(),
                    "accepted": accepted,
                    "gate_reason": reason,
                    "objectives": objectives.as_dict(),
                    "path": [item.action for item in path[1:]],
                }
            )

        return SearchReport(
            pareto_archive=pareto_front(archive_candidates),
            evaluated=evaluated,
            rejected_by_gate=rejected,
            rejected_by_plausibility=rejected_plausibility,
            pruned=sum(1 for item in trace if item.get("event") == "prune"),
            trace=tuple(trace),
        )

    def _expand(self, node: _Node, trace: list[dict[str, object]], iteration: int) -> None:
        proposals = sorted(
            self.expander(node.hypothesis),
            key=lambda item: (-item.prior, item.action, item.hypothesis.hypothesis_id),
        )
        seen: set[str] = set()
        for proposal in proposals:
            digest = proposal.hypothesis.canonical_sha256()
            if digest in seen:
                continue
            seen.add(digest)
            admitted, reason = self.prune(proposal)
            if not admitted:
                trace.append(
                    {
                        "event": "prune",
                        "iteration": iteration,
                        "parent_hypothesis_id": node.hypothesis.hypothesis_id,
                        "hypothesis_id": proposal.hypothesis.hypothesis_id,
                        "hypothesis_sha256": digest,
                        "reason": reason,
                    }
                )
                continue
            node.children.append(
                _Node(
                    hypothesis=proposal.hypothesis,
                    action=proposal.action,
                    prior=proposal.prior,
                    parent=node,
                    depth=node.depth + 1,
                )
            )
        node.expanded = True
        trace.append(
            {
                "event": "expansion",
                "iteration": iteration,
                "hypothesis_id": node.hypothesis.hypothesis_id,
                "children": [child.hypothesis.hypothesis_id for child in node.children],
            }
        )

    def _select_child(self, node: _Node, weights: tuple[float, ...]) -> _Node:
        parent_scale = math.sqrt(max(1, node.visits))

        def score(child: _Node) -> tuple[float, float, str]:
            exploitation = sum(weight * value for weight, value in zip(weights, child.mean_values))
            exploration = self.exploration * child.prior * parent_scale / (1 + child.visits)
            return exploitation + exploration, child.prior, child.hypothesis.hypothesis_id

        return max(node.children, key=score)

    @staticmethod
    def _backpropagate(path: Sequence[_Node], reward: Sequence[float]) -> None:
        for node in path:
            node.visits += 1
            for index, value in enumerate(reward):
                node.value_sum[index] += value

    @staticmethod
    def _normalized_weights(weights: Sequence[float]) -> tuple[float, ...]:
        total = sum(weights)
        return tuple(value / total for value in weights)
