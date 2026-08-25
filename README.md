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
- `src/discovery`: counterevidence-aware Pareto-MCTS and external evidence gates;
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
