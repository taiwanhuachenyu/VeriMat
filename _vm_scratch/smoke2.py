import sys, tempfile, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
print("step1 import", flush=True)
from src.evaluation.claude_code_transport import ClaudeCodeStructuredTransport
from src.evaluation.model_backend import PLAN_SCHEMA
print("step2 imported", flush=True)
tmp = Path(tempfile.mkdtemp(prefix="vm-smoke-"))
t = ClaudeCodeStructuredTransport(operation_db=tmp / "operations.db", usage_log=tmp / "usage.jsonl")
print("step3 constructed:", t.cli_path, flush=True)
print("step4 cmd:", t._command(system="S", response_schema=PLAN_SCHEMA), flush=True)
try:
    payload = t._invoke(system="Return only JSON. Never use tools.",
                        user="Propose two literature search queries about perovskite stability.",
                        response_schema=PLAN_SCHEMA)
    print("step5 invoke ok, keys:", sorted(payload)[:8], flush=True)
    resp, acc = t._decode(payload)
    print("step6 decoded:", resp.text, resp.input_tokens, resp.output_tokens, flush=True)
    print("step7 accounting:", acc, flush=True)
except Exception:
    traceback.print_exc()
    sys.stdout.flush()
t.close()
print("step8 done", flush=True)
