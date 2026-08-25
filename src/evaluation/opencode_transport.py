"""Fail-closed local OpenCode transport for one-call structured benchmark operations."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from src.core.events import canonical_json
from src.core.portability import extended_path
from src.operations.runtime_migrations import (
    MODEL_OPERATION_SPEC, assert_runtime_compatibility, prepare_runtime_database,
    schema_script,
)

from .baseline_runner import BaselineContractError
from .circuit_breaker import CircuitOpenError, PersistentCircuitBreaker
from .model_backend import ModelResponse, StructuredModelTransport

SCHEMA = schema_script(MODEL_OPERATION_SPEC)


class IndeterminateModelOperation(BaselineContractError):
    """A request may have reached the model but no durable response was committed."""


class OpenCodeStructuredTransport(StructuredModelTransport):
    """Use a local OpenCode server while suppressing all tool access.

    A PENDING operation is never retried automatically: OpenCode does not expose an upstream
    idempotency guarantee, so a crash after request transmission is an indeterminate paid call.
    This avoids silently double-charging a benchmark run.
    """

    DISABLED_TOOLS = (
        "bash", "read", "write", "edit", "glob", "grep", "webfetch", "task",
        "todowrite", "todoread", "apply_patch",
    )

    def __init__(
        self, *, base_url: str, provider_id: str, model_id: str,
        operation_db: str | Path, agent: str = "benchmark",
        timeout_seconds: float = 600, max_response_bytes: int = 2_000_000,
        circuit_failure_threshold: int = 3,
        circuit_recovery_seconds: float = 60.0,
    ):
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password
        ):
            raise ValueError("OpenCode benchmark transport requires a local HTTP server")
        if not all(value.strip() for value in (provider_id, model_id, agent)):
            raise ValueError("provider, model and agent are required")
        if timeout_seconds <= 0 or max_response_bytes < 1024:
            raise ValueError("transport bounds are invalid")
        self.base_url = base_url.rstrip("/")
        self.provider_id = provider_id
        self.model_id = model_id
        self.agent = agent
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
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
            circuit_id=f"model:{provider_id}:{model_id}",
            failure_threshold=circuit_failure_threshold,
            recovery_timeout_seconds=circuit_recovery_seconds,
            probe_timeout_seconds=timeout_seconds + 30,
        )

    def close(self) -> None:
        self.circuit.close()
        self.conn.close()

    def __enter__(self) -> "OpenCodeStructuredTransport":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _http_json(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        data = canonical_json(body).encode()
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise BaselineContractError("OpenCode response exceeded byte limit")
                value = json.loads(raw.decode()) if raw else {}
                if not isinstance(value, dict):
                    raise BaselineContractError("OpenCode response must be a JSON object")
                return response.status, value
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", "replace")
            raise BaselineContractError(
                f"OpenCode HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise IndeterminateModelOperation(
                "OpenCode request state is indeterminate; operator reconciliation required"
            ) from exc

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

    def _reserve(
        self, operation_id: str, request_hash: str,
    ) -> ModelResponse | None:
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
                raise IndeterminateModelOperation(
                    "model operation changed during reservation"
                )
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

    def complete(
        self, *, operation_id: str, system: str, user: str,
        response_schema: dict,
    ) -> ModelResponse:
        if not operation_id.strip() or len(operation_id) > 500:
            raise BaselineContractError("model operation id is invalid")
        request_hash = self._request_hash(
            system=system, user=user, response_schema=response_schema,
        )
        cached = self._lookup(operation_id, request_hash)
        if cached is not None:
            return cached

        status, session = self._http_json(
            "POST", "/session", {"title": f"benchmark-{request_hash[:16]}"},
        )
        if status != 200 or not (session_id := session.get("id") or session.get("sessionID")):
            raise BaselineContractError("OpenCode did not create a benchmark session")
        self.circuit.before_call(operation_id=operation_id)
        try:
            raced = self._reserve(operation_id, request_hash)
            if raced is not None:
                self.circuit.record_success(operation_id=operation_id)
                return raced
            status, value = self._http_json(
                "POST", f"/session/{session_id}/message",
                {
                "model": {"providerID": self.provider_id, "modelID": self.model_id},
                "agent": self.agent,
                "tools": {name: False for name in self.DISABLED_TOOLS},
                "format": {
                    "type": "json_schema", "schema": response_schema,
                    "retryCount": 0,
                },
                "system": system,
                "parts": [{"type": "text", "text": user}],
                },
            )
            if status != 200:
                raise IndeterminateModelOperation("OpenCode structured request did not complete")
            info, parts = value.get("info"), value.get("parts")
            if not isinstance(info, dict) or not isinstance(parts, list):
                raise IndeterminateModelOperation("OpenCode returned an incomplete message envelope")
            if info.get("error"):
                raise IndeterminateModelOperation("OpenCode reported a model error")
            if (
                info.get("providerID") != self.provider_id
                or info.get("modelID") != self.model_id
            ):
                raise IndeterminateModelOperation("OpenCode reported a different provider/model alias")
            if any(part.get("type") in {"tool", "tool-invocation"} for part in parts
                   if isinstance(part, dict)):
                raise IndeterminateModelOperation("tool use occurred in a tool-free benchmark session")
            structured = info.get("structured")
            if structured is not None:
                response_text = canonical_json(structured)
            else:
                response_text = "".join(
                    str(part.get("text", "")) for part in parts
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            tokens = info.get("tokens") or {}
            try:
                response = ModelResponse(
                    text=response_text,
                    input_tokens=int(tokens["input"]),
                    output_tokens=int(tokens["output"]),
                    request_id=str(info["id"]),
                )
                response.usage()
            except (KeyError, TypeError, ValueError) as exc:
                raise IndeterminateModelOperation(
                    "OpenCode omitted authoritative usage metadata"
                ) from exc
        except CircuitOpenError:
            raise
        except Exception:
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
        cached = self._lookup(operation_id, request_hash)
        if cached is None:
            raise IndeterminateModelOperation("model response cache commit failed")
        return cached
