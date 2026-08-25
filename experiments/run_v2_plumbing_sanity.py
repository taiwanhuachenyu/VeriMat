#!/usr/bin/env python3
"""Run the V2 stack without network/model calls; never use as scientific evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.portability import extended_path, fsync_directory
from src.evaluation.baseline_runner import BaselineTaskRunner, MethodSpec
from src.evaluation.blinding import verify_blind_bundle
from src.evaluation.challenge import evaluate_predictions, seal_benchmark
from src.evaluation.offline_sanity import (
    DeterministicPlumbingBackend,
    SnapshotCorpusRetriever,
    assert_sanity_only,
)
from src.orchestration.artifacts import ArtifactStore
from src.orchestration.job_store import JobStore

DISCLAIMER = (
    "ENGINEERING PLUMBING SANITY ONLY. The backend is a deterministic rule fixture, "
    "the retriever searches benchmark evidence capsules, and these scores are not "
    "scientific baseline results and must not appear in a paper Results section."
)
DEFAULT_METHODS = (
    "same_budget_rag", "rag_self_critic", "retrieval_verifier", "cedg_no_memory",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _require_new_output(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)


def run_blind_methods(
    *, tasks_path: Path, task_manifest_path: Path, snapshot_path: Path,
    methods_path: Path, output: Path, method_ids: tuple[str, ...], run_id: str,
) -> list[dict[str, Any]]:
    """Execute methods using only the blind bundle and public evidence snapshots."""
    verify_blind_bundle(task_path=tasks_path, manifest_path=task_manifest_path)
    tasks = _rows(tasks_path)
    if not tasks:
        raise ValueError("blind task bundle is empty")
    backend = DeterministicPlumbingBackend()
    retriever = SnapshotCorpusRetriever(snapshot_path)
    assert_sanity_only(backend, retriever)
    records: list[dict[str, Any]] = []

    for method_id in method_ids:
        method, budget = MethodSpec.load(methods_path, method_id)
        if method.memory != "none":
            raise ValueError("plumbing sanity supports only memory=none treatments")
        method_dir = output / method_id
        method_dir.mkdir(parents=True, exist_ok=False)
        store = JobStore(method_dir / "jobs.db")
        artifacts = ArtifactStore(method_dir / "artifacts")
        try:
            runner = BaselineTaskRunner(
                store=store,
                artifacts=artifacts,
                ledger_root=method_dir / "ledgers",
                backend=backend,
                retriever=retriever,
                worker_id=f"plumbing-{method_id}",
                tenant_id="benchmark-plumbing-sanity",
            )
            predictions = []
            for task in tasks:
                prediction, report = runner.run_task(
                    task_value=task,
                    method=method,
                    run_id=f"{run_id}:{method_id}",
                    max_calls=budget["max_calls"],
                    max_tokens=budget["max_tokens"],
                )
                predictions.append(prediction)
        finally:
            artifacts.close()
            store.close()
        prediction_path = method_dir / "predictions.jsonl"
        _atomic_write(
            prediction_path,
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in predictions
            ),
        )
        records.append({
            "method_id": method_id,
            "budget": budget,
            "predictions": str(prediction_path.relative_to(output)),
            "prediction_sha256": _sha256(prediction_path),
            "ledger_root": str((method_dir / "ledgers").relative_to(output)),
            "tasks_completed": len(predictions),
        })
    return records


def evaluate_after_execution(
    *, challenges_path: Path, output: Path, records: list[dict[str, Any]],
) -> None:
    """Join gold only after every blind execution has completed."""
    for record in records:
        method_dir = output / record["method_id"]
        summary, items = evaluate_predictions(
            challenge_path=challenges_path,
            prediction_path=output / record["predictions"],
            ledger_root=output / record["ledger_root"],
            max_calls=record["budget"]["max_calls"],
            max_tokens=record["budget"]["max_tokens"],
        )
        summary.update({"scientific_result": False, "disclaimer": DISCLAIMER})
        _atomic_write(
            method_dir / "summary.json",
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        _atomic_write(
            method_dir / "per_challenge.jsonl",
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in items
            ),
        )
        record.update({
            "summary": str((method_dir / "summary.json").relative_to(output)),
            "summary_sha256": _sha256(method_dir / "summary.json"),
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DISCLAIMER)
    parser.add_argument("--tasks", required=True, type=extended_path)
    parser.add_argument("--task-manifest", required=True, type=extended_path)
    parser.add_argument("--evidence-snapshots", required=True, type=extended_path)
    parser.add_argument("--challenges", required=True, type=extended_path)
    parser.add_argument("--methods", required=True, type=extended_path)
    parser.add_argument("--output", required=True, type=extended_path)
    parser.add_argument("--run-id", default="v2-dev-plumbing-sanity-v1")
    parser.add_argument(
        "--method-ids", nargs="+", default=list(DEFAULT_METHODS),
        help="memory=none method identifiers from the method registry",
    )
    args = parser.parse_args(argv)
    if not args.run_id.strip() or len(set(args.method_ids)) != len(args.method_ids):
        parser.error("run-id must be non-empty and method-ids must be unique")
    try:
        blind_manifest = json.loads(args.task_manifest.read_text(encoding="utf-8"))
        if blind_manifest.get("source_challenge_sha256") != _sha256(args.challenges):
            raise ValueError("blind bundle was built from a different challenge file")
        seal_benchmark(args.challenges, args.evidence_snapshots)
        _require_new_output(args.output)
        records = run_blind_methods(
            tasks_path=args.tasks,
            task_manifest_path=args.task_manifest,
            snapshot_path=args.evidence_snapshots,
            methods_path=args.methods,
            output=args.output,
            method_ids=tuple(args.method_ids),
            run_id=args.run_id,
        )
        evaluate_after_execution(
            challenges_path=args.challenges, output=args.output, records=records,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scientific_result": False,
        "disclaimer": DISCLAIMER,
        "run_id": args.run_id,
        "blind_task_sha256": _sha256(args.tasks),
        "challenge_sha256": _sha256(args.challenges),
        "evidence_snapshot_sha256": _sha256(args.evidence_snapshots),
        "method_registry_sha256": _sha256(args.methods),
        "runtime": {
            "python": platform.python_version(),
            "implementation_sha256": {
                str(path.relative_to(Path(__file__).resolve().parents[1])): _sha256(path)
                for path in (
                    Path(__file__).resolve(),
                    Path(__file__).resolve().parents[1]
                    / "src/evaluation/baseline_runner.py",
                    Path(__file__).resolve().parents[1]
                    / "src/evaluation/offline_sanity.py",
                    Path(__file__).resolve().parents[1]
                    / "src/orchestration/worker.py",
                )
            },
        },
        "methods": records,
    }
    _atomic_write(
        args.output / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
