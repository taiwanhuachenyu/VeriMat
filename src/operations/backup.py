"""Verified online backup and restore for the single-node trusted runtime."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.events import canonical_json
from src.core.portability import (
    extended_path, fsync_directory, fsync_file, lock_shared, sqlite_readonly_uri,
)
from src.evidence.ledger import EventLedger


class BackupError(RuntimeError):
    """Raised when a backup cannot prove a complete, untampered restore point."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sqlite_backup(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise BackupError(f"SQLite source is absent or unsafe: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(sqlite_readonly_uri(source), uri=True, timeout=30)
    destination_connection = sqlite3.connect(str(destination), timeout=30)
    try:
        source_connection.backup(destination_connection)
        # A backup is a cold, self-contained restore point.  Do not inherit WAL mode because a
        # later read could create unmanifested -wal/-shm sidecars inside the sealed package.
        destination_connection.execute("PRAGMA journal_mode=DELETE")
        result = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise BackupError(f"SQLite snapshot failed integrity check: {source}")
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    fsync_file(destination)
    fsync_directory(destination.parent)


def _copy_locked_ledger(source: Path, destination: Path) -> dict[str, Any]:
    if source.is_symlink() or not source.is_file():
        raise BackupError(f"ledger source is unsafe: {source}")
    with source.open("rb") as handle:
        lock_shared(handle)
        content = handle.read()
    _write_bytes(destination, content)
    receipt = EventLedger(destination).verify()
    if not receipt.ok:
        raise BackupError(f"ledger verification failed for {source}: {receipt.error}")
    return {"events": receipt.event_count, "head_sha256": receipt.head_hash}


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise BackupError(f"source root is absent or unsafe: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackupError(f"symbolic links are forbidden in backup sources: {path}")


def _file_record(root: Path, path: Path, kind: str, **extra: Any) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(), "kind": kind,
        "size_bytes": path.stat().st_size, "sha256": _sha256(path), **extra,
    }


def _artifact_blob_path(root: Path, tenant_id: str, digest: str) -> Path:
    tenant_hash = hashlib.sha256(tenant_id.encode()).hexdigest()
    return root / "tenants" / tenant_hash / "blobs" / digest[:2] / digest


