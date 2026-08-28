"""Deterministic scoring, calibration, cost accounting and paired statistics.

Everything here is offline and reproducible: given the per-method prediction files and the oracle
result file, every number in the report recomputes to the same value.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.experiments.claims import VerifiedClaim
from src.survey.records import SurveyContractError, normalise_quote

#: Label ↔ oracle-state agreement map.  ``NARROWED`` agrees with ``narrowed`` and earns half
#: credit against ``supported`` (a bounded claim is not wrong, merely less general than the
#: literature later allowed).
AGREEMENT = {
    ("ACCEPTED", "supported"): 1.0,
    ("REFUTED", "contradicted"): 1.0,
    ("NARROWED", "narrowed"): 1.0,
    ("NARROWED", "supported"): 0.5,
    ("UNRESOLVED", "unresolved"): 1.0,
}


def agreement_score(label: str, oracle_state: str) -> float:
    return AGREEMENT.get((label, oracle_state), 0.0)


@dataclass
class MethodScores:
    method: str
    n_claims: int
    decision_accuracy: float
    counterevidence_recall: float
    overclaim_rate: float
    n_contradicted: int
    brier: float | None
    ece: float | None
    replay_precision: float
    tokens: int
    tokens_per_valid: float | None

    def as_dict(self) -> dict[str, Any]:
        def finite(value: float | None, digits: int = 4) -> float | None:
            if value is None or value != value or value in (float("inf"), float("-inf")):
                return None
            return round(value, digits)

        return {
            "method": self.method, "n_claims": self.n_claims,
            "decision_accuracy": finite(self.decision_accuracy),
            "counterevidence_recall": finite(self.counterevidence_recall),
            "overclaim_rate": finite(self.overclaim_rate),
            "n_contradicted": self.n_contradicted,
            "brier": finite(self.brier),
            "ece": finite(self.ece),
            "replay_precision": finite(self.replay_precision),
            "tokens": self.tokens,
            "tokens_per_valid": finite(self.tokens_per_valid, 1),
        }


def replay_precision(predictions: Sequence[VerifiedClaim], passage_text: dict[str, str]) -> float:
    """Fraction of predictions whose claim quote verbatim occurs in its snapshot passage."""
    if not predictions:
        return 0.0
    good = sum(
        1 for p in predictions
        if normalise_quote(p.claim.quote) in normalise_quote(
            passage_text.get(p.claim.passage_id, ""))
    )
    return good / len(predictions)


def score_method(
    method: str, predictions: Sequence[VerifiedClaim], oracle: dict[str, str],
    passage_text: dict[str, str], tokens: int = 0,
) -> MethodScores:
    if not predictions:
        raise SurveyContractError(f"method {method!r} produced no predictions")
    agreements = [agreement_score(p.label, oracle.get(p.claim.claim_id, "unresolved"))
                  for p in predictions]
    decision_accuracy = sum(agreements) / len(agreements)
    contradicted = [p for p in predictions if oracle.get(p.claim.claim_id) == "contradicted"]
    recall = (
        sum(1 for p in contradicted if p.label in {"REFUTED", "NARROWED"}) / len(contradicted)
        if contradicted else float("nan")
    )
    overclaim = (
        sum(1 for p in predictions if p.label == "ACCEPTED"
            and oracle.get(p.claim.claim_id) == "contradicted") / len(predictions)
    )
    # Calibration on the survival event: confidence is the method's confidence the claim
    # survives; the oracle outcome is whether it did.
    decided = [p for p in predictions
               if oracle.get(p.claim.claim_id) in {"supported", "contradicted"}]
    brier = ece = None
    if decided:
        outcomes = [0.0 if oracle[p.claim.claim_id] == "contradicted" else 1.0 for p in decided]
        brier = sum((p.confidence - o) ** 2 for p, o in zip(decided, outcomes)) / len(decided)
        bins: dict[int, list[float]] = {}
        for p, o in zip(decided, outcomes):
            bins.setdefault(min(9, int(p.confidence * 10)), []).append(o - p.confidence)
        ece = sum(len(v) / len(decided) * abs(sum(v) / len(v)) for v in bins.values())
    valid = sum(
        1 for p in predictions
        if agreement_score(p.label, oracle.get(p.claim.claim_id, "unresolved")) >= 1.0
    )
    return MethodScores(
        method=method, n_claims=len(predictions), decision_accuracy=decision_accuracy,
        counterevidence_recall=recall, overclaim_rate=overclaim,
        n_contradicted=len(contradicted), brier=brier, ece=ece,
        replay_precision=replay_precision(predictions, passage_text), tokens=tokens,
        tokens_per_valid=(tokens / valid) if valid else None,
    )


def false_gap_rate(gaps_novel: Sequence[str], addressed: Iterable[str]) -> float:
    """Share of system-declared-new gaps the validation window already addresses."""
    novel = list(gaps_novel)
    if not novel:
        return float("nan")
    hit = sum(1 for gap_id in novel if gap_id in set(addressed))
    return hit / len(novel)


# ------------------------------------------------------------------------------- statistics

def _perm_p_value(left: Sequence[float], right: Sequence[float], *, n: int = 20000,
                  seed: int = 20260903) -> float:
    """Two-sided paired permutation test on the mean difference."""
    if len(left) != len(right) or not left:
        return float("nan")
    observed = sum(a - b for a, b in zip(left, right)) / len(left)
    diffs = [a - b for a, b in zip(left, right)]
    rng = random.Random(seed)
    extreme = 0
    for _ in range(n):
        total = sum(d if rng.random() < 0.5 else -d for d in diffs)
        if abs(total / len(diffs)) >= abs(observed) - 1e-12:
            extreme += 1
    return (extreme + 1) / (n + 1)


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm–Bonferroni step-down adjusted p-values, monotone and bounded by 1."""
    order = sorted(range(len(p_values)), key=lambda i: p_values[i])
    adjusted = [0.0] * len(p_values)
    running = 0.0
    m = len(order)
    for rank, index in enumerate(order):
        value = min(1.0, (m - rank) * p_values[index])
        running = max(running, value)
        adjusted[index] = running
    return adjusted


def paired_comparison(
    left: Sequence[VerifiedClaim], right: Sequence[VerifiedClaim], oracle: dict[str, str],
) -> dict[str, Any]:
    """Per-claim paired comparison of two methods on their common claims."""
    right_by_id = {p.claim.claim_id: p for p in right}
    pairs = [
        (p, right_by_id[p.claim.claim_id]) for p in left if p.claim.claim_id in right_by_id
    ]
    if not pairs:
        return {"n_common": 0}
    l_scores = [agreement_score(p.label, oracle.get(p.claim.claim_id, "unresolved"))
                for p, _ in pairs]
    r_scores = [agreement_score(q.label, oracle.get(p.claim.claim_id, "unresolved"))
                for p, q in pairs]
    p_value = _perm_p_value(l_scores, r_scores)
    return {
        "n_common": len(pairs),
        "mean_left": round(sum(l_scores) / len(l_scores), 4),
        "mean_right": round(sum(r_scores) / len(r_scores), 4),
        "mean_delta": round(sum(a - b for a, b in zip(l_scores, r_scores)) / len(pairs), 4),
        "p_value": round(p_value, 5) if not math.isnan(p_value) else None,
    }
