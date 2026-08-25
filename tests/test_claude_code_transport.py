import json
import subprocess

import pytest

from src.evaluation.baseline_runner import BaselineContractError
from src.evaluation.claude_code_transport import ClaudeCodeStructuredTransport
from src.evaluation.opencode_transport import IndeterminateModelOperation

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {"queries": {}}}


def _envelope(**overrides):
    structured = overrides.pop("structured_output", {"queries": ["a query"]})
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(structured),
        "structured_output": structured,
        "usage": {
            "input_tokens": 11, "output_tokens": 7,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        },
        "modelUsage": {"claude-opus-5": {"costUSD": 0.01}},
        "total_cost_usd": 0.01,
        "uuid": "envelope-uuid",
        "session_id": "session",
        "duration_ms": 100,
        "num_turns": 2,
        "permission_denials": [],
    }
    payload.update(overrides)
    return payload


class FakeTransport(ClaudeCodeStructuredTransport):
    """Replace only the subprocess boundary, so durability logic runs for real."""

    def __init__(self, tmp_path, responses, **kwargs):
        self.responses = list(responses)
        self.invocations = []
        super().__init__(
            operation_db=tmp_path / "operations.db",
            cli_path="fake-claude",
            usage_log=tmp_path / "usage.jsonl",
            **kwargs,
        )

    def _invoke(self, *, system, user, response_schema):
        self.invocations.append({
            "command": self._command(system=system, response_schema=response_schema),
            "stdin": user,
        })
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _call(transport, **overrides):
    kwargs = {
        "operation_id": "operation", "system": "system", "user": "user",
        "response_schema": SCHEMA,
    }
    kwargs.update(overrides)
    return transport.complete(**kwargs)


def test_transport_caches_one_structured_call(tmp_path):
    transport = FakeTransport(tmp_path, [_envelope()])
    first = _call(transport)
    second = _call(transport)
    assert first == second
    assert first.input_tokens == 11 and first.output_tokens == 7
    assert first.request_id == "envelope-uuid"
    assert json.loads(first.text) == {"queries": ["a query"]}
    assert len(transport.invocations) == 1


def test_prompt_travels_on_stdin_and_never_on_the_command_line(tmp_path):
    """A decision context can exceed the Windows command-line limit, so argv must stay short."""
    prompt = "material evidence " * 4000
    transport = FakeTransport(tmp_path, [_envelope()])
    _call(transport, user=prompt)
    command, stdin = transport.invocations[0]["command"], transport.invocations[0]["stdin"]
    assert stdin == prompt
    assert prompt not in command
    assert len(" ".join(command)) < 32_767


def test_command_suppresses_tools_and_pins_a_reproducible_system_prompt(tmp_path):
    transport = FakeTransport(tmp_path, [_envelope()])
    _call(transport)
    command = transport.invocations[0]["command"]
    assert "--exclude-dynamic-system-prompt-sections" in command
    assert command[command.index("--system-prompt") + 1] == "system"
    disallowed = command[command.index("--disallowedTools") + 1:]
    assert set(ClaudeCodeStructuredTransport.DISALLOWED_TOOLS) == set(disallowed)
    assert not any(item.startswith("-") for item in disallowed), (
        "a later flag was swallowed by the variadic tool list"
    )


def test_session_default_route_sends_no_model_override(tmp_path):
    transport = FakeTransport(tmp_path, [_envelope()])
    _call(transport)
    assert "--model" not in transport.invocations[0]["command"]


def test_explicit_model_alias_is_forwarded(tmp_path):
    transport = FakeTransport(tmp_path, [_envelope()], model="claude-opus-5")
    _call(transport)
    command = transport.invocations[0]["command"]
    assert command[command.index("--model") + 1] == "claude-opus-5"


def test_cache_tokens_are_counted_as_billed_input(tmp_path):
    envelope = _envelope()
    envelope["usage"] = {
        "input_tokens": 100, "output_tokens": 5,
        "cache_creation_input_tokens": 50_000, "cache_read_input_tokens": 700,
    }
    transport = FakeTransport(tmp_path, [envelope])
    assert _call(transport).input_tokens == 50_800


def test_operation_semantic_conflict_is_rejected_without_another_call(tmp_path):
    transport = FakeTransport(tmp_path, [_envelope()])
    _call(transport)
    with pytest.raises(BaselineContractError, match="different request semantics"):
        _call(transport, system="changed")
    assert len(transport.invocations) == 1


def test_indeterminate_paid_call_is_never_retried_automatically(tmp_path):
    transport = FakeTransport(tmp_path, [
        IndeterminateModelOperation("killed after the request was sent"),
        _envelope(),
    ])
    with pytest.raises(IndeterminateModelOperation):
        _call(transport)
    with pytest.raises(IndeterminateModelOperation, match="PENDING"):
        _call(transport)
    assert len(transport.invocations) == 1


