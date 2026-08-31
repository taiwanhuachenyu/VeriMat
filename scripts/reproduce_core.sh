#!/bin/bash
# Reproduce the core semifinal result (v2 confirmatory run).
#
# Prerequisites (one-time):
#   1. Python 3.11+, pip install -r requirements-dev.lock
#   2. A local OpenCode server proxying zhipuai/glm-5.3-flash on 127.0.0.1:4124
#      (isolated config: see docs/semifinal_report.md §3.4; tools disabled, agent "benchmark")
#   3. .env with SCIVERSE_API_TOKEN (Sciverse corpus access) --
#      copy config/verimat.env.example to .env and fill it in.
#   4. The corpus/retrieval snapshots are committed under results/semifinal_v2/, so stages
#      with existing products are skipped; delete a product file to force its recomputation.
#
# Everything is resumable: completed LLM calls are served from the per-stage operation
# caches, and the retrieval snapshot is content-addressed, so re-invoking this script never
# repeats a billed call.
set -e
cd "$(dirname "$0")/.."

OUT=results/semifinal_v2
PREREG=preregistration/semifinal_v2.json

for STAGE in extract claims verify gaps oracle packs score report; do
  echo "== stage: $STAGE =="
  python3 experiments/run_semifinal_v1.py --stage "$STAGE" --out "$OUT" --prereg "$PREREG"
done

echo
echo "CORE REPRODUCTION DONE"
echo "  main table:      $OUT/summary.json"
echo "  report:          $OUT/REPORT.md"
echo "  discovery packs: $OUT/discovery_packages.jsonl"
