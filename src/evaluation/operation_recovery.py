"""Audited operator reconciliation for indeterminate external operations."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.core.events import canonical_json
from src.operations.runtime_migrations import (
    MODEL_OPERATION_SPEC, RECONCILIATION_STATEMENTS, RETRIEVAL_OPERATION_SPEC,
)
from src.operations.runtime_migrations import verify_runtime_database

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KINDS = {"model", "retrieval"}
_ACTIONS = {"complete", "authorize_retry", "abandon"}

RECONCILIATION_SCHEMA = ";\n".join(RECONCILIATION_STATEMENTS) + ";\n"


class OperationRecoveryError(ValueError):
    """Raised when an operator action is unsafe or lacks auditable provenance."""


def _spec(kind: str) -> tuple[str, tuple[str, ...]]:
    if kind == "model":
        return "model_operations", (
            "response_text", "input_tokens", "output_tokens", "request_id",
        )
    if kind == "retrieval":
        return "retrieval_operations", ("response_json",)
    raise OperationRecoveryError(f"unsupported operation kind {kind!r}")


def _connect(path: str | Path, kind: str) -> sqlite3.Connection:
    target = Path(path)
    if not target.is_file():
        raise OperationRecoveryError(f"operation database does not exist: {target}")
    spec = MODEL_OPERATION_SPEC if kind == "model" else RETRIEVAL_OPERATION_SPEC
    verify_runtime_database(target, spec)
    connection = sqlite3.connect(str(target), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _validate_attestation(
    *, actor: str, reason: str, evidence_receipt_sha256: str,
) -> None:
    if not actor.strip() or len(actor) > 200:
        raise OperationRecoveryError("actor is required and must be at most 200 characters")
    if not reason.strip() or len(reason) > 2000:
        raise OperationRecoveryError("reason is required and must be at most 2000 characters")
    if not _HEX64.fullmatch(evidence_receipt_sha256):
        raise OperationRecoveryError("evidence receipt must be a SHA-256 digest")


def list_operations(
    *, database: str | Path, kind: str, include_completed: bool = False,
) -> list[dict[str, Any]]:
    """Return operation metadata without exposing prompts or cached response bodies."""
    table, _ = _spec(kind)
    connection = _connect(database, kind)
    try:
        where = "" if include_completed else " WHERE status != 'COMPLETED'"
        rows = connection.execute(
            f"SELECT operation_id,request_sha256,status,created_at,completed_at "
            f"FROM {table}{where} ORDER BY created_at,operation_id"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise OperationRecoveryError(f"invalid {kind} operation database: {exc}") from exc
    finally:
        connection.close()


def reconciliation_history(
    *, database: str | Path, kind: str, operation_id: str,
) -> list[dict[str, Any]]:
    _spec(kind)
    connection = _connect(database, kind)
    try:
        rows = connection.execute(
            """SELECT operation_kind,operation_id,request_sha256,prior_status,action,
                      actor,reason,evidence_receipt_sha256,reconciled_at
               FROM operation_reconciliations
               WHERE operation_kind=? AND operation_id=?
               ORDER BY reconciliation_id""",
            (kind, operation_id),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _validated_response(kind: str, response: Any) -> dict[str, Any]:
    if kind == "model":
        required = {"response_text", "input_tokens", "output_tokens", "request_id"}
        if not isinstance(response, dict) or set(response) != required:
            raise OperationRecoveryError(
                "model completion requires response_text, input_tokens, output_tokens, request_id"
            )
        if not isinstance(response["response_text"], str) or not response["request_id"].strip():
            raise OperationRecoveryError("model response text and request id are required")
        for field in ("input_tokens", "output_tokens"):
            if (
                isinstance(response[field], bool) or not isinstance(response[field], int)
                or response[field] < 0
            ):
                raise OperationRecoveryError(f"{field} must be a non-negative integer")
        return response
    if not isinstance(response, (dict, list)):
        raise OperationRecoveryError("retrieval completion response must be an object or array")
    try:
        rendered = canonical_json(response)
    except (TypeError, ValueError) as exc:
        raise OperationRecoveryError(f"retrieval response must be finite JSON: {exc}") from exc
    return {"response_json": rendered}


def reconcile_operation(
    *, database: str | Path, kind: str, operation_id: str, request_sha256: str,
    action: str, actor: str, reason: str, evidence_receipt_sha256: str,
    response: Any | None = None,
) -> dict[str, Any]:
    """Resolve one PENDING operation under a recorded, fail-closed operator attestation."""
    table, response_columns = _spec(kind)
    if action not in _ACTIONS:
        raise OperationRecoveryError(f"unsupported reconciliation action {action!r}")
    if not operation_id.strip() or len(operation_id) > 500:
        raise OperationRecoveryError("operation id is invalid")
    if not _HEX64.fullmatch(request_sha256):
        raise OperationRecoveryError("request_sha256 must be a SHA-256 digest")
    _validate_attestation(
        actor=actor, reason=reason,
        evidence_receipt_sha256=evidence_receipt_sha256,
    )
    completion = _validated_response(kind, response) if action == "complete" else None
    if action != "complete" and response is not None:
        raise OperationRecoveryError("response is accepted only for complete action")

    connection = _connect(database, kind)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            f"SELECT operation_id,request_sha256,status FROM {table} WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise OperationRecoveryError("operation does not exist")
        if row["request_sha256"] != request_sha256:
            raise OperationRecoveryError("request hash does not match reserved operation")
        if row["status"] != "PENDING":
            raise OperationRecoveryError(
                f"only PENDING operations can be reconciled; current status={row['status']}"
            )
        if action == "complete":
            if kind == "model":
                cursor = connection.execute(
                    f"""UPDATE {table} SET status='COMPLETED',response_text=?,input_tokens=?,
                              output_tokens=?,request_id=?,completed_at=?
                         WHERE operation_id=? AND request_sha256=? AND status='PENDING'""",
                    (
                        completion["response_text"], completion["input_tokens"],
                        completion["output_tokens"], completion["request_id"], time.time(),
                        operation_id, request_sha256,
                    ),
                )
            else:
                cursor = connection.execute(
                    f"""UPDATE {table} SET status='COMPLETED',response_json=?,completed_at=?
                         WHERE operation_id=? AND request_sha256=? AND status='PENDING'""",
                    (
                        completion[response_columns[0]], time.time(),
                        operation_id, request_sha256,
                    ),
                )
            next_status = "COMPLETED"
        else:
            next_status = "RETRY_AUTHORIZED" if action == "authorize_retry" else "ABANDONED"
            cursor = connection.execute(
                f"""UPDATE {table} SET status=?
                     WHERE operation_id=? AND request_sha256=? AND status='PENDING'""",
                (next_status, operation_id, request_sha256),
            )
        if cursor.rowcount != 1:
            raise OperationRecoveryError("operation changed during reconciliation")
        connection.execute(
            """INSERT INTO operation_reconciliations(
                   operation_kind,operation_id,request_sha256,prior_status,action,actor,reason,
                   evidence_receipt_sha256,reconciled_at
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                kind, operation_id, request_sha256, "PENDING", action, actor, reason,
                evidence_receipt_sha256, time.time(),
            ),
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return {
        "kind": kind, "operation_id": operation_id, "request_sha256": request_sha256,
        "prior_status": "PENDING", "status": next_status, "action": action,
        "actor": actor, "evidence_receipt_sha256": evidence_receipt_sha256,
    }
