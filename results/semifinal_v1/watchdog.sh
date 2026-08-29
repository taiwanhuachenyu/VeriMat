#!/bin/bash
# Semifinal run watchdog: resume the chain if it dies before summary.json exists.
RUN_DIR=/data/corp/yike.gui/AIresearch/VeriMat/results/semifinal_v1
LOG=/data/corp/yike.gui/AIresearch/VeriMat/results/semifinal_v1/watchdog.log
while true; do
  if [ -f "$RUN_DIR/summary.json" ] && [ -f "$RUN_DIR/REPORT.md" ]; then
    echo "$(date '+%H:%M:%S') complete, watchdog exiting" >> "$LOG"; break
  fi
  if ! pgrep -f "run_semifinal_v1.py" > /dev/null; then
    echo "$(date '+%H:%M:%S') chain dead, resuming" >> "$LOG"
    cd /data/corp/yike.gui/AIresearch/VeriMat
    python3 -u experiments/run_semifinal_v1.py --stage verify --out results/semifinal_v1 >> /tmp/sse_full.log 2>&1
    python3 -u experiments/run_semifinal_v1.py --stage oracle --out results/semifinal_v1 >> /tmp/sse_full.log 2>&1
    python3 -u experiments/run_semifinal_v1.py --stage score --out results/semifinal_v1 >> /tmp/sse_full.log 2>&1
    python3 -u experiments/run_semifinal_v1.py --stage report --out results/semifinal_v1 >> /tmp/sse_full.log 2>&1
  fi
  sleep 120
done
