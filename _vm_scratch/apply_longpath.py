"""Apply the MAX_PATH conversion at every caller-supplied path entry point."""
import sys
from pathlib import Path

NL = chr(10)
PORT = "from src.core.portability import extended_path"
ROOT = Path(__file__).resolve().parents[1]


def after(anchor):
    return (anchor, anchor + NL + PORT)


def before(anchor, blank=False):
    return (anchor, PORT + NL + (NL if blank else "") + anchor)


EDITS = {
    "src/evaluation/baseline_runner.py": [
        before("from src.evidence.graph import ClaimState, EvidenceRelation"),
        ('json.loads(Path(path).read_text(encoding="utf-8"))',
         'json.loads(extended_path(path).read_text(encoding="utf-8"))'),
        ("        self.ledger_root = Path(ledger_root)",
         "        self.ledger_root = extended_path(ledger_root)"),
    ],
    "src/evaluation/blinding.py": [
        after("from src.core.events import canonical_json"),
        ("    source = Path(challenge_path)", "    source = extended_path(challenge_path)"),
        ("    output = Path(output_dir)", "    output = extended_path(output_dir)"),
        ('json.loads(Path(manifest_path).read_text(encoding="utf-8"))',
         'json.loads(extended_path(manifest_path).read_text(encoding="utf-8"))'),
        ("_sha256(Path(task_path))", "_sha256(extended_path(task_path))"),
    ],
    "src/evaluation/challenge.py": [
        after("from src.core.events import canonical_json"),
        ('enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1)',
         'enumerate(extended_path(path).read_text(encoding="utf-8").splitlines(), 1)'),
        ("    target = Path(path)", "    target = extended_path(path)"),
        ("    snapshot_path = Path(evidence_snapshot_path)",
         "    snapshot_path = extended_path(evidence_snapshot_path)"),
        ("ledger_root=Path(ledger_root), context=context,",
         "ledger_root=extended_path(ledger_root), context=context,"),
        ('"challenge_sha256": _sha256(Path(challenge_path)),',
         '"challenge_sha256": _sha256(extended_path(challenge_path)),'),
        ('"prediction_sha256": _sha256(Path(prediction_path)),',
         '"prediction_sha256": _sha256(extended_path(prediction_path)),'),
    ],
    "src/evaluation/circuit_breaker.py": [
        before("from src.operations.runtime_migrations import ("),
        ("        path = Path(database)", "        path = extended_path(database)"),
    ],
    "src/evaluation/claude_code_transport.py": [
        ("from src.core.portability import exclusive_lock",
         "from src.core.portability import exclusive_lock, extended_path"),
        ("self.usage_log = Path(usage_log) if usage_log else None",
         "self.usage_log = extended_path(usage_log) if usage_log else None"),
        ("        db_path = Path(operation_db)", "        db_path = extended_path(operation_db)"),
    ],
    "src/evaluation/credit.py": [
        before("from .challenge import COUNTER_RELATIONS, validate_challenges", blank=True),
        ("        self.path = Path(challenge_path)",
         "        self.path = extended_path(challenge_path)"),
    ],
    "src/evaluation/evidence_drift.py": [
        after("from src.core.events import canonical_json"),
        ("    snapshots_target = Path(snapshot_path)",
         "    snapshots_target = extended_path(snapshot_path)"),
        ("    observations_target = Path(observation_path)",
         "    observations_target = extended_path(observation_path)"),
    ],
    "src/evaluation/literature_retriever.py": [
        after("from src.core.events import canonical_json"),
        ("        path = Path(operation_db)", "        path = extended_path(operation_db)"),
    ],
    "src/evaluation/offline_sanity.py": [
        before("from .baseline_runner import (", blank=True),
        ("for line in Path(snapshot_path).read_text(",
         "for line in extended_path(snapshot_path).read_text("),
    ],
    "src/evaluation/opencode_transport.py": [
        after("from src.core.events import canonical_json"),
        ("        db_path = Path(operation_db)", "        db_path = extended_path(operation_db)"),
    ],
    "src/evaluation/operation_recovery.py": [
        after("from src.core.events import canonical_json"),
        ("    target = Path(path)", "    target = extended_path(path)"),
    ],
    "src/evaluation/ordered_runner.py": [
        after("from src.core.events import canonical_json"),
        ("        self.ledger_root = Path(ledger_root)",
         "        self.ledger_root = extended_path(ledger_root)"),
    ],
    "src/evidence/ledger.py": [
        ("from src.core.portability import lock_exclusive, lock_shared",
         "from src.core.portability import extended_path, lock_exclusive, lock_shared"),
        ("        self.path = Path(path)", "        self.path = extended_path(path)"),
        ("        target = Path(destination)", "        target = extended_path(destination)"),
    ],
    "src/learning/policy_store.py": [
        before("from src.operations.runtime_migrations import ("),
        ("        self.path = str(path)", "        self.path = str(extended_path(path))"),
    ],
    "src/operations/migrations.py": [
        after("from src.core.events import canonical_json"),
        ("    target = Path(path)", "    target = extended_path(path)"),
    ],
    "src/operations/runtime_migrations.py": [
        after("from src.core.events import canonical_json"),
        ("    target = Path(path)", "    target = extended_path(path)"),
    ],
    "src/orchestration/job_store.py": [
        after("from src.core.events import EventValidationError, validate_durable_payload"),
        ("        self.path = str(path)", "        self.path = str(extended_path(path))"),
    ],
    "src/orchestration/worker.py": [
        after("from src.core.events import EventValidationError"),
        ("        self.ledger_root = Path(ledger_root)",
         "        self.ledger_root = extended_path(ledger_root)"),
    ],
    "src/service/api.py": [
        before("from src.orchestration.job_store import ("),
        ("        self.store_path = str(store_path)",
         "        self.store_path = str(extended_path(store_path))"),
    ],
    "src/service/tls.py": [
        ("from src.core.portability import is_group_or_world_accessible",
         "from src.core.portability import extended_path, is_group_or_world_accessible"),
        ("    path = Path(value)", "    path = extended_path(value)"),
    ],
    "src/service/tracing.py": [
        ("    create_private_file, lock_exclusive, open_append_nofollow,",
         "    create_private_file, extended_path, lock_exclusive, open_append_nofollow,"),
        ("        self.path = Path(path)", "        self.path = extended_path(path)"),
    ],
    "src/tools/jsonl_ops.py": [
        ("from pathlib import Path", "from pathlib import Path" + NL + NL + PORT),
        ('for line in Path(path).read_text(encoding="utf-8", errors="ignore")',
         'for line in extended_path(path).read_text(encoding="utf-8", errors="ignore")'),
        ("        Path(path).write_text(out + ", "        extended_path(path).write_text(out + "),
        ('json.loads(Path(a.schema).read_text(encoding="utf-8"))',
         'json.loads(extended_path(a.schema).read_text(encoding="utf-8"))'),
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
print(NL + f"{len(EDITS)} files converted")
