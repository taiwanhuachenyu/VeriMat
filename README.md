# VeriMat

**Evidence-grounded, falsification-aware materials discovery**

VeriMat is an auditable agent core for literature-driven materials research. It does not treat a fluent model answer as a scientific conclusion. The model may propose candidates and local judgments; deterministic components enforce evidence admission, time cutoffs, state transitions, budgets, and final acceptance.

The output is a **discovery package**: a bounded, falsifiable claim with replayable supporting evidence, counterevidence considered, and a minimal verification experiment.

## Why VeriMat

Conventional RAG is optimized to retrieve relevant support. Materials research also needs to ask whether a claim survives counterevidence, whether it holds only under narrower conditions, and whether the evidence is sufficient to decide at all.

VeriMat addresses this with a **Claim–Evidence Decision Graph (CEDG)**:

```text
CLAIM → QUERY → EVIDENCE → DECISION
```

- `SUPPORTS`, `CONTRADICTS`, `BOUNDS`, and `PRECEDES` are typed, auditable relations.
- Evidence enters the graph only after verbatim-quote, numerical-replay, and closed-vocabulary gates.
- A deterministic projection maps the graph to `SURVIVED`, `NARROWED`, `REFUTED`, or `UNRESOLVED`.
- Model confidence is recorded, but it is never sufficient for a terminal decision.

See [Methods](docs/METHODS.md) for the algorithm and [Prompts](docs/PROMPTS.md) for the complete model prompts and response schemas.

## Reproducible evaluation snapshot

The sealed `semifinal-v2-thermo` evaluation contains 258 claims and six method variants sharing the same corpus snapshot, retrieval cache, model route, budget, and random seed.

| Question | Result |
|---|---:|
| Does CEDG improve decision accuracy beyond dual retrieval? | `0.1589 → 0.4826`, Δ `+0.3236`, Holm `p=0.0003` |
| Does it reduce unsupported overclaiming? | `0.1395 → 0.0426`, down `69.5%` |
| Can accepted evidence be replayed to source text? | `1.000` replay precision for all six methods |
| Are downstream outputs actionable? | 44 falsifiable discovery packages; 54 invalid packages refused and logged |

The evaluation measures closed-loop literature-verification behavior under an automated time-split oracle. It is not a substitute for expert-attested materials discovery. Full results and limitations are documented in [Results](docs/RESULTS.md).

## One-command workflows

Linux and Python 3.11+ are recommended.

```bash
# 1. Create .venv and install pinned dependencies
bash scripts/install.sh

# 2. Offline installation and core-logic check; no API key required
bash scripts/smoke_test.sh

# 3. Recompute metrics and the report from the sealed snapshot; no API key required
bash scripts/evaluate.sh
```

To rebuild the complete pipeline, including model-backed stages:

```bash
cp config/verimat.env.example .env
# Fill SCIVERSE_API_TOKEN and start the configured local OpenCode server.
bash scripts/reproduce_core.sh
```

`reproduce_core.sh` executes `extract → claims → verify → gaps → oracle → packs → score → report`. Completed model operations are replayed from persistent caches; stages with existing products are skipped or rebuilt deterministically.

## What the engineering layer guarantees

| Failure mode | Control | Observable effect |
|---|---|---|
| Evidence changes or is mislocated | `doc_id` + locator + SHA-256, verified on admission and replay | tampering, deletion, reordering, and silent byte drift are detectable |
| Long runs fail halfway | persistent `JobStore`, checkpoints, stable `operation_id` | completed operations resume without duplicate model calls |
| A paid request has an uncertain outcome | fail-closed `PENDING` state and hard token budget | no automatic retry that may double-charge or double-write |
| Ablations receive different retrieval results | shared content-addressed retrieval snapshot | method differences are attributable to the verification layer |
| Platform behavior drifts | portability layer and multi-OS/Python CI | the same core protocol is exercised on Linux, macOS, and Windows |

## Traceability

Every reported number follows the same chain:

```text
code version
  → preregistration/semifinal_v2.json
  → corpus_snapshot.json + retrieval_cache.jsonl
  → model operation ledgers
  → {method}/predictions.jsonl
  → summary.json / REPORT.md / discovery_packages.jsonl
```

Use [Reproducibility](docs/REPRODUCIBILITY.md) for clean-install, offline replay, full-run, expected-output, and troubleshooting instructions.

## Repository map

```text
src/                 deterministic core, transports, MCTS, audit and scoring
experiments/         frozen evaluation pipeline and analysis entry points
preregistration/     immutable evaluation definitions
results/             sealed, replayable result snapshots
schemas/             machine-readable artifact contracts
config/              non-secret configuration templates
scripts/             install, smoke, evaluate and full reproduction commands
tests/               unit and integration tests
docs/                methods, results, prompts, data and reproducibility notes
```

Generated submission documents, competition ZIPs, secrets, local scratch data, copyrighted source PDFs, and private agent/session traces are intentionally excluded from the public release.

## Data, models, and cost

- Discovery corpus: Sciverse, years 2000–2021; 100 candidate documents, 22 full-text-locatable papers, 227 passages.
- Validation window: 2022–2025, isolated from the discovery process.
- Formal snapshot route: `zhipuai/glm-5.3-flash` through OpenCode 1.18.21. The same tool-disabled structured route was live-revalidated on OpenCode 1.18.25 on 2026-09-02.
- Sampling: provider defaults; no temperature or top-p override exposed by the transport.
- Statistical seed: `20260903`; method budget: 3M tokens each; observed total: 1.28M tokens.
- The repository contains no credentials. Copy `config/verimat.env.example` to `.env` for local use.

Dataset provenance, redistribution boundaries, external services, and dependency licenses are listed in [Data and licenses](docs/DATA_AND_LICENSES.md).

## Limitations

- The oracle uses model-assisted local judgments constrained by verbatim evidence; expert review is still required.
- 144 of 258 claims remain `UNRESOLVED`, which is a deliberate conservative outcome rather than a forced discovery.
- The current confirmatory snapshot covers one thermoelectric domain and one model route.
- Pareto-MCTS and database validation did not show significant marginal gains at this sample size; the supported performance claim is specifically about CEDG.

## License

Code is released under the [MIT License](LICENSE). Third-party data and services retain their own terms; see [Data and licenses](docs/DATA_AND_LICENSES.md).
