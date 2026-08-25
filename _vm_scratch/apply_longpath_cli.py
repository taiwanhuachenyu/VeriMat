"""Convert the argv-supplied path entry points of the command-line scripts."""
import sys
from pathlib import Path

NL = chr(10)
PORT = "from src.core.portability import extended_path"
ROOT = Path(__file__).resolve().parents[1]

GUARD = (
    'if __package__ in {None, ""}:' + NL
    + "    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))"
)

EDITS = {
    "src/tools/audit_queries.py": [
        ("from src.harness.validator import _artifact_after_replays, _counter_replay_coverage",
         PORT + NL
         + "from src.harness.validator import _artifact_after_replays, _counter_replay_coverage"),
        ('parser.add_argument("--audit", type=Path, required=True)',
         'parser.add_argument("--audit", type=extended_path, required=True)'),
        ('parser.add_argument("outputs", nargs="+", type=Path)',
         'parser.add_argument("outputs", nargs="+", type=extended_path)'),
    ],
    "experiments/compare_v2.py": [
        ("from src.evaluation.statistics import StatisticsError, compare_methods",
         PORT + NL + "from src.evaluation.statistics import StatisticsError, compare_methods"),
        ("    return identifier, Path(directory)",
         "    return identifier, extended_path(directory)"),
        ('parser.add_argument("--benchmark-manifest", required=True, type=Path)',
         'parser.add_argument("--benchmark-manifest", required=True, type=extended_path)'),
        ('parser.add_argument("--output", required=True, type=Path)',
         'parser.add_argument("--output", required=True, type=extended_path)'),
    ],
    "experiments/plot_dev_results.py": [
        ("import argparse" + NL + "import json" + NL + "from pathlib import Path",
         "import argparse" + NL + "import json" + NL + "import sys" + NL
         + "from pathlib import Path"),
        ("import seaborn as sns" + NL,
         "import seaborn as sns" + NL + NL + GUARD + NL + NL + PORT + NL),
        ('parser.add_argument("--results", type=Path, required=True)',
         'parser.add_argument("--results", type=extended_path, required=True)'),
        ('parser.add_argument("--output", type=Path, required=True)',
         'parser.add_argument("--output", type=extended_path, required=True)'),
    ],
    "experiments/run_v2_plumbing_sanity.py": [
        ("from src.core.portability import fsync_directory" + NL
         + "from typing import Any" + NL + NL
         + GUARD + NL + NL
         + "from src.evaluation.baseline_runner import BaselineTaskRunner, MethodSpec",
         "from typing import Any" + NL + NL
         + GUARD + NL + NL
         + "from src.core.portability import extended_path, fsync_directory" + NL
         + "from src.evaluation.baseline_runner import BaselineTaskRunner, MethodSpec"),
        ('parser.add_argument("--tasks", required=True, type=Path)',
         'parser.add_argument("--tasks", required=True, type=extended_path)'),
        ('parser.add_argument("--task-manifest", required=True, type=Path)',
         'parser.add_argument("--task-manifest", required=True, type=extended_path)'),
        ('parser.add_argument("--evidence-snapshots", required=True, type=Path)',
         'parser.add_argument("--evidence-snapshots", required=True, type=extended_path)'),
        ('parser.add_argument("--challenges", required=True, type=Path)',
         'parser.add_argument("--challenges", required=True, type=extended_path)'),
        ('parser.add_argument("--methods", required=True, type=Path)',
         'parser.add_argument("--methods", required=True, type=extended_path)'),
        ('parser.add_argument("--output", required=True, type=Path)',
         'parser.add_argument("--output", required=True, type=extended_path)'),
    ],
}

problems = []
for relative, replacements in EDITS.items():
    target = ROOT / relative
    text = target.read_text(encoding="utf-8")
    for old, new in replacements:
        count = text.count(old)
        if count != 1:
            problems.append(f"{relative}: {count} matches for {old!r}")
            continue
        text = text.replace(old, new)
    target.write_text(text, encoding="utf-8")
    print(f"ok  {relative} ({len(replacements)} replacements)")

if problems:
    print(NL + "FAILED:")
    for problem in problems:
        print("  " + problem)
    sys.exit(1)
print(NL + f"{len(EDITS)} CLI files converted")
