"""Small production-oriented HTTP control plane built on the standard library."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import ssl
import time
import uuid
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from src.orchestration.job_store import (
    IdempotencyConflict, IllegalTransition, Job, JobStatus, JobStore, JobStoreError,
)
from src.service.metrics import ControlPlaneMetrics
from src.service.rate_limit import PrincipalRateLimiter
from src.service.tracing import (
    HttpTraceRecord, NullTraceRecorder, TraceContext, TraceRecorder,
)
from src.service.tls import host_is_loopback

MAX_BODY_BYTES = 64 * 1024
MAX_TASK_CHARS = 20_000
MAX_IDEMPOTENCY_CHARS = 200
MAX_CALL_BUDGET = 10_000
MAX_TOKEN_BUDGET = 100_000_000
MAX_COST_BUDGET = 10**15
ROLES = {"submit", "read", "cancel", "admin"}
JOB_PATH = re.compile(r"^/v1/jobs/([0-9a-fA-F-]{36})(/cancel)?$")


@dataclass(frozen=True)
class Principal:
    principal_id: str
    tenant_id: str
    roles: frozenset[str]

    def permits(self, role: str) -> bool:
        return "admin" in self.roles or role in self.roles


class AuthRegistry:
    """Resolve bearer or peer-certificate SHA-256 digests without storing credentials."""

    def __init__(
        self, token_entries: list[tuple[str, Principal]],
        certificate_entries: list[tuple[str, Principal]] | None = None,
    ):
        self._token_entries = tuple(token_entries)
        self._certificate_entries = tuple(certificate_entries or ())

    @classmethod
    def from_json(cls, value: str) -> "AuthRegistry":
        try:
            rows = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("auth registry must be valid JSON") from exc
        if not isinstance(rows, list) or not rows:
            raise ValueError("auth registry must be a non-empty array")
        token_entries: list[tuple[str, Principal]] = []
        certificate_entries: list[tuple[str, Principal]] = []
        seen: set[tuple[str, str]] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"auth entry {index} must be an object")
            unknown = set(row) - {
                "token_sha256", "client_cert_sha256", "principal_id", "tenant_id", "roles",
            }
            if unknown:
                raise ValueError(f"auth entry {index} has unknown fields")
            credential_fields = [
                field for field in ("token_sha256", "client_cert_sha256") if field in row
            ]
            if len(credential_fields) != 1:
                raise ValueError(f"auth entry {index} requires exactly one credential digest")
            credential_kind = credential_fields[0]
            digest = str(row.get(credential_kind, ""))
            principal_id = str(row.get("principal_id", "")).strip()
            tenant_id = str(row.get("tenant_id", "")).strip()
            roles = row.get("roles")
            if (
                len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
                or not principal_id or not tenant_id or not isinstance(roles, list)
            ):
                raise ValueError(f"auth entry {index} has invalid required fields")
            role_set = frozenset(str(role) for role in roles)
            if not role_set or not role_set <= ROLES:
                raise ValueError(f"auth entry {index} has unsupported roles")
            seen_key = (credential_kind, digest)
            if seen_key in seen:
                raise ValueError("duplicate credential digest")
            seen.add(seen_key)
            entry = (digest, Principal(principal_id, tenant_id, role_set))
            if credential_kind == "token_sha256":
                token_entries.append(entry)
            else:
                certificate_entries.append(entry)
        return cls(token_entries, certificate_entries)

    @staticmethod
    def _match(digest: str, entries: tuple[tuple[str, Principal], ...]) -> Principal | None:
        matched: Principal | None = None
        for expected, principal in entries:
            if hmac.compare_digest(digest, expected):
                matched = principal
        return matched

    def authenticate(self, bearer_token: str) -> Principal | None:
        if not bearer_token:
            return None
        digest = hashlib.sha256(bearer_token.encode()).hexdigest()
        # Visit every entry to avoid revealing which digest matched through early-exit timing.
        return self._match(digest, self._token_entries)

    def authenticate_client_certificate(self, certificate_der: bytes) -> Principal | None:
        if not certificate_der:
            return None
        return self._match(
            hashlib.sha256(certificate_der).hexdigest(), self._certificate_entries,
        )

    @property
    def has_certificate_entries(self) -> bool:
        return bool(self._certificate_entries)


def _job_json(job: Job) -> dict[str, Any]:
    value = asdict(job)
    value["status"] = job.status.value
    value["stage"] = job.stage.value
    return value


class ControlPlaneApp:
    def __init__(
        self, *, store_path: str | Path, auth: AuthRegistry,
        metrics: ControlPlaneMetrics | None = None,
        rate_limiter: PrincipalRateLimiter | None = None,
        trace_recorder: TraceRecorder | None = None,
    ):
        self.store_path = str(store_path)
        self.auth = auth
        self.metrics = metrics or ControlPlaneMetrics()
        self.rate_limiter = rate_limiter or PrincipalRateLimiter()
        self.trace_recorder = trace_recorder or NullTraceRecorder()
        Path(self.store_path).parent.mkdir(parents=True, exist_ok=True)
        # Fail early if the database cannot be initialized.
        JobStore(self.store_path).close()

    def _store(self) -> JobStore:
        # ControlPlaneApp completed a full integrity/history verification at startup. Requests keep
        # a constant-time identity/version guard so a hot-swapped incompatible DB still fails.
        return JobStore(self.store_path, full_schema_verification=False)

    @staticmethod
    def _validate_submission(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        allowed = {
            "idempotency_key", "task", "max_calls", "max_tokens",
            "max_cost_microunits",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError("unknown fields: " + ", ".join(unknown))
        idempotency_key = value.get("idempotency_key")
        task = value.get("task")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= MAX_IDEMPOTENCY_CHARS:
            raise ValueError("idempotency_key length is invalid")
        if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_CHARS:
            raise ValueError("task length is invalid")
        limits = {
            "max_calls": MAX_CALL_BUDGET,
            "max_tokens": MAX_TOKEN_BUDGET,
            "max_cost_microunits": MAX_COST_BUDGET,
        }
        normalized = {"idempotency_key": idempotency_key, "task": task}
        for field, maximum in limits.items():
            amount = value.get(field)
            if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= maximum:
                raise ValueError(f"{field} must be an integer in [0,{maximum}]")
            normalized[field] = amount
        return normalized

    def make_server(
        self, host: str, port: int, *, tls_context: ssl.SSLContext | None = None,
    ) -> ThreadingHTTPServer:
        if tls_context is None and not host_is_loopback(host):
            raise ValueError("non-loopback control-plane listeners require TLS")
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GoAI-Control/2"
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:
                # Do not log headers, bodies, tasks, or bearer material.
                return

            def _respond(
                self, status: HTTPStatus, body: dict[str, Any],
                headers: dict[str, str] | None = None,
            ) -> None:
                body = {"request_id": self.request_id, **body}
                rendered = json.dumps(
                    body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
                self.send_response(status.value)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(rendered)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Request-ID", self.request_id)
                self.send_header("traceparent", self.trace_context.response_header())
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(rendered)
                finally:
                    self._record(status.value)

            def _respond_metrics(self, body: str) -> None:
                rendered = body.encode("utf-8")
                self.send_response(HTTPStatus.OK.value)
                self.send_header(
                    "Content-Type", "text/plain; version=0.0.4; charset=utf-8",
                )
                self.send_header("Content-Length", str(len(rendered)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Request-ID", self.request_id)
                self.send_header("traceparent", self.trace_context.response_header())
                self.end_headers()
                try:
                    self.wfile.write(rendered)
                finally:
                    self._record(HTTPStatus.OK.value)

            def _begin(self) -> None:
                self.request_id = self._request_id()
                self.trace_context = TraceContext.from_header(
                    self.headers.get("traceparent", "")
                )
                self.request_started = time.perf_counter()
                path = self.path.split("?", 1)[0]
                if path == "/healthz":
                    self.route_label = "healthz"
                elif path == "/readyz":
                    self.route_label = "readyz"
                elif path == "/metrics":
                    self.route_label = "metrics"
                elif path == "/v1/jobs":
                    self.route_label = "jobs_collection"
                else:
                    match = JOB_PATH.fullmatch(path)
                    self.route_label = (
                        "job_cancel" if match and match.group(2)
                        else "job_item" if match else "not_found"
                    )

            def _record(self, status: int) -> None:
                if getattr(self, "request_recorded", False):
                    return
                self.request_recorded = True
                duration = max(0.0, time.perf_counter() - self.request_started)
                app.metrics.record(
                    method=self.command, route=self.route_label, status=status,
                    duration_seconds=duration,
                )
                try:
                    app.trace_recorder.record(HttpTraceRecord.build(
                        context=self.trace_context, request_id=self.request_id,
                        method=self.command, route=self.route_label, status=status,
                        duration_seconds=duration,
                    ))
                except (OSError, ValueError):
                    # Diagnostic telemetry must not change an already committed HTTP outcome.
                    app.metrics.record_trace_write_failure()

            def _request_id(self) -> str:
                supplied = self.headers.get("X-Request-ID", "")
                if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", supplied):
                    return supplied
                return str(uuid.uuid4())

            def _error(self, status: HTTPStatus, code: str, message: str) -> None:
                self._respond(status, {"error": {"code": code, "message": message}})

            def _principal(self, role: str) -> Principal | None:
                certificate = b""
                certificate_getter = getattr(self.connection, "getpeercert", None)
                if certificate_getter is not None:
                    try:
                        certificate = certificate_getter(binary_form=True) or b""
                    except (OSError, ssl.SSLError):
                        certificate = b""
                if certificate:
                    # A presented certificate is authoritative; never downgrade a failed mapping
                    # to a bearer credential on the same request.
                    principal = app.auth.authenticate_client_certificate(certificate)
                else:
                    authorization = self.headers.get("Authorization", "")
                    scheme, _, token = authorization.partition(" ")
                    principal = app.auth.authenticate(
                        token if scheme.lower() == "bearer" else ""
                    )
                if principal is None:
                    self.close_connection = True
                    self._error(
                        HTTPStatus.UNAUTHORIZED, "UNAUTHENTICATED",
                        "valid configured credential required",
                    )
                    return None
                if not principal.permits(role):
                    self.close_connection = True
                    self._error(HTTPStatus.FORBIDDEN, "FORBIDDEN", "role is not permitted")
                    return None
                allowed, retry_after = app.rate_limiter.allow(
                    tenant_id=principal.tenant_id, principal_id=principal.principal_id,
                )
                if not allowed:
                    self._respond(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": {
                            "code": "RATE_LIMITED",
                            "message": "principal request rate exceeded",
                        }},
                        headers={"Retry-After": str(retry_after)},
                    )
                    return None
                return principal

            def _body(self) -> Any:
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
                if content_type != "application/json":
                    raise ValueError("Content-Type must be application/json")
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise ValueError("Content-Length is required")
                try:
                    length = int(raw_length)
                except ValueError as exc:
                    raise ValueError("invalid Content-Length") from exc
                if length < 0 or length > MAX_BODY_BYTES:
                    raise OverflowError("request body is too large")
                try:
                    return json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ValueError("request body is not valid UTF-8 JSON") from exc

            def _job_match(self):
                return JOB_PATH.fullmatch(self.path.split("?", 1)[0])

            def do_GET(self) -> None:
                self._begin()
                if self.path == "/healthz":
                    self._respond(HTTPStatus.OK, {"status": "ok", "time": time.time()})
                    return
                if self.path == "/readyz":
                    try:
                        with app._store() as store:
                            readiness = store.readiness_check()
                    except (JobStoreError, OSError):
                        self._error(
                            HTTPStatus.SERVICE_UNAVAILABLE, "NOT_READY",
                            "control-plane storage is unavailable",
                        )
                        return
                    self._respond(HTTPStatus.OK, {"status": "ready", **readiness})
                    return
                if self.path == "/metrics":
                    principal = self._principal("admin")
                    if principal is None:
                        return
                    try:
                        with app._store() as store:
                            snapshot = store.operational_snapshot()
                    except (JobStoreError, OSError):
                        self._error(
                            HTTPStatus.SERVICE_UNAVAILABLE, "METRICS_UNAVAILABLE",
                            "control-plane metrics are unavailable",
                        )
                        return
                    self._respond_metrics(app.metrics.render(snapshot))
                    return
                match = self._job_match()
                if match is None or match.group(2):
                    self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "resource not found")
                    return
                principal = self._principal("read")
                if principal is None:
                    return
                try:
                    with app._store() as store:
                        job = store.get(match.group(1), tenant_id=principal.tenant_id)
                except JobStoreError:
                    # Same response for absent and cross-tenant jobs prevents enumeration.
                    self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "resource not found")
                    return
                self._respond(HTTPStatus.OK, {"job": _job_json(job)})

            def do_POST(self) -> None:
                self._begin()
                if self.path == "/v1/jobs":
                    principal = self._principal("submit")
                    if principal is None:
                        return
                    try:
                        submission = app._validate_submission(self._body())
                        with app._store() as store:
                            job = store.create_job(tenant_id=principal.tenant_id, **submission)
                    except OverflowError as exc:
                        self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "BODY_TOO_LARGE", str(exc))
                        return
                    except (ValueError, IdempotencyConflict) as exc:
                        self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", str(exc))
                        return
                    self._respond(HTTPStatus.OK, {"job": _job_json(job)})
                    return
                match = self._job_match()
                if match is None or not match.group(2):
                    self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "resource not found")
                    return
                principal = self._principal("cancel")
                if principal is None:
                    return
                try:
                    with app._store() as store:
                        job = store.get(match.group(1), tenant_id=principal.tenant_id)
                        if job.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
                            job = store.transition(job.job_id, target=JobStatus.CANCELLED)
                except IllegalTransition as exc:
                    self._error(HTTPStatus.CONFLICT, "ILLEGAL_TRANSITION", str(exc))
                    return
                except JobStoreError:
                    self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "resource not found")
                    return
                self._respond(HTTPStatus.OK, {"job": _job_json(job)})

        server = ThreadingHTTPServer((host, port), Handler)
        if tls_context is not None:
            server.socket = tls_context.wrap_socket(server.socket, server_side=True)
        return server
