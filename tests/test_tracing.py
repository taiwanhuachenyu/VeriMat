import json
import os

import pytest

from src.core.portability import FILE_MODE_ENFORCED
from src.service.tracing import HttpTraceRecord, StructuredTraceLog, TraceContext


def test_invalid_or_zero_traceparent_generates_new_nonzero_context():
    for value in ("", "not-a-trace", "00-" + "0" * 32 + "-" + "0" * 16 + "-01"):
        context = TraceContext.from_header(value)
        assert context.trace_id != "0" * 32 and context.span_id != "0" * 16
        assert context.parent_span_id == ""


def test_structured_trace_log_is_0600_append_only_and_exact_schema(tmp_path):
    path = tmp_path / "trace.jsonl"
    sink = StructuredTraceLog(path)
    context = TraceContext.from_header(
        "00-0123456789abcdef0123456789abcdef-0123456789abcdef-00"
    )
    record = HttpTraceRecord.build(
        context=context, request_id="request", method="GET", route="healthz",
        status=200, duration_seconds=0.012,
    )
    sink.record(record)
    sink.record(record)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2 and rows[0] == rows[1]
    assert rows[0]["request_id_sha256"] != "request"
    if FILE_MODE_ENFORCED:
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_trace_log_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("")
    link = tmp_path / "trace"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        StructuredTraceLog(link)
