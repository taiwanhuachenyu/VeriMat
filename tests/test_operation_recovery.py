import hashlib

import pytest

from src.evaluation.literature_retriever import (
    CachedSciverseTransport, IndeterminateRetrievalOperation,
)
from src.evaluation.opencode_transport import IndeterminateModelOperation
from src.evaluation.operation_recovery import (
    OperationRecoveryError, list_operations, reconcile_operation,
    reconciliation_history,
)

from test_opencode_transport import FakeTransport, _message


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _attestation():
    return {
        "actor": "on-call-operator", "reason": "verified against provider request audit",
        "evidence_receipt_sha256": _hash("provider audit receipt"),
    }


def _pending_model(tmp_path, responses=None):
    transport = FakeTransport(tmp_path, responses or [
        (200, {"id": "session"}), IndeterminateModelOperation("lost"),
    ])
    kwargs = {
        "operation_id": "operation", "system": "system", "user": "user",
        "response_schema": {"type": "object"},
    }
    with pytest.raises(IndeterminateModelOperation):
        transport.complete(**kwargs)
    row = list_operations(
        database=tmp_path / "operations.db", kind="model",
    )[0]
    return transport, kwargs, row


def test_operator_can_commit_authoritative_model_response_without_new_call(tmp_path):
    transport, kwargs, row = _pending_model(tmp_path)
    result = reconcile_operation(
        database=tmp_path / "operations.db", kind="model",
        operation_id="operation", request_sha256=row["request_sha256"],
        action="complete", response={
            "response_text": '{"queries":["recovered"]}',
            "input_tokens": 13, "output_tokens": 5, "request_id": "provider-request-7",
        }, **_attestation(),
    )
    recovered = transport.complete(**kwargs)
    assert result["status"] == "COMPLETED"
    assert recovered.input_tokens == 13 and recovered.request_id == "provider-request-7"
    assert len(transport.http_calls) == 2
    history = reconciliation_history(
        database=tmp_path / "operations.db", kind="model", operation_id="operation",
    )
    assert [entry["action"] for entry in history] == ["complete"]


def test_retry_requires_explicit_not_executed_attestation_and_is_consumed_once(tmp_path):
    responses = [
        (200, {"id": "session-1"}), IndeterminateModelOperation("lost"),
        (200, {"id": "session-2"}), _message(),
    ]
    transport, kwargs, row = _pending_model(tmp_path, responses)
    reconcile_operation(
        database=tmp_path / "operations.db", kind="model",
        operation_id="operation", request_sha256=row["request_sha256"],
        action="authorize_retry", **_attestation(),
    )
    recovered = transport.complete(**kwargs)
    assert recovered.request_id == "message"
    assert len(transport.http_calls) == 4
    assert list_operations(database=tmp_path / "operations.db", kind="model") == []


def test_abandon_is_terminal_and_second_reconciliation_is_rejected(tmp_path):
    transport, kwargs, row = _pending_model(tmp_path)
    reconcile_operation(
        database=tmp_path / "operations.db", kind="model",
        operation_id="operation", request_sha256=row["request_sha256"],
        action="abandon", **_attestation(),
    )
    with pytest.raises(IndeterminateModelOperation, match="ABANDONED"):
        transport.complete(**kwargs)
    with pytest.raises(OperationRecoveryError, match="only PENDING"):
        reconcile_operation(
            database=tmp_path / "operations.db", kind="model",
            operation_id="operation", request_sha256=row["request_sha256"],
            action="abandon", **_attestation(),
        )


class FailingRetrievalClient:
    def agentic_search(self, *args, **kwargs):
        from src.tools.sciverse import SciverseError
        raise SciverseError("lost")

    def content(self, *args, **kwargs):
        raise AssertionError("not used")


def test_retrieval_response_can_be_reconciled_and_replayed(tmp_path):
    database = tmp_path / "retrieval.db"
    transport = CachedSciverseTransport(
        client=FailingRetrievalClient(), operation_db=database,
    )
    kwargs = {
        "operation_id": "search-op", "query": "query", "top_k": 2,
        "filters": {"year": 2020},
    }
    with pytest.raises(IndeterminateRetrievalOperation):
        transport.search(**kwargs)
    row = list_operations(database=database, kind="retrieval")[0]
    response = [{"doc_id": "doc", "offset": 1}]
    reconcile_operation(
        database=database, kind="retrieval", operation_id="search-op",
        request_sha256=row["request_sha256"], action="complete", response=response,
        **_attestation(),
    )
    assert transport.search(**kwargs) == response


def test_reconciliation_rejects_wrong_request_hash_and_missing_receipt(tmp_path):
    _, _, row = _pending_model(tmp_path)
    with pytest.raises(OperationRecoveryError, match="does not match"):
        reconcile_operation(
            database=tmp_path / "operations.db", kind="model",
            operation_id="operation", request_sha256="0" * 64,
            action="abandon", **_attestation(),
        )
    invalid = _attestation()
    invalid["evidence_receipt_sha256"] = "not-a-hash"
    with pytest.raises(OperationRecoveryError, match="receipt"):
        reconcile_operation(
            database=tmp_path / "operations.db", kind="model",
            operation_id="operation", request_sha256=row["request_sha256"],
            action="abandon", **invalid,
        )
