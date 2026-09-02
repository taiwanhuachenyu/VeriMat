#!/bin/bash
# v2 confirmatory run: fully self-healing. Safe to kill and re-invoke at any time.
set -eu
VERIMAT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$VERIMAT_ROOT"
OUT=results/semifinal_v2
PREREG=preregistration/semifinal_v2.json
LOG=/tmp/sse2_chain.log

run_stage() {
  python3 -u experiments/run_semifinal_v1.py --out "$OUT" --prereg "$PREREG" "$@"
}

while true; do
  if [ -f "$OUT/summary.json" ] && [ -f "$OUT/REPORT.md" ] && [ -f "$OUT/discovery_packages.jsonl" ]; then
    echo "$(date '+%m-%d %H:%M') CHAIN COMPLETE" >> "$LOG"; break
  fi
  echo "$(date '+%m-%d %H:%M') pass start" >> "$LOG"
  [ -f "$OUT/corpus_snapshot.json" ] || run_stage --stage freeze  >> "$LOG" 2>&1
  [ -f "$OUT/relations.jsonl" ]      || run_stage --stage extract >> "$LOG" 2>&1
  [ -f "$OUT/claims.jsonl" ]         || run_stage --stage claims  >> "$LOG" 2>&1
  [ -f "$OUT/gaps.jsonl" ]           || run_stage --stage gaps    >> "$LOG" 2>&1
  [ -f "$OUT/V1-dual-retrieval/predictions.jsonl" ] || run_stage --stage verify --only V1-dual-retrieval >> "$LOG" 2>&1
  for METHOD in V2-dual-cedg V3-full A1-no-mcts A2-no-db V0-vanilla-rag; do
    [ -f "$OUT/$METHOD/predictions.jsonl" ] || run_stage --stage verify --only "$METHOD" >> "$LOG.$METHOD" 2>&1 &
  done
  wait
  [ -f "$OUT/oracle_claims.json" ] || run_stage --stage oracle >> "$LOG" 2>&1
  [ -f "$OUT/discovery_packages.jsonl" ] || run_stage --stage packs >> "$LOG" 2>&1
  run_stage --stage score >> "$LOG" 2>&1
  run_stage --stage report >> "$LOG" 2>&1
  sleep 20
done
