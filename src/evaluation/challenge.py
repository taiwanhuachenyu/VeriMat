"""Strict, dependency-free scoring and sealing of V2 challenge sets."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.core.events import canonical_json
from src.evidence.ledger import EventLedger

COUNTER_RELATIONS = {"CONTRADICTS", "BOUNDS", "PRECEDENT_FOR"}
DECISIONS = {"SURVIVED", "NARROWED", "REFUTED", "UNRESOLVED"}
PREDICTION_FIELDS = {
    "schema_version", "challenge_id", "run_id", "method_id", "status",
    "predicted_decision", "counterevidence_probability", "evidence",
    "calls", "tokens", "ledger_head", "ledger_event_count", "ledger_relpath",
    "prediction_commitment_sha256",
}
TRACKS = {"known_answer", "temporal_holdout"}
SPLITS = {"development", "test"}
HEX64 = set("0123456789abcdef")


class BenchmarkError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise BenchmarkError(f"{path}:{line_number}: row must be an object")
        rows.append(value)
    return rows


def _require(row: dict[str, Any], fields: set[str], context: str) -> None:
    missing = sorted(field for field in fields if row.get(field) in (None, ""))
    if missing:
        raise BenchmarkError(f"{context}: missing fields: {', '.join(missing)}")


def _parse_date(value: Any, context: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise BenchmarkError(f"{context}: date must be YYYY-MM-DD") from exc


def _is_sha256(value: Any) -> bool:
    rendered = str(value)
    return len(rendered) == 64 and all(char in HEX64 for char in rendered)


def _locator(value: Any, context: str) -> tuple[str, int]:
    if not isinstance(value, dict):
        raise BenchmarkError(f"{context}: locator must be an object")
    choices = [(name, value.get(name)) for name in ("offset", "page_no")
               if value.get(name) is not None]
    if len(choices) != 1:
        raise BenchmarkError(f"{context}: locator requires exactly one of offset/page_no")
    name, position = choices[0]
    if isinstance(position, bool) or not isinstance(position, int) or position < 0:
        raise BenchmarkError(f"{context}: locator must be a non-negative integer")
    return name, position


def validate_challenges(rows: list[dict[str, Any]]) -> None:
    ids: set[str] = set()
    evidence_ids: set[str] = set()
    leakage_splits: dict[str, set[str]] = defaultdict(set)
    required = {
        "schema_version", "challenge_id", "benchmark_track", "split", "task_family",
        "leakage_group", "prompt", "cutoff_date", "counterevidence_exists",
        "expected_decision", "accepted_evidence", "construction_provenance",
    }
    for index, row in enumerate(rows):
        context = f"challenge[{index}]"
        _require(row, required, context)
        if set(row) != required:
            raise BenchmarkError(f"{context}: unexpected challenge fields")
        if row["schema_version"] != 1 or row["benchmark_track"] not in TRACKS:
            raise BenchmarkError(f"{context}: unsupported schema version or track")
        if row["split"] not in SPLITS or row["expected_decision"] not in DECISIONS:
            raise BenchmarkError(f"{context}: unsupported split or decision")
        if not isinstance(row["counterevidence_exists"], bool):
            raise BenchmarkError(f"{context}: counterevidence_exists must be boolean")
        challenge_id = str(row["challenge_id"])
        if challenge_id in ids:
            raise BenchmarkError(f"{context}: duplicate challenge_id {challenge_id!r}")
        ids.add(challenge_id)
        leakage_splits[str(row["leakage_group"])].add(str(row["split"]))
        cutoff = _parse_date(row["cutoff_date"], context)
        evidence = row["accepted_evidence"]
        if not isinstance(evidence, list):
            raise BenchmarkError(f"{context}: accepted_evidence must be an array")
        for ev_index, item in enumerate(evidence):
            ev_context = f"{context}.accepted_evidence[{ev_index}]"
            if not isinstance(item, dict):
                raise BenchmarkError(f"{ev_context}: evidence must be an object")
            _require(item, {
                "evidence_id", "doc_id", "relation", "locator", "content_sha256",
                "publication_date", "oracle_scope", "snapshot_id", "source_url", "doi",
                "license_spdx", "source_locator",
            }, ev_context)
            if set(item) != {
                "evidence_id", "doc_id", "relation", "locator", "content_sha256",
                "publication_date", "oracle_scope", "snapshot_id", "source_url", "doi",
                "license_spdx", "source_locator",
            }:
                raise BenchmarkError(f"{ev_context}: unexpected evidence fields")
            evidence_id = str(item["evidence_id"])
            if evidence_id in evidence_ids:
                raise BenchmarkError(f"{ev_context}: duplicate evidence_id {evidence_id!r}")
            evidence_ids.add(evidence_id)
            if item["relation"] not in COUNTER_RELATIONS | {"SUPPORTS"}:
                raise BenchmarkError(f"{ev_context}: unsupported evidence relation")
            _locator(item["locator"], ev_context)
            if not _is_sha256(item["content_sha256"]):
                raise BenchmarkError(f"{ev_context}: invalid content_sha256")
            if not str(item["source_url"]).startswith("https://"):
                raise BenchmarkError(f"{ev_context}: source_url must use HTTPS")
            if not str(item["doi"]).lower().startswith("10."):
                raise BenchmarkError(f"{ev_context}: invalid DOI")
            if item["license_spdx"] != "CC-BY-4.0":
                raise BenchmarkError(f"{ev_context}: unsupported evidence licence")
            publication = _parse_date(item["publication_date"], ev_context)
            scope = item["oracle_scope"]
            if scope not in {"available_by_cutoff", "post_cutoff_validation"}:
                raise BenchmarkError(f"{ev_context}: invalid oracle_scope")
            if scope == "available_by_cutoff" and publication > cutoff:
                raise BenchmarkError(f"{ev_context}: pre-cutoff evidence is dated after cutoff")
            if scope == "post_cutoff_validation" and publication <= cutoff:
                raise BenchmarkError(f"{ev_context}: post-cutoff evidence is not after cutoff")
            if row["benchmark_track"] == "known_answer" and scope != "available_by_cutoff":
                raise BenchmarkError(f"{ev_context}: known-answer gold must be pre-cutoff")
        available_counter = any(
            item["oracle_scope"] == "available_by_cutoff"
            and item["relation"] in COUNTER_RELATIONS for item in evidence
        )
        if bool(row["counterevidence_exists"]) != available_counter:
            raise BenchmarkError(
                f"{context}: label disagrees with available counterevidence gold"
            )
        relations = {
            item["relation"] for item in evidence
            if item["oracle_scope"] == "available_by_cutoff"
        }
        required_relation = {
            "SURVIVED": {"SUPPORTS"},
            "NARROWED": {"BOUNDS"},
            "REFUTED": {"CONTRADICTS", "PRECEDENT_FOR"},
            "UNRESOLVED": set(),
        }[row["expected_decision"]]
        if required_relation and not relations & required_relation:
            raise BenchmarkError(
                f"{context}: expected decision lacks a compatible gold relation"
            )
        provenance = row["construction_provenance"]
        if not isinstance(provenance, dict) or set(provenance) != {
            "source", "curator", "status",
        }:
            raise BenchmarkError(f"{context}: invalid construction provenance fields")
        if provenance["status"] not in {"draft", "independently_checked", "frozen"}:
            raise BenchmarkError(f"{context}: invalid curation status")
    leaked = sorted(group for group, splits in leakage_splits.items() if len(splits) > 1)
    if leaked:
        raise BenchmarkError("leakage groups cross development/test: " + ", ".join(leaked))


def _validate_snapshots(
    rows: list[dict[str, Any]], challenges: list[dict[str, Any]],
) -> dict[str, int]:
    indexed: dict[str, dict[str, Any]] = {}
    required = {
        "schema_version", "snapshot_id", "content", "content_sha256", "source_url",
        "doi", "source_locator", "retrieved_at", "license_spdx", "attribution",
        "normalization", "publication_date",
    }
    for index, row in enumerate(rows):
        context = f"snapshot[{index}]"
        _require(row, required, context)
        if set(row) != required:
            raise BenchmarkError(f"{context}: unexpected snapshot fields")
        if row["schema_version"] != 1:
            raise BenchmarkError(f"{context}: unsupported schema version")
        snapshot_id = str(row["snapshot_id"])
        if snapshot_id in indexed:
            raise BenchmarkError(f"{context}: duplicate snapshot_id {snapshot_id!r}")
        content = str(row["content"])
        if not content.strip() or len(content.split()) > 120:
            raise BenchmarkError(f"{context}: content must contain 1-120 words")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if row["content_sha256"] != content_hash:
            raise BenchmarkError(f"{context}: content hash mismatch")
        if not str(row["source_url"]).startswith("https://"):
            raise BenchmarkError(f"{context}: source_url must use HTTPS")
        if not str(row["doi"]).lower().startswith("10."):
            raise BenchmarkError(f"{context}: invalid DOI")
        if row["license_spdx"] != "CC-BY-4.0":
            raise BenchmarkError(f"{context}: only CC-BY-4.0 capsules are publishable")
        _parse_date(row["publication_date"], context)
        try:
            retrieved = datetime.fromisoformat(
                str(row["retrieved_at"]).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise BenchmarkError(f"{context}: retrieved_at must be ISO-8601") from exc
        if retrieved.utcoffset() is None:
            raise BenchmarkError(f"{context}: retrieved_at must include a timezone")
        indexed[snapshot_id] = row

    referenced: set[str] = set()
    for challenge in challenges:
        for evidence in challenge["accepted_evidence"]:
            snapshot_id = str(evidence["snapshot_id"])
            snapshot = indexed.get(snapshot_id)
            if snapshot is None:
                raise BenchmarkError(
                    f"challenge {challenge['challenge_id']!r}: missing snapshot {snapshot_id!r}"
                )
            referenced.add(snapshot_id)
            for field in (
                "content_sha256", "source_url", "doi", "source_locator", "license_spdx",
                "publication_date",
            ):
                if evidence[field] != snapshot[field]:
                    raise BenchmarkError(
                        f"challenge {challenge['challenge_id']!r}: snapshot {snapshot_id!r} "
                        f"disagrees on {field}"
                    )
            if evidence["locator"] != {"offset": 0}:
                raise BenchmarkError(
                    f"challenge {challenge['challenge_id']!r}: capsule locator must be offset 0"
                )
    orphaned = sorted(set(indexed) - referenced)
    if orphaned:
        raise BenchmarkError("unreferenced evidence snapshots: " + ", ".join(orphaned))
    return {"snapshots": len(indexed), "referenced_snapshots": len(referenced)}


def seal_benchmark(
    path: str | Path, evidence_snapshot_path: str | Path,
) -> dict[str, Any]:
    target = Path(path)
    rows = _read_jsonl(target)
    if not rows:
        raise BenchmarkError("challenge set is empty")
    validate_challenges(rows)
    snapshot_path = Path(evidence_snapshot_path)
    snapshots = _read_jsonl(snapshot_path)
    snapshot_counts = _validate_snapshots(snapshots, rows)
    counts = Counter((row["benchmark_track"], row["split"]) for row in rows)
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "challenge_file": target.name,
        "challenge_sha256": _sha256(target),
        "evidence_snapshot_file": snapshot_path.name,
        "evidence_snapshot_sha256": _sha256(snapshot_path),
        "challenges": len(rows),
        "leakage_groups": len({row["leakage_group"] for row in rows}),
        "counts": {
            f"{track}:{split}": count
            for (track, split), count in sorted(counts.items())
        },
        "curation_statuses": dict(sorted(Counter(
            row["construction_provenance"]["status"] for row in rows
        ).items())),
        "publication_ready": all(
            row["construction_provenance"]["status"] == "frozen" for row in rows
        ),
        **snapshot_counts,
    }


def _evidence_key(item: dict[str, Any], context: str) -> tuple[str, str, str, int, str]:
    relation = str(item.get("relation", ""))
    if relation not in COUNTER_RELATIONS | {"SUPPORTS"}:
        raise BenchmarkError(f"{context}: unsupported evidence relation")
    if not _is_sha256(item.get("content_sha256")):
        raise BenchmarkError(f"{context}: invalid content_sha256")
    locator_name, locator_value = _locator(item.get("locator"), context)
    return (
        str(item.get("doc_id", "")).strip().lower(), relation,
        locator_name, locator_value, str(item["content_sha256"]),
    )


def _validate_prediction(row: dict[str, Any], context: str) -> None:
    _require(row, PREDICTION_FIELDS, context)
    if set(row) != PREDICTION_FIELDS:
        extra = sorted(set(row) - PREDICTION_FIELDS)
        raise BenchmarkError(f"{context}: unexpected prediction fields: {extra}")
    if row["schema_version"] != 1 or row["status"] not in {"completed", "failed"}:
        raise BenchmarkError(f"{context}: unsupported schema version or status")
    if row["predicted_decision"] not in DECISIONS:
        raise BenchmarkError(f"{context}: unsupported predicted_decision")
    probability = row["counterevidence_probability"]
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        raise BenchmarkError(f"{context}: probability must be numeric")
    if not math.isfinite(float(probability)) or not 0 <= float(probability) <= 1:
        raise BenchmarkError(f"{context}: probability must be in [0,1]")
    for field in ("calls", "tokens", "ledger_event_count"):
        if isinstance(row[field], bool) or not isinstance(row[field], int) or row[field] < 0:
            raise BenchmarkError(f"{context}: {field} must be a non-negative integer")
    if not isinstance(row["evidence"], list):
        raise BenchmarkError(f"{context}: evidence must be an array")
    for index, item in enumerate(row["evidence"]):
        if set(item) != {"doc_id", "relation", "locator", "content_sha256"}:
            raise BenchmarkError(f"{context}.evidence[{index}]: unexpected fields")
        _evidence_key(item, f"{context}.evidence[{index}]")
    if row["status"] == "failed" and not (
        row["predicted_decision"] == "UNRESOLVED"
        and float(row["counterevidence_probability"]) == 0.5
        and row["evidence"] == []
    ):
        raise BenchmarkError(
            f"{context}: failed predictions must be neutral UNRESOLVED rows"
        )
    if not _is_sha256(row["ledger_head"]):
        raise BenchmarkError(f"{context}: prediction requires ledger SHA-256 head")
    if not _is_sha256(row["prediction_commitment_sha256"]):
        raise BenchmarkError(f"{context}: invalid prediction commitment")
    relpath = Path(str(row["ledger_relpath"]))
    if relpath.is_absolute() or ".." in relpath.parts or relpath.suffix != ".jsonl":
        raise BenchmarkError(f"{context}: ledger_relpath must be a safe relative JSONL path")


def prediction_commitment(row: dict[str, Any]) -> str:
    semantic_fields = (
        "schema_version", "challenge_id", "run_id", "method_id", "status",
        "predicted_decision", "counterevidence_probability", "evidence", "calls", "tokens",
    )
    return hashlib.sha256(
        canonical_json({field: row[field] for field in semantic_fields}).encode()
    ).hexdigest()


def _verify_prediction_ledger(
    prediction: dict[str, Any], *, ledger_root: Path, context: str,
) -> None:
    expected_commitment = prediction_commitment(prediction)
    if prediction["prediction_commitment_sha256"] != expected_commitment:
        raise BenchmarkError(f"{context}: prediction commitment does not match semantics")
    ledger_path = ledger_root / str(prediction["ledger_relpath"])
    try:
        ledger_path.resolve().relative_to(ledger_root.resolve())
    except ValueError as exc:
        raise BenchmarkError(f"{context}: ledger path escapes ledger root") from exc
    if not ledger_path.is_file():
        raise BenchmarkError(f"{context}: prediction ledger does not exist")
    ledger = EventLedger(ledger_path)
    receipt = ledger.verify()
    if not receipt.ok:
        raise BenchmarkError(f"{context}: invalid prediction ledger: {receipt.error}")
    if (
        receipt.head_hash != prediction["ledger_head"]
        or receipt.event_count != prediction["ledger_event_count"]
    ):
        raise BenchmarkError(f"{context}: prediction ledger receipt mismatch")
    commitments = [
        event for event in ledger.events()
        if event.event_type == "benchmark.prediction_committed"
        and event.payload.get("challenge_id") == prediction["challenge_id"]
        and event.payload.get("run_id") == prediction["run_id"]
        and event.payload.get("method_id") == prediction["method_id"]
        and event.payload.get("prediction_commitment_sha256") == expected_commitment
    ]
    if len(commitments) != 1:
        raise BenchmarkError(
            f"{context}: ledger must contain exactly one matching prediction commitment"
        )


def _ece(labels: list[int], probabilities: list[float], bins: int = 10) -> float:
    total = len(labels)
    if total == 0:
        return math.nan
    error = 0.0
    for bin_index in range(bins):
        lower, upper = bin_index / bins, (bin_index + 1) / bins
        members = [index for index, probability in enumerate(probabilities)
                   if lower <= probability < upper or (bin_index == bins - 1 and probability == 1)]
        if not members:
            continue
        confidence = sum(probabilities[index] for index in members) / len(members)
        accuracy = sum(labels[index] for index in members) / len(members)
        error += len(members) / total * abs(accuracy - confidence)
    return error


def evaluate_predictions(
    *, challenge_path: str | Path, prediction_path: str | Path,
    ledger_root: str | Path, max_calls: int, max_tokens: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    challenges = _read_jsonl(challenge_path)
    validate_challenges(challenges)
    predictions = _read_jsonl(prediction_path)
    indexed: dict[str, dict[str, Any]] = {}
    for index, prediction in enumerate(predictions):
        context = f"prediction[{index}]"
        _validate_prediction(prediction, context)
        _verify_prediction_ledger(
            prediction, ledger_root=Path(ledger_root), context=context,
        )
        challenge_id = str(prediction["challenge_id"])
        if challenge_id in indexed:
            raise BenchmarkError(f"duplicate prediction for {challenge_id!r}")
        indexed[challenge_id] = prediction
    expected_ids = {str(row["challenge_id"]) for row in challenges}
    unknown = sorted(set(indexed) - expected_ids)
    missing = sorted(expected_ids - set(indexed))
    if unknown or missing:
        raise BenchmarkError(
            f"prediction coverage mismatch; missing={missing}, unknown={unknown}"
        )
    methods = {str(row["method_id"]) for row in predictions}
    runs = {str(row["run_id"]) for row in predictions}
    if len(methods) != 1 or len(runs) != 1:
        raise BenchmarkError("one prediction file must contain exactly one method_id and run_id")

    items: list[dict[str, Any]] = []
    labels: list[int] = []
    probabilities: list[float] = []
    for challenge in challenges:
        challenge_id = str(challenge["challenge_id"])
        prediction = indexed[challenge_id]
        gold = {
            _evidence_key(item, f"challenge[{challenge_id}].gold")
            for item in challenge["accepted_evidence"]
            if item["oracle_scope"] == "available_by_cutoff"
        }
        predicted = {
            _evidence_key(item, f"prediction[{challenge_id}].evidence")
            for item in prediction["evidence"]
        }
        matched = gold & predicted
        matched_counter = {item for item in matched if item[1] in COUNTER_RELATIONS}
        predicted_counter = {item for item in predicted if item[1] in COUNTER_RELATIONS}
        label = int(challenge["counterevidence_exists"])
        probability = float(prediction["counterevidence_probability"])
        labels.append(label)
        probabilities.append(probability)
        completed = prediction["status"] == "completed"
        over_budget = prediction["calls"] > max_calls or prediction["tokens"] > max_tokens
        detected = bool(matched_counter) if label else bool(predicted_counter)
        items.append({
            "challenge_id": challenge_id,
            "benchmark_track": challenge["benchmark_track"],
            "split": challenge["split"],
            "task_family": challenge["task_family"],
            "leakage_group": challenge["leakage_group"],
            "completed": completed,
            "over_budget": over_budget,
            "counterevidence_label": label,
            "counterevidence_detected": bool(detected),
            "true_positive": bool(label and matched_counter and completed and not over_budget),
            "false_positive": bool(not label and predicted_counter),
            "decision_correct": bool(
                prediction["predicted_decision"] == challenge["expected_decision"]
                and completed and not over_budget
            ),
            "predicted_evidence": len(predicted),
            "matched_evidence": len(matched),
            "replay_precision": len(matched) / len(predicted) if predicted else 1.0,
            "probability": probability,
            "brier": (probability - label) ** 2,
            "calls": prediction["calls"],
            "tokens": prediction["tokens"],
        })
    positives = [item for item in items if item["counterevidence_label"]]
    negatives = [item for item in items if not item["counterevidence_label"]]
    summary = {
        "schema_version": 1,
        "method_id": next(iter(methods)), "run_id": next(iter(runs)),
        "challenge_sha256": _sha256(Path(challenge_path)),
        "prediction_sha256": _sha256(Path(prediction_path)),
        "n": len(items),
        "completed_rate": sum(item["completed"] for item in items) / len(items),
        "budget_compliance_rate": 1 - sum(item["over_budget"] for item in items) / len(items),
        "counterevidence_recall": (
            sum(item["true_positive"] for item in positives) / len(positives)
            if positives else None
        ),
        "counterevidence_false_positive_rate": (
            sum(item["false_positive"] for item in negatives) / len(negatives)
            if negatives else None
        ),
        "decision_accuracy": sum(item["decision_correct"] for item in items) / len(items),
        "evidence_replay_precision": (
            sum(item["matched_evidence"] for item in items)
            / sum(item["predicted_evidence"] for item in items)
            if sum(item["predicted_evidence"] for item in items) else None
        ),
        "brier_score": sum(item["brier"] for item in items) / len(items),
        "ece_10": _ece(labels, probabilities, bins=10),
        "total_calls": sum(item["calls"] for item in items),
        "total_tokens": sum(item["tokens"] for item in items),
    }
    return summary, items
