import pytest

from src.evaluation.baseline_runner import BaselineContractError
from src.evaluation.opencode_transport import (
    IndeterminateModelOperation, OpenCodeStructuredTransport,
)


class FakeTransport(OpenCodeStructuredTransport):
    def __init__(self, tmp_path, responses):
        self.responses = list(responses)
        self.http_calls = []
        super().__init__(
            base_url="http://127.0.0.1:9999", provider_id="provider",
            model_id="model", operation_db=tmp_path / "operations.db",
        )

    def _http_json(self, method, path, body):
        self.http_calls.append((method, path, body))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _message():
    return (200, {
        "info": {
            "id": "message", "providerID": "provider", "modelID": "model",
            "tokens": {"input": 11, "output": 7},
            "structured": {"queries": ["query"]},
        },
        "parts": [{"type": "text", "text": "ignored when structured exists"}],
    })


def test_transport_caches_one_structured_call_and_disables_tools(tmp_path):
    transport = FakeTransport(tmp_path, [
        (200, {"id": "session"}), _message(),
    ])
    kwargs = dict(
        operation_id="operation", system="system", user="user",
        response_schema={"type": "object"},
    )
    first = transport.complete(**kwargs)
    second = transport.complete(**kwargs)
    assert first == second
    assert first.input_tokens == 11 and first.output_tokens == 7
    assert len(transport.http_calls) == 2
    prompt = transport.http_calls[1][2]
    assert prompt["format"]["retryCount"] == 0
    assert prompt["tools"] and not any(prompt["tools"].values())


def test_operation_semantic_conflict_is_rejected_without_another_call(tmp_path):
    transport = FakeTransport(tmp_path, [(200, {"id": "session"}), _message()])
    transport.complete(
        operation_id="operation", system="system", user="user",
        response_schema={"type": "object"},
    )
    with pytest.raises(BaselineContractError, match="different request semantics"):
        transport.complete(
            operation_id="operation", system="changed", user="user",
            response_schema={"type": "object"},
        )
    assert len(transport.http_calls) == 2


def test_indeterminate_paid_call_is_never_retried_automatically(tmp_path):
    transport = FakeTransport(tmp_path, [
        (200, {"id": "session"}),
        IndeterminateModelOperation("connection lost after request"),
        (200, {"id": "another-session"}),
    ])
    kwargs = dict(
        operation_id="operation", system="system", user="user",
        response_schema={"type": "object"},
    )
    with pytest.raises(IndeterminateModelOperation):
        transport.complete(**kwargs)
    with pytest.raises(IndeterminateModelOperation, match="PENDING"):
        transport.complete(**kwargs)
    assert len(transport.http_calls) == 2


def test_transport_rejects_nonlocal_server(tmp_path):
    with pytest.raises(ValueError, match="local HTTP"):
        OpenCodeStructuredTransport(
            base_url="https://example.org", provider_id="provider", model_id="model",
            operation_db=tmp_path / "operations.db",
        )


def test_completed_reservation_race_reuses_cache_without_second_message(tmp_path):
    transport = FakeTransport(tmp_path, [(200, {"id": "session"})])
    request_hash = transport._request_hash(
        system="system", user="user", response_schema={"type": "object"},
    )
    transport.conn.execute(
        """INSERT INTO model_operations VALUES (?,?,?,?,?,?,?,?,?)""",
        ("operation", request_hash, "COMPLETED", "{}", 3, 2, "request", 1.0, 2.0),
    )
    original_lookup = transport._lookup
    lookup_calls = 0

    def race_lookup(operation_id, candidate_hash):
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            return None
        return original_lookup(operation_id, candidate_hash)

    transport._lookup = race_lookup
    response = transport.complete(
        operation_id="operation", system="system", user="user",
        response_schema={"type": "object"},
    )
    assert response.request_id == "request"
    assert [path for _, path, _ in transport.http_calls] == ["/session"]
