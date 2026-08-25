"""Fail-closed, tenant-scoped retention planning and destructive execution.

The public API deliberately separates immutable planning from execution.  Callers must persist the
plan, review its digest, and repeat that digest as an acknowledgement.  No task text, idempotency
key, checkpoint payload, provider detail, or artifact content enters the plan or audit receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.events import canonical_json
from src.core.portability import extended_path, lock_exclusive, sqlite_readonly_uri
from src.evidence.ledger import EventLedger
from src.orchestration.job_store import JobStatus, TERMINAL


SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class RetentionError(RuntimeError):
    """A retention request is ambiguous, stale, unsafe, or incompletely durable."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _plan_digest(core: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(core).encode()).hexdigest()


def _safe_id(label: str, value: str) -> None:
    if not SAFE_ID.fullmatch(value):
        raise RetentionError(f"{label} contains unsafe characters or length")


def _under(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts)
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise RetentionError("resolved retention path escapes its declared root")
    return candidate


def _artifact_blob(root: Path, tenant_id: str, digest: str) -> Path:
    tenant_hash = hashlib.sha256(tenant_id.encode()).hexdigest()
    return _under(root, "tenants", tenant_hash, "blobs", digest[:2], digest)


def _readonly(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise RetentionError(f"database is absent or unsafe: {path}")
    connection = sqlite3.connect(sqlite_readonly_uri(path.resolve()), uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def build_retention_plan(
    *, job_database: str | Path, artifact_root: str | Path, ledger_root: str | Path,
    tenant_id: str, cutoff_epoch: float, now_epoch: float | None = None,
) -> dict[str, Any]:
    """Snapshot eligible terminal jobs and their exact deletion preconditions."""
    _safe_id("tenant_id", tenant_id)
    now = time.time() if now_epoch is None else float(now_epoch)
    cutoff = float(cutoff_epoch)
    if not (0 <= cutoff <= now):
        raise RetentionError("cutoff must be a finite past epoch")
    job_path = extended_path(job_database)
    artifact_root_path = extended_path(artifact_root)
    ledger_root_path = extended_path(ledger_root)
    artifact_db = artifact_root_path / "artifacts.db"
    jobs = _readonly(job_path)
    artifacts = _readonly(artifact_db)
    try:
        rows = jobs.execute(
            """SELECT job_id,status,updated_at FROM jobs
               WHERE tenant_id=? AND updated_at<=? ORDER BY job_id""",
            (tenant_id, cutoff),
        ).fetchall()
        targets: list[dict[str, Any]] = []
        target_ids: set[str] = set()
        for row in rows:
            status = JobStatus(str(row["status"]))
            if status not in TERMINAL:
                continue
            job_id = str(row["job_id"])
            _safe_id("job_id", job_id)
            ledger_path = _under(ledger_root_path, tenant_id, job_id, "events.jsonl")
            if ledger_path.is_symlink() or not ledger_path.is_file():
                raise RetentionError(f"eligible job has no safe event ledger: {job_id}")
            verification = EventLedger(ledger_path).verify()
            if not verification.ok:
                raise RetentionError(f"eligible job has an invalid event ledger: {job_id}")
            bindings = artifacts.execute(
                """SELECT logical_key,content_sha256,size_bytes,media_type FROM artifacts
                   WHERE tenant_id=? AND job_id=? ORDER BY logical_key""",
                (tenant_id, job_id),
            ).fetchall()
            targets.append({
                "job_id": job_id,
                "status": status.value,
                "updated_at": float(row["updated_at"]),
                "ledger": {
                    "size_bytes": ledger_path.stat().st_size,
                    "sha256": _sha256(ledger_path),
                    "event_count": verification.event_count,
                    "head_sha256": verification.head_hash,
                },
                "artifact_bindings": [
                    {
                        "logical_key": str(item["logical_key"]),
                        "content_sha256": str(item["content_sha256"]),
                        "size_bytes": int(item["size_bytes"]),
                        "media_type": str(item["media_type"]),
                    }
                    for item in bindings
                ],
            })
            target_ids.add(job_id)

        candidate_digests = sorted({
            binding["content_sha256"]
            for target in targets for binding in target["artifact_bindings"]
        })
        deletable_blobs: list[dict[str, Any]] = []
        for digest in candidate_digests:
            remaining = artifacts.execute(
                """SELECT job_id FROM artifacts
                   WHERE tenant_id=? AND content_sha256=?""",
                (tenant_id, digest),
            ).fetchall()
            if any(str(row["job_id"]) not in target_ids for row in remaining):
                continue
            blob = _artifact_blob(artifact_root_path, tenant_id, digest)
            if blob.is_symlink() or not blob.is_file():
                raise RetentionError(f"indexed deletion blob is absent or unsafe: {digest}")
            expected_sizes = {
                binding["size_bytes"] for target in targets
                for binding in target["artifact_bindings"]
                if binding["content_sha256"] == digest
            }
            if expected_sizes != {blob.stat().st_size} or _sha256(blob) != digest:
                raise RetentionError(f"indexed deletion blob failed verification: {digest}")
            deletable_blobs.append({
                "content_sha256": digest, "size_bytes": blob.stat().st_size,
            })
    finally:
        artifacts.close()
        jobs.close()

    core = {
        "schema_version": 1,
        "plan_id": str(uuid.uuid4()),
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "cutoff_epoch": cutoff,
        "eligibility": "terminal jobs with updated_at at or before cutoff",
        "targets": targets,
        "deletable_blobs": deletable_blobs,
    }
    return {**core, "plan_sha256": _plan_digest(core)}


def write_plan(plan: dict[str, Any], destination: str | Path) -> None:
    target = extended_path(destination)
    if target.exists() or target.is_symlink():
        raise RetentionError("retention plan output must not already exist")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_plan(plan: dict[str, Any], acknowledged_sha256: str) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise RetentionError("unsupported retention plan")
    supplied = str(plan.get("plan_sha256", ""))
    core = {key: value for key, value in plan.items() if key != "plan_sha256"}
    calculated = _plan_digest(core)
    if supplied != calculated or acknowledged_sha256 != calculated:
        raise RetentionError("plan digest or explicit acknowledgement does not match")
    _safe_id("tenant_id", str(plan.get("tenant_id", "")))
    _safe_id("plan_id", str(plan.get("plan_id", "")))
    return core


def _append_audit(path: Path, receipt: dict[str, Any]) -> None:
    if path.is_symlink():
        raise RetentionError("retention audit path must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as handle:
            lock_exclusive(handle)
            handle.write(canonical_json(receipt) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        raise


def execute_retention_plan(
    *, plan: dict[str, Any], acknowledged_sha256: str,
    job_database: str | Path, artifact_root: str | Path, ledger_root: str | Path,
    maintenance_root: str | Path, audit_log: str | Path,
) -> dict[str, Any]:
    """Execute an exact reviewed plan under a single-node maintenance lock.

    Files are first moved into an operation quarantine. Database deletion then uses one SQLite
    transaction spanning the control and artifact indexes. Any pre-commit failure restores moved
    files. Successful deletion enables SQLite secure-delete and truncates both WALs before a
    content-free audit receipt is fsynced.
    """
    core = _validate_plan(plan, acknowledged_sha256)
    tenant_id, plan_id = str(core["tenant_id"]), str(core["plan_id"])
    # Prefixed before resolving, not after: resolving a plain path that is already too long fails
    # outright on Windows, whereas the prefix survives ``resolve`` untouched.
    job_path = extended_path(job_database).resolve()
    artifact_root_path = extended_path(artifact_root).resolve()
    ledger_root_path = extended_path(ledger_root).resolve()
    artifact_db = artifact_root_path / "artifacts.db"
    audit_path = extended_path(audit_log)
    maintenance = extended_path(maintenance_root).resolve()
    maintenance.mkdir(parents=True, exist_ok=True)
    lock_path = maintenance / "retention.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    moved: list[tuple[Path, Path]] = []
    quarantine = maintenance / "quarantine" / plan_id
    connection: sqlite3.Connection | None = None
    committed = False
    audit_prepared = False
    try:
        lock_exclusive(lock_descriptor, blocking=False)
        audit_identity = {
            "schema_version": 1,
            "operation_id": plan_id,
            "tenant_sha256": hashlib.sha256(tenant_id.encode()).hexdigest(),
            "plan_sha256": acknowledged_sha256,
        }
        _append_audit(audit_path, {
            **audit_identity,
            "event_type": "RETENTION_PREPARED",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "planned_jobs": len(core["targets"]),
        })
        audit_prepared = True
        if quarantine.exists() or quarantine.is_symlink():
            raise RetentionError("retention operation quarantine already exists")
        quarantine.mkdir(parents=True)
        connection = sqlite3.connect(str(job_path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("ATTACH DATABASE ? AS artifact_index", (str(artifact_db),))
        connection.execute("PRAGMA artifact_index.secure_delete=ON")
        connection.execute("BEGIN IMMEDIATE")

        target_ids: list[str] = []
        for target in core["targets"]:
            job_id = str(target["job_id"])
            _safe_id("job_id", job_id)
            row = connection.execute(
                "SELECT status,updated_at FROM jobs WHERE tenant_id=? AND job_id=?",
                (tenant_id, job_id),
            ).fetchone()
            if row is None or str(row["status"]) != target["status"] or float(row["updated_at"]) != float(target["updated_at"]):
                raise RetentionError(f"job state changed after planning: {job_id}")
            if JobStatus(str(row["status"])) not in TERMINAL:
                raise RetentionError(f"non-terminal job cannot be deleted: {job_id}")
            if float(row["updated_at"]) > float(core["cutoff_epoch"]):
                raise RetentionError(f"job falls outside the retention cutoff: {job_id}")
            actual_bindings = [dict(item) for item in connection.execute(
                """SELECT logical_key,content_sha256,size_bytes,media_type
                   FROM artifact_index.artifacts WHERE tenant_id=? AND job_id=?
                   ORDER BY logical_key""",
                (tenant_id, job_id),
            ).fetchall()]
            if actual_bindings != target["artifact_bindings"]:
                raise RetentionError(f"artifact bindings changed after planning: {job_id}")
            ledger = _under(ledger_root_path, tenant_id, job_id, "events.jsonl")
            expected_ledger = target["ledger"]
            if ledger.is_symlink() or not ledger.is_file():
                raise RetentionError(f"event ledger changed after planning: {job_id}")
            verification = EventLedger(ledger).verify()
            if (
                not verification.ok or ledger.stat().st_size != expected_ledger["size_bytes"]
                or _sha256(ledger) != expected_ledger["sha256"]
                or verification.event_count != expected_ledger["event_count"]
                or verification.head_hash != expected_ledger["head_sha256"]
            ):
                raise RetentionError(f"event ledger changed after planning: {job_id}")
            destination = quarantine / "ledgers" / job_id / "events.jsonl"
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(ledger, destination)
            moved.append((destination, ledger))
            target_ids.append(job_id)

        for blob_record in core["deletable_blobs"]:
            digest = str(blob_record["content_sha256"])
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise RetentionError("plan contains an invalid content digest")
            remaining = connection.execute(
                """SELECT job_id FROM artifact_index.artifacts
                   WHERE tenant_id=? AND content_sha256=?""",
                (tenant_id, digest),
            ).fetchall()
            if any(str(row["job_id"]) not in target_ids for row in remaining):
                raise RetentionError("planned blob gained a surviving reference")
            blob = _artifact_blob(artifact_root_path, tenant_id, digest)
            if (
                blob.is_symlink() or not blob.is_file()
                or blob.stat().st_size != int(blob_record["size_bytes"])
                or _sha256(blob) != digest
            ):
                raise RetentionError("planned blob changed after planning")
            destination = quarantine / "blobs" / digest
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(blob, destination)
            moved.append((destination, blob))

        for job_id in target_ids:
            connection.execute(
                "DELETE FROM artifact_index.artifacts WHERE tenant_id=? AND job_id=?",
                (tenant_id, job_id),
            )
            cursor = connection.execute(
                "DELETE FROM jobs WHERE tenant_id=? AND job_id=?", (tenant_id, job_id),
            )
            if cursor.rowcount != 1:
                raise RetentionError("control deletion lost its planned target")
        connection.execute("COMMIT")
        committed = True
        checkpoint_main = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        checkpoint_artifact = connection.execute(
            "PRAGMA artifact_index.wal_checkpoint(TRUNCATE)"
        ).fetchone()
        checkpoint_complete = (
            checkpoint_main is not None and int(checkpoint_main[0]) == 0
            and checkpoint_artifact is not None and int(checkpoint_artifact[0]) == 0
        )
        shutil.rmtree(quarantine)
        receipt = {
            **audit_identity,
            "event_type": "RETENTION_COMPLETED",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "deleted_jobs": len(target_ids),
            "deleted_artifact_bindings": sum(
                len(target["artifact_bindings"]) for target in core["targets"]
            ),
            "deleted_exclusive_blobs": len(core["deletable_blobs"]),
            "deleted_ledgers": len(target_ids),
            "secure_delete_enabled": True,
            "wal_checkpoint_complete": checkpoint_complete,
            "external_api_calls": 0,
        }
        _append_audit(audit_path, receipt)
        return receipt
    except BlockingIOError as exc:
        raise RetentionError("another retention operation holds the maintenance lock") from exc
    except Exception:
        if connection is not None and connection.in_transaction:
            connection.execute("ROLLBACK")
        if not committed:
            for source, destination in reversed(moved):
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.exists() and not destination.exists():
                    os.replace(source, destination)
            shutil.rmtree(quarantine, ignore_errors=True)
            if audit_prepared:
                try:
                    _append_audit(audit_path, {
                        "schema_version": 1,
                        "operation_id": plan_id,
                        "event_type": "RETENTION_ABORTED",
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                        "tenant_sha256": hashlib.sha256(tenant_id.encode()).hexdigest(),
                        "plan_sha256": acknowledged_sha256,
                    })
                except Exception:
                    pass
        raise
    finally:
        if connection is not None:
            connection.close()
        os.close(lock_descriptor)
