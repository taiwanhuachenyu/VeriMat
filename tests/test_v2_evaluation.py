import hashlib
import json

import pytest

from src.evaluation.challenge import (
    BenchmarkError, evaluate_predictions, prediction_commitment, seal_benchmark,
)
from src.evidence.ledger import EventLedger


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _evidence(identifier, relation, *, scope="available_by_cutoff", year=2019):
    return {
        "evidence_id": identifier,
        "doc_id": f"doc-{identifier}",
        "relation": relation,
        "locator": {"offset": 0},
        "content_sha256": _hash(identifier),
        "snapshot_id": f"snapshot-{identifier}",
        "source_url": f"https://example.org/{identifier}",
        "doi": f"10.0000/{identifier}",
        "license_spdx": "CC-BY-4.0",
        "source_locator": "Abstract",
        "publication_date": f"{year}-01-01",
        "oracle_scope": scope,
    }


def _challenge(identifier, *, positive, split="test", leakage=None):
    evidence = [_evidence(identifier, "PRECEDENT_FOR" if positive else "SUPPORTS")]
    return {
        "schema_version": 1,
        "challenge_id": identifier,
        "benchmark_track": "known_answer",
        "split": split,
        "task_family": f"family-{identifier}",
        "leakage_group": leakage or f"group-{identifier}",
        "prompt": f"Assess claim {identifier}",
        "cutoff_date": "2020-01-01",
        "counterevidence_exists": positive,
        "expected_decision": "REFUTED" if positive else "SURVIVED",
        "accepted_evidence": evidence,
        "construction_provenance": {
            "source": "test fixture", "curator": "fixture",
            "status": "independently_checked",
        },
    }


def _prediction(challenge, *, probability, cite=True, calls=2):
    return {
        "schema_version": 1,
        "challenge_id": challenge["challenge_id"],
        "run_id": "run-1",
        "method_id": "cedg_no_memory",
        "status": "completed",
        "predicted_decision": challenge["expected_decision"],
        "counterevidence_probability": probability,
        "evidence": [
            {key: value for key, value in challenge["accepted_evidence"][0].items()
             if key in {"doc_id", "relation", "locator", "content_sha256"}}
        ] if cite else [],
        "calls": calls,
        "tokens": 100,
        "ledger_head": _hash("pending"),
        "ledger_event_count": 1,
        "ledger_relpath": f"{challenge['challenge_id']}.jsonl",
        "prediction_commitment_sha256": _hash("pending"),
    }


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _bind_ledgers(predictions, ledger_root):
    ledger_root.mkdir()
    for prediction in predictions:
        commitment = prediction_commitment(prediction)
        ledger = EventLedger(ledger_root / prediction["ledger_relpath"])
        ledger.append(
            tenant_id="benchmark", job_id=prediction["run_id"],
            aggregate_type="benchmark_prediction",
            aggregate_id=prediction["challenge_id"],
            event_type="benchmark.prediction_committed",
            payload={
                "challenge_id": prediction["challenge_id"],
                "run_id": prediction["run_id"],
                "method_id": prediction["method_id"],
                "prediction_commitment_sha256": commitment,
            },
            idempotency_key=f"prediction:{prediction['challenge_id']}",
        )
        receipt = ledger.verify()
        prediction["ledger_head"] = receipt.head_hash
        prediction["ledger_event_count"] = receipt.event_count
        prediction["prediction_commitment_sha256"] = commitment


def _snapshot_rows(challenges):
    rows = []
    for challenge in challenges:
        for evidence in challenge["accepted_evidence"]:
            identifier = evidence["evidence_id"]
            rows.append({
                "schema_version": 1,
                "snapshot_id": evidence["snapshot_id"],
                "content": identifier,
                "content_sha256": evidence["content_sha256"],
                "source_url": evidence["source_url"],
                "doi": evidence["doi"],
                "source_locator": evidence["source_locator"],
                "publication_date": evidence["publication_date"],
                "retrieved_at": "2026-08-12T00:00:00+00:00",
                "license_spdx": evidence["license_spdx"],
                "attribution": "fixture",
                "normalization": "none",
            })
    return rows


def test_seal_rejects_leakage_group_crossing_splits(tmp_path):
    rows = [
        _challenge("a", positive=True, split="development", leakage="shared"),
        _challenge("b", positive=False, split="test", leakage="shared"),
    ]
    path = tmp_path / "challenges.jsonl"
    snapshots = tmp_path / "snapshots.jsonl"
    _write_jsonl(path, rows)
    _write_jsonl(snapshots, _snapshot_rows(rows))
    with pytest.raises(BenchmarkError, match="cross development/test"):
        seal_benchmark(path, snapshots)


def test_seal_rejects_post_cutoff_gold_mislabeled_as_available(tmp_path):
    row = _challenge("a", positive=True)
    row["accepted_evidence"][0]["publication_date"] = "2021-01-01"
    path = tmp_path / "challenges.jsonl"
    snapshots = tmp_path / "snapshots.jsonl"
    _write_jsonl(path, [row])
    _write_jsonl(snapshots, _snapshot_rows([row]))
    with pytest.raises(BenchmarkError, match="dated after cutoff"):
        seal_benchmark(path, snapshots)


