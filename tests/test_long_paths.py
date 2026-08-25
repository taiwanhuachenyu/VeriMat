"""Every caller-supplied path root must work at a length Windows refuses by default.

Windows applies a 260-character ``MAX_PATH`` limit to any path reaching the Win32 API without the
extended-length prefix, and the registry opt-out is off by default.  A deploy directory, a CI
temporary directory or a tenant/content digest is enough to cross it, so each store converts its
root once at its entry point via ``extended_path``.  These tests hand the plain, unconverted long
path to production code: the conversion has to happen inside, not in the caller.

On POSIX the same tests still run, because ``extended_path`` only absolutises there.  That is the
point of running them on every platform: they assert behaviour that is free on POSIX and only holds
on Windows because of the conversion.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.core.portability import WINDOWS, extended_path, fsync_directory
from src.evaluation.circuit_breaker import PersistentCircuitBreaker
from src.evidence.ledger import EventLedger
from src.learning.policy_store import PolicyStore
from src.orchestration.job_store import JobStore
from src.service.tls import ServerTLSConfig, TLSConfigurationError, build_server_tls_context
from src.service.tracing import HttpTraceRecord, StructuredTraceLog, TraceContext
from src.tools import jsonl_ops

ROOT = Path(__file__).resolve().parents[1]
PORTABILITY = ROOT / "src" / "core" / "portability.py"


def _deep(tmp_path: Path) -> Path:
    """A caller-supplied root long enough that Windows refuses it without the prefix."""
    root = tmp_path.joinpath(*("nested" + "d" * 60,) * 4)
    assert len(str(root)) > 260
    return root


def test_the_limit_the_tests_below_bypass_is_actually_in_force(tmp_path):
    """The premise the rest of this file rests on: the host must refuse the unconverted path.

    A Windows host with the ``LongPathsEnabled`` registry opt-out on opens a 300-character path
    without the prefix, so every test below would pass whether or not the conversion existed.
    Reporting that as a skip keeps a green run from being read as evidence it cannot carry.
    """
    deep = _deep(tmp_path)
    extended_path(deep).mkdir(parents=True)
    probe = deep / "probe.txt"
    try:
        probe.write_text("plain", encoding="utf-8")
    except OSError:
        if not WINDOWS:
            raise
        return
    if WINDOWS:
        pytest.skip("LongPathsEnabled is on for this host; the tests below prove nothing here")
    assert probe.read_text(encoding="utf-8") == "plain"


def test_the_control_store_creates_charges_and_reads_past_the_limit(tmp_path):
    with JobStore(_deep(tmp_path) / "control" / "jobs.db") as jobs:
        job = jobs.create_job(
            tenant_id="tenant-a", idempotency_key="request-1", task="task",
            max_calls=4, max_tokens=100, max_cost_microunits=20,
        )
        jobs.charge(
            job.job_id, charge_key="call-1", provider="provider",
            calls=1, tokens=7, cost_microunits=2,
        )
        assert jobs.get(job.job_id, tenant_id="tenant-a").used_tokens == 7


def test_the_policy_store_opens_past_the_limit(tmp_path):
    """Covers the second store that keeps its path as a string rather than a ``Path``."""
    with PolicyStore(_deep(tmp_path) / "learning" / "policy.db") as store:
        assert store.recall_active(tenant_id="tenant-a", target_task_family="family") == []


def test_the_event_ledger_appends_verifies_and_receipts_past_the_limit(tmp_path):
    deep = _deep(tmp_path)
    ledger = EventLedger(deep / "ledgers" / "tenant-a" / "job-a" / "events.jsonl")
    ledger.append(
        tenant_id="tenant-a", job_id="job-a", aggregate_type="job", aggregate_id="job-a",
        event_type="job.created", payload={"version": 1}, idempotency_key="created",
    )
    assert ledger.verify().ok
    receipt = deep / "receipts" / "verification.json"
    assert ledger.write_verification_receipt(receipt).ok
    # Read back through the converted form: a bare ``exists()`` on a path this long answers False
    # on Windows whether or not the file is there, so it would pass for the wrong reason.
    assert extended_path(receipt).is_file()


def test_the_circuit_breaker_database_opens_past_the_limit(tmp_path):
    breaker = PersistentCircuitBreaker(
        database=_deep(tmp_path) / "runtime" / "circuits.db", circuit_id="provider",
    )
    try:
        breaker.before_call(operation_id="operation-1")
        breaker.record_success(operation_id="operation-1")
        assert breaker.snapshot()["state"] == "CLOSED"
    finally:
        breaker.close()


def test_the_trace_log_appends_past_the_limit(tmp_path):
    sink = StructuredTraceLog(_deep(tmp_path) / "traces" / "http.jsonl")
    sink.record(HttpTraceRecord.build(
        context=TraceContext.from_header(
            "00-0123456789abcdef0123456789abcdef-0123456789abcdef-00"
        ),
        request_id="request", method="GET", route="healthz",
        status=200, duration_seconds=0.012,
    ))
    assert sink.path.read_bytes().count(b"\n") == 1


def test_tls_credentials_are_validated_past_the_limit(tmp_path):
    """Before the conversion a long credential path failed validation as if the file were absent."""
    deep = _deep(tmp_path)
    extended_path(deep).mkdir(parents=True)
    certificate, private_key = deep / "server.crt", deep / "server.key"
    extended_path(certificate).write_text("present but not a usable certificate")
    extended_path(private_key).write_text("private")
    extended_path(private_key).chmod(0o600)
    # Reaching the client-CA complaint proves both files passed the regular-file check; an
    # unconverted path stops earlier with "must be a regular non-symlink file".
    with pytest.raises(TLSConfigurationError, match="client CA"):
        build_server_tls_context(ServerTLSConfig(
            certificate=certificate, private_key=private_key,
            require_client_certificate=True,
        ))


def test_the_jsonl_tool_round_trips_past_the_limit(tmp_path):
    deep = _deep(tmp_path)
    extended_path(deep).mkdir(parents=True)
    target = deep / "rows.jsonl"
    jsonl_ops.write_jsonl([{"doc_id": "one"}, {"doc_id": "two"}], str(target))
    assert jsonl_ops.read_jsonl(str(target)) == [{"doc_id": "one"}, {"doc_id": "two"}]


def test_the_rename_barrier_holds_past_the_limit(tmp_path):
    """The durable-rename barrier is taken on directories the store itself created.

    A bare ``is_dir`` answers False for a path over the limit -- it swallows the ``OSError``
    rather than reporting it -- so the type guard inside ``fsync_directory`` would reject the
    content-addressed store's own directories and fail every durable write on a deep root.
    """
    deep = _deep(tmp_path)
    extended_path(deep).mkdir(parents=True)
    fsync_directory(deep)


# --------------------------------------------------------------------- structural guard

# The only ``Path()`` constructor calls left in ``src`` outside the portability layer, each with
# the reason it must stay unconverted.  ``extended_path`` absolutises, so applying it to a logical
# relative name would silently anchor that name to the working directory and defeat the safety
# checks built on it.  Adding an entry here is a decision; the assertion below makes it a visible
# one.  The scan cannot see a path that never reaches ``Path()``, so it is a net, not a proof.
UNCONVERTED = {
    ("src/evaluation/challenge.py", 'relpath = Path(str(row["ledger_relpath"]))'):
        "a logical relative name; the is_absolute/.. check depends on it staying relative",
    ("src/evaluation/ordered_runner.py",
     'parts = Path(str(prediction["ledger_relpath"])).parts'):
        "reads the parts of the same logical relative name",
    ("src/learning/policy_store.py", "Path(self.path).parent.mkdir(parents=True, exist_ok=True)"):
        "self.path was already converted in __init__",
    ("src/operations/backup.py", "temporary = Path(temporary_name)"):
        "mkstemp(dir=...) returns a name already under a converted parent",
    ("src/operations/backup.py", 'relative = Path(str(record["path"]))'):
        "a logical relative name read back out of the backup manifest",
    ("src/operations/retention.py", "temporary = Path(temporary_name)"):
        "mkstemp(dir=...) returns a name already under a converted parent",
    ("src/orchestration/job_store.py", "Path(self.path).parent.mkdir(parents=True, exist_ok=True)"):
        "self.path was already converted in __init__",
    ("src/orchestration/job_store.py", "if not Path(self.path).exists():"):
        "self.path was already converted in __init__",
    ("src/orchestration/runtime.py",
     'ledger = EventLedger(Path(ledger_root) / tenant_id / job.job_id / "events.jsonl")'):
        "the composed path goes straight into EventLedger, which converts its own root",
    ("src/service/api.py", "Path(self.store_path).parent.mkdir(parents=True, exist_ok=True)"):
        "self.store_path was already converted in __init__",
}


def _path_constructor_sites() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for source in sorted((ROOT / "src").rglob("*.py")):
        if "__pycache__" in source.parts or source == PORTABILITY:
            continue
        text = source.read_text(encoding="utf-8")
        lines = text.splitlines()
        for node in ast.walk(ast.parse(text, filename=str(source))):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Path"
            ):
                found.add((source.relative_to(ROOT).as_posix(), lines[node.lineno - 1].strip()))
    return found


def test_every_path_entry_point_is_converted_or_deliberately_exempt():
    found = _path_constructor_sites()
    unexplained = sorted(found - set(UNCONVERTED))
    stale = sorted(set(UNCONVERTED) - found)
    assert not unexplained, (
        f"{unexplained} construct a Path from an unconverted value; either wrap the root in "
        "extended_path at its entry point or record why it must stay relative in UNCONVERTED"
    )
    assert not stale, f"UNCONVERTED lists sites that no longer exist: {stale}"
