# Reproducibility

This guide separates fast offline verification from a complete model-backed rebuild.

## 1. Clean installation

Requirements: Linux or macOS, Bash, Python 3.11+, and internet access for the initial dependency download.

```bash
bash scripts/install.sh
source .venv/bin/activate
```

The installer creates `.venv`, upgrades packaging tools, and installs the pinned `requirements-dev.lock`. It never reads or creates credentials.

## 2. Offline smoke test

```bash
bash scripts/smoke_test.sh
```

Expected final line:

```text
SMOKE TEST PASSED (offline; no API keys needed)
```

The smoke test covers the CEDG/evaluation core, Pareto-MCTS example, OpenCode transport contract, and deterministic scoring fixture.

## 3. Offline metric replay

```bash
bash scripts/evaluate.sh
```

This command reads the sealed artifacts in `results/semifinal_v2/`, recomputes all six method metrics and pre-registered paired comparisons, rebuilds `REPORT.md`, and asserts the headline values:

- V2 decision accuracy: `0.4826`
- V2 versus V1 mean difference: `0.3236`
- Holm-adjusted p-value: `0.0003`
- V2 overclaim rate: `0.0426`

No network connection or API credential is required.

## 4. Full pipeline rebuild

1. Copy `config/verimat.env.example` to `.env`.
2. Set `SCIVERSE_API_TOKEN`.
3. Start an OpenCode server compatible with the route declared in `preregistration/semifinal_v2.json`.
4. Run `bash scripts/reproduce_core.sh`.

Stages run in this order:

```text
extract → claims → verify → gaps → oracle → packs → score → report
```

| Field | Formal value |
|---|---|
| Provider/model | `zhipuai/glm-5.3-flash` |
| OpenCode (formal snapshot) | `1.18.21` |
| Current validated runtime | `1.18.25` (live JSON-schema request passed 2026-09-02) |
| Endpoint | configured local server, default `http://127.0.0.1:4124` |
| Agent | `build` |
| Model tools | disabled |
| Sampling | provider defaults |
| Per-method budget | 3,000,000 tokens |
| Statistical seed | `20260903` |

## 5. Expected outputs

- `results/semifinal_v2/summary.json`
- `results/semifinal_v2/REPORT.md`
- `results/semifinal_v2/discovery_packages.jsonl`
- `results/semifinal_v2/packs_refused.json`
- `results/semifinal_v2/{method}/predictions.jsonl`
- `results/semifinal_v2/{method}/usage.json`

Model-operation SQLite ledgers and retrieval audit logs provide the intermediate trace. Submission archives additionally carry a SHA-256 manifest.

## 6. Determinism and resumption

- Stable content-derived IDs make repeated stages address the same operations.
- Completed model calls are replayed from operation ledgers.
- Retrieval results are read from the shared content-addressed cache.
- Uncertain paid-call outcomes remain `PENDING` and are not retried automatically.
- The paired permutation test uses the frozen seed `20260903`.

## 7. Troubleshooting

- Missing `SCIVERSE_API_TOKEN`: offline smoke and evaluation still work; only retrieval-backed rebuild stages require it.
- OpenCode unavailable: confirm `VERIMAT_OPENCODE_BASE_URL`, provider, model, and agent in `.env`.
- A stage appears skipped: existing products are treated as completed. Preserve the formal snapshot; use a separate output directory for exploratory reruns.
- Hash or replay failure: do not overwrite the evidence. Inspect the corresponding retrieval audit and operation ledger first.
