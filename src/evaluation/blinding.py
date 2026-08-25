"""Build and verify blind task bundles that contain no benchmark gold fields."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.events import canonical_json
from src.core.portability import extended_path

from .challenge import BenchmarkError, _read_jsonl, _sha256, validate_challenges

TASK_FIELDS = (
    "schema_version", "challenge_id", "benchmark_track", "split", "task_family",
    "prompt", "cutoff_date",
)
FORBIDDEN_GOLD_KEYS = {
    "accepted_evidence", "counterevidence_exists", "expected_decision",
    "construction_provenance", "leakage_group", "oracle_scope", "evidence_id",
}


def _forbidden_paths(value: Any, prefix: str = "task") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            if key in FORBIDDEN_GOLD_KEYS:
                found.append(path)
            found.extend(_forbidden_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_forbidden_paths(item, f"{prefix}[{index}]"))
    return found


def validate_blind_tasks(rows: list[dict[str, Any]]) -> None:
    identifiers: set[str] = set()
    for index, row in enumerate(rows):
        context = f"task[{index}]"
        if set(row) != set(TASK_FIELDS):
            raise BenchmarkError(
                f"{context}: fields must be exactly {', '.join(TASK_FIELDS)}"
            )
        forbidden = _forbidden_paths(row, context)
        if forbidden:
            raise BenchmarkError(f"{context}: gold fields leaked: {', '.join(forbidden)}")
        if row["schema_version"] != 1:
            raise BenchmarkError(f"{context}: unsupported schema version")
        identifier = str(row["challenge_id"])
        if not identifier or identifier in identifiers:
            raise BenchmarkError(f"{context}: empty or duplicate challenge_id")
        identifiers.add(identifier)
        for field in TASK_FIELDS[2:]:
            if not str(row[field]).strip():
                raise BenchmarkError(f"{context}: {field} must not be empty")


def materialize_blind_bundle(
    *, challenge_path: str | Path, output_dir: str | Path,
) -> dict[str, Any]:
    source = extended_path(challenge_path)
    challenges = _read_jsonl(source)
    validate_challenges(challenges)
    tasks = [{field: challenge[field] for field in TASK_FIELDS} for challenge in challenges]
    validate_blind_tasks(tasks)
    output = extended_path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    task_path = output / "tasks.jsonl"
    task_path.write_text(
        "".join(canonical_json(row) + "\n" for row in tasks), encoding="utf-8"
    )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "task_file": task_path.name,
        "task_sha256": _sha256(task_path),
        "source_challenge_sha256": _sha256(source),
        "tasks": len(tasks),
        "challenge_ids_sha256": hashlib.sha256(
            canonical_json(sorted(row["challenge_id"] for row in tasks)).encode()
        ).hexdigest(),
        "forbidden_gold_keys": sorted(FORBIDDEN_GOLD_KEYS),
    }
    (output / "task_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def verify_blind_bundle(*, task_path: str | Path, manifest_path: str | Path) -> None:
    tasks = _read_jsonl(task_path)
    validate_blind_tasks(tasks)
    manifest = json.loads(extended_path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("task_sha256") != _sha256(extended_path(task_path)):
        raise BenchmarkError("blind task bundle hash does not match manifest")
    if manifest.get("tasks") != len(tasks):
        raise BenchmarkError("blind task bundle count does not match manifest")
