"""The last open questions, all of which change the shape of the code.

1. SciverseBenchmarkRetriever promises "no evidence published after the cutoff".  That promise
   currently rests on an agentic-search `filters` block the spec calls *soft*.  Observe the years
   actually returned rather than trusting the absence of an error.
2. meta-search never returned `doc_id` in round 2, so the "pin candidates with meta-search, then
   scope agentic-search by doc_id" bridge may not exist.  Find out what `is_content_accessible`
   really means and whether `doc_id` ever comes back.
3. /content bounds against a real doc_id (round 2 accidentally probed a fake one).
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
SPENT = {"calls": 0}


def fetch(path, method="POST", body=None, params=None):
    url = f"{BASE}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json",
                 "Content-Type": "application/json"},
    )
    SPENT["calls"] += 1
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
            ctype = response.headers.get("Content-Type", "")
            if "json" not in ctype:
                return response.status, {"__binary__": len(raw), "content_type": ctype}
            return response.status, json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw[:300]}
    except Exception as error:  # noqa: BLE001
        return None, {"transport_error": f"{type(error).__name__}: {error}"}


def why(parsed):
    if "details" in parsed:
        return "; ".join(f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg')}"
                         for d in parsed["details"])[:300]
    error = parsed.get("error") or {}
    return f"{error.get('code') or parsed.get('code')}: {error.get('message') or parsed.get('message')}"


report = {}
RULE = "-" * 78
QUERY = "lithium ion battery cathode"

print("A. agentic-search year filter: is the publication cutoff a real guarantee?")
for label, filters in {
    "none": None,
    "lte 2000": {"publication_published_year": {"lte": 2000}},
    "lte 1990": {"publication_published_year": {"lte": 1990}},
    "gte 2023": {"publication_published_year": {"gte": 2023}},
    "range 2005-2010": {"publication_published_year": {"gte": 2005, "lte": 2010}},
    "bogus field": {"__no_such_field__": {"lte": 2000}},
    "lang zz": {"lang": "zz"},
}.items():
    body = {"query": QUERY, "top_k": 30}
    if filters is not None:
        body["filters"] = filters
    status, parsed = fetch("/agentic-search", body=body)
    hits = parsed.get("hits") or []
    got = [int(float(h["publication_published_year"])) for h in hits
           if isinstance(h.get("publication_published_year"), (int, float))]
    missing = sum(1 for h in hits if h.get("publication_published_year") in (None, ""))
    note = (f"n={len(hits):<3} years={min(got) if got else None}..{max(got) if got else None} "
            f"year_missing={missing}" if status == 200 else why(parsed))
    print(f"   {label:<16} -> {status}  {note}")
    report[f"agentic_{label}"] = {"status": status, "n": len(hits),
                                  "year_min": min(got) if got else None,
                                  "year_max": max(got) if got else None,
                                  "year_missing": missing}
print(RULE)

print("B. meta-search: is_content_accessible, and does doc_id ever come back?")
status, parsed = fetch("/meta-search", body={"query": QUERY, "page_size": 25})
rows = parsed.get("results") or []
accessible = [r for r in rows if r.get("is_content_accessible")]
print(f"   plain n={len(rows)} accessible={len(accessible)} "
      f"with_doc_id={sum(1 for r in rows if r.get('doc_id'))}")
status, parsed = fetch("/meta-search", body={
    "query": QUERY, "page_size": 10,
    "filters": [{"field": "doc_id", "operator": "FILTER_OP_NE", "value": ""}]})
rows2 = parsed.get("results") or []
print(f"   filter doc_id NE '' -> {status} n={len(rows2)} "
      f"with_doc_id={sum(1 for r in rows2 if r.get('doc_id'))} "
      f"accessible={sum(1 for r in rows2 if r.get('is_content_accessible'))}"
      if status == 200 else f"   filter doc_id NE '' -> {status} {why(parsed)}")
# take a doc_id that agentic-search knows has full text, then look it up in meta-search
status, parsed = fetch("/agentic-search", body={"query": QUERY, "top_k": 3})
hits = parsed.get("hits") or []
real_doc_id = hits[0].get("doc_id") if hits else None
print(f"   agentic hit source={hits[0].get('source')!r} "
      f"recall_source={hits[0].get('recall_source')!r} "
      f"model={hits[0].get('model_name')!r}/{hits[0].get('model_version')!r}")
report["agentic_provenance"] = {
    "source": hits[0].get("source") if hits else None,
    "recall_source": hits[0].get("recall_source") if hits else None,
    "model_name": hits[0].get("model_name") if hits else None,
    "model_version": hits[0].get("model_version") if hits else None,
}
status, parsed = fetch("/meta-search", body={
    "query": "", "page_size": 3,
    "filters": [{"field": "doc_id", "value": real_doc_id}]})
rows3 = parsed.get("results") or []
print(f"   meta-search by doc_id={str(real_doc_id)[:12]}... -> {status} n={len(rows3)} "
      + (f"unique_id={rows3[0].get('unique_id')} doc_id_back={rows3[0].get('doc_id')} "
         f"accessible={rows3[0].get('is_content_accessible')}" if rows3 else why(parsed)))
report["meta_by_doc_id"] = {"status": status, "n": len(rows3),
                            "doc_id_returned": bool(rows3 and rows3[0].get("doc_id")),
                            "keys": sorted(rows3[0]) if rows3 else []}
if rows3:
    print(f"        keys: {sorted(rows3[0])}")
print(RULE)

print(f"C. /content bounds with a real doc_id ({str(real_doc_id)[:16]}...)")
for label, limit in [("4096", 4096), ("16384", 16384), ("16385", 16385), ("10^6", 10 ** 6)]:
    status, parsed = fetch("/content", method="GET",
                           params={"doc_id": real_doc_id, "offset": 0, "limit": limit})
    print(f"   limit={label:<7} -> {status} bytes={parsed.get('bytes_returned')} "
          f"more={parsed.get('more')} next_offset={parsed.get('next_offset')} "
          f"text_len={len(parsed.get('text') or '')}"
          + ("" if status == 200 else f"  {why(parsed)}"))
    report[f"content_{label}"] = {"status": status,
                                  "bytes_returned": parsed.get("bytes_returned"),
                                  "more": parsed.get("more"),
                                  "next_offset": parsed.get("next_offset"),
                                  "text_len": len(parsed.get("text") or "")}
status, first = fetch("/content", method="GET",
                      params={"doc_id": real_doc_id, "offset": 0, "limit": 2000})
status, second = fetch("/content", method="GET",
                       params={"doc_id": real_doc_id,
                               "offset": first.get("next_offset") or 0, "limit": 2000})
print(f"   continuation: page2 differs = "
      f"{(first.get('text') or '')[:60] != (second.get('text') or '')[:60]}  "
      f"next_offset {first.get('next_offset')} -> {second.get('next_offset')}")
status, parsed = fetch("/content", method="GET",
                       params={"doc_id": "0" * 64, "offset": 0, "limit": 512})
print(f"   unknown doc_id -> {status}  {why(parsed)}")
report["content_unknown_doc"] = {"status": status, "error": why(parsed)}
print(RULE)

print("D. /resource")
text = (first.get("text") or "")
marker = text.find("](")
ref = None
if marker != -1:
    ref = text[marker + 2: text.find(")", marker)]
print(f"   first image ref in markdown: {ref!r}")
if ref:
    status, parsed = fetch("/resource", method="GET", params={"file_name": ref})
    print(f"   -> {status} {parsed if status != 200 else parsed}")
status, parsed = fetch("/resource", method="GET", params={"file_name": "../etc/passwd"})
print(f"   traversal guard -> {status}  {why(parsed) if status != 200 else parsed}")
print(RULE)
print(f"{SPENT['calls']} calls")
(OUT / "catalog3.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
print("saved", OUT / "catalog3.json")