def create_backup(
    *, job_database: str | Path, ledger_root: str | Path,
    artifact_root: str | Path, output: str | Path,
) -> dict[str, Any]:
    """Create an atomic backup directory from online SQLite and immutable file sources."""
    job_source = extended_path(job_database).resolve()
    ledger_source = extended_path(ledger_root).resolve()
    artifact_source = extended_path(artifact_root).resolve()
    target = extended_path(output).resolve()
    if target.exists():
        raise BackupError(f"backup output already exists: {target}")
    _reject_symlinks(ledger_source)
    _reject_symlinks(artifact_source)
    stage = target.parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    stage.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    ledger_events = 0
    try:
        job_snapshot = stage / "control" / "jobs.db"
        _sqlite_backup(job_source, job_snapshot)
        records.append(_file_record(stage, job_snapshot, "sqlite_control"))

        artifact_database_source = artifact_source / "artifacts.db"
        artifact_snapshot = stage / "artifacts" / "artifacts.db"
        _sqlite_backup(artifact_database_source, artifact_snapshot)
        records.append(_file_record(stage, artifact_snapshot, "sqlite_artifacts"))

        artifact_connection = sqlite3.connect(sqlite_readonly_uri(artifact_snapshot), uri=True)
        artifact_connection.row_factory = sqlite3.Row
        try:
            rows = artifact_connection.execute(
                """SELECT tenant_id,content_sha256,size_bytes FROM artifacts
                   ORDER BY tenant_id,content_sha256"""
            ).fetchall()
        except sqlite3.Error as exc:
            raise BackupError(f"artifact snapshot schema is invalid: {exc}") from exc
        finally:
            artifact_connection.close()
        copied_blobs: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row["tenant_id"]), str(row["content_sha256"]))
            if key in copied_blobs:
                continue
            copied_blobs.add(key)
            source_blob = _artifact_blob_path(artifact_source, *key)
            if source_blob.is_symlink() or not source_blob.is_file():
                raise BackupError(f"indexed artifact blob is absent or unsafe: {source_blob}")
            if source_blob.stat().st_size != int(row["size_bytes"]) or _sha256(source_blob) != key[1]:
                raise BackupError(f"indexed artifact blob failed verification: {source_blob}")
            destination_blob = _artifact_blob_path(stage / "artifacts", *key)
            _write_bytes(destination_blob, source_blob.read_bytes())
            records.append(_file_record(
                stage, destination_blob, "artifact_blob", content_sha256=key[1],
            ))

        for source_ledger in sorted(
            path for path in ledger_source.rglob("*.jsonl") if path.is_file()
        ):
            relative = source_ledger.relative_to(ledger_source)
            destination_ledger = stage / "ledgers" / relative
            ledger_receipt = _copy_locked_ledger(source_ledger, destination_ledger)
            ledger_events += int(ledger_receipt["events"])
            records.append(_file_record(
                stage, destination_ledger, "event_ledger", **ledger_receipt,
            ))
        if any(path.is_file() and path.suffix != ".jsonl" for path in ledger_source.rglob("*")):
            raise BackupError("ledger root may contain only .jsonl ledger files")

        records.sort(key=lambda item: item["path"])
        summary = {
            "control_databases": 1, "artifact_databases": 1,
            "artifact_blobs": len(copied_blobs),
            "event_ledgers": sum(record["kind"] == "event_ledger" for record in records),
            "ledger_events": ledger_events,
        }
        manifest_core = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "format": "goai-single-node-backup-v1",
            "summary": summary,
            "files": records,
            "security": {
                "contains_sensitive_runtime_data": True,
                "encryption": "external storage layer required",
                "credentials_expected": False,
            },
        }
        manifest = {
            **manifest_core,
            "backup_id": hashlib.sha256(canonical_json(manifest_core).encode()).hexdigest(),
        }
        _write_bytes(
            stage / "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
        )
        verify_backup(stage)
        fsync_directory(stage)
        os.replace(stage, target)
        fsync_directory(target.parent)
        return manifest
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"backup manifest is unreadable: {exc}") from exc
    required = {"schema_version", "created_at", "format", "summary", "files", "security", "backup_id"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise BackupError("backup manifest fields are invalid")
    if manifest["schema_version"] != 1 or manifest["format"] != "goai-single-node-backup-v1":
        raise BackupError("backup format is unsupported")
    core = {key: value for key, value in manifest.items() if key != "backup_id"}
    if hashlib.sha256(canonical_json(core).encode()).hexdigest() != manifest["backup_id"]:
        raise BackupError("backup manifest identity does not match its content")
    return manifest


def _verify_sqlite(path: Path, required_tables: set[str]) -> None:
    connection = sqlite3.connect(sqlite_readonly_uri(path), uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        tables = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        connection.close()
    if result is None or str(result[0]).lower() != "ok" or not required_tables <= tables:
        raise BackupError(f"restored SQLite snapshot is invalid: {path}")


def verify_backup(backup: str | Path) -> dict[str, Any]:
    root = extended_path(backup).resolve()
    _reject_symlinks(root)
    manifest = _manifest(root)
    records = manifest["files"]
    if not isinstance(records, list) or not records:
        raise BackupError("backup file list is empty or invalid")
    expected_paths: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not {
            "path", "kind", "size_bytes", "sha256",
        } <= set(record):
            raise BackupError(f"backup file record {index} is invalid")
        relative = Path(str(record["path"]))
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in expected_paths:
            raise BackupError(f"backup file path is unsafe or duplicate: {relative}")
        expected_paths.add(relative.as_posix())
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise BackupError(f"backup file is missing or unsafe: {relative}")
        if path.stat().st_size != record["size_bytes"] or _sha256(path) != record["sha256"]:
            raise BackupError(f"backup file hash or size mismatch: {relative}")
        if record["kind"] == "event_ledger":
            receipt = EventLedger(path).verify()
            if (
                not receipt.ok or receipt.event_count != record.get("events")
                or receipt.head_hash != record.get("head_sha256")
            ):
                raise BackupError(f"backup ledger receipt mismatch: {relative}")
    actual_paths = {
        path.relative_to(root).as_posix() for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if actual_paths != expected_paths:
        raise BackupError("backup contains unmanifested or missing files")

    control = root / "control" / "jobs.db"
    artifacts = root / "artifacts" / "artifacts.db"
    _verify_sqlite(control, {"jobs", "checkpoints", "usage_ledger"})
    _verify_sqlite(artifacts, {"artifacts"})
    connection = sqlite3.connect(sqlite_readonly_uri(artifacts), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT tenant_id,content_sha256,size_bytes FROM artifacts"
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        blob = _artifact_blob_path(
            root / "artifacts", str(row["tenant_id"]), str(row["content_sha256"]),
        )
        if (
            not blob.is_file() or blob.stat().st_size != int(row["size_bytes"])
            or _sha256(blob) != row["content_sha256"]
        ):
            raise BackupError("backup artifact index does not match its blobs")
    return {
        "schema_version": 1, "backup_id": manifest["backup_id"],
        "verified": True, "summary": manifest["summary"],
    }


def restore_backup(*, backup: str | Path, target_root: str | Path) -> dict[str, Any]:
    source = extended_path(backup).resolve()
    target = extended_path(target_root).resolve()
    if target.exists():
        raise BackupError(f"restore target already exists: {target}")
    verification = verify_backup(source)
    stage = target.parent / f".{target.name}.restoring-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, stage, symlinks=False)
        verify_backup(stage)
        receipt = {
            **verification,
            "restored_at": datetime.now(timezone.utc).isoformat(),
            "source_manifest_sha256": _sha256(source / "manifest.json"),
        }
        _write_bytes(
            stage / "restore_receipt.json",
            (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
        )
        fsync_directory(stage)
        os.replace(stage, target)
        fsync_directory(target.parent)
        return receipt
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
