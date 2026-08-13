# Development benchmark results

This directory contains the complete scored outputs used in the initial-round feasibility
analysis.

- `manifest.json`: benchmark, runtime, method, budget, and input hashes;
- `comparison.json`: cluster-aware bootstrap intervals and exact paired sign-flip tests;
- `<method>/predictions.jsonl`: one prediction for each blinded task;
- `<method>/per_challenge.jsonl`: one scored record for each challenge;
- `<method>/summary.json`: aggregate metrics and scientific-status marker;
- `figures/`: architecture-independent visual summaries of these files.

The four configurations use the same frozen task bundle and budget. The backend is deterministic,
so the results validate the evaluation and evidence-handling pipeline rather than open-world
materials discovery performance.
