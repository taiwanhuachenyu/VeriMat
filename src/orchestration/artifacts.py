"""Tenant-scoped, content-addressed artifacts with idempotent logical bindings."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.events import canonical_json, validate_durable_payload
from src.core.portability import extended_path, fsync_directory
from src.operations.runtime_migrations import (
    ARTIFACT_SPEC, assert_runtime_compatibility, prepare_runtime_database, schema_script,
)

SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
SCHEMA = schema_script(ARTIFACT_SPEC)


class ArtifactError(RuntimeError):
    pass


class ArtifactConflict(ArtifactError):
    pass


class ArtifactIntegrityError(ArtifactError):
    pass


@dataclass(frozen=True)
class ArtifactRef:
    tenant_id: str
    job_id: str
    logical_key: str
    content_sha256: str
    size_bytes: int
    media_type: str

    def checkpoint_value(self) -> dict[str, Any]:
        return {
            "logical_key": self.logical_key,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }


class ArtifactStore:
    def __init__(self, root: str | Path):
        # Converted once here rather than at each syscall: a blob path adds a 64-character tenant
        # digest, a shard and a 64-character content digest to the root, and pathlib carries the
        # conversion through every ``/``, ``resolve`` and ``rglob`` derived from it.
        self.root = extended_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        database = self.root / "artifacts.db"
        prepare_runtime_database(database, ARTIFACT_SPEC)
        self.conn = sqlite3.connect(
            str(database), timeout=30, isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA journal_mode=WAL")
        assert_runtime_compatibility(self.conn, ARTIFACT_SPEC)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ArtifactStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def _validate_id(name: str, value: str) -> None:
        if not SAFE_ID.fullmatch(value):
            raise ArtifactError(f"{name} contains unsafe characters or length")

    def _blob_path(self, tenant_id: str, digest: str) -> Path:
        tenant_hash = hashlib.sha256(tenant_id.encode()).hexdigest()
        return self.root / "tenants" / tenant_hash / "blobs" / digest[:2] / digest

    @staticmethod
    def _ref(row: sqlite3.Row) -> ArtifactRef:
        return ArtifactRef(
            tenant_id=str(row["tenant_id"]), job_id=str(row["job_id"]),
            logical_key=str(row["logical_key"]),
            content_sha256=str(row["content_sha256"]),
            size_bytes=int(row["size_bytes"]), media_type=str(row["media_type"]),
        )

    def put_bytes(
        self, *, tenant_id: str, job_id: str, logical_key: str,
        content: bytes, media_type: str,
    ) -> ArtifactRef:
        for name, value in (
            ("tenant_id", tenant_id), ("job_id", job_id), ("logical_key", logical_key),
        ):
            self._validate_id(name, value)
        if not isinstance(content, bytes) or not content:
            raise ArtifactError("artifact content must be non-empty bytes")
        if not media_type.strip() or len(media_type) > 200:
            raise ArtifactError("media_type is required and bounded")
        digest, size = hashlib.sha256(content).hexdigest(), len(content)
        existing = self.conn.execute(
            """SELECT * FROM artifacts
               WHERE tenant_id=? AND job_id=? AND logical_key=?""",
            (tenant_id, job_id, logical_key),
        ).fetchone()
        if existing is not None:
            ref = self._ref(existing)
            if (ref.content_sha256, ref.size_bytes, ref.media_type) != (
                digest, size, media_type,
            ):
                raise ArtifactConflict("logical artifact key already has different content")
            self.read_bytes(ref)
            return ref

        destination = self._blob_path(tenant_id, digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.", dir=str(destination.parent)
            )
            try:
                with os.fdopen(temporary_fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary_name, destination)
                except FileExistsError:
                    pass
                fsync_directory(destination.parent)
            finally:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ArtifactIntegrityError("content-addressed blob failed verification")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                """SELECT * FROM artifacts
                   WHERE tenant_id=? AND job_id=? AND logical_key=?""",
                (tenant_id, job_id, logical_key),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)",
                    (tenant_id, job_id, logical_key, digest, size, media_type, time.time()),
                )
            else:
                ref = self._ref(existing)
                if (ref.content_sha256, ref.size_bytes, ref.media_type) != (
                    digest, size, media_type,
                ):
                    raise ArtifactConflict(
                        "logical artifact key raced with different content"
                    )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return self.get_ref(
            tenant_id=tenant_id, job_id=job_id, logical_key=logical_key,
        )

    def put_json(
        self, *, tenant_id: str, job_id: str, logical_key: str,
        value: dict[str, Any],
    ) -> ArtifactRef:
        validate_durable_payload(value, prefix="artifact")
        return self.put_bytes(
            tenant_id=tenant_id, job_id=job_id, logical_key=logical_key,
            content=(canonical_json(value) + "\n").encode(),
            media_type="application/json",
        )

    def get_ref(self, *, tenant_id: str, job_id: str, logical_key: str) -> ArtifactRef:
        row = self.conn.execute(
            """SELECT * FROM artifacts
               WHERE tenant_id=? AND job_id=? AND logical_key=?""",
            (tenant_id, job_id, logical_key),
        ).fetchone()
        if row is None:
            raise ArtifactError("artifact not found")
        return self._ref(row)

    def read_bytes(self, ref: ArtifactRef) -> bytes:
        current = self.get_ref(
            tenant_id=ref.tenant_id, job_id=ref.job_id, logical_key=ref.logical_key,
        )
        if current != ref:
            raise ArtifactIntegrityError("artifact reference does not match index")
        path = self._blob_path(ref.tenant_id, ref.content_sha256)
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError("artifact blob is missing") from exc
        if len(content) != ref.size_bytes:
            raise ArtifactIntegrityError("artifact size does not match index")
        if hashlib.sha256(content).hexdigest() != ref.content_sha256:
            raise ArtifactIntegrityError("artifact content hash does not match index")
        return content

    def read_json(self, ref: ArtifactRef) -> dict[str, Any]:
        if ref.media_type != "application/json":
            raise ArtifactError("artifact is not JSON")
        try:
            value = json.loads(self.read_bytes(ref))
        except json.JSONDecodeError as exc:
            raise ArtifactIntegrityError("JSON artifact cannot be decoded") from exc
        if not isinstance(value, dict):
            raise ArtifactIntegrityError("JSON artifact root is not an object")
        return value
