"""Recover the Sciverse endpoint contracts empirically; the deployment serves no OpenAPI spec.

Raw responses land in _vm_scratch/sciverse_probe/ so the console never has to encode them
(this terminal is GBK).  Only ASCII-safe structural summaries are printed.
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "_vm_scratch" / "sciverse_probe"
OUT.mkdir(parents=True, exist_ok=True)

for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

TOKEN = os.environ["SCIVERSE_API_TOKEN"]
BASE = os.environ.get("SCIVERSE_BASE_URL", "https://api.sciverse.space").rstrip("/")


def fetch(path, method="POST", body=None, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001
        return None, json.dumps({"transport_error": f"{type(error).__name__}: {error}"})


import urllib.parse  # noqa: E402  (after fetch so the helper reads top-down)


def shape(value, depth=0, key=""):
    """An ASCII-only description of a JSON value's structure."""
    pad = "  " * depth
    if isinstance(value, dict):
        lines = [f"{pad}{key}{'{' } dict, {len(value)} keys"]
        for name, item in list(value.items())[:40]:
            lines += shape(item, depth + 1, f"{name}: ")
        return lines
    if isinstance(value, list):
        head = f"{pad}{key}[list, len={len(value)}"
        if not value:
            return [head + ", empty]"]
        return [head + "] first element:"] + shape(value[0], depth + 1)
    if isinstance(value, str):
        return [f"{pad}{key}str(len={len(value)}) "
                + (repr(value[:60]) if value.isascii() else "<non-ascii>")]
    return [f"{pad}{key}{type(value).__name__} = {value!r}"]


PROBES = [
    ("01-agentic-empty", "POST", "/agentic-search", {}, None),
    ("02-meta-empty", "POST", "/meta-search", {}, None),
    ("03-content-empty", "GET", "/content", None, None),
    ("04-agentic-wrong-types", "POST", "/agentic-search",
     {"query": "lithium cathode", "top_k": "x", "limit": "x", "size": "x",
      "k": "x", "sub_queries": "x", "filters": "x", "offset": "x",
      "page": "x", "page_no": "x", "sort": 1, "fields": 1}, None),
    ("05-meta-wrong-types", "POST", "/meta-search",
     {"query": "lithium cathode", "top_k": "x", "limit": "x", "size": "x",
      "k": "x", "filters": "x", "offset": "x", "page": "x", "page_no": "x",
      "sort": 1, "sort_by": 1, "order": 1, "fields": 1}, None),
    ("06-agentic-topk-huge", "POST", "/agentic-search",
     {"query": "lithium cathode", "top_k": 100000}, None),
    ("07-agentic-topk-zero", "POST", "/agentic-search",
     {"query": "lithium cathode", "top_k": 0}, None),
    ("08-meta-topk-huge", "POST", "/meta-search",
     {"query": "lithium cathode", "top_k": 100000}, None),
    ("09-meta-topk-zero", "POST", "/meta-search",
     {"query": "lithium cathode", "top_k": 0}, None),
    ("10-meta-unknown-field", "POST", "/meta-search",
     {"query": "lithium cathode", "definitely_not_a_field": 1}, None),
    ("11-meta-minimal", "POST", "/meta-search", {"query": "lithium cathode"}, None),
    ("12-agentic-minimal", "POST", "/agentic-search",
     {"query": "lithium cathode", "top_k": 2}, None),
]

summary = {}
for label, method, path, body, params in PROBES:
    status, text = fetch(path, method, body, params)
    (OUT / f"{label}.json").write_text(text, encoding="utf-8")
    print(f"=== {label}  [{method} {path}] -> HTTP {status}  ({len(text)} bytes)")
    try:
        parsed = json.loads(text)
    except ValueError:
        print("    <not json>")
        continue
    summary[label] = {"status": status, "top_level_keys": sorted(parsed)
                      if isinstance(parsed, dict) else type(parsed).__name__}
    for line in shape(parsed)[:60]:
        print("    " + line)
    print()

(OUT / "summary.json").write_text(
    json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
print("raw responses in", OUT)
