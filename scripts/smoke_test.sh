#!/bin/bash
# Smoke test: offline, no API keys, ~1 minute. Verifies the install end to end.
set -e
cd "$(dirname "$0")/.."

echo "== 1. unit tests =="
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_semifinal_eval.py tests/test_pareto_mcts.py tests/test_opencode_transport.py -q

echo "== 2. Pareto-MCTS offline example =="
python3 examples/pareto_mcts_demo.py | head -5

echo "== 3. scorer determinism (fixed fixture) =="
python3 - <<'EOF'
import sys; sys.path.insert(0, ".")
from src.experiments.scoring import score_method
from src.experiments.claims import Claim, VerifiedClaim
claim = Claim(claim_id="c1", relation_id="r1", material="Bi2Te3",
              structural_feature="Se vacancy", property_name="ZT", direction="increase",
              quote="Se vacancies raise ZT", passage_id="psg-1")
preds = [VerifiedClaim(method="m", claim=claim, label="ACCEPTED", confidence=0.9)]
s = score_method("m", preds, {"c1": "supported"}, passage_text={}, tokens=100).as_dict()
assert s["decision_accuracy"] == 1.0 and s["tokens_per_valid"] == 100.0
print("scorer OK:", s)
EOF

echo
echo "SMOKE TEST PASSED (offline; no API keys needed)"
