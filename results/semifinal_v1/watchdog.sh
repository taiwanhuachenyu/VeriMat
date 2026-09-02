#!/bin/bash
# Semifinal run watchdog: resume the chain if it dies before summary.json exists.
set -eu
VERIMAT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
RUN_DIR="$VERIMAT_ROOT/results/semifinal_v1"
LOG="$RUN_DIR/watchdog.log"
while true; do
  if [ -f "$RUN_DIR/summary.json" ] && [ -f "$RUN_DIR/REPORT.md" ]; then
    echo "$(date '+%H:%M:%S') complete, watchdog exiting" >> "$LOG"; break
  fi
  if ! pgrep -f "run_semifinal_v1.py" > /dev/null; then
    echo "$(date '+%H:%M:%S') chain dead, resuming" >> "$LOG"
    cd "$VERIMAT_ROOT"
    python3 -u experiments/run_semifinal_v1.py --stage verify --out results/semifinal_v1 >> /tmp/sse_full.log 2>&1
    python3 -u experiments/run_semifinal_v1.py --stage oracle --out results/semifinal_v1 >> /tmp/sse_full.log 2>&1
    python3 -u experiments/run_semifinal_v1.py --stage score --out results/semifinal_v1 >> /tmp/sse_full.log 2>&1
    python3 -u experiments/run_semifinal_v1.py --stage report --out results/semifinal_v1 >> /tmp/sse_full.log 2>&1
  fi
  sleep 120
done
