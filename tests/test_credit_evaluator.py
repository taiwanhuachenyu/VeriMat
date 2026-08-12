import hashlib
import json

from src.evaluation.credit import SealedKnownAnswerCreditEvaluator


def test_known_answer_credit_is_post_execution_exact_and_budget_aware(tmp_path):
    content_hash = hashlib.sha256(b"evidence").hexdigest()
    evidence = {
        "evidence_id": "evidence", "doc_id": "doc-evidence",
        "relation": "PRECEDENT_FOR", "locator": {"offset": 0},
        "content_sha256": content_hash, "snapshot_id": "snapshot-evidence",
        "source_url": "https://example.org/evidence", "doi": "10.0000/evidence",
        "license_spdx": "CC-BY-4.0", "source_locator": "Abstract",
        "publication_date": "2019-01-01", "oracle_scope": "available_by_cutoff",
    }
    challenge = {
        "schema_version": 1, "challenge_id": "item",
        "benchmark_track": "known_answer", "split": "development",
        "task_family": "fixture", "leakage_group": "fixture-item",
        "prompt": "Assess fixture claim", "cutoff_date": "2020-01-01",
        "counterevidence_exists": True, "expected_decision": "REFUTED",
        "accepted_evidence": [evidence],
        "construction_provenance": {
            "source": "fixture", "curator": "fixture",
            "status": "independently_checked",
        },
    }
    path = tmp_path / "challenges.jsonl"
    path.write_text(json.dumps(challenge) + "\n", encoding="utf-8")
    evaluator = SealedKnownAnswerCreditEvaluator(
        path, max_calls=5, max_tokens=1000,
    )
    prediction = {
        "challenge_id": "item", "status": "completed",
        "predicted_decision": "REFUTED",
        "evidence": [{
            "doc_id": evidence["doc_id"], "relation": evidence["relation"],
            "locator": evidence["locator"],
            "content_sha256": evidence["content_sha256"],
        }],
        "calls": 2, "tokens": 100,
    }
    outcome = evaluator.evaluate(challenge_id="item", prediction=prediction)
    assert outcome.success
    assert outcome.false_gap_avoided
    assert outcome.evidence_ref.startswith("sha256:")

    prediction["calls"] = 6
    over_budget = evaluator.evaluate(challenge_id="item", prediction=prediction)
    assert not over_budget.success
    assert not over_budget.false_gap_avoided
