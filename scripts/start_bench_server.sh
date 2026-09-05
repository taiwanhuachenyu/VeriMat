#!/bin/bash
# GLM-5.3-Flash runs at reasoningEffort=max: 6/6 first-attempt schema
# compliance in testing (medium was 5/6), ~9s per call.
# Start the isolated benchmark OpenCode server (Linux, port 4124).
# One-time setup: copy config/opencode_bench.json to $BENCH_HOME/.config/opencode/opencode.json
# and put your zhipu coding-plan key inside it. Then:
BENCH_HOME="${BENCH_HOME:-/tmp/oc-bench}"
mkdir -p "$BENCH_HOME/.config/opencode"
[ -f "$BENCH_HOME/.config/opencode/opencode.json" ] || cp "$(dirname "$0")/../config/opencode_bench.json" "$BENCH_HOME/.config/opencode/opencode.json"
setsid nohup env HOME="$BENCH_HOME" opencode serve --port 4124 --hostname 127.0.0.1 \
  > /tmp/opencode_serve_4124.log 2>&1 < /dev/null &
sleep 10
ss -tln | grep 4124 && echo "bench server UP on 127.0.0.1:4124"
