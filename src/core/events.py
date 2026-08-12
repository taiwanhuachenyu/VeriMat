"""Versioned event envelope for the append-only execution and evidence ledger."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(^|_)(api_?key|authorization|cookie|credential|password|secret|token)($|_)",
    re.IGNORECASE,
)


class EventValidationError(ValueError):
    """Raised when an event violates the durable event contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _sensitive_paths(value: Any, prefix: str = "payload") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            if _SENSITIVE_KEY_RE.search(str(key)):
                found.append(path)
            found.extend(_sensitive_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_sensitive_paths(item, f"{prefix}[{index}]"))
    return found


def validate_durable_payload(value: Any, *, prefix: str = "payload") -> None:
    """Reject structured secret-bearing data before it reaches durable storage."""
    if not isinstance(value, dict):
        raise EventValidationError(f"{prefix} must be an object")
    sensitive = _sensitive_paths(value, prefix)
    if sensitive:
        raise EventValidationError(
            "sensitive fields are forbidden in durable data: " + ", ".join(sensitive)
        )
    try:
        canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise EventValidationError(f"{prefix} must be finite JSON data: {exc}") from exc


@dataclass(frozen=True)
class EventEnvelope:
    """One immutable, hash-linked domain event.

    The hash covers every field except ``event_hash``. Sequence numbers are local to a
    ledger, while ``event_id`` and ``idempotency_key`` protect against duplicate effects.
    """

    schema_version: int
    sequence: int
    event_id: str
    occurred_at: str
    tenant_id: str
    job_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str
    previous_hash: str
    event_hash: str

    def unsigned_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("event_hash")
        return value

    def calculated_hash(self) -> str:
        return hashlib.sha256(
            canonical_json(self.unsigned_dict()).encode("utf-8")
        ).hexdigest()

    def validate(self) -> None:
        if self.schema_version != 1:
            raise EventValidationError(f"unsupported schema_version={self.schema_version}")
        if self.sequence < 1:
            raise EventValidationError("sequence must be >= 1")
        for field_name in (
            "event_id", "occurred_at", "tenant_id", "job_id", "aggregate_type",
            "aggregate_id", "event_type", "idempotency_key",
        ):
            if not str(getattr(self, field_name)).strip():
                raise EventValidationError(f"{field_name} must not be empty")
        try:
            parsed = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EventValidationError("occurred_at must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise EventValidationError("occurred_at must include a timezone")
        validate_durable_payload(self.payload)
        if self.previous_hash and not _HASH_RE.fullmatch(self.previous_hash):
            raise EventValidationError("previous_hash must be empty or a SHA-256 hex digest")
        if not _HASH_RE.fullmatch(self.event_hash):
            raise EventValidationError("event_hash must be a SHA-256 hex digest")
        if self.calculated_hash() != self.event_hash:
            raise EventValidationError("event_hash does not match event content")

    def to_json(self) -> str:
        self.validate()
        return canonical_json(asdict(self))

    @classmethod
    def build(
        cls,
        *,
        sequence: int,
        event_id: str,
        tenant_id: str,
        job_id: str,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        previous_hash: str = "",
        occurred_at: str | None = None,
    ) -> "EventEnvelope":
        timestamp = occurred_at or datetime.now(timezone.utc).isoformat()
        provisional = cls(
            schema_version=1,
            sequence=sequence,
            event_id=event_id,
            occurred_at=timestamp,
            tenant_id=tenant_id,
            job_id=job_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
            previous_hash=previous_hash,
            event_hash="0" * 64,
        )
        envelope = cls(**{**asdict(provisional), "event_hash": provisional.calculated_hash()})
        envelope.validate()
        return envelope

    @classmethod
    def from_json(cls, line: str) -> "EventEnvelope":
        try:
            value = json.loads(line)
            envelope = cls(**value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise EventValidationError(f"invalid event JSON: {exc}") from exc
        envelope.validate()
        return envelope
