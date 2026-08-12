import json
from datetime import datetime, timezone

import pytest

from src.operations.slo import SLOError, evaluate_operational_slos


NOW = datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc)


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _reports(tmp_path):
    load, fault, backup = tmp_path / "load.json", tmp_path / "fault.json", tmp_path / "backup.json"
    _write(load, {
        "requests": 200, "successes": 200, "success_rate": 1.0,
        "latency_ms": {"p95": 50, "p99": 100},
    })
    _write(fault, {"scenarios": 11, "passed": 11, "failed": 0})
    _write(backup, {
        "created_at": "2026-08-12T06:00:00+00:00",
        "backup_verified": True, "restore_verified": True,
        "source_restore_snapshot_equal": True,
    })
    return load, fault, backup


def test_healthy_evidence_passes_with_hash_bound_inputs(tmp_path):
    load, fault, backup = _reports(tmp_path)
    report = evaluate_operational_slos(
        load_report=load, fault_report=fault, backup_report=backup,
        control_snapshot={"expired_leases": 0, "oldest_runnable_age_seconds": 0},
        circuit_snapshots=[{"state": "CLOSED"}], now=NOW,
    )
    assert report["status"] == "PASS" and report["alerts"] == []
    assert all(len(value) == 64 for value in report["input_sha256"].values())


def test_failures_backlog_and_open_circuit_produce_deterministic_critical_alerts(tmp_path):
    load, fault, backup = _reports(tmp_path)
    _write(load, {
        "requests": 100, "successes": 98, "success_rate": 0.98,
        "latency_ms": {"p95": 350, "p99": 1200},
    })
    _write(fault, {"scenarios": 11, "passed": 10, "failed": 1})
    report = evaluate_operational_slos(
        load_report=load, fault_report=fault, backup_report=backup,
        control_snapshot={"expired_leases": 2, "oldest_runnable_age_seconds": 301},
        circuit_snapshots=[{"state": "OPEN"}, {"state": "HALF_OPEN"}], now=NOW,
    )
    assert report["status"] == "FAIL"
    identifiers = {item["alert_id"] for item in report["alerts"]}
    assert {
        "CONTROL_SUCCESS_RATE", "CONTROL_P95_LATENCY", "CONTROL_P99_LATENCY",
        "FAULT_INJECTION_FAILURE", "EXPIRED_WORKER_LEASES",
        "RUNNABLE_BACKLOG_AGE", "PROVIDER_CIRCUIT_OPEN",
        "PROVIDER_CIRCUIT_HALF_OPEN",
    } <= identifiers


def test_inconsistent_or_nonfinite_evidence_is_rejected(tmp_path):
    load, fault, backup = _reports(tmp_path)
    _write(load, {
        "requests": 10, "successes": 9, "success_rate": 1.0,
        "latency_ms": {"p95": 1, "p99": 2},
    })
    with pytest.raises(SLOError, match="disagrees"):
        evaluate_operational_slos(
            load_report=load, fault_report=fault, backup_report=backup, now=NOW,
        )
    _write(load, {
        "requests": 10.5, "successes": 10, "success_rate": 1.0,
        "latency_ms": {"p95": 1, "p99": 2},
    })
    with pytest.raises(SLOError, match="integer"):
        evaluate_operational_slos(
            load_report=load, fault_report=fault, backup_report=backup, now=NOW,
        )
