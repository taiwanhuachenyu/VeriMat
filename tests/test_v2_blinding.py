import json

import pytest

from src.evaluation.blinding import (
    FORBIDDEN_GOLD_KEYS, materialize_blind_bundle, verify_blind_bundle,
)
from src.evaluation.challenge import BenchmarkError

from test_v2_evaluation import _challenge, _write_jsonl


def test_blind_bundle_contains_no_gold_fields(tmp_path):
    challenges = [_challenge("a", positive=True), _challenge("b", positive=False)]
    challenge_path = tmp_path / "challenges.jsonl"
    _write_jsonl(challenge_path, challenges)
    output = tmp_path / "blind"
    manifest = materialize_blind_bundle(
        challenge_path=challenge_path, output_dir=output,
    )
    text = (output / "tasks.jsonl").read_text()
    assert not any(key in text for key in FORBIDDEN_GOLD_KEYS)
    assert manifest["tasks"] == 2
    verify_blind_bundle(
        task_path=output / "tasks.jsonl", manifest_path=output / "task_manifest.json",
    )


def test_blind_bundle_tamper_is_detected(tmp_path):
    challenge_path = tmp_path / "challenges.jsonl"
    _write_jsonl(challenge_path, [_challenge("a", positive=True)])
    output = tmp_path / "blind"
    materialize_blind_bundle(challenge_path=challenge_path, output_dir=output)
    task_path = output / "tasks.jsonl"
    row = json.loads(task_path.read_text())
    row["prompt"] = "changed"
    _write_jsonl(task_path, [row])
    with pytest.raises(BenchmarkError, match="hash does not match"):
        verify_blind_bundle(
            task_path=task_path, manifest_path=output / "task_manifest.json",
        )
