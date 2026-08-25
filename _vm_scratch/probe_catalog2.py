"""Finish the contract: does `filters` filter, does `sort` sort, and what does relations return?

The first pass died because `is_content_accessible` is server-injected and is rejected inside a
`fields` projection.  Everything downstream of that needed a real `unique_id`, so it is re-run here.
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
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw[:400]}
    except Exception as error:  # noqa: BLE001
        return None, {"transport_error": f"{type(error).__name__}: {error}"}


def why(parsed):
    if "details" in parsed:
        return "; ".join(f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg')}"
                         for d in parsed["details"])[:400]
    error = parsed.get("error") or {}
    return f"{error.get('code') or parsed.get('code')}: {error.get('message') or parsed.get('message')}"


def years(rows):
    got = [int(float(r["publication_published_year"])) for r in rows
           if isinstance(r.get("publication_published_year"), (int, float))]
    return (min(got), max(got), len(got)) if got else (None, None, 0)


report = {}
RULE = "-" * 78
QUERY = "high nickel layered cathode cycling stability"

print("1. meta-search: projection, filters, sort")
probes = {
    "plain": {"query": QUERY, "page_size": 5},
    "fields projection": {"query": QUERY, "page_size": 5,
                          "fields": ["unique_id", "doc_id", "title", "doi",
                                     "publication_published_year", "citation_count"]},
    "filter gte 2020": {"query": QUERY, "page_size": 5, "filters": [
        {"field": "publication_published_year", "operator": "FILTER_OP_GTE", "value": 2020}]},
    "filter lte 2005": {"query": QUERY, "page_size": 5, "filters": [
        {"field": "publication_published_year", "operator": "FILTER_OP_LTE", "value": 2005}]},
    "filter short op": {"query": QUERY, "page_size": 5, "filters": [
        {"field": "publication_published_year", "operator": "GTE", "value": 2020}]},
    "filter no op": {"query": QUERY, "page_size": 5, "filters": [
        {"field": "language", "value": "en"}]},
    "filter bogus field": {"query": QUERY, "page_size": 5, "filters": [
        {"field": "__no_such_field__", "value": 1}]},
    "sort cites desc": {"query": QUERY, "page_size": 5, "sort": [
        {"field": "citation_count", "order": "SORT_ORDER_DESC"}]},
    "sort short order": {"query": QUERY, "page_size": 5, "sort": [
        {"field": "citation_count", "order": "DESC"}]},
    "sort bogus field": {"query": QUERY, "page_size": 5, "sort": [
        {"field": "title", "order": "SORT_ORDER_DESC"}]},
    "page_size 50": {"query": QUERY, "page_size": 50},
    "page_size 51": {"query": QUERY, "page_size": 51},
    "page_size 200": {"query": QUERY, "page_size": 200},
    "empty query": {"query": "", "page_size": 3, "filters": [
        {"field": "publication_published_year", "operator": "FILTER_OP_GTE", "value": 2024}]},
}
seed_unique_id = None
seed_doc_id = None
for label, body in probes.items():
    status, parsed = fetch("/meta-search", body=body)
    rows = parsed.get("results") or []
    if status != 200:
        print(f"   {label:<20} -> {status}  {why(parsed)}")
        report[label] = {"status": status, "error": why(parsed)}
        continue
    low, high, n = years(rows)
    cites = [r.get("citation_count") for r in rows]
    keys = sorted(rows[0]) if rows else []
    print(f"   {label:<20} -> 200  n={len(rows):<3} total={parsed.get('total_count'):<6} "
          f"years={low}..{high} cites={cites[:5]}")
    report[label] = {"status": 200, "n": len(rows), "total_count": parsed.get("total_count"),
                     "year_min": low, "year_max": high, "citation_counts": cites[:5],
                     "keys": keys}
    if label == "plain":
        print(f"        keys: {keys}")
        report["plain_keys"] = keys
        for row in rows:
            if row.get("unique_id") and not seed_unique_id:
                seed_unique_id = row["unique_id"]
            if row.get("doc_id") and not seed_doc_id:
                seed_doc_id = row["doc_id"]
    if label == "fields projection":
        print(f"        projected keys: {keys}")
print(RULE)

print(f"2. meta-paper-relations  seed={seed_unique_id}")
for relation in ("REFERENCES", "CITATIONS", "RELATED_WORKS"):
    status, parsed = fetch("/meta-paper-relations", body={
        "unique_id": seed_unique_id, "relation": relation, "page": 1, "page_size": 5})
    if status != 200:
        print(f"   {relation:<14} -> {status}  {why(parsed)}")
        report[f"rel_{relation}"] = {"status": status, "error": why(parsed)}
        continue
    items = parsed.get("items") or []
    print(f"   {relation:<14} -> 200  total={parsed.get('total_count')} "
          f"pages={parsed.get('total_pages')} n={len(items)}")
    if items:
        print(f"        item keys: {sorted(items[0])}  id_type={items[0].get('id_type')}")
    report[f"rel_{relation}"] = {"status": 200, "total_count": parsed.get("total_count"),
                                 "n": len(items),
                                 "item_keys": sorted(items[0]) if items else []}
status, parsed = fetch("/meta-paper-relations", body={
    "unique_id": seed_unique_id, "relation": "NOT_A_RELATION"})
print(f"   bad relation   -> {status}  {why(parsed)}")
print(RULE)

print("3. citation reverse-lookup via references_unique_id")
status, parsed = fetch("/meta-search", body={
    "query": "", "page_size": 3,
    "filters": [{"field": "references_unique_id", "value": seed_unique_id}]})
rows = parsed.get("results") or []
print(f"   -> {status} n={len(rows)} total={parsed.get('total_count') if status == 200 else why(parsed)}")
report["reverse_citation"] = {"status": status, "n": len(rows),
                              "total_count": parsed.get("total_count")}
print(RULE)

print("4. does agentic-search `mode` change anything? (same query, compare doc_id order)")
orders = {}
for mode in ("fast", "balanced", "quality"):
    status, parsed = fetch("/agentic-search",
                           body={"query": QUERY, "top_k": 10, "mode": mode})
    hits = parsed.get("hits") or []
    orders[mode] = [h.get("doc_id") for h in hits]
    print(f"   mode={mode:<9} -> {status} n={len(hits)} "
          f"scores={[round(h.get('score', 0), 4) for h in hits[:3]]}")
print(f"   fast == balanced: {orders['fast'] == orders['balanced']}")
print(f"   balanced == quality: {orders['balanced'] == orders['quality']}")
status, parsed = fetch("/agentic-search",
                       body={"query": QUERY, "top_k": 10, "mode": "__nonsense__"})
print(f"   bogus mode -> {status}  "
      f"{len(parsed.get('hits') or []) if status == 200 else why(parsed)}")
report["modes"] = {m: len(v) for m, v in orders.items()}
report["mode_identical"] = {"fast_balanced": orders["fast"] == orders["balanced"],
                            "balanced_quality": orders["balanced"] == orders["quality"]}
print(RULE)

print("5. agentic-search hit keys + source_types + saturation")
status, parsed = fetch("/agentic-search", body={"query": QUERY, "top_k": 100})
hits = parsed.get("hits") or []
print(f"   top_k=100 -> {status} n={len(hits)} distinct_docs={len({h.get('doc_id') for h in hits})}")
if hits:
    print(f"   hit keys: {sorted(hits[0])}")
    print(f"   envelope keys: {sorted(parsed)}")
    report["agentic_hit_keys"] = sorted(hits[0])
    report["agentic_envelope_keys"] = sorted(parsed)
status, parsed = fetch("/agentic-search",
                       body={"query": QUERY, "top_k": 5, "source_types": ["pdf"]})
print(f"   source_types=[pdf] -> {status} n={len(parsed.get('hits') or [])} "
      f"types={sorted({str(h.get('source_type')) for h in (parsed.get('hits') or [])})}")
status, parsed = fetch("/agentic-search",
                       body={"query": QUERY, "top_k": 5, "source_types": ["__bogus__"]})
print(f"   source_types bogus -> {status} "
      f"{len(parsed.get('hits') or []) if status == 200 else why(parsed)}")
print(RULE)

print(f"6. /resource + /content edge  (seed doc_id={str(seed_doc_id)[:16]}...)")
status, parsed = fetch("/content", method="GET",
                       params={"doc_id": seed_doc_id or "0" * 64, "offset": 0, "limit": 16384})
print(f"   limit 16384 -> {status} bytes={parsed.get('bytes_returned')} "
      f"more={parsed.get('more')} next_offset={parsed.get('next_offset')}")
status, parsed = fetch("/content", method="GET",
                       params={"doc_id": seed_doc_id or "0" * 64, "offset": 0, "limit": 16385})
print(f"   limit 16385 -> {status} bytes={parsed.get('bytes_returned')} "
      f"{'' if status == 200 else why(parsed)}")
print(RULE)
print(f"{SPENT['calls']} calls")
(OUT / "catalog2.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
print("saved", OUT / "catalog2.json")
