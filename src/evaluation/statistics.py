"""Deterministic cluster-aware paired comparisons for frozen V2 outputs."""
from __future__ import annotations

import itertools
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable


class StatisticsError(ValueError):
    pass


@dataclass(frozen=True)
class MetricSpec:
    name: str
    value: Callable[[dict[str, Any]], float]
    applicable: Callable[[dict[str, Any]], bool] = lambda _row: True
    higher_is_better: bool = True


METRICS = (
    MetricSpec("decision_accuracy", lambda row: float(row["decision_correct"])),
    MetricSpec(
        "counterevidence_recall", lambda row: float(row["true_positive"]),
        applicable=lambda row: bool(row["counterevidence_label"]),
    ),
    MetricSpec(
        "counterevidence_false_positive_rate", lambda row: float(row["false_positive"]),
        applicable=lambda row: not bool(row["counterevidence_label"]),
        higher_is_better=False,
    ),
    MetricSpec("evidence_replay_precision", lambda row: float(row["replay_precision"])),
    MetricSpec("brier_score", lambda row: float(row["brier"]), higher_is_better=False),
    MetricSpec("completed_rate", lambda row: float(row["completed"])),
    MetricSpec(
        "budget_compliance_rate", lambda row: float(not row["over_budget"]),
    ),
)


def _index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed = {str(row["challenge_id"]): row for row in rows}
    if len(indexed) != len(rows):
        raise StatisticsError("duplicate challenge IDs in scored items")
    return indexed


def _cluster_differences(
    reference: list[dict[str, Any]], treatment: list[dict[str, Any]], metric: MetricSpec,
) -> tuple[list[float], int]:
    left, right = _index(reference), _index(treatment)
    if set(left) != set(right):
        raise StatisticsError("paired methods do not cover identical challenge IDs")
    grouped: dict[str, list[float]] = defaultdict(list)
    observations = 0
    for identifier in sorted(left):
        ref, test = left[identifier], right[identifier]
        if (
            ref.get("leakage_group") != test.get("leakage_group")
            or ref.get("task_family") != test.get("task_family")
        ):
            raise StatisticsError("paired rows disagree on cluster metadata")
        if not metric.applicable(ref):
            continue
        if not metric.applicable(test):
            raise StatisticsError("metric applicability differs between paired rows")
        grouped[str(ref["leakage_group"])].append(
            metric.value(test) - metric.value(ref)
        )
        observations += 1
    differences = [sum(values) / len(values) for _, values in sorted(grouped.items())]
    return differences, observations


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _bootstrap_ci(
    differences: list[float], *, iterations: int, seed: int,
) -> tuple[float, float]:
    rng = random.Random(seed)
    count = len(differences)
    samples = [
        sum(differences[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(iterations)
    ]
    return _quantile(samples, 0.025), _quantile(samples, 0.975)


def _paired_sign_pvalue(
    differences: list[float], *, iterations: int, seed: int,
) -> tuple[float, str, int]:
    observed = abs(sum(differences) / len(differences))
    count = len(differences)
    if count <= 20:
        signs = itertools.product((-1, 1), repeat=count)
        total, extreme = 0, 0
        for values in signs:
            statistic = abs(sum(sign * value for sign, value in zip(values, differences)) / count)
            total += 1
            extreme += statistic >= observed - 1e-15
        return extreme / total, "exact_cluster_sign_flip", total
    rng = random.Random(seed)
    extreme = 0
    for _ in range(iterations):
        statistic = abs(sum(
            (-1 if rng.randrange(2) == 0 else 1) * value for value in differences
        ) / count)
        extreme += statistic >= observed - 1e-15
    return (extreme + 1) / (iterations + 1), "monte_carlo_cluster_sign_flip", iterations


def _holm(results: list[dict[str, Any]]) -> None:
    ordered = sorted(enumerate(results), key=lambda item: item[1]["p_value_raw"])
    running = 0.0
    total = len(ordered)
    for rank, (index, result) in enumerate(ordered):
        adjusted = min(1.0, (total - rank) * result["p_value_raw"])
        running = max(running, adjusted)
        results[index]["p_value_holm"] = running


def compare_methods(
    *, reference_rows: list[dict[str, Any]], treatment_rows: list[dict[str, Any]],
    bootstrap_iterations: int = 10000, seed: int = 20260812,
) -> list[dict[str, Any]]:
    if bootstrap_iterations < 100:
        raise StatisticsError("at least 100 bootstrap iterations are required")
    results = []
    for metric_index, metric in enumerate(METRICS):
        differences, observations = _cluster_differences(
            reference_rows, treatment_rows, metric,
        )
        if not differences:
            continue
        effect = sum(differences) / len(differences)
        lower, upper = _bootstrap_ci(
            differences, iterations=bootstrap_iterations,
            seed=seed + metric_index * 1009,
        )
        p_value, test, permutations = _paired_sign_pvalue(
            differences, iterations=bootstrap_iterations,
            seed=seed + metric_index * 2027,
        )
        results.append({
            "metric": metric.name,
            "effect_treatment_minus_reference": effect,
            "higher_is_better": metric.higher_is_better,
            "ci_95_cluster_bootstrap": [lower, upper],
            "p_value_raw": p_value,
            "test": test,
            "permutations": permutations,
            "clusters": len(differences),
            "observations": observations,
        })
    _holm(results)
    return results
