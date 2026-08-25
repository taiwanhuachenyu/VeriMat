# VeriMat

VeriMat is an auditable research-agent core for evidence-grounded materials discovery.
Language models may propose hypotheses and search actions, while deterministic gates,
replayable evidence records, and counterevidence-aware search determine which bounded
claims survive.

## Core components

![VeriMat architecture](docs/assets/verimat_methods_architecture.png)

- `src/core`: the platform boundary, holding advisory file locks, durable renames, and
  long-path handling, so one set of durability guarantees covers both Windows and POSIX;
- `src/evidence`: hash-chained event ledger and Claim–Evidence Decision Graph projection;
- `src/discovery`: counterevidence-aware Pareto-MCTS, external evidence gates, and audited LLM guidance hooks;
- `src/tools`: the Sciverse literature client, whose call log is itself an evidence chain;
- `src/orchestration`: durable jobs, leases, checkpoints, budgets, and content-addressed artifacts;
- `src/learning`: delayed external credit and auditable policy updates;
- `src/evaluation`: blinded evaluation, calibration, evidence replay, statistical comparisons,
  and the structured language-model transports;
- `src/operations` and `src/service`: migrations, backup, retention, rate limits, tracing, and control APIs.

## Quick start

VeriMat requires Python 3.11 or newer. The test suite and the bundled example run offline:
no network access, no API key.

Linux and macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.lock
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
python examples/pareto_mcts_demo.py
```

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
python -m pytest -q
python examples\pareto_mcts_demo.py
```

