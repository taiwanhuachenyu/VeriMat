#!/usr/bin/env python3
"""Compare the failed baseline and bounded recovery experiments without inflating coverage."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "_vm_scratch" / "thermolectric_pilot_20260826" / "experiment_manifest.json"
TARGET = ROOT / "_vm_scratch" / "thermoelectric_targeted_gap_recovery_20260826" / "experiment_manifest.json"
OUT = ROOT / "_vm_scratch" / "thermoelectric_round_comparison_20260826.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    pilot, targeted = load(PILOT), load(TARGET)
    summary = {
        "schema_version": 1,
        "baseline": "thermoelectric_pilot_20260826",
        "targeted_recovery": "thermoelectric_targeted_gap_recovery_20260826",
        "metrics": {
            "documents": {
                "pilot": pilot["corpus"]["n_documents"],
                "targeted": targeted["corpus"]["n_documents"],
            },
            "documents_with_evidence": {
                "pilot": pilot["corpus"].get("n_documents_with_evidence", 1),
                "targeted": targeted["corpus"]["n_documents_with_evidence"],
            },
            "passages": {
                "pilot": pilot["corpus"]["n_passages"],
                "targeted": targeted["corpus"]["n_passages"],
            },
            "queries": {
                "pilot": pilot["corpus"]["n_queries"],
                "targeted": targeted["corpus"]["n_queries"],
            },
            "document_probes": {
                "pilot": 0,
                "targeted": targeted["corpus"]["n_document_probes"],
            },
            "empty_document_probes": {
                "pilot": 0,
                "targeted": targeted["corpus"]["n_empty_document_probes"],
            },
            "relations_proposed": {
                "pilot": pilot["extraction"]["n_proposed"],
                "targeted": targeted["extraction"]["n_proposed"],
            },
            "relations_admitted": {
                "pilot": pilot["extraction"]["n_admitted"],
                "targeted": targeted["extraction"]["n_admitted"],
            },
            "admission_rate": {
                "pilot": pilot["extraction"]["admission_rate"],
                "targeted": targeted["extraction"]["admission_rate"],
            },
            "gap_candidates_started": {
                "pilot": pilot["candidates"]["n_candidates"],
                "targeted": targeted["candidates"]["n_candidates"],
            },
            "gaps_admitted": {
                "pilot": pilot["gaps"]["n_gaps"],
                "targeted": targeted["gaps"]["n_gaps"],
            },
            "gap_model_calls": {
                "pilot": pilot["gaps"]["model_calls"],
                "targeted": targeted["gaps"]["model_calls"],
            },
            "report_verified": {
                "pilot": pilot["report_audit"]["verified"],
                "targeted": targeted["report_audit"]["verified"],
            },
        },
        "interpretation": {
            "retrieval_outcome": (
                "Targeted document probes increased passages and recovered extractable relations, "
                "but evidence remains concentrated in one of four candidate documents."
            ),
            "gap_outcome": (
                "Two admitted unvalidated-mechanism gaps are bounded hypotheses from a single "
                "simulation source. They require independent literature retrieval and database or "
                "experimental validation before any scientific-discovery claim."
            ),
            "coverage_limit": (
                "Six of eight document probes were empty; three candidate documents remain unread. "
                "The recovery evaluated only five candidates reached before the interrupted run, "
                "and 26 deterministic candidates were intentionally not started."
            ),
            "next_gate": (
                "Do not scale gap narration until a new retrieval strategy obtains evidence from "
                "multiple documents, including the 2025 co-doping paper."
            ),
        },
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
