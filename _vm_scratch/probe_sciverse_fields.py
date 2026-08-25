"""Recover the exact accepted field set and nested shapes for each Sciverse endpoint.

meta-search rejects unknown fields with ``extra_forbidden`` and names each one, so a single
request carrying many candidates partitions them: the names the error omits are the real ones.
agentic-search answers with an opaque envelope, so it is probed one field at a time with the
smallest top_k that still returns, to keep the token spend down.
"""
import json
import os
import urllib.error
import urllib.parse
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
TOKENS_SPENT = {"request": 0, "response": 0, "calls": 0}


def fetch(path, method="POST", body=None, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json",
                 "Content-Type": "application/json"},
    )
    TOKENS_SPENT["calls"] += 1
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            status, text = response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001
        return None, {"transport_error": f"{type(error).__name__}: {error}"}
    try:
        parsed = json.loads(text)
    except ValueError:
        return status, {"unparsable": text[:400]}
    if isinstance(parsed, dict):
        TOKENS_SPENT["request"] += parsed.get("request_tokens") or 0
        TOKENS_SPENT["response"] += parsed.get("response_tokens") or 0
    return status, parsed


def rejected_and_typed(parsed):
    """Split a validation error into (unknown field names, {known field: expected type})."""
    unknown, typed = set(), {}
    for item in (parsed.get("details") or []):
        location = [str(part) for part in item.get("loc", [])]
        name = ".".join(location[1:]) or ".".join(location)
        if item.get("type") == "extra_forbidden":
            unknown.add(name)
        else:
            typed[name] = f"{item.get('type')}: {item.get('msg')}"
    return unknown, typed


report = {}
line = "-" * 78

# ---------------------------------------------------------------- A: meta-search field set
CANDIDATES = [
    "query", "page", "page_size", "filters", "sort", "fields", "cursor", "next_cursor",
    "facets", "highlight", "include_facets", "return_facets", "aggs", "aggregations",
    "min_year", "max_year", "year", "year_from", "year_to", "doi", "doc_id", "unique_id",
    "lang", "language", "type", "metadata_type", "search_mode", "mode", "topics", "venue",
    "author", "keywords", "open_access", "access_is_oa", "citation_count", "sort_order",
    "operator", "must", "should", "exclude", "id", "ids", "total_count", "count",
    "sub_queries", "rerank", "expand", "explain", "timeout", "seed",
]
SENTINEL = {"__probe__": True}  # a dict type-errors on str/int/list fields
status, parsed = fetch("/meta-search", body={name: SENTINEL for name in CANDIDATES})
unknown, typed = rejected_and_typed(parsed)
accepted = sorted(set(CANDIDATES) - unknown)
print(f"A. meta-search  HTTP {status}")
print(f"   accepted fields ({len(accepted)}): {accepted}")
print(f"   rejected        ({len(unknown)}): {sorted(unknown)}")
for name in accepted:
    print(f"     - {name}: {typed.get(name, '<no type complaint: a dict is valid here>')}")
report["meta_search_accepted"] = accepted
report["meta_search_types"] = typed
report["meta_search_rejected"] = sorted(unknown)
print(line)

# ------------------------------------------------- B: nested shapes for filters / sort / fields
for field, probe_values in [
    ("filters", [[], [SENTINEL], ["x"], [{}], [1]]),
    ("sort", [[], [SENTINEL], ["x"], [{}]]),
    ("fields", [[], [SENTINEL], ["x"], [{}]]),
]:
    print(f"B. meta-search {field} element shape")
    for value in probe_values:
        status, parsed = fetch("/meta-search",
                               body={"query": "lithium cathode", field: value})
        if status == 200:
            note = (f"OK  results={len(parsed.get('results') or [])} "
                    f"total={parsed.get('total_count')}")
        else:
            _, detail = rejected_and_typed(parsed)
            note = json.dumps(detail or parsed, ensure_ascii=False)[:420]
        print(f"   {json.dumps(value)[:60]:<62} -> {status}  {note}")
    print(line)

# ---------------------------------------------------- C: agentic-search strictness + fields
print("C. agentic-search field probes (top_k=1)")
BASELINE = {"query": "lithium cathode", "top_k": 1}
agentic = {}
for name, value in [
    ("__definitely_not_a_field__", 1), ("sub_queries", 1), ("filters", {}),
    ("filters_as_list", None), ("page", 1), ("page_size", 1), ("offset", 0),
    ("rerank", True), ("fields", ["title"]), ("sort", []), ("cursor", ""),
]:
    body = dict(BASELINE)
    if name == "filters_as_list":
        body["filters"] = []
    else:
        body[name] = value
    status, parsed = fetch("/agentic-search", body=body)
    code = (parsed.get("error") or {}).get("code") or parsed.get("code")
    hits = len(parsed.get("hits") or []) if status == 200 else None
    agentic[name] = {"status": status, "code": code, "hits": hits}
    print(f"   {name:<28} -> {status} {code} hits={hits}")
report["agentic_field_probes"] = agentic
print(line)

# ---------------------------------------------------------------- D: agentic top_k bounds
print("D. agentic-search top_k bounds")
bounds = {}
for value in (1, 10, 20, 30, 50, 51, 100, 101, 200, 1000):
    status, parsed = fetch("/agentic-search", body={"query": "lithium cathode", "top_k": value})
    code = (parsed.get("error") or {}).get("code") or parsed.get("code")
    hits = len(parsed.get("hits") or []) if status == 200 else None
    bounds[value] = {"status": status, "code": code, "hits": hits}
    print(f"   top_k={value:<6} -> {status} {code} hits={hits}")
    if status != 200 and code == "INVALID_TOP_K":
        break
report["agentic_top_k"] = bounds
print(line)

# ------------------------------------------------------------------------ E: content params
print("E. content parameters")
status, parsed = fetch("/agentic-search", body={"query": "lithium cathode", "top_k": 1})
seed = (parsed.get("hits") or [{}])[0]
doc_id, offset = seed.get("doc_id"), seed.get("offset")
print(f"   seed doc_id={str(doc_id)[:16]}... offset={offset}")
content = {}
for label, params in [
    ("doc_id only", {"doc_id": doc_id}),
    ("doc_id+offset+limit", {"doc_id": doc_id, "offset": offset, "limit": 512}),
    ("limit huge", {"doc_id": doc_id, "offset": 0, "limit": 10 ** 7}),
    ("negative offset", {"doc_id": doc_id, "offset": -1, "limit": 512}),
    ("unknown param", {"doc_id": doc_id, "definitely_not_a_param": 1}),
    ("bad doc_id", {"doc_id": "0" * 64}),
]:
    status, parsed = fetch("/content", method="GET", params=params)
    keys = sorted(parsed) if isinstance(parsed, dict) else type(parsed).__name__
    text = parsed.get("text") if isinstance(parsed, dict) else None
    code = (parsed.get("error") or {}).get("code") or parsed.get("code")
    content[label] = {"status": status, "code": code, "keys": keys,
                      "text_len": len(text) if isinstance(text, str) else None}
    print(f"   {label:<22} -> {status} {code} text_len="
          f"{len(text) if isinstance(text, str) else None} keys={keys}")
report["content"] = content
print(line)

print(f"probe cost: {TOKENS_SPENT['calls']} calls, "
      f"{TOKENS_SPENT['request']} request tokens, {TOKENS_SPENT['response']} response tokens")
(OUT / "field_report.json").write_text(
    json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print("saved", OUT / "field_report.json")
