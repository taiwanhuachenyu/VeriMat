"""Process-safe append-only, SHA-256 hash-chained event ledger."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from src.core.events import EventEnvelope, EventValidationError, canonical_json
from src.core.portability import extended_path, lock_exclusive, lock_shared


class LedgerIntegrityError(RuntimeError):
    """The ledger is truncated, reordered, duplicated, or tampered with."""


class IdempotencyConflict(LedgerIntegrityError):
    """An idempotency key was reused for a different event."""


@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    event_count: int
    head_hash: str
    error: str = ""


class EventLedger:
    """A minimal durable event store suitable for single-node execution.

    The file is locked across processes during verification and append. Every append is
    flushed and fsynced before returning. Reusing an idempotency key with identical event
    semantics returns the existing event; reusing it with different content fails closed.
    """

    def __init__(self, path: str | Path):
        self.path = extended_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _semantic_fingerprint(
        *, tenant_id: str, job_id: str, aggregate_type: str, aggregate_id: str,
        event_type: str, payload: dict[str, Any],
    ) -> str:
        return canonical_json({
            "tenant_id": tenant_id,
            "job_id": job_id,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "event_type": event_type,
            "payload": payload,
        })

    @staticmethod
    def _load_locked(handle) -> list[EventEnvelope]:
        handle.seek(0)
        events: list[EventEnvelope] = []
        previous_hash = ""
        event_ids: set[str] = set()
        idempotency_keys: set[str] = set()
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                event = EventEnvelope.from_json(raw)
            except EventValidationError as exc:
                raise LedgerIntegrityError(f"line {line_number}: {exc}") from exc
            expected_sequence = len(events) + 1
            if event.sequence != expected_sequence:
                raise LedgerIntegrityError(
                    f"line {line_number}: expected sequence {expected_sequence}, "
                    f"got {event.sequence}"
                )
            if event.previous_hash != previous_hash:
                raise LedgerIntegrityError(
                    f"line {line_number}: previous_hash does not match ledger head"
                )
            if event.event_id in event_ids:
                raise LedgerIntegrityError(f"line {line_number}: duplicate event_id")
            if event.idempotency_key in idempotency_keys:
                raise LedgerIntegrityError(f"line {line_number}: duplicate idempotency_key")
            events.append(event)
            event_ids.add(event.event_id)
            idempotency_keys.add(event.idempotency_key)
            previous_hash = event.event_hash
        return events

    def append(
        self,
        *,
        tenant_id: str,
        job_id: str,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
        expected_head: str | None = None,
    ) -> EventEnvelope:
        with open(self.path, "a+", encoding="utf-8", newline="\n") as handle:
            lock_exclusive(handle)
            events = self._load_locked(handle)
            semantic = self._semantic_fingerprint(
                tenant_id=tenant_id, job_id=job_id, aggregate_type=aggregate_type,
                aggregate_id=aggregate_id, event_type=event_type, payload=payload,
            )
            for existing in events:
                if existing.idempotency_key != idempotency_key:
                    continue
                existing_semantic = self._semantic_fingerprint(
                    tenant_id=existing.tenant_id, job_id=existing.job_id,
                    aggregate_type=existing.aggregate_type,
                    aggregate_id=existing.aggregate_id,
                    event_type=existing.event_type, payload=existing.payload,
                )
                if existing_semantic != semantic:
                    raise IdempotencyConflict(
                        f"idempotency key {idempotency_key!r} already has different semantics"
                    )
                return existing
            head = events[-1].event_hash if events else ""
            if expected_head is not None and expected_head != head:
                raise LedgerIntegrityError(
                    f"optimistic append failed: expected head {expected_head!r}, got {head!r}"
                )
            envelope = EventEnvelope.build(
                sequence=len(events) + 1,
                event_id=event_id or str(uuid.uuid4()),
                tenant_id=tenant_id,
                job_id=job_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
                previous_hash=head,
                occurred_at=occurred_at,
            )
            handle.seek(0, os.SEEK_END)
            handle.write(envelope.to_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return envelope

    def events(self) -> Iterator[EventEnvelope]:
        if not self.path.exists():
            return iter(())
        with open(self.path, encoding="utf-8") as handle:
            lock_shared(handle)
            events = self._load_locked(handle)
        return iter(events)

    def verify(self) -> VerificationReport:
        if not self.path.exists():
            return VerificationReport(ok=True, event_count=0, head_hash="")
        try:
            events = list(self.events())
        except LedgerIntegrityError as exc:
            return VerificationReport(ok=False, event_count=0, head_hash="", error=str(exc))
        return VerificationReport(
            ok=True,
            event_count=len(events),
            head_hash=events[-1].event_hash if events else "",
        )

    def write_verification_receipt(self, destination: str | Path) -> VerificationReport:
        report = self.verify()
        target = extended_path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report.__dict__, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return report