def test_a_requested_tool_permission_fails_the_tool_free_call(tmp_path):
    transport = FakeTransport(tmp_path, [
        _envelope(permission_denials=[{"tool_name": "Bash"}]),
    ])
    with pytest.raises(IndeterminateModelOperation, match="tool permission"):
        _call(transport)


def test_missing_structured_output_is_refused_rather_than_parsed_from_prose(tmp_path):
    envelope = _envelope()
    envelope.pop("structured_output")
    envelope["result"] = "Here are some queries: perovskite stability"
    transport = FakeTransport(tmp_path, [envelope])
    with pytest.raises(IndeterminateModelOperation, match="no structured object"):
        _call(transport)


def test_missing_usage_metadata_is_refused_rather_than_estimated(tmp_path):
    envelope = _envelope()
    envelope["usage"] = {"output_tokens": 7}
    transport = FakeTransport(tmp_path, [envelope])
    with pytest.raises(IndeterminateModelOperation, match="usage metadata"):
        _call(transport)


def test_error_envelope_is_surfaced(tmp_path):
    transport = FakeTransport(tmp_path, [
        _envelope(is_error=True, result="rate limited"),
    ])
    with pytest.raises(IndeterminateModelOperation, match="rate limited"):
        _call(transport)


def test_usage_log_records_billing_facts_and_never_the_prompt(tmp_path):
    transport = FakeTransport(tmp_path, [_envelope()])
    _call(transport, user="a secret hypothesis about a proprietary alloy")
    lines = (tmp_path / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["total_cost_usd"] == 0.01
    assert record["served_by"] == ["claude-opus-5"]
    assert record["input_tokens"] == 11 and record["output_tokens"] == 7
    assert record["operation_id"] == "operation"
    assert len(record["request_sha256"]) == 64
    assert "proprietary alloy" not in lines[0]


def test_usage_log_is_append_only_across_operations(tmp_path):
    transport = FakeTransport(tmp_path, [_envelope(), _envelope(uuid="second")])
    _call(transport, operation_id="first")
    _call(transport, operation_id="second")
    raw = (tmp_path / "usage.jsonl").read_bytes()
    assert raw.count(b"\n") == 2
    assert b"\r\n" not in raw, "the usage log must be byte-identical across platforms"


def test_timeout_is_reported_as_indeterminate_not_as_a_failed_call(tmp_path, monkeypatch):
    transport = ClaudeCodeStructuredTransport(
        operation_db=tmp_path / "operations.db", cli_path="fake-claude", timeout_seconds=1,
    )

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="fake-claude", timeout=1)

    monkeypatch.setattr("src.evaluation.claude_code_transport.subprocess.run", timeout)
    with pytest.raises(IndeterminateModelOperation, match="may already have been charged"):
        _call(transport)
    transport.close()


def test_non_json_output_is_indeterminate(tmp_path, monkeypatch):
    transport = ClaudeCodeStructuredTransport(
        operation_db=tmp_path / "operations.db", cli_path="fake-claude",
    )
    monkeypatch.setattr(
        "src.evaluation.claude_code_transport.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json at all", stderr="",
        ),
    )
    with pytest.raises(IndeterminateModelOperation, match="JSON result envelope"):
        _call(transport)
    transport.close()


def test_a_missing_cli_is_refused_at_construction(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.evaluation.claude_code_transport.shutil.which", lambda _name: None,
    )
    with pytest.raises(ValueError, match="claude CLI was not found"):
        ClaudeCodeStructuredTransport(operation_db=tmp_path / "operations.db")


def test_invalid_bounds_are_refused(tmp_path):
    with pytest.raises(ValueError, match="bounds are invalid"):
        ClaudeCodeStructuredTransport(
            operation_db=tmp_path / "operations.db", cli_path="fake-claude",
            timeout_seconds=0,
        )


def test_indeterminate_envelope_is_audited_without_committing_operation(tmp_path):
    envelope = _envelope()
    envelope.pop("structured_output")
    transport = FakeTransport(
        tmp_path, [envelope], request_response_log=tmp_path / "requests.jsonl",
    )
    with pytest.raises(IndeterminateModelOperation, match="no structured object"):
        _call(transport)
    records = [json.loads(line) for line in (
        tmp_path / "requests.jsonl"
    ).read_text(encoding="utf-8").splitlines()]
    assert records == [
        {
            **records[0],
            "status": "INDETERMINATE",
            "operation_id": "operation",
            "result_envelope": envelope,
        },
    ]
    assert records[0]["response_text"] is None
    with pytest.raises(IndeterminateModelOperation, match="PENDING"):
        _call(transport)
    assert len(transport.invocations) == 1
