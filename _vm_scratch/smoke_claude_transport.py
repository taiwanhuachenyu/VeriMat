import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.claude_code_transport import ClaudeCodeStructuredTransport
from src.evaluation.model_backend import PLAN_SCHEMA

tmp = Path(tempfile.mkdtemp(prefix="vm-smoke-"))
t = ClaudeCodeStructuredTransport(
    operation_db=tmp / "operations.db",
    usage_log=tmp / "usage.jsonl",
)
print("cli:", t.cli_path)
print("cmd:", t._command(system="S", response_schema=PLAN_SCHEMA))

r = t.complete(
    operation_id="smoke-1",
    system="Return only JSON matching the schema. Never use tools.",
    user="Propose two literature search queries about perovskite solar cell stability.",
    response_schema=PLAN_SCHEMA,
)
print("text:", r.text)
print("tokens:", r.input_tokens, r.output_tokens, "req:", r.request_id)
print("backends:", sorted(t.observed_backends))

r2 = t.complete(
    operation_id="smoke-1",
    system="Return only JSON matching the schema. Never use tools.",
    user="Propose two literature search queries about perovskite solar cell stability.",
    response_schema=PLAN_SCHEMA,
)
print("cache hit identical:", r == r2)
print("usage log:", (tmp / "usage.jsonl").read_text())
t.close()
