"""End-to-end smoke test of the rewritten client against the live deployment.

The unit suite proves the logic against a scripted transport; this proves the contract.  It also
exercises the capability the rewrite exists to enable: pin a candidate set with a hard metadata
filter, then scope semantic search to those documents with the one hard constraint the semantic
endpoint offers.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

from src.tools.sciverse import (  # noqa: E402
    SciverseClient, metadata_filters, metadata_sort, semantic_filters,
)

AUDIT = ROOT / "_vm_scratch" / "sciverse_probe" / "smoke_audit.jsonl"
if AUDIT.exists():
    AUDIT.unlink()
api = SciverseClient(audit_log=str(AUDIT), quiet=True)
RULE = "-" * 78
token = os.environ["SCIVERSE_API_TOKEN"]
print(f"token {token[:4]}...{token[-4:]} (len {len(token)})   audit -> {AUDIT.name}")
print(RULE)

print("1. meta-catalog: load the schema instead of guessing at field names")
catalog = api.meta_catalog("papers")
entries = catalog["fields"]
print(f"   {len(entries)} fields, {len(catalog['default_fields'])} returned by default")
print(f"   operators: {len(catalog['filter_operators'])}  "
      f"filterable: {sum(1 for e in entries if e.get('filterable'))}  "
      f"sortable: {sum(1 for e in entries if e.get('sortable'))}")
print(RULE)

print("2. meta-search: hard filters + sort + full-text requirement")
envelope = api.meta_search(
    "high nickel layered oxide cathode degradation",
    filters=metadata_filters(lang="en", year_from=2015, year_to=2024, require_full_text=True),
    sort=metadata_sort("-citation_count"),
    page_size=8,
)
rows = envelope["results"]
print(f"   n={len(rows)} total_count={envelope['total_count']} "
      f"pages={envelope.get('total_pages')}")
for row in rows[:4]:
    print(f"     {int(row.get('publication_published_year') or 0)}  "
          f"cites={int(row.get('citation_count') or 0):>6}  "
          f"full_text={'yes' if row.get('doc_id') else 'no '}  "
          f"{str(row.get('title'))[:48]}")
years = [int(row["publication_published_year"]) for row in rows
         if row.get("publication_published_year")]
have_text = [row for row in rows if row.get("doc_id")]
print(f"   year range honoured: {min(years)}..{max(years)} within 2015..2024 = "
      f"{min(years) >= 2015 and max(years) <= 2024}")
print(f"   every row carries a doc_id: {len(have_text)}/{len(rows)}")
print(RULE)

print("3. semantic search scoped to those documents (the one hard constraint)")
scope = [row["doc_id"] for row in have_text][:20]
hits = api.agentic_search(
    "what mechanism limits capacity retention during extended cycling",
    top_k=20, filters=semantic_filters(doc_ids=scope),
)
returned = {hit["doc_id"] for hit in hits}
print(f"   scope={len(scope)} docs -> {len(hits)} chunks over {len(returned)} docs")
print(f"   nothing escaped the scope: {returned <= set(scope)}")
print(RULE)

print("4. unscoped semantic search with a hard year ceiling")
capped = api.agentic_search(
    "solid electrolyte interphase formation on graphite",
    top_k=25, filters=semantic_filters(lang="en", year_to=2010, domain="Physical Sciences"),
)
capped_years = [int(hit["publication_published_year"]) for hit in capped
                if hit.get("publication_published_year")]
missing = sum(1 for hit in capped if not hit.get("publication_published_year"))
print(f"   n={len(capped)} years={min(capped_years)}..{max(capped_years)} "
      f"missing_year={missing}")
print(f"   cutoff holds (max <= 2010 and nothing undated): "
      f"{max(capped_years) <= 2010 and missing == 0}")
print(RULE)

print("5. content assembly by following next_offset")
document = api.read_document(hits[0]["doc_id"], max_bytes=60000)
print(f"   pages={document['pages']} bytes={document['bytes']} "
      f"offsets {document['start_offset']}..{document['end_offset']} "
      f"truncated={document['truncated']}")
print(f"   sha256={document['content_sha256'][:16]}...")
print(RULE)

print("6. citation graph")
seed = have_text[0]["unique_id"]
for relation in ("REFERENCES", "CITATIONS", "RELATED_WORKS"):
    graph = api.paper_relations(seed, relation, page_size=5)
    kinds = sorted({item.get("id_type") for item in graph["items"]})
    print(f"   {relation:<14} total={graph['total_count']:<5} "
          f"pages={graph.get('total_pages')} id_type={kinds}")
citing = api.meta_search("", filters=metadata_filters(cites=seed), page_size=5)
print(f"   reverse lookup (papers citing {seed[:34]}): {citing['total_count']}")
print(RULE)

print("7. an empty match set is a result, not a failure")
empty = api.agentic_search("lithium", top_k=5, filters=semantic_filters(lang="zz"))
print(f"   filters={{'lang': 'zz'}} -> {empty!r}  (would have raised before the rewrite)")
print(RULE)

records = [json.loads(line) for line in AUDIT.read_text(encoding="utf-8").splitlines()]
print(f"audit chain: {len(records)} records")
for record in records:
    print(f"   {record['tool']:<22} n_hits={record['n_hits']:<3} "
          f"latency={record.get('latency_ms', '-')}")
projected = records[1]["hits"][0] if records[1]["hits"] else {}
print(f"   meta-search projection keys: {sorted(projected)}")
print("OK")
