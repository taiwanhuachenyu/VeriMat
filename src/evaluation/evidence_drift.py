"""Fail-closed replay audit for sealed evidence capsules.

The auditor is deliberately transport-agnostic.  A separate acquisition process captures a
passage observation and the SHA-256 of its raw retrieval receipt; this module compares that
observation with the immutable benchmark capsule without making network calls itself.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.events import canonical_json
from src.core.portability import extended_path

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OBSERVATION_FIELDS = {
    "schema_version", "snapshot_id", "source_url", "doi", "source_locator",
    "observed_at", "status", "content", "content_sha256", "retrieval",
}
_RETRIEVAL_FIELDS = {
    "method", "actor", "independently_verified", "receipt_sha256",
}
_METHODS = {"publisher_api", "publisher_page", "repository", "web_archive", "manual"}


class EvidenceDriftError(ValueError):
    """Raised when replay inputs are incomplete, ambiguous, or internally inconsistent."""


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceDriftError(
                f"{label}:{line_number}: invalid JSON: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise EvidenceDriftError(f"{label}:{line_number}: row must be an object")
        rows.append(row)
    if not rows:
        raise EvidenceDriftError(f"{label}: input is empty")
    return rows


def _timestamp(value: Any, context: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceDriftError(f"{context}: timestamp must be ISO-8601") from exc
    if parsed.utcoffset() is None:
        raise EvidenceDriftError(f"{context}: timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _normalized_passage(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _validate_observation(
    observation: dict[str, Any], snapshot: dict[str, Any], context: str,
) -> tuple[str, bool, str | None]:
    if set(observation) != _OBSERVATION_FIELDS:
        raise EvidenceDriftError(f"{context}: unexpected or missing observation fields")
    if observation["schema_version"] != 1:
        raise EvidenceDriftError(f"{context}: unsupported schema version")
    for field in ("source_url", "doi"):
        if observation[field] != snapshot[field]:
            raise EvidenceDriftError(f"{context}: {field} does not match sealed snapshot")
    observed_at = _timestamp(observation["observed_at"], context)
    sealed_at = _timestamp(snapshot["retrieved_at"], f"snapshot {snapshot['snapshot_id']}")
    if observed_at <= sealed_at:
        raise EvidenceDriftError(f"{context}: observation must postdate the sealed retrieval")

    retrieval = observation["retrieval"]
    if not isinstance(retrieval, dict) or set(retrieval) != _RETRIEVAL_FIELDS:
        raise EvidenceDriftError(f"{context}: invalid retrieval provenance fields")
    if retrieval["method"] not in _METHODS or not str(retrieval["actor"]).strip():
        raise EvidenceDriftError(f"{context}: invalid retrieval method or actor")
    if not isinstance(retrieval["independently_verified"], bool):
        raise EvidenceDriftError(f"{context}: independently_verified must be boolean")
    if not _HEX64.fullmatch(str(retrieval["receipt_sha256"])):
        raise EvidenceDriftError(f"{context}: receipt_sha256 must be a SHA-256 digest")

    status = observation["status"]
    if status == "unavailable":
        if observation["content"] is not None or observation["content_sha256"] is not None:
            raise EvidenceDriftError(f"{context}: unavailable observation must not carry content")
        return "UNAVAILABLE", retrieval["independently_verified"], None
    if status != "available":
        raise EvidenceDriftError(f"{context}: status must be available or unavailable")
    content = observation["content"]
    if not isinstance(content, str) or not content.strip():
        raise EvidenceDriftError(f"{context}: available observation requires content")
    observed_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if observation["content_sha256"] != observed_hash:
        raise EvidenceDriftError(f"{context}: observed content hash mismatch")
    if observed_hash == snapshot["content_sha256"]:
        classification = (
            "EXACT" if observation["source_locator"] == snapshot["source_locator"]
            else "LOCATOR_DRIFT"
        )
    elif _normalized_passage(content) == _normalized_passage(str(snapshot["content"])):
        classification = "NORMALIZATION_DRIFT"
    else:
        classification = "CONTENT_DRIFT"
    return classification, retrieval["independently_verified"], observed_hash


def audit_evidence_drift(
    *, snapshot_path: str | Path, observation_path: str | Path,
) -> dict[str, Any]:
    """Replay one later observation per sealed capsule and return a fail-closed report."""
    snapshots_target = extended_path(snapshot_path)
    observations_target = extended_path(observation_path)
    snapshots = _read_jsonl(snapshots_target, label="snapshots")
    observations = _read_jsonl(observations_target, label="observations")

    snapshot_index: dict[str, dict[str, Any]] = {}
    for index, snapshot in enumerate(snapshots):
        snapshot_id = str(snapshot.get("snapshot_id") or "")
        if not snapshot_id or snapshot_id in snapshot_index:
            raise EvidenceDriftError(f"snapshot[{index}]: empty or duplicate snapshot_id")
        content = snapshot.get("content")
        if not isinstance(content, str) or hashlib.sha256(content.encode()).hexdigest() != snapshot.get(
            "content_sha256"
        ):
            raise EvidenceDriftError(f"snapshot[{index}]: sealed content hash mismatch")
        _timestamp(snapshot.get("retrieved_at"), f"snapshot[{index}]")
        snapshot_index[snapshot_id] = snapshot

    observation_index: dict[str, dict[str, Any]] = {}
    for index, observation in enumerate(observations):
        snapshot_id = str(observation.get("snapshot_id") or "")
        if not snapshot_id or snapshot_id in observation_index:
            raise EvidenceDriftError(f"observation[{index}]: empty or duplicate snapshot_id")
        observation_index[snapshot_id] = observation
    missing = sorted(set(snapshot_index) - set(observation_index))
    extra = sorted(set(observation_index) - set(snapshot_index))
    if missing or extra:
        raise EvidenceDriftError(
            f"observation coverage mismatch; missing={missing}, unexpected={extra}"
        )

    records: list[dict[str, Any]] = []
    for snapshot_id in sorted(snapshot_index):
        snapshot = snapshot_index[snapshot_id]
        observation = observation_index[snapshot_id]
        classification, independently_verified, observed_hash = _validate_observation(
            observation, snapshot, f"observation {snapshot_id!r}",
        )
        records.append({
            "snapshot_id": snapshot_id,
            "classification": classification,
            "expected_sha256": snapshot["content_sha256"],
            "observed_sha256": observed_hash,
            "source_locator_changed": (
                observation["source_locator"] != snapshot["source_locator"]
            ),
            "independently_verified": independently_verified,
            "receipt_sha256": observation["retrieval"]["receipt_sha256"],
        })
    counts = Counter(record["classification"] for record in records)
    all_exact = all(record["classification"] == "EXACT" for record in records)
    all_independent = all(record["independently_verified"] for record in records)
    snapshot_sha = _sha256_path(snapshots_target)
    observation_sha = _sha256_path(observations_target)
    policy = {
        "required_classification": "EXACT",
        "require_independent_verification": True,
        "coverage": "exactly one later observation per sealed snapshot",
    }
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "audit_id": hashlib.sha256(canonical_json({
            "snapshot_sha256": snapshot_sha,
            "observation_sha256": observation_sha,
            "policy": policy,
        }).encode()).hexdigest(),
        "snapshot_sha256": snapshot_sha,
        "observation_sha256": observation_sha,
        "snapshots": len(records),
        "counts": dict(sorted(counts.items())),
        "all_exact": all_exact,
        "all_independently_verified": all_independent,
        "publication_gate_passed": all_exact and all_independent,
        "policy": policy,
        "records": records,
    }