def test_seal_rejects_snapshot_content_drift(tmp_path):
    rows = [_challenge("a", positive=True)]
    path, snapshots = tmp_path / "challenges.jsonl", tmp_path / "snapshots.jsonl"
    _write_jsonl(path, rows)
    capsules = _snapshot_rows(rows)
    capsules[0]["content"] = "silently changed"
    _write_jsonl(snapshots, capsules)
    with pytest.raises(BenchmarkError, match="content hash mismatch"):
        seal_benchmark(path, snapshots)


def test_development_seal_reports_nonpublication_status(tmp_path):
    rows = [_challenge("a", positive=True)]
    rows[0]["construction_provenance"]["status"] = "draft"
    path, snapshots = tmp_path / "challenges.jsonl", tmp_path / "snapshots.jsonl"
    _write_jsonl(path, rows)
    _write_jsonl(snapshots, _snapshot_rows(rows))
    manifest = seal_benchmark(path, snapshots)
    assert not manifest["publication_ready"]
    assert manifest["snapshots"] == 1


def test_evaluator_scores_evidence_decisions_calibration_and_budget(tmp_path):
    challenges = [_challenge("positive", positive=True), _challenge("negative", positive=False)]
    predictions = [
        _prediction(challenges[0], probability=0.9),
        _prediction(challenges[1], probability=0.1, calls=9),
    ]
    challenge_path, prediction_path = tmp_path / "c.jsonl", tmp_path / "p.jsonl"
    _write_jsonl(challenge_path, challenges)
    ledger_root = tmp_path / "ledgers"
    _bind_ledgers(predictions, ledger_root)
    _write_jsonl(prediction_path, predictions)
    summary, items = evaluate_predictions(
        challenge_path=challenge_path, prediction_path=prediction_path,
        ledger_root=ledger_root,
        max_calls=5, max_tokens=1000,
    )
    assert summary["counterevidence_recall"] == 1.0
    assert summary["counterevidence_false_positive_rate"] == 0.0
    assert summary["decision_accuracy"] == 0.5
    assert summary["budget_compliance_rate"] == 0.5
    assert summary["brier_score"] == pytest.approx(0.01)
    assert summary["ece_10"] == pytest.approx(0.1)
    assert items[1]["over_budget"]


def test_prediction_coverage_is_fail_closed(tmp_path):
    challenges = [_challenge("a", positive=True), _challenge("b", positive=False)]
    challenge_path, prediction_path = tmp_path / "c.jsonl", tmp_path / "p.jsonl"
    _write_jsonl(challenge_path, challenges)
    predictions = [_prediction(challenges[0], probability=0.8)]
    ledger_root = tmp_path / "ledgers"
    _bind_ledgers(predictions, ledger_root)
    _write_jsonl(prediction_path, predictions)
    with pytest.raises(BenchmarkError, match="coverage mismatch"):
        evaluate_predictions(
            challenge_path=challenge_path, prediction_path=prediction_path,
            ledger_root=ledger_root,
            max_calls=5, max_tokens=1000,
        )


def test_prediction_semantic_tamper_breaks_ledger_commitment(tmp_path):
    challenges = [_challenge("a", positive=True)]
    prediction = _prediction(challenges[0], probability=0.8)
    challenge_path, prediction_path = tmp_path / "c.jsonl", tmp_path / "p.jsonl"
    ledger_root = tmp_path / "ledgers"
    _write_jsonl(challenge_path, challenges)
    _bind_ledgers([prediction], ledger_root)
    prediction["predicted_decision"] = "SURVIVED"
    _write_jsonl(prediction_path, [prediction])
    with pytest.raises(BenchmarkError, match="commitment does not match"):
        evaluate_predictions(
            challenge_path=challenge_path, prediction_path=prediction_path,
            ledger_root=ledger_root, max_calls=5, max_tokens=1000,
        )


def test_failed_prediction_must_be_neutral_and_remains_in_denominator(tmp_path):
    challenge = _challenge("failed", positive=True)
    prediction = _prediction(challenge, probability=0.9)
    prediction.update({
        "status": "failed", "predicted_decision": "UNRESOLVED",
        "counterevidence_probability": 0.5, "evidence": [],
    })
    challenge_path, prediction_path = tmp_path / "c.jsonl", tmp_path / "p.jsonl"
    ledger_root = tmp_path / "ledgers"
    _write_jsonl(challenge_path, [challenge])
    _bind_ledgers([prediction], ledger_root)
    _write_jsonl(prediction_path, [prediction])
    summary, _ = evaluate_predictions(
        challenge_path=challenge_path, prediction_path=prediction_path,
        ledger_root=ledger_root, max_calls=5, max_tokens=1000,
    )
    assert summary["completed_rate"] == 0
    assert summary["decision_accuracy"] == 0

    prediction["counterevidence_probability"] = 0.9
    prediction["prediction_commitment_sha256"] = prediction_commitment(prediction)
    _write_jsonl(prediction_path, [prediction])
    with pytest.raises(BenchmarkError, match="neutral UNRESOLVED"):
        evaluate_predictions(
            challenge_path=challenge_path, prediction_path=prediction_path,
            ledger_root=ledger_root, max_calls=5, max_tokens=1000,
        )
