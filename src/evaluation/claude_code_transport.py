"""Structured model transport backed by the Claude Code CLI in headless mode.

The prompt travels on stdin rather than argv: a decision context is allowed 60,000 characters,
while the Windows ``CreateProcess`` command line stops at 32,767, so passing it as an argument
would fail on one platform and succeed on the other.

Durability matches ``OpenCodeStructuredTransport`` and reuses the same ``model_operations``
table, so ``src.evaluation.operation_recovery`` reconciles a row without needing to know which
backend produced it.  ``IndeterminateModelOperation`` is imported rather than redefined for the
same reason: a caller catches one exception type regardless of the configured backend.

Two flags carry the reproducibility guarantee.  ``--system-prompt`` replaces the CLI's default
prompt instead of appending to it, and ``--exclude-dynamic-system-prompt-sections`` strips the
working directory, current date and git state.  Without both, the effective prompt would vary by
machine and by day, and a cached response could not be attributed to the request that produced it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.events import canonical_json
from src.core.portability import exclusive_lock, extended_path
from src.operations.runtime_migrations import (
    MODEL_OPERATION_SPEC, assert_runtime_compatibility, prepare_runtime_database,
    schema_script,
)

from .baseline_runner import BaselineContractError
from .circuit_breaker import CircuitOpenError, PersistentCircuitBreaker
from .model_backend import ModelResponse, ProviderProvenance, StructuredModelTransport
from .opencode_transport import IndeterminateModelOperation

SCHEMA = schema_script(MODEL_OPERATION_SPEC)

CLAUDE_CODE_ROUTE_ID = "claude-code-cli"


class ClaudeCodeStructuredTransport(StructuredModelTransport):
    """Run one tool-free, schema-constrained Claude Code turn per benchmark operation."""

    DISALLOWED_TOOLS = (
        "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "NotebookEdit",
        "Glob", "Grep", "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite",
        "Skill", "SlashCommand", "Artifact",
    )

    def __init__(
        self, *, operation_db: str | Path, cli_path: str | None = None,
        model: str | None = None, usage_log: str | Path | None = None,
        timeout_seconds: float = 600, max_response_bytes: int = 4_000_000,
        request_response_log: str | Path | None = None,
        circuit_failure_threshold: int = 3, circuit_recovery_seconds: float = 60.0,
    ):
        resolved = cli_path or shutil.which("claude")
        if not resolved:
            raise ValueError(
                "the claude CLI was not found; install Claude Code or pass cli_path"
            )
        if timeout_seconds <= 0 or max_response_bytes < 1024:
            raise ValueError("transport bounds are invalid")
        if model is not None and not model.strip():
            raise ValueError("model alias must be non-empty when supplied")
        self.cli_path = str(resolved)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.usage_log = extended_path(usage_log) if usage_log else None
        self.request_response_log = (
            extended_path(request_response_log) if request_response_log else None
        )
        self.observed_backends: set[str] = set()
        db_path = extended_path(operation_db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        prepare_runtime_database(db_path, MODEL_OPERATION_SPEC)
        self.conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA journal_mode=WAL")
        assert_runtime_compatibility(self.conn, MODEL_OPERATION_SPEC)
        self.circuit = PersistentCircuitBreaker(
            database=db_path,
            database_spec=MODEL_OPERATION_SPEC,
            circuit_id=f"model:{CLAUDE_CODE_ROUTE_ID}:{model or 'session-default'}",
            failure_threshold=circuit_failure_threshold,
            recovery_timeout_seconds=circuit_recovery_seconds,
            probe_timeout_seconds=timeout_seconds + 30,
        )

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self.circuit.close()
        self.conn.close()

    def __enter__(self) -> "ClaudeCodeStructuredTransport":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def provenance(self, *, operator_declared_backend: str) -> ProviderProvenance:
        """Provenance for this route; the declared backend stays an operator statement."""
        return ProviderProvenance(
            route_id=CLAUDE_CODE_ROUTE_ID,
            request_alias=self.model or "session-default",
            operator_declared_backend=operator_declared_backend,
            backend_independently_attested=False,
        )

    # ------------------------------------------------------------------ operation ledger

    @staticmethod
    def _request_hash(*, system: str, user: str, response_schema: dict) -> str:
        return hashlib.sha256(canonical_json({
            "system": system, "user": user, "response_schema": response_schema,
        }).encode()).hexdigest()

    @staticmethod
    def _response(row: sqlite3.Row) -> ModelResponse:
        return ModelResponse(
            text=str(row["response_text"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            request_id=str(row["request_id"]),
        )

    def _lookup(self, operation_id: str, request_hash: str) -> ModelResponse | None:
        row = self.conn.execute(
            "SELECT * FROM model_operations WHERE operation_id=?", (operation_id,),
        ).fetchone()
        if row is None:
            return None
        if str(row["request_sha256"]) != request_hash:
            raise BaselineContractError(
                "model operation id was reused with different request semantics"
            )
        if row["status"] in {"PENDING", "ABANDONED"}:
            raise IndeterminateModelOperation(
                f"model operation is {row['status']}; never auto-retry a possibly charged call"
            )
        if row["status"] == "RETRY_AUTHORIZED":
            return None
        if row["status"] != "COMPLETED":
            raise BaselineContractError("model operation cache has an invalid status")
        return self._response(row)

    def _reserve(self, operation_id: str, request_hash: str) -> ModelResponse | None:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                "SELECT * FROM model_operations WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_sha256"]) != request_hash:
                    self.conn.execute("COMMIT")
                    raise BaselineContractError(
                        "model operation id was reused with different request semantics"
                    )
                if existing["status"] == "RETRY_AUTHORIZED":
                    cursor = self.conn.execute(
                        """UPDATE model_operations SET status='PENDING',created_at=?
                           WHERE operation_id=? AND request_sha256=?
                             AND status='RETRY_AUTHORIZED'""",
                        (time.time(), operation_id, request_hash),
                    )
                    if cursor.rowcount != 1:
                        raise IndeterminateModelOperation(
                            "retry authorization was consumed concurrently"
                        )
                    self.conn.execute("COMMIT")
                    return None
                self.conn.execute("COMMIT")
                cached = self._lookup(operation_id, request_hash)
                if cached is not None:
                    return cached
                raise IndeterminateModelOperation("model operation changed during reservation")
            self.conn.execute(
                "INSERT INTO model_operations VALUES (?,?,?,NULL,NULL,NULL,NULL,?,NULL)",
                (operation_id, request_hash, "PENDING", time.time()),
            )
            self.conn.execute("COMMIT")
            return None
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ CLI call

    def _command(self, *, system: str, response_schema: dict) -> list[str]:
        command = [
            self.cli_path, "-p", "--bare",
            "--output-format", "json",
            "--json-schema", canonical_json(response_schema),
            "--system-prompt", system,
            "--exclude-dynamic-system-prompt-sections",
        ]
        if self.model:
            command += ["--model", self.model]
        # Variadic option kept last so no later flag is swallowed as a tool name.
        command += ["--disallowedTools", *self.DISALLOWED_TOOLS]
        return command

    def _invoke(self, *, system: str, user: str, response_schema: dict) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                self._command(system=system, response_schema=response_schema),
                input=user, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=self.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise IndeterminateModelOperation(
                "Claude Code exceeded the timeout; the call may already have been charged"
            ) from exc
        except OSError as exc:
            raise IndeterminateModelOperation("Claude Code could not be launched") from exc
        stdout = completed.stdout or ""
        if len(stdout.encode("utf-8", "ignore")) > self.max_response_bytes:
            raise BaselineContractError("Claude Code response exceeded the byte limit")
        if completed.returncode != 0 and not stdout.strip():
            detail = (completed.stderr or "").strip()[:400]
            raise IndeterminateModelOperation(
                f"Claude Code exited {completed.returncode} without a result envelope: {detail}"
            )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise IndeterminateModelOperation(
                "Claude Code did not emit a JSON result envelope"
            ) from exc
        if not isinstance(payload, dict):
            raise IndeterminateModelOperation("Claude Code result envelope must be an object")
        return payload

    def _decode(self, payload: dict[str, Any]) -> tuple[ModelResponse, dict[str, Any]]:
        if payload.get("type") != "result" or payload.get("subtype") != "success":
            detail = str(payload.get("result", ""))[:400]
            raise IndeterminateModelOperation(f"Claude Code reported no usable result: {detail}")
        if payload.get("is_error"):
            raise IndeterminateModelOperation(
                f"Claude Code reported an error: {str(payload.get('result', ''))[:400]}"
            )
        if payload.get("permission_denials"):
            raise IndeterminateModelOperation(
                "a tool permission was requested during a tool-free benchmark call"
            )
        structured = payload.get("structured_output")
        if not isinstance(structured, dict):
            raise IndeterminateModelOperation(
                "Claude Code returned no structured object for the supplied schema"
            )
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            raise IndeterminateModelOperation("Claude Code omitted authoritative usage metadata")
        try:
            # Cache creation and cache reads are billed input, so they belong in the budget.
            billed_input = (
                int(usage["input_tokens"])
                + int(usage.get("cache_creation_input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
            )
            response = ModelResponse(
                text=canonical_json(structured),
                input_tokens=billed_input,
                output_tokens=int(usage["output_tokens"]),
                request_id=str(payload["uuid"]),
            )
            response.usage()
        except (KeyError, TypeError, ValueError) as exc:
            raise IndeterminateModelOperation(
                "Claude Code omitted authoritative usage metadata"
            ) from exc
        served_by = sorted((payload.get("modelUsage") or {}).keys())
        self.observed_backends.update(served_by)
        accounting = {
            "request_alias": self.model or "session-default",
            "served_by": served_by,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "total_cost_usd": payload.get("total_cost_usd"),
            "duration_ms": payload.get("duration_ms"),
            "num_turns": payload.get("num_turns"),
            "session_id": payload.get("session_id"),
        }
        return response, accounting

    def _append_request_response(self, record: dict[str, Any]) -> None:
        """Durably append a complete or indeterminate call audit record."""
        if self.request_response_log is None:
            return
        self.request_response_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.request_response_log, "a", encoding="utf-8", newline="\n") as handle:
            with exclusive_lock(handle):
                handle.write(canonical_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _record_request_response(
        self, *, operation_id: str, request_hash: str, system: str, user: str,
        response_schema: dict, response: ModelResponse, accounting: dict[str, Any] | None,
        cache_hit: bool,
    ) -> None:
        self._append_request_response({
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "route_id": CLAUDE_CODE_ROUTE_ID, "operation_id": operation_id,
            "request_sha256": request_hash, "system": system, "user": user,
            "response_schema": response_schema, "response_text": response.text,
            "response_sha256": hashlib.sha256(response.text.encode("utf-8")).hexdigest(),
            "request_id": response.request_id, "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens, "accounting": accounting,
            "cache_hit": cache_hit, "status": "COMPLETED",
        })

    def _record_indeterminate_request(
        self, *, operation_id: str, request_hash: str, system: str, user: str,
        response_schema: dict, payload: dict[str, Any] | None, error: Exception,
    ) -> None:
        """Preserve the returned envelope without changing a PENDING ledger row."""
        self._append_request_response({
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "route_id": CLAUDE_CODE_ROUTE_ID, "operation_id": operation_id,
            "request_sha256": request_hash, "system": system, "user": user,
            "response_schema": response_schema, "response_text": None,
            "response_sha256": None, "request_id": None, "input_tokens": None,
            "output_tokens": None, "accounting": None, "cache_hit": False,
            "status": "INDETERMINATE", "reason": str(error), "result_envelope": payload,
        })

    def _record_usage(self, *, operation_id: str, request_hash: str, accounting: dict) -> None:
        """Append the billing facts the dependency disclosure has to quote."""
        if self.usage_log is None:
            return
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "route_id": CLAUDE_CODE_ROUTE_ID,
            "operation_id": operation_id,
            "request_sha256": request_hash,
            **accounting,
        }
        with open(self.usage_log, "a", encoding="utf-8", newline="\n") as handle:
            with exclusive_lock(handle):
                handle.write(canonical_json(record) + "\n")
                handle.flush()

    # ------------------------------------------------------------------ protocol

    def complete(
        self, *, operation_id: str, system: str, user: str, response_schema: dict,
    ) -> ModelResponse:
        if not operation_id.strip() or len(operation_id) > 500:
            raise BaselineContractError("model operation id is invalid")
        request_hash = self._request_hash(
            system=system, user=user, response_schema=response_schema,
        )
        cached = self._lookup(operation_id, request_hash)
        if cached is not None:
            self._record_request_response(
                operation_id=operation_id, request_hash=request_hash, system=system, user=user,
                response_schema=response_schema, response=cached, accounting=None, cache_hit=True,
            )
            return cached

        self.circuit.before_call(operation_id=operation_id)
        reserved = False
        payload: dict[str, Any] | None = None
        try:
            raced = self._reserve(operation_id, request_hash)
            if raced is not None:
                self.circuit.record_success(operation_id=operation_id)
                return raced
            reserved = True
            payload = self._invoke(system=system, user=user, response_schema=response_schema)
            response, accounting = self._decode(payload)
        except CircuitOpenError:
            raise
        except Exception as exc:
            if reserved:
                self._record_indeterminate_request(
                    operation_id=operation_id, request_hash=request_hash, system=system, user=user,
                    response_schema=response_schema, payload=payload, error=exc,
                )
            self.circuit.record_failure(
                operation_id=operation_id, reason_code="model_operation_failed",
            )
            raise
        self.circuit.record_success(operation_id=operation_id)
        self.conn.execute(
            """UPDATE model_operations SET status='COMPLETED',response_text=?,
                      input_tokens=?,output_tokens=?,request_id=?,completed_at=?
               WHERE operation_id=? AND request_sha256=? AND status='PENDING'""",
            (response.text, response.input_tokens, response.output_tokens,
             response.request_id, time.time(), operation_id, request_hash),
        )
        committed = self._lookup(operation_id, request_hash)
        if committed is None:
            raise IndeterminateModelOperation("model response cache commit failed")
        self._record_usage(
            operation_id=operation_id, request_hash=request_hash, accounting=accounting,
        )
        self._record_request_response(
            operation_id=operation_id, request_hash=request_hash, system=system, user=user,
            response_schema=response_schema, response=committed, accounting=accounting, cache_hit=False,
        )
        return committed
