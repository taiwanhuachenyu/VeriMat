"""Pull the authoritative field catalog and exercise the three endpoints we never used."""
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


def fetch(path, method="GET", body=None, params=None):
    url = f"{BASE}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json",
                 "Content-Type": "application/json"},
    )
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


line = "-" * 78

# 1. the authoritative catalog for papers
status, catalog = fetch("/meta-catalog", params={"collection": "papers",
                                                 "include_sample_values": "true"})
print(f"/meta-catalog -> {status}")
if status == 200:
    (OUT / "meta_catalog_papers.json").write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    fields = catalog.get("fields") or []
    print(f"  {len(fields)} fields, "
          f"{len(catalog.get('default_fields') or [])} default-returned")
    print(f"  filter_operators: {catalog.get('filter_operators')}")
    filterable = [f["name"] for f in fields if f.get("filterable")]
    sortable = [f["name"] for f in fields if f.get("sortable")]
    print(f"  filterable ({len(filterable)}): {filterable}")
    print(f"  sortable   ({len(sortable)}): {sortable}")
    print(f"  default_fields: {catalog.get('default_fields')}")
else:
    print("  ", json.dumps(catalog, ensure_ascii=False)[:400])
print(line)

# 2. does the wire really take the *_advanced item shape under the short names?
print("meta-search with a real FieldFilter + sort + fields projection")
status, parsed = fetch("/meta-search", method="POST", body={
    "query": "lithium ion cathode voltage",
    "filters": [
        {"field": "publication_published_year", "operator": "FILTER_OP_GTE", "value": 2020},
        {"field": "language", "value": "en"},
    ],
    "sort": [{"field": "citation_count", "order": "SORT_ORDER_DESC"}],
    "fields": ["unique_id", "doc_id", "title", "doi", "publication_published_year",
               "citation_count", "is_content_accessible"],
    "page": 1, "page_size": 3,
})
print(f"  -> {status}")
if status == 200:
    for item in parsed.get("results") or []:
        print(f"     {item.get('publication_published_year')!s:>8} "
              f"cites={item.get('citation_count')!s:>7} "
              f"full_text={item.get('is_content_accessible')} "
              f"{str(item.get('unique_id'))[:40]}")
    print(f"     keys returned: {sorted((parsed.get('results') or [{}])[0])}")
    (OUT / "meta_search_projected.json").write_text(
        json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
else:
    print("  ", json.dumps(parsed, ensure_ascii=False)[:600])
print(line)

# 3. the citation graph endpoint we have never called
print("meta-paper-relations")
seed = None
if status == 200:
    for item in parsed.get("results") or []:
        if item.get("unique_id"):
            seed = item["unique_id"]
            break
print(f"  seed unique_id = {seed}")
for relation in ("REFERENCES", "CITATIONS", "RELATED_WORKS"):
    code, rel = fetch("/meta-paper-relations", method="POST", body={
        "unique_id": seed, "relation": relation, "page": 1, "page_size": 3,
    })
    items = rel.get("items") or []
    print(f"  {relation:<14} -> {code}  total={rel.get('total_count')} "
          f"pages={rel.get('total_pages')} first={[str(i.get('title'))[:38] for i in items[:2]]}")
print(line)

# 4. semantic search modes, and whether the doc_id hard constraint really scopes
print("agentic-search mode + doc_id scoping")
for mode in ("fast", "balanced", "quality"):
    code, res = fetch("/agentic-search", method="POST", body={
        "query": "what limits the cycling stability of high-nickel layered cathodes",
        "top_k": 5, "mode": mode,
    })
    hits = res.get("hits") or []
    print(f"  mode={mode:<9} -> {code} hits={len(hits)} "
          f"top_score={hits[0].get('score') if hits else None} "
          f"docs={len({h.get('doc_id') for h in hits})}")
    if mode == "balanced" and hits:
        scope = [h["doc_id"] for h in hits[:2]]
code, res = fetch("/agentic-search", method="POST", body={
    "query": "cycling stability", "top_k": 10, "filters": {"doc_id": scope},
})
returned = {h.get("doc_id") for h in (res.get("hits") or [])}
print(f"  doc_id scope of {len(scope)} -> {code} hits={len(res.get('hits') or [])} "
      f"outside_scope={returned - set(scope)}")
code, res = fetch("/agentic-search", method="POST", body={
    "query": "cycling stability", "top_k": 5, "filters": {"doc_id": []},
})
print(f"  empty doc_id list -> {code} hits={len(res.get('hits') or [])} "
      f"(spec: 200 with empty hits, not a global search)")
print(line)
print("saved probes to", OUT)
