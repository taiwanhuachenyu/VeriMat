"""Deterministic, offline SLO and alert evaluation over sealed operational evidence."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SLOError(ValueError):
    """Raised when operational evidence is malformed or cannot support an alert decision."""


def _load(path: Path, required: set[str], label: str) -> tuple[dict[str, Any], str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SLOError(f"{label} report is unreadable: {exc}") from exc
    if not isinstance(value, dict) or not required <= set(value):
        raise SLOError(f"{label} report is missing required fields")
    return value, hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SLOError(f"{context} must be a finite number")
    return float(value)


def _count(value: Any, context: str) -> int:
    rendered = _number(value, context)
    if rendered < 0 or not rendered.is_integer():
        raise SLOError(f"{context} must be a non-negative integer")
    return int(rendered)


def _timestamp(value: Any, context: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SLOError(f"{context} must be ISO-8601") from exc
    if parsed.utcoffset() is None:
        raise SLOError(f"{context} must include a timezone")
    return parsed.astimezone(timezone.utc)


def evaluate_operational_slos(
    *, load_report: str | Path, fault_report: str | Path,
    backup_report: str | Path, control_snapshot: dict[str, Any] | None = None,
    circuit_snapshots: list[dict[str, Any]] | None = None,
    now: datetime | None = None, max_backup_age_seconds: float = 86400,
    max_runnable_age_seconds: float = 300,
) -> dict[str, Any]:
    """Evaluate preregistered local operational gates; it never sends notifications."""
    if max_backup_age_seconds <= 0 or max_runnable_age_seconds <= 0:
        raise SLOError("SLO age thresholds must be positive")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    load_path, fault_path, backup_path = map(
        Path, (load_report, fault_report, backup_report),
    )
    load, load_hash = _load(
        load_path, {"requests", "successes", "success_rate", "latency_ms"}, "load",
    )
    fault, fault_hash = _load(
        fault_path, {"scenarios", "passed", "failed"}, "fault",
    )
    backup, backup_hash = _load(
        backup_path,
        {"created_at", "backup_verified", "restore_verified", "source_restore_snapshot_equal"},
        "backup",
    )
    requests = _count(load["requests"], "load.requests")
    successes = _count(load["successes"], "load.successes")
    success_rate = _number(load["success_rate"], "load.success_rate")
    latency = load["latency_ms"]
    if not isinstance(latency, dict) or not {"p95", "p99"} <= set(latency):
        raise SLOError("load.latency_ms lacks p95/p99")
    p95, p99 = _number(latency["p95"], "load.p95"), _number(latency["p99"], "load.p99")
    scenarios = _count(fault["scenarios"], "fault.scenarios")
    passed = _count(fault["passed"], "fault.passed")
    failed = _count(fault["failed"], "fault.failed")
    if requests < 1 or successes < 0 or successes > requests or not 0 <= success_rate <= 1:
        raise SLOError("load counts or success rate are inconsistent")
    if abs(success_rate - successes / requests) > 1e-12:
        raise SLOError("load success_rate disagrees with counts")
    if scenarios < 1 or min(passed, failed) < 0 or passed + failed != scenarios:
        raise SLOError("fault counts are inconsistent")
    backup_created = _timestamp(backup["created_at"], "backup.created_at")
    if (backup_created - current).total_seconds() > 60:
        raise SLOError("backup.created_at is implausibly in the future")
    backup_age = max(0.0, (current - backup_created).total_seconds())

    alerts: list[dict[str, Any]] = []

    def alert(
        identifier: str, severity: str, observed: Any, threshold: Any,
        evidence: str,
    ) -> None:
        alerts.append({
            "alert_id": identifier, "severity": severity, "observed": observed,
            "threshold": threshold, "evidence": evidence,
        })

    if success_rate < 0.999:
        alert("CONTROL_SUCCESS_RATE", "critical", success_rate, ">=0.999", "load_report")
    if p95 >= 300:
        alert("CONTROL_P95_LATENCY", "critical", p95, "<300 ms", "load_report")
    if p99 >= 1000:
        alert("CONTROL_P99_LATENCY", "warning", p99, "<1000 ms", "load_report")
    if failed:
        alert("FAULT_INJECTION_FAILURE", "critical", failed, "0", "fault_report")
    if not all(
        isinstance(backup.get(field), bool) and backup[field]
        for field in ("backup_verified", "restore_verified", "source_restore_snapshot_equal")
    ):
        alert(
            "BACKUP_RESTORE_VERIFICATION", "critical",
            {
                field: backup.get(field) for field in (
                    "backup_verified", "restore_verified", "source_restore_snapshot_equal",
                )
            }, {"all": True}, "backup_report",
        )
    if backup_age > max_backup_age_seconds:
        alert(
            "BACKUP_EVIDENCE_STALE", "warning", round(backup_age, 3),
            f"<={max_backup_age_seconds} seconds", "backup_report",
        )

    if control_snapshot is not None:
        required = {"expired_leases", "oldest_runnable_age_seconds"}
        if not isinstance(control_snapshot, dict) or not required <= set(control_snapshot):
            raise SLOError("control snapshot lacks lease/backlog fields")
        expired = _count(control_snapshot["expired_leases"], "expired_leases")
        runnable_age = _number(
            control_snapshot["oldest_runnable_age_seconds"], "oldest_runnable_age_seconds",
        )
        if expired:
            alert("EXPIRED_WORKER_LEASES", "warning", expired, "0", "control_snapshot")
        if runnable_age > max_runnable_age_seconds:
            alert(
                "RUNNABLE_BACKLOG_AGE", "critical", runnable_age,
                f"<={max_runnable_age_seconds} seconds", "control_snapshot",
            )

    circuit_states = {"CLOSED": 0, "OPEN": 0, "HALF_OPEN": 0}
    for index, snapshot in enumerate(circuit_snapshots or []):
        if not isinstance(snapshot, dict) or snapshot.get("state") not in circuit_states:
            raise SLOError(f"circuit snapshot {index} has invalid state")
        state = str(snapshot["state"])
        circuit_states[state] += 1
    if circuit_states["OPEN"]:
        alert("PROVIDER_CIRCUIT_OPEN", "critical", circuit_states["OPEN"], "0", "circuits")
    if circuit_states["HALF_OPEN"]:
        alert(
            "PROVIDER_CIRCUIT_HALF_OPEN", "warning", circuit_states["HALF_OPEN"],
            "0", "circuits",
        )

    severity_counts = {
        severity: sum(item["severity"] == severity for item in alerts)
        for severity in ("critical", "warning")
    }
    status = (
        "FAIL" if severity_counts["critical"] else
        "WARN" if severity_counts["warning"] else "PASS"
    )
    return {
        "schema_version": 1,
        "created_at": current.isoformat(),
        "scope": "deterministic local operational SLO evaluation; no notification delivery",
        "status": status,
        "scientific_result": False,
        "thresholds": {
            "success_rate_min": 0.999, "p95_latency_ms_max_exclusive": 300,
            "p99_latency_ms_max_exclusive": 1000,
            "fault_failures_max": 0,
            "max_backup_age_seconds": max_backup_age_seconds,
            "max_runnable_age_seconds": max_runnable_age_seconds,
        },
        "observations": {
            "load_requests": requests, "load_success_rate": success_rate,
            "load_p95_ms": p95, "load_p99_ms": p99,
            "fault_scenarios": scenarios, "fault_failures": failed,
            "backup_age_seconds": round(backup_age, 3),
            "circuit_states": circuit_states,
        },
        "input_sha256": {
            "load_report": load_hash, "fault_report": fault_hash,
            "backup_report": backup_hash,
        },
        "severity_counts": severity_counts,
        "alerts": alerts,
    }
