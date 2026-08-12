import pytest

from src.evaluation.statistics import StatisticsError, compare_methods


def _rows(values):
    rows = []
    for index, value in enumerate(values):
        rows.append({
            "challenge_id": f"c{index}", "leakage_group": f"g{index // 2}",
            "task_family": f"f{index}", "decision_correct": bool(value),
            "counterevidence_label": True, "true_positive": bool(value),
            "false_positive": False, "replay_precision": float(value),
            "brier": float(not value), "completed": True, "over_budget": False,
        })
    return rows


def test_cluster_comparison_is_deterministic_and_reports_exact_permutation():
    reference = _rows([0, 0, 0, 0, 0, 0])
    treatment = _rows([1, 1, 1, 1, 1, 1])
    first = compare_methods(
        reference_rows=reference, treatment_rows=treatment,
        bootstrap_iterations=1000, seed=7,
    )
    second = compare_methods(
        reference_rows=reference, treatment_rows=treatment,
        bootstrap_iterations=1000, seed=7,
    )
    assert first == second
    decision = next(row for row in first if row["metric"] == "decision_accuracy")
    assert decision["effect_treatment_minus_reference"] == 1.0
    assert decision["clusters"] == 3
    assert decision["test"] == "exact_cluster_sign_flip"
    assert decision["permutations"] == 8


def test_comparison_rejects_unpaired_or_reclustered_rows():
    reference, treatment = _rows([0, 1]), _rows([1, 1])
    treatment[0]["leakage_group"] = "changed"
    with pytest.raises(StatisticsError, match="cluster metadata"):
        compare_methods(
            reference_rows=reference, treatment_rows=treatment,
            bootstrap_iterations=100,
        )
