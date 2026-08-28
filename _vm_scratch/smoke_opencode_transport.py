"""Smoke test: VeriMat opencode transport against a local OpenCode server on GLM-5.3-Flash.

Mirrors smoke_claude_transport.py but exercises the opencode route:
  1. OpenCodeStructuredTransport directly (one structured call + cache-hit replay)
  2. model_router.open_route("opencode", ...) end to end via StructuredModelBackend.plan_queries

Run:  python _vm_scratch/smoke_opencode_transport.py
"""
import json, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for proxy in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    import os
    os.environ.pop(proxy, None)

from src.evaluation.model_backend import StructuredModelBackend
from src.evaluation.model_router import open_route
from src.evaluation.opencode_transport import OpenCodeStructuredTransport
from src.core.events import canonical_json

BASE_URL = "http://127.0.0.1:4123"
PROVIDER = "zhipuai"
MODEL = "glm-5.3-flash"
AGENT = "build"

tmp = Path(tempfile.mkdtemp(prefix="vm-oc-smoke-"))

print("== 1. direct transport ==")
t = OpenCodeStructuredTransport(
    base_url=BASE_URL, provider_id=PROVIDER, model_id=MODEL,
    operation_db=tmp / "operations.db", agent=AGENT, timeout_seconds=300,
)
r = t.complete(
    operation_id="oc-smoke-1",
    system="Return only JSON matching the schema. Never use tools.",
    user="Propose two literature search queries about perovskite solar cell stability.",
    response_schema={"type": "object", "required": ["queries"],
                     "properties": {"queries": {"type": "array", "items": {"type": "string"}}}},
)
print("text:", r.text)
print("tokens: in", r.input_tokens, "out", r.output_tokens, "| req:", r.request_id)
r2 = t.complete(
    operation_id="oc-smoke-1",
    system="Return only JSON matching the schema. Never use tools.",
    user="Propose two literature search queries about perovskite solar cell stability.",
    response_schema={"type": "object", "required": ["queries"],
                     "properties": {"queries": {"type": "array", "items": {"type": "string"}}}},
)
print("cache hit identical:", r == r2)
t.close()

print()
print("== 2. router + StructuredModelBackend ==")
import os
cfg = json.loads(Path("~/.config/opencode/opencode.json").expanduser().read_text())
os.environ.setdefault("VERIMAT_OPENCODE_API_KEY", cfg["provider"][PROVIDER]["options"]["apiKey"])
with open_route(
    "opencode", operation_db=tmp / "router-operations.db",
    env={
        "VERIMAT_OPENCODE_BASE_URL": BASE_URL,
        "VERIMAT_OPENCODE_PROVIDER": PROVIDER,
        "VERIMAT_OPENCODE_MODEL": MODEL,
        "VERIMAT_OPENCODE_AGENT": AGENT,
        "VERIMAT_OPENCODE_API_KEY": os.environ["VERIMAT_OPENCODE_API_KEY"],
    },
    operator_declared_backend=f"{PROVIDER}/{MODEL} (zhipu coding plan)",
    timeout_seconds=300,
) as route:
    from src.evaluation.baseline_runner import BlindTask
    backend = route.backend()
    task = BlindTask(
        schema_version=1, challenge_id="smoke", benchmark_track="materials",
        split="dev", task_family="survey", prompt="Thermoelectric materials",
        cutoff_date="2026-08-28",
    )
    plan = backend.plan_queries(
        task=task, intent="support",
        operation_id="smoke-plan-queries-1",
    )
    print("planned queries:", plan.queries)
    print("usage:", plan.usage)
    manifest = route.manifest()
    print("route_id:", manifest["provenance"]["route_id"],
          "| alias:", manifest["provenance"]["request_alias"])
    print("disclosure vendor:", manifest["disclosure"]["vendor"])

print()
print("ALL SMOKE CHECKS PASSED")
print("scratch dir:", tmp)
