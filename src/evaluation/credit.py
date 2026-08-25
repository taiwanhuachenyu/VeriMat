"""Post-execution external-credit adapters; never pass these objects to task backends."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.core.portability import extended_path

from .challenge import COUNTER_RELATIONS, validate_challenges
from .ordered_runner import CreditOutcome


def _rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _key(item: dict[str, Any]) -> tuple[str, str, str, int, str]:
    locator = item["locator"]
    locator_name = "offset" if "offset" in locator else "page_no"
    return (
        str(item["doc_id"]).lower(), str(item["relation"]), locator_name,
        int(locator[locator_name]), str(item["content_sha256"]),
    )


class SealedKnownAnswerCreditEvaluator:
    """Credit prior-task strategies only after comparing a committed prediction with gold."""

    def __init__(
        self, challenge_path: str | Path, *, max_calls: int, max_tokens: int,
    ):
        self.path = extended_path(challenge_path)
        rows = _rows(self.path)
        validate_challenges(rows)
        self.rows = {str(row["challenge_id"]): row for row in rows}
        self.challenge_sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.max_calls = max_calls
        self.max_tokens = max_tokens

    def evaluate(
        self, *, challenge_id: str, prediction: dict[str, Any],
    ) -> CreditOutcome:
        challenge = self.rows.get(challenge_id)
        if challenge is None:
            raise ValueError(f"unknown sealed challenge {challenge_id!r}")
        if prediction.get("challenge_id") != challenge_id:
            raise ValueError("prediction/challenge identity mismatch")
        gold = {
            _key(item) for item in challenge["accepted_evidence"]
            if item["oracle_scope"] == "available_by_cutoff"
        }
        predicted = {_key(item) for item in prediction["evidence"]}
        matched = gold & predicted
        matched_counter = {
            item for item in matched if item[1] in COUNTER_RELATIONS
        }
        within_budget = (
            prediction["calls"] <= self.max_calls
            and prediction["tokens"] <= self.max_tokens
        )
        completed = prediction["status"] == "completed"
        success = bool(
            completed and within_budget and matched
            and prediction["predicted_decision"] == challenge["expected_decision"]
        )
        false_gap_avoided = bool(
            challenge["counterevidence_exists"] and matched_counter
            and completed and within_budget
        )
        return CreditOutcome(
            evaluator_kind="known_answer",
            success=success,
            false_gap_avoided=false_gap_avoided,
            valid_finding_delta=0.0,
            evidence_ref=(
                f"sha256:{self.challenge_sha256}#challenge:{challenge_id}"
            ),
        )
