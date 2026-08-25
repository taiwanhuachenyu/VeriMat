#!/usr/bin/env python3
"""Render a publication-style summary from checked-in development summaries."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.portability import extended_path


METHODS = {
    "same_budget_rag": "Support RAG",
    "rag_self_critic": "RAG + self-critic",
    "retrieval_verifier": "Retrieval verifier",
    "cedg_no_memory": "CEDG verifier",
}
PERFORMANCE = {
    "decision_accuracy": "Decision accuracy",
    "counterevidence_recall": "Counterevidence recall",
    "evidence_replay_precision": "Evidence replay precision",
}
CALIBRATION = {
    "brier_score": "Brier score",
    "ece_10": "ECE (10 bins)",
}
PALETTE = ["#9AA4B2", "#C7A76C", "#3E7CB1", "#147D84"]


def read_rows(results: Path, metrics: dict[str, str]) -> pd.DataFrame:
    rows = []
    for method_id, method_label in METHODS.items():
        summary = json.loads((results / method_id / "summary.json").read_text(encoding="utf-8"))
        if summary.get("scientific_result") is not False:
            raise ValueError(f"unexpected scientific status for {method_id}")
        for key, label in metrics.items():
            rows.append({"Method": method_label, "Metric": label, "Value": float(summary[key])})
    return pd.DataFrame(rows)


def add_labels(ax, digits: int = 3) -> None:
    for container in ax.containers:
        labels = [f"{bar.get_height():.{digits}g}" for bar in container]
        ax.bar_label(container, labels=labels, padding=2, fontsize=7.5, color="#27313A")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=extended_path, required=True)
    parser.add_argument("--output", type=extended_path, required=True)
    args = parser.parse_args()

    performance = read_rows(args.results, PERFORMANCE)
    calibration = read_rows(args.results, CALIBRATION)
    sns.set_theme(style="whitegrid", context="paper", font_scale=1.0)
    fig, axes = plt.subplots(1, 2, figsize=(11.4, 4.5), dpi=240, gridspec_kw={"width_ratios": [1.55, 1]})

    sns.barplot(
        data=performance, x="Metric", y="Value", hue="Method", hue_order=list(METHODS.values()),
        palette=PALETTE, edgecolor="white", linewidth=0.8, ax=axes[0],
    )
    axes[0].set_ylim(0, 1.03)
    axes[0].set_xlabel("")
    axes[0].set_ylabel("Score")
    axes[0].set_title("a  Evidence-handling performance", loc="left", weight="bold")
    handles, labels = axes[0].get_legend_handles_labels()
    axes[0].legend_.remove()
    add_labels(axes[0])

    sns.barplot(
        data=calibration, x="Metric", y="Value", hue="Method", hue_order=list(METHODS.values()),
        palette=PALETTE, edgecolor="white", linewidth=0.8, ax=axes[1],
    )
    axes[1].set_ylim(0, 0.82)
    axes[1].set_xlabel("")
    axes[1].set_ylabel("Error (lower is better)")
    axes[1].set_title("b  Calibration", loc="left", weight="bold")
    add_labels(axes[1])
    axes[1].legend_.remove()

    for ax in axes:
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.grid(axis="y", color="#DCE1E6", linewidth=0.7)
        ax.grid(axis="x", visible=False)
        ax.tick_params(axis="x", labelrotation=12)

    fig.legend(
        handles, labels, ncol=4, frameon=False, loc="upper center",
        bbox_to_anchor=(0.5, 1.01), fontsize=8.5, columnspacing=1.7, handlelength=2.2,
    )
    fig.text(
        0.5, -0.01,
        "Deterministic offline development benchmark (n=10); engineering feasibility only; scientific_result=false.",
        ha="center", va="bottom", fontsize=8.3, color="#58636E",
    )
    fig.tight_layout(rect=(0, 0.035, 1, 0.94))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
