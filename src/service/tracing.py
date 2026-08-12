"""W3C-compatible, secret-free HTTP trace records for the control plane."""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from src.core.events import canonical_json
from src.service.metrics import METHOD_LABELS, ROUTE_LABELS

TRACEPARENT = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str
    flags: str

    @classmethod
    def from_header(cls, value: str) -> "TraceContext":
        match = TRACEPARENT.fullmatch(value.strip().lower())
        if match and match.group(1) != "0" * 32 and match.group(2) != "0" * 16:
            trace_id, parent, flags = match.groups()
        else:
            trace_id, parent, flags = secrets.token_hex(16), "", "01"
        span_id = secrets.token_hex(8)
        return cls(trace_id=trace_id, span_id=span_id, parent_span_id=parent, flags=flags)

    def response_header(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.flags}"


@dataclass(frozen=True)
class HttpTraceRecord:
    schema_version: int
    occurred_at: str
    trace_id: str
    span_id: str
    parent_span_id: str
    request_id_sha256: str
    method: str
    route: str
    status: int
    duration_ms: float

    @classmethod
    def build(
        cls, *, context: TraceContext, request_id: str, method: str, route: str,
        status: int, duration_seconds: float,
    ) -> "HttpTraceRecord":
        record = cls(
            schema_version=1,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            trace_id=context.trace_id, span_id=context.span_id,
            parent_span_id=context.parent_span_id,
            request_id_sha256=hashlib.sha256(request_id.encode()).hexdigest(),
            method=method, route=route, status=status,
            duration_ms=round(duration_seconds * 1000, 6),
        )
        record.validate()
        return record

    def validate(self) -> None:
        if self.schema_version != 1 or self.method not in METHOD_LABELS or self.route not in ROUTE_LABELS:
            raise ValueError("trace schema, method, or route is invalid")
        if (
            not re.fullmatch(r"[0-9a-f]{32}", self.trace_id)
            or self.trace_id == "0" * 32
            or not re.fullmatch(r"[0-9a-f]{16}", self.span_id)
            or self.span_id == "0" * 16
            or (
                self.parent_span_id
                and not re.fullmatch(r"[0-9a-f]{16}", self.parent_span_id)
            )
            or not re.fullmatch(r"[0-9a-f]{64}", self.request_id_sha256)
        ):
            raise ValueError("trace identities are invalid")
        if self.status < 100 or self.status > 599 or self.duration_ms < 0:
            raise ValueError("trace status or duration is invalid")


class TraceRecorder(Protocol):
    def record(self, record: HttpTraceRecord) -> None: ...


class NullTraceRecorder:
    def record(self, record: HttpTraceRecord) -> None:
        record.validate()


class StructuredTraceLog:
    """Append-only NDJSON trace sink with process locking and 0600 file permissions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError("trace log must not be a symbolic link")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def record(self, record: HttpTraceRecord) -> None:
        record.validate()
        rendered = (canonical_json(asdict(record)) + "\n").encode()
        descriptor = os.open(
            self.path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            written = 0
            while written < len(rendered):
                written += os.write(descriptor, rendered[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
