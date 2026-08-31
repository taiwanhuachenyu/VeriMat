#!/bin/bash
# v2 confirmatory run: fully self-healing. Safe to kill and re-invoke at any time.
cd /data/corp/yike.gui/AIresearch/VeriMat
OUT=results/semifinal_v2
PREREG=preregistration/semifinal_v2.json
LOG=/tmp/sse2_chain.log
RUN="python3 -u experiments/run_semifinal_v1.py --out $OUT --prereg $PREREG"

while true; do
  if [ -f $OUT/summary.json ] && [ -f $OUT/REPORT.md ] && [ -f $OUT/discovery_packages.jsonl ]; then
    echo "$(date '+%m-%d %H:%M') CHAIN COMPLETE" >> $LOG; break
  fi
  echo "$(date '+%m-%d %H:%M') pass start" >> $LOG
  [ -f $OUT/corpus_snapshot.json ] || $RUN --stage freeze  >> $LOG 2>&1
  [ -f $OUT/relations.jsonl ]      || $RUN --stage extract >> $LOG 2>&1
  [ -f $OUT/claims.jsonl ]         || $RUN --stage claims  >> $LOG 2>&1
  [ -f $OUT/gaps.jsonl ]           || $RUN --stage gaps    >> $LOG 2>&1
  [ -f $OUT/V1-dual-retrieval/predictions.jsonl ] || $RUN --stage verify --only V1-dual-retrieval >> $LOG 2>&1
  for M in V2-dual-cedg V3-full A1-no-mcts A2-no-db V0-vanilla-rag; do
    [ -f $OUT/$M/predictions.jsonl ] || $RUN --stage verify --only $M >> $LOG.$M 2>&1 &
  done
  wait
  [ -f $OUT/oracle_claims.json ]   || $RUN --stage oracle >> $LOG 2>&1
  [ -f $OUT/discovery_packages.jsonl ] || $RUN --stage packs >> $LOG 2>&1
  $RUN --stage score >> $LOG 2>&1
  $RUN --stage report >> $LOG 2>&1
  sleep 20
done
