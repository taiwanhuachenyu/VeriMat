import json

import pytest

from src.core.events import EventEnvelope, EventValidationError
from src.evidence.ledger import EventLedger, IdempotencyConflict


def _append(ledger, *, key="key-1", payload=None):
    return ledger.append(
        tenant_id="tenant-a",
        job_id="job-a",
        aggregate_type="claim",
        aggregate_id="claim-a",
        event_type="claim.proposed",
        payload=payload or {"claim_id": "claim-a", "text": "claim", "scope": "scope"},
        idempotency_key=key,
    )


def test_ledger_appends_hash_chained_events_and_verifies(tmp_path):
    ledger = EventLedger(tmp_path / "events.jsonl")
    first = _append(ledger)
    second = ledger.append(
        tenant_id="tenant-a", job_id="job-a", aggregate_type="query",
        aggregate_id="query-a", event_type="query.executed",
        payload={"query_id": "query-a", "claim_id": "claim-a", "text": "counter",
                 "intent": "counterevidence", "n_hits": 0},
        idempotency_key="key-2",
    )
    report = ledger.verify()
    assert report.ok and report.event_count == 2
    assert second.previous_hash == first.event_hash
    assert report.head_hash == second.event_hash


def test_ledger_idempotent_replay_returns_existing_event(tmp_path):
    ledger = EventLedger(tmp_path / "events.jsonl")
    first = _append(ledger)
    replay = _append(ledger)
    assert replay == first
    assert ledger.verify().event_count == 1


def test_ledger_rejects_idempotency_key_reuse_with_different_semantics(tmp_path):
    ledger = EventLedger(tmp_path / "events.jsonl")
    _append(ledger)
    with pytest.raises(IdempotencyConflict):
        _append(ledger, payload={"claim_id": "claim-a", "text": "different", "scope": "scope"})


def test_ledger_detects_historical_tampering(tmp_path):
    path = tmp_path / "events.jsonl"
    ledger = EventLedger(path)
    _append(ledger)
    row = json.loads(path.read_text())
    row["payload"]["text"] = "tampered"
    path.write_text(json.dumps(row) + "\n")
    report = ledger.verify()
    assert not report.ok
    assert "event_hash" in report.error


def test_event_rejects_secret_fields():
    with pytest.raises(EventValidationError, match="sensitive fields"):
        EventEnvelope.build(
            sequence=1, event_id="event", tenant_id="tenant", job_id="job",
            aggregate_type="provider", aggregate_id="call", event_type="provider.called",
            payload={"nested": {"api_key": "EXAMPLE_MUST_NOT_PERSIST"}},
            idempotency_key="idempotent",
        )
