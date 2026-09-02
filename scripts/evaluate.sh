#!/usr/bin/env bash
# Recompute the formal metrics and report from the sealed snapshot (offline).
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="results/semifinal_v2"
PREREG_PATH="preregistration/semifinal_v2.json"

python3 experiments/run_semifinal_v1.py --stage score --out "$OUT_DIR" --prereg "$PREREG_PATH"
python3 experiments/run_semifinal_v1.py --stage report --out "$OUT_DIR" --prereg "$PREREG_PATH"

python3 - <<'PY'
import json
from pathlib import Path

summary = json.loads(Path("results/semifinal_v2/summary.json").read_text(encoding="utf-8"))
v2 = summary["methods"]["V2-dual-cedg"]
comparison = summary["comparisons"]["V2-dual-cedg__vs__V1-dual-retrieval"]
assert v2["decision_accuracy"] == 0.4826, v2
assert v2["overclaim_rate"] == 0.0426, v2
assert comparison["mean_delta"] == 0.3236, comparison
assert comparison["p_holm_adjusted"] == 0.0003, comparison
print("EVALUATION PASSED: accuracy=0.4826 delta=+0.3236 Holm-p=0.0003")
PY
