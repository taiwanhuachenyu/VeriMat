# V2 materials challenge set — development draft

This directory is a cryptographically sealed **development set**, not the hidden publication test
set. It exists to exercise retrieval, evidence replay, decision calibration, and the shared baseline
runner before spending model budget.

The ten challenges contain eight deliberately over-broad or false claims with retrievable
counterevidence and two narrowly scoped empirical controls. Each evidence capsule is a short passage
from a primary CC-BY-4.0 research article. Mathematical typography and citation markers may be
normalized to plain ASCII; the normalization is disclosed per capsule. The capsule SHA-256, DOI,
source URL, section locator, attribution, retrieval timestamp, and license are bound into the seal.

Important limitations:

- Curation status is `draft`; no independent materials-domain curator has checked the labels.
- The set is small, battery/perovskite-heavy, and not suitable for a scientific quality claim.
- Negative controls assert only the narrowly stated reported result, not global absence of contrary
  evidence.
- Development items and evidence are public and must never be used as a hidden test set.
- Two halide questions share one leakage group intentionally; all are in the development split.
- The source-by-source audit is in `SOURCE_AUDIT.md`.

Seal and inspect:

```bash
python3 experiments/seal_v2_benchmark.py benchmarks/v2_dev/challenges.jsonl \
  --evidence-snapshots benchmarks/v2_dev/evidence_snapshots.jsonl \
  --output benchmarks/v2_dev/manifest.json

python3 experiments/materialize_v2_tasks.py \
  --challenges benchmarks/v2_dev/challenges.jsonl \
  --output benchmarks/v2_dev/blind
```

The runner receives only `blind/tasks.jsonl`; benchmark labels and accepted evidence are joined by
the evaluator only after execution. The current seal intentionally reports
`"publication_ready": false` while curation remains draft.

For a zero-network, zero-model end-to-end plumbing check:

```bash
python3 experiments/run_v2_plumbing_sanity.py \
  --tasks benchmarks/v2_dev/blind/tasks.jsonl \
  --task-manifest benchmarks/v2_dev/blind/task_manifest.json \
  --evidence-snapshots benchmarks/v2_dev/evidence_snapshots.jsonl \
  --challenges benchmarks/v2_dev/challenges.jsonl \
  --methods experiments/v2_methods.json \
  --output reports/v2_dev_plumbing_sanity_v4
```

That command uses a deterministic rule fixture and lexical capsule search. Its scores are explicitly
marked `scientific_result: false`; they validate plumbing, not method quality.

The ordered memory plumbing check is separate because task order and policy state are part of the
treatment:

```bash
python3 experiments/run_v2_memory_plumbing_sanity.py \
  --tasks benchmarks/v2_dev/blind/tasks.jsonl \
  --task-manifest benchmarks/v2_dev/blind/task_manifest.json \
  --evidence-snapshots benchmarks/v2_dev/evidence_snapshots.jsonl \
  --challenges benchmarks/v2_dev/challenges.jsonl \
  --methods experiments/v2_methods.json \
  --output reports/v2_dev_memory_plumbing_sanity_v2 \
  --run-id v2-dev-memory-plumbing-sanity-v2
```

`experiments/run_v2_strong_baselines.py` is the paid, gold-free no-memory executor. It refuses to
start without an explicit `--acknowledge-paid-api`, requires a declared retrieval snapshot ID, and
writes unscored predictions. Do not launch it on this draft set merely because the plumbing passes.
First complete independent curation, freeze the manifests and snapshot declaration, then archive the
exact command and code digests.

`experiments/run_v2_strong_memory.py` applies the same refusal rule to the two ordered-memory arms.
It must be run only after the four frozen no-memory methods have completed and been audited. The
known answers are joined after each prediction solely to create delayed external credit for later
tasks; they are not passed to the model backend.

Before a publication freeze, a separate curator or acquisition service must reacquire each capsule
and preserve the SHA-256 of its raw retrieval receipt. The offline drift gate then checks those
observations without contacting a publisher or model service:

```bash
python3 experiments/audit_v2_evidence_drift.py \
  --snapshots benchmarks/v2_dev/evidence_snapshots.jsonl \
  --observations benchmarks/v2_dev/evidence_observations.jsonl \
  --output reports/v2_dev_evidence_drift.json \
  --enforce-publication-gate
```

The observation contract is `schemas/v2_evidence_observation.schema.json`. The example path above
does not exist yet by design: copying the sealed capsules into it would not constitute independent
reacquisition.
