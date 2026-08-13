# Offline experiment

The development experiment compares four retrieval and verification configurations on the
bundled blinded benchmark. It uses a deterministic backend and does not make network or model
calls.

Run from the repository root:

```bash
python experiments/run_v2_plumbing_sanity.py \
  --tasks benchmarks/v2_dev/blind/tasks.jsonl \
  --task-manifest benchmarks/v2_dev/blind/task_manifest.json \
  --evidence-snapshots benchmarks/v2_dev/evidence_snapshots.jsonl \
  --challenges benchmarks/v2_dev/challenges.jsonl \
  --methods experiments/v2_methods.json \
  --output /tmp/verimat-dev-run \
  --run-id verimat-dev-reproduction
```

Each method directory contains `predictions.jsonl`, `per_challenge.jsonl`, and `summary.json`.
The checked-in results are under `results/v2_dev_plumbing_sanity_v4`.

To regenerate the summary figure:

```bash
python -m pip install -r requirements-figures.lock
python experiments/plot_dev_results.py \
  --results results/v2_dev_plumbing_sanity_v4 \
  --output results/v2_dev_plumbing_sanity_v4/figures/dev_benchmark_summary.png
```

The benchmark is a small engineering development set. Every result manifest carries
`scientific_result=false`.
