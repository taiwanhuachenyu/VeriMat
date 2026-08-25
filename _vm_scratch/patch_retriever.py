"""Apply the three evidence-backed fixes to literature_retriever.py.

The file has CRLF endings, which the editing tools refuse, so the anchors are replaced here.
Each anchor must match exactly once; anything else means the file moved under me and the patch
should not be guessed at.
"""
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "src" / "evaluation" / "literature_retriever.py"
NL = chr(10)

PATCHES = [
    (
        "import the shared bound and filter builder",
        "from src.tools.sciverse import SciverseClient",
        "from src.tools.sciverse import MAX_CONTENT_LIMIT, SciverseClient, semantic_filters",
    ),
    (
        "match the client's actual search signature",
        """    def agentic_search(
        self, query: str, top_k: int = 10, sub_queries: int = 0,
        filters: dict | None = None, request_id: str | None = None,
        max_retries: int = 4,
    ) -> list: ...""",
        """    def agentic_search(
        self, query: str, top_k: int = 10, *, filters: dict | None = None,
        mode: str | None = None, source_types: list[str] | None = None,
        request_id: str | None = None, max_retries: int = 4,
    ) -> list: ...""",
    ),
    (
        "read the year out of whichever numeric shape the endpoint used",
        '''def _publication_date(hit: dict[str, Any]) -> date | None:
    for key in ("publication_date", "publication_published_date", "published_at"):
        value = hit.get(key)
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                return None
    year = hit.get("publication_published_year") or hit.get("year")
    if isinstance(year, str) and year.isdigit():
        year = int(year)
    if isinstance(year, int) and 1000 <= year <= 9999:
        # December 31 is conservative: a same-year hit is included only for a year-end cutoff.
        return date(year, 12, 31)
    return None''',
        '''def _year_of(*candidates: Any) -> int | None:
    """Read a publication year out of whichever numeric shape the endpoint used.

    Semantic search reports this field as an integer while metadata search reports the same field
    as a float (``1999.0``), so an int-only check silently discarded every metadata row.  Values
    outside a plausible range are treated as unknown rather than converted: the corpus uses ``0``
    as a missing-year sentinel and ``date(0, ...)`` is not constructible.
    """
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                continue
        if isinstance(value, (int, float)):
            year = int(value)
            if 1000 <= year <= 9999:
                return year
    return None


def _publication_date(hit: dict[str, Any]) -> date | None:
    for key in ("publication_date", "publication_published_date", "published_at"):
        value = hit.get(key)
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                return None
    year = _year_of(hit.get("publication_published_year"), hit.get("year"))
    if year is None:
        return None
    # December 31 is conservative: a same-year hit is included only for a year-end cutoff.
    return date(year, 12, 31)''',
    ),
    (
        "clamp content_limit to the bound the provider documents",
        """            not index_snapshot_id.strip() or top_k < 1 or top_k > 20
            or content_limit < 256 or content_limit > 20000""",
        """            not index_snapshot_id.strip() or top_k < 1 or top_k > 20
            or content_limit < 256 or content_limit > MAX_CONTENT_LIMIT""",
    ),
    (
        "build the filter block through the one module that knows the wire shape",
        '''        filters = {
            "lang": "en",
            "publication_published_year": {"lte": cutoff.year},
            "topics": {
                "logic": "and",
                "dimensions": {"primary_topic_domain": "Physical Sciences"},
            },
        }''',
        '''        # The year bound is load-bearing for the cutoff claim, and it does hold: a `lte` bound
        # was observed to return only years at or below it, with no hit missing a year.  Every
        # hit is still re-checked against the cutoff below, so a provider that quietly loosened
        # this would cost recall rather than turn into a false provenance claim.
        filters = semantic_filters(
            lang="en", year_to=cutoff.year, domain="Physical Sciences",
        )''',
    ),
]

source = TARGET.read_text(encoding="utf-8")
crlf = NL not in source.replace(chr(13) + NL, "")
print(f"{TARGET.name}: {len(source)} chars, CRLF={crlf}")

for label, old, new in PATCHES:
    needle = old.replace(NL, chr(13) + NL) if crlf else old
    count = source.count(needle)
    if count != 1:
        raise SystemExit(f"anchor for {label!r} matched {count} times, expected 1")
    source = source.replace(needle, new.replace(NL, chr(13) + NL) if crlf else new)
    print(f"  applied: {label}")

TARGET.write_text(source, encoding="utf-8", newline="")
print(f"wrote {len(source)} chars")