If `python` opens the Microsoft Store rather than starting an interpreter, the name is
resolving to the Windows app alias and no interpreter is installed. Install one from
[python.org](https://www.python.org/downloads/windows/), then use `py -3` for the first
line; every later command runs from `.venv` and is unaffected.

The bundled development benchmark validates software plumbing only. It is not evidence
of a new materials discovery or open-world scientific performance.

## Cross-platform behaviour

Windows and POSIX are both first-class, and the same run is expected to produce the same
bytes on either. CI covers Linux, Windows and macOS across Python 3.11, 3.12 and 3.13, and
a final job fails the build if the platforms disagree on the digest of the example output.

Three differences are handled rather than assumed away. File locking uses `LockFileEx` on
Windows and `fcntl` elsewhere. A directory cannot be fsynced from user mode on Windows, so
the rename durability barrier is platform-specific; both platforms still refuse a
non-directory with the same `ENOTDIR`, so no caller can come to rely on one of them quietly
accepting a file. Paths beyond 260 characters need the
`\\?\` prefix on a default Windows install, so every filesystem entry point converts through
`extended_path`. `tests/test_long_paths.py` fails if a new entry point skips it, and CI first
proves the limit is genuinely in force on the runner, so those tests cannot pass by accident.

## Literature retrieval

`src/tools/sciverse.py` is a dependency-free client for the Sciverse corpus, covering
semantic search, metadata search, catalog introspection, full-text assembly, the citation
graph, and figure resources. Each call appends a projected record (the request, the hit
identifiers, a digest of the response) to a JSONL evidence chain, so a citation can be traced
back to the exact query that produced it. Behaviour is written against the live deployment,
which diverges from the published specification in several places; the module docstring
records each divergence together with the observation behind it.

| variable | meaning |
| --- | --- |
| `SCIVERSE_API_TOKEN` | required, of the form `sci_...` |
| `SCIVERSE_BASE_URL` | optional, defaults to `https://api.sciverse.space` |
| `SCIVERSE_AUDIT_LOG` | optional; when set, every call appends to this evidence chain |

The client is also a command line tool:

```bash
python -m src.tools.sciverse catalog --collection papers
python -m src.tools.sciverse search "high nickel cathode degradation" --top-k 20
```

## Language-model backends

Structured model calls go through a single router, so a run can use whichever backend the
operator has available without touching the code. `VERIMAT_MODEL_ROUTE` selects it.

| route | requires | variables |
| --- | --- | --- |
| `claude-code` (default) | the Claude Code CLI on `PATH` | `VERIMAT_CLAUDE_CLI`, `VERIMAT_CLAUDE_CODE_MODEL` |
| `opencode` | a reachable OpenCode server and a provider API key | `VERIMAT_OPENCODE_API_KEY`, `VERIMAT_OPENCODE_PROVIDER`, `VERIMAT_OPENCODE_MODEL`, `VERIMAT_OPENCODE_BASE_URL`, `VERIMAT_OPENCODE_AGENT` |

Both transports honour the same durability contract: a call is reserved in an operation
table before it is issued and marked complete only once the response has been recorded, and
an operation left `PENDING` is never retried automatically, because it may already have been
charged. `VERIMAT_MODEL_USAGE_LOG` records what each route actually spent.

## LLM-guided discovery search

`ParetoMCTS` keeps model participation distinct from deterministic admission. A structured
model may supply seed hypotheses and expansion priors, evaluate the scientific plausibility
of an intermediate hypothesis, and prune an expansion that falls outside the evidence-backed
focus. The objective evaluator, the hard evidence gate, and exact Pareto archive remain the
final authority. Every model-directed rejection is recorded as a `plausibility:` gate reason;
every removed expansion is emitted as a `prune` trace event with its hypothesis digest and
reason. `SearchReport` exposes separate plausibility-rejection and pruning counts so the
search can be replayed and audited.

## Reproducibility

The default random seed is `20260812`, used by the experiment runner (`--seed`) and by the
bootstrap in `src/evaluation/statistics.py`. The complete offline experiment runner, method
registry, per-task predictions, scored outputs, aggregate summaries, statistical comparison,
and figure source are in `experiments/` and `results/`; see
[`experiments/README.md`](experiments/README.md) for the exact command.

![Development benchmark summary](results/v2_dev_plumbing_sanity_v4/figures/dev_benchmark_summary.png)

## Dependency and API disclosure

- Trusted runtime: the Python 3.11+ standard library only (`requirements-runtime.lock`).
  Nothing under `src/` imports a third-party package, and `tests/test_dependency_surface.py`
  verifies that structurally rather than on trust.
- Tests: `pytest==9.1.1` (`requirements-dev.lock`).
- Figures: `matplotlib`, `pandas`, `seaborn` (`requirements-figures.lock`), used only to
  render figures from committed result files.
- External services, contacted only when the corresponding variable is set: the Sciverse
  literature API, and Anthropic models reached either through the Claude Code CLI or through
  an OpenCode provider. Credentials are read from the environment and are never written to
  the repository, to a log, or to an evidence record.

## License

VeriMat is released under the [MIT License](LICENSE).

## Evidence-grounded literature survey

The thermoelectric survey pipeline is under `src/survey/`. It uses Sciverse metadata search
for a hard candidate scope, then scoped semantic retrieval for passages. Every extracted
structure-property relation carries a passage quote, document identifier, database name,
content digest, and query identifier. The gap stage applies deterministic detectors first;
the selected model route only narrates and classifies those candidates.

The extraction and gap stages are guarded by deterministic checks: the cited passage must
have been exposed, the quote must occur literally, and every reported numeric token must be
present in that quote. `src/survey/report.py` emits `survey.tex`, `references.bib`, JSON
sidecars, and a verification audit. The report writer refuses to write an unverified bundle.
The generated LaTeX source and BibTeX file are the submission sources; compile them with
`build.py` or `make` after installing a TeX distribution:

```bash
python build.py
```

The report date is an explicit input rather than `\today`, and the generated build pins
`SOURCE_DATE_EPOCH` so repeated builds are comparable. The default author is
`taiwanhuachenyu`. API credentials remain environment-only; never place `SCIVERSE_API_TOKEN`
in a report, fixture, commit, or evidence log.

## Materials database cross-validation

`src/materials/cross_validation.py` provides the provider-neutral validation contract for
Sciverse literature observations and records returned by Materials Project, OQMD, or NOMAD
adapters. Each `MaterialObservation` retains provider ID, source locator, content digest,
composition, property, numeric value, unit, temperature, method, and uncertainty. Formula and
property aliases are normalized deterministically; only explicitly supported unit dimensions are
converted. Pairing requires matching composition, property, method (configurable), and
measurement temperature within a declared tolerance. Unmatched records are retained with a
reason rather than silently dropped.

The resulting report separates numerical database agreement from the discovery gate and includes
MAE, RMSE, signed bias, R-squared when defined, and tolerance pass rate. The module is standard
library only and its tests use synthetic, offline observations, so the same comparison is
reproducible on Windows and POSIX. Provider adapters must implement `MaterialsProvider` and
return these normalized, provenance-bearing records; credentials and response payloads stay
outside durable evidence unless explicitly sanitized.

## Real Literature Experiments

The survey package has two distinct modes. Unit tests and the bundled benchmark use only local
fixtures. A real literature experiment requires `SCIVERSE_API_TOKEN` in an untracked `.env` or
environment variable, an authenticated Claude Code session (or a configured OpenCode server), and
may incur model and literature-provider charges. Real-run artifacts must be treated as experimental
evidence: their corpus, model route, usage and model-response audit are retained together and must
not be replaced by a plumbing benchmark.

The initial thermoelectric run is deliberately small (four metadata candidates) because observed
structured-model latency is about two minutes per call. It first performs relevance-ranked metadata
search, then hard-scoped semantic search. Each metadata candidate receives additional structure
variable probes for doping/co-doping, defects/vacancies, grain size/nanostructure, carrier
concentration, and ZT/Seebeck/thermal-conductivity/power-factor relations. The coverage manifest
records probe count, empty probes, documents with evidence and passages per document; expand the
corpus only after those indicators show evidence is spread beyond a single document.

Every real run writes the following ignored artifacts under its experiment directory:

- `corpus_snapshot.json`: canonical topic, documents, queries and full passages, with a top-level
  SHA-256 digest. `SurveyCorpus.read_snapshot` rejects a tampered snapshot.
- `corpus_manifest.json`: retrieval coverage and the hard scope protocol.
- `sciverse_audit.jsonl`: query/result evidence chain without credentials.
- `model_operations.sqlite`: durable model-operation ledger. A `PENDING` operation is never
  automatically retried, since the provider may have charged it.
- `model_usage.jsonl`: per-call authoritative tokens, duration and reported cost, without prompts.
- `model_request_response.jsonl`: opt-in reproducibility audit containing prompts, corpus passages,
  schemas, structured responses and, for an indeterminate response, the raw CLI envelope. It can
  contain copyrighted corpus text and must remain local unless its retention and sharing status are
  reviewed.
- `report/`: verified LaTeX, BibTeX and JSON survey deliverables.

Use the same runner on both supported shells after creating `.env` locally; the token is not an
argument and must never be copied into a command history, report or commit.

Linux/macOS:

```bash
. .venv/bin/activate
python experiments/run_thermoelectric_real.py
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python .\_vm_scratch\run_thermoelectric_real.py
```

The current runner remains in `_vm_scratch` while the experiment protocol is being iterated. Once a
round closes cleanly, it will be promoted with its configuration schema and frozen reproducibility
inputs into the committed experiment entry point.
