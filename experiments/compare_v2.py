#!/usr/bin/env python3
"""Run preregistered cluster-aware paired comparisons over scored V2 method directories."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.statistics import StatisticsError, compare_methods


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _method(value: str) -> tuple[str, Path]:
    identifier, separator, directory = value.partition("=")
    if not separator or not identifier.strip() or not directory.strip():
        raise argparse.ArgumentTypeError("method must be METHOD_ID=DIRECTORY")
    return identifier, Path(directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-manifest", required=True, type=Path)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--method", action="append", required=True, type=_method)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bootstrap-iterations", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260812)
    args = parser.parse_args(argv)
    methods = dict(args.method)
    if len(methods) != len(args.method) or args.reference not in methods:
        parser.error("method IDs must be unique and include the reference")
    benchmark = json.loads(args.benchmark_manifest.read_text(encoding="utf-8"))
    expected_hash = benchmark.get("challenge_sha256")
    summaries, rows, inputs = {}, {}, {}
    for identifier, directory in methods.items():
        summary_path, item_path = directory / "summary.json", directory / "per_challenge.jsonl"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("method_id") != identifier:
            parser.error(f"summary method mismatch for {identifier}")
        if summary.get("challenge_sha256") != expected_hash:
            parser.error(f"challenge hash mismatch for {identifier}")
        summaries[identifier], rows[identifier] = summary, _rows(item_path)
        inputs[identifier] = {
            "summary_sha256": _sha256(summary_path),
            "per_challenge_sha256": _sha256(item_path),
        }
    comparisons = []
    try:
        for identifier in methods:
            if identifier == args.reference:
                continue
            comparisons.append({
                "reference": args.reference,
                "treatment": identifier,
                "metrics": compare_methods(
                    reference_rows=rows[args.reference],
                    treatment_rows=rows[identifier],
                    bootstrap_iterations=args.bootstrap_iterations,
                    seed=args.seed,
                ),
            })
    except StatisticsError as exc:
        parser.error(str(exc))
    scientific_result = bool(
        benchmark.get("publication_ready")
        and all(summary.get("scientific_result") is not False
                for summary in summaries.values())
    )
    report = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "scientific_result": scientific_result,
        "disclaimer": (
            "Confirmatory comparison requires a publication-ready benchmark and non-sanity "
            "method inputs. Current inputs do not satisfy that gate."
            if not scientific_result else ""
        ),
        "benchmark_manifest_sha256": _sha256(args.benchmark_manifest),
        "challenge_sha256": expected_hash,
        "reference": args.reference,
        "bootstrap_iterations": args.bootstrap_iterations,
        "seed": args.seed,
        "cluster_unit": "leakage_group",
        "multiplicity": "Holm correction within each treatment/reference comparison",
        "effect_definition": "treatment minus reference; inspect higher_is_better",
        "inputs": inputs,
        "comparisons": comparisons,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
