"""Decide whether the filters actually filter, and recover the operator/sort vocabularies.

This matters beyond tidiness: SciverseBenchmarkRetriever states a publication cutoff guarantee
that rests entirely on agentic-search honouring a year filter.  agentic-search ignores unknown
fields silently, so the guarantee has to be demonstrated by observing the returned years, not
assumed from the absence of an error.
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
SPENT = {"calls": 0, "response_tokens": 0}


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
    SPENT["calls"] += 1
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
        SPENT["response_tokens"] += parsed.get("response_tokens") or 0
    return status, parsed


def why(parsed):
    if "details" in parsed:
        return "; ".join(
            f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}"
            for item in parsed["details"]
        )[:600]
    error = parsed.get("error") or {}
    return f"{error.get('code') or parsed.get('code')} {error.get('message') or parsed.get('message')}"


def years_of(hits, key="publication_published_year"):
    values = [h.get(key) for h in hits if isinstance(h, dict) and h.get(key)]
    numbers = [int(float(v)) for v in values if str(v).replace(".", "").isdigit()]
    return (min(numbers), max(numbers), len(numbers)) if numbers else (None, None, 0)


report = {}
RULE = "-" * 78
QUERY = "lithium cathode voltage"

# =============================================== 1. does agentic-search honour a year filter?
print("1. agentic-search year filter: is the cutoff guarantee real?")
trials = {
    "no filter": None,
    "dict lte 2000": {"publication_published_year": {"lte": 2000}},
    "dict lte 1995": {"publication_published_year": {"lte": 1995}},
    "dict gte 2020": {"publication_published_year": {"gte": 2020}},
    "dict bogus field": {"totally_made_up_field": {"lte": 2000}},
    "dict bogus operator": {"publication_published_year": {"nonsense": 2000}},
    "flat int": {"publication_published_year": 2000},
    "lang en": {"lang": "en"},
    "lang zz": {"lang": "zz"},
}
agentic_filters = {}
for label, filters in trials.items():
    body = {"query": QUERY, "top_k": 20}
    if filters is not None:
        body["filters"] = filters
    status, parsed = fetch("/agentic-search", body=body)
    hits = parsed.get("hits") or []
    low, high, counted = years_of(hits)
    langs = sorted({h.get("lang") for h in hits if isinstance(h, dict)})
    agentic_filters[label] = {"status": status, "hits": len(hits),
                              "year_min": low, "year_max": high, "langs": langs}
    note = (f"hits={len(hits):<3} years={low}..{high} (n={counted}) langs={langs}"
            if status == 200 else why(parsed))
    print(f"   {label:<22} -> {status}  {note}")
report["agentic_filters"] = agentic_filters
print(RULE)

# ============================================== 2. meta-search filter operator vocabulary
print("2. meta-search filters[].operator vocabulary (an enum names itself when violated)")
status, parsed = fetch("/meta-search", body={
    "query": QUERY,
    "filters": [{"field": "publication_published_year", "value": 2000,
                 "operator": "__not_a_real_operator__"}],
})
print(f"   invalid operator -> {status}  {why(parsed)}")
report["meta_operator_error"] = why(parsed)

status, parsed = fetch("/meta-search", body={
    "query": QUERY, "sort": [{"field": "citation_count", "order": "__nope__"}],
})
print(f"   invalid sort.order -> {status}  {why(parsed)}")
status2, parsed2 = fetch("/meta-search", body={
    "query": QUERY, "sort": [{"field": "citation_count", "direction": "__nope__"}],
})
print(f"   invalid sort.direction -> {status2}  {why(parsed2)}")
report["meta_sort_error"] = {"order": why(parsed), "direction": why(parsed2)}

status, parsed = fetch("/meta-search", body={"query": QUERY, "facets": [{}]})
print(f"   facets[{{}}] -> {status}  {why(parsed)}")
report["meta_facet_error"] = why(parsed)
print(RULE)

# ============================================== 3. does the meta-search filter actually filter?
print("3. meta-search filters: observed effect on the result set")
meta_filters = {}
for label, filters in {
    "none": [],
    "year lte 2000": [{"field": "publication_published_year", "operator": "lte", "value": 2000}],
    "year gte 2020": [{"field": "publication_published_year", "operator": "gte", "value": 2020}],
    "bogus field": [{"field": "__no_such_field__", "operator": "eq", "value": 1}],
}.items():
    body = {"query": QUERY, "page_size": 25}
    if filters:
        body["filters"] = filters
    status, parsed = fetch("/meta-search", body=body)
    results = parsed.get("results") or []
    low, high, counted = years_of(results)
    meta_filters[label] = {"status": status, "results": len(results),
                           "total_count": parsed.get("total_count"),
                           "year_min": low, "year_max": high}
    note = (f"n={len(results):<3} total={parsed.get('total_count'):<7} years={low}..{high}"
            if status == 200 else why(parsed))
    print(f"   {label:<18} -> {status}  {note}")
report["meta_filters"] = meta_filters
print(RULE)

# ====================================================== 4. meta-search paging and page_size
print("4. meta-search paging")
paging = {}
for label, body in {
    "page_size 5": {"query": QUERY, "page_size": 5},
    "page_size 100": {"query": QUERY, "page_size": 100},
    "page_size 500": {"query": QUERY, "page_size": 500},
    "page 2 size 5": {"query": QUERY, "page_size": 5, "page": 2},
    "page 0": {"query": QUERY, "page": 0},
    "page 401 size 25": {"query": QUERY, "page_size": 25, "page": 401},
}.items():
    status, parsed = fetch("/meta-search", body=body)
    results = parsed.get("results") or []
    first = (results[0] or {}).get("unique_id") if results else None
    paging[label] = {"status": status, "n": len(results),
                     "page": parsed.get("page"), "page_size": parsed.get("page_size"),
                     "total_pages": parsed.get("total_pages"),
                     "has_cursor": bool(parsed.get("next_cursor")),
                     "first_unique_id": first}
    note = (f"n={len(results):<4} page={parsed.get('page')} size={parsed.get('page_size')} "
            f"pages={parsed.get('total_pages')} first={str(first)[:34]}"
            if status == 200 else why(parsed))
    print(f"   {label:<18} -> {status}  {note}")
report["meta_paging"] = paging

# cursor continuation: does it advance past the page-based window?
status, first_page = fetch("/meta-search", body={"query": QUERY, "page_size": 5})
cursor = first_page.get("next_cursor")
ids_first = [r.get("unique_id") for r in (first_page.get("results") or [])]
status, second = fetch("/meta-search", body={"query": QUERY, "page_size": 5, "cursor": cursor})
ids_second = [r.get("unique_id") for r in (second.get("results") or [])]
overlap = set(ids_first) & set(ids_second)
print(f"   cursor continuation -> {status}  n={len(ids_second)} overlap_with_page1={len(overlap)}")
report["meta_cursor"] = {"status": status, "n": len(ids_second), "overlap": len(overlap)}
print(RULE)

# ============================================================= 5. content paging via next_offset
print("5. content paging via more/next_offset")
status, parsed = fetch("/agentic-search", body={"query": QUERY, "top_k": 1})
seed = (parsed.get("hits") or [{}])[0]
doc_id = seed.get("doc_id")
status, first = fetch("/content", method="GET",
                      params={"doc_id": doc_id, "offset": 0, "limit": 400})
print(f"   page1 -> {status} bytes_returned={first.get('bytes_returned')} "
      f"more={first.get('more')} next_offset={first.get('next_offset')} "
      f"text_len={len(first.get('text') or '')}")
status, second = fetch("/content", method="GET",
                       params={"doc_id": doc_id, "offset": first.get("next_offset"),
                               "limit": 400})
print(f"   page2 -> {status} bytes_returned={second.get('bytes_returned')} "
      f"more={second.get('more')} next_offset={second.get('next_offset')} "
      f"text_len={len(second.get('text') or '')}")
distinct = (first.get("text") or "")[:80] != (second.get("text") or "")[:80]
print(f"   page2 text differs from page1: {distinct}")
report["content_paging"] = {
    "page1": {k: first.get(k) for k in ("bytes_returned", "more", "next_offset")},
    "page2": {k: second.get(k) for k in ("bytes_returned", "more", "next_offset")},
    "distinct": distinct,
}
print(RULE)

print(f"probe cost: {SPENT['calls']} calls, {SPENT['response_tokens']} response tokens")
(OUT / "filter_report.json").write_text(
    json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print("saved", OUT / "filter_report.json")
