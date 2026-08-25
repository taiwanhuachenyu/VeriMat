#!/usr/bin/env python3
"""Sciverse API client covering all six endpoints, using only the standard library.

VeriMat's literature evidence comes from https://api.sciverse.space (OpenDataLab / Shanghai AI
Lab).  The client is built on ``urllib`` because ``requirements-runtime.lock`` declares the
trusted runtime to be stdlib-only, and it is usable three ways: as a library, as a CLI an agent
can drive over ``bash``, and as an audited transport whose every call appends to a JSONL evidence
chain.

The published contract is OpenAPI ``0.14.0``.  It documents the SDK/MCP tool layer rather than
the raw HTTP surface, and the live deployment diverges from it in ways that change how the client
must behave.  Every divergence recorded below was established by observation, not inference:

  * ``meta-search`` takes ``filters``/``sort``, not the spec's ``filters_advanced``/
    ``sort_advanced``.  The spec's convenience fields (``year_from``, ``authors``, ``journals``,
    ``collection``, ``title_contains``, the three ``*_boost`` knobs) are rejected outright with
    ``extra_forbidden``; the wire accepts exactly the eight keys in ``_META_SEARCH_KEYS``.
  * ``agentic-search`` filters are *hard*, not the "soft/approximate" the spec warns about:
    ``{"publication_published_year": {"lte": 1990}}`` returned 1753..1988 across 30 hits with no
    hit missing a year.  The cutoff guarantee in ``SciverseBenchmarkRetriever`` rests on this.
  * A legitimately empty result arrives as **HTTP 400 with ``code=EMPTY_RESULT``**, not as 200
    with an empty list.  The search methods therefore translate it back to an empty result while
    ``_request`` still raises ``SciverseEmptyResult``, so a caller that needs to tell "no matches"
    apart from "no query" can.  Leaving it as an error is not cosmetic: the cached transport marks
    a failed call indeterminate, so one unlucky query would strand an operation in ``PENDING``
    and demand manual reconciliation.
  * ``is_content_accessible`` is always ``false``, including for rows whose ``doc_id`` reads back
    44 kB of text.  Presence of ``doc_id`` is the only usable signal that full text exists, and
    ``filters=[{"field": "doc_id", "operator": "FILTER_OP_NE", "value": ""}]`` restricts a
    metadata search to those rows.
  * ``mode`` is inert: ``fast``, ``balanced`` and ``quality`` returned identical hit ordering and
    identical scores, and an unknown value still answered 200.  It is still sent, because the
    contract defines it and a later deployment may honour it, but nothing may be claimed of it.
  * ``page_size`` above the documented 50 is accepted (200 observed), and ``limit`` above the
    documented 16384 is accepted.  The client clamps to the documented bounds anyway so behaviour
    does not silently depend on which deployment answers.

Environment:
  SCIVERSE_API_TOKEN   required, of the form ``sci_...``
  SCIVERSE_BASE_URL    optional, defaults to https://api.sciverse.space
  SCIVERSE_AUDIT_LOG   optional; when set, every call appends to this JSONL evidence chain

CLI (every subcommand prints JSON on stdout):
  python -m src.tools.sciverse search "lithium cathode voltage" --top-k 10 --year-to 2024
  python -m src.tools.sciverse meta "graphene anode" --require-full-text --sort -citation_count
  python -m src.tools.sciverse content --doc-id <sha256> --offset 0 --limit 4096
  python -m src.tools.sciverse read --doc-id <sha256> --max-bytes 200000
  python -m src.tools.sciverse catalog --sample-values
  python -m src.tools.sciverse relations --unique-id paper:10.1021/acs.iecr.5c00582 -r REFERENCES
  python -m src.tools.sciverse resource --file-name images/fig1.jpg --out fig1.jpg
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

from src.core.portability import extended_path, lock_exclusive

__all__ = [
    "DEFAULT_BASE",
    "FILTER_OPERATORS",
    "MAX_CONTENT_LIMIT",
    "MAX_DOC_ID_SCOPE",
    "MAX_TOP_K",
    "RELATIONS",
    "SATURATION_TOP_K",
    "SORTABLE_FIELDS",
    "SciverseClient",
    "SciverseEmptyResult",
    "SciverseError",
    "SciverseNotFound",
    "SciverseScopeTooLarge",
    "main",
    "metadata_filters",
    "metadata_sort",
    "semantic_filters",
]

DEFAULT_BASE = "https://api.sciverse.space"

# Retry only what a retry can fix.  A 400 means the request was wrong and will stay wrong.
_RETRY_STATUS = frozenset({500, 502, 503})

# Documented bounds.  The deployment enforces none of them, so the client does.
_MAX_QUERY_CHARS = 4096
MAX_TOP_K = 100
SATURATION_TOP_K = 50  # observed: top_k=100 yields 50 chunks over ~40 distinct papers
MAX_CONTENT_LIMIT = 16384
_MAX_PAGE_SIZE = 50
_MAX_RELATION_PAGE_SIZE = 200
# total_count saturates here, and page*page_size beyond it must switch to cursor paging.
_RESULT_WINDOW = 10000
MAX_DOC_ID_SCOPE = 1000

# The eight keys the deployment accepts on /meta-search; anything else is extra_forbidden.
_META_SEARCH_KEYS = ("query", "filters", "sort", "fields", "facets", "page", "page_size", "cursor")

_DOC_ID = re.compile(r"^[0-9a-f]{64}$")

FILTER_OPERATORS = (
    "FILTER_OP_EQ", "FILTER_OP_NE", "FILTER_OP_GT", "FILTER_OP_GTE", "FILTER_OP_LT",
    "FILTER_OP_LTE", "FILTER_OP_IN", "FILTER_OP_NIN", "FILTER_OP_CONTAINS", "FILTER_OP_MATCH",
    "FILTER_OP_MATCH_PHRASE",
)

# The deployment rejects the short forms ("GTE", "DESC"); only these long names are accepted.
_SORT_ORDERS = ("SORT_ORDER_ASC", "SORT_ORDER_DESC")

# Returned verbatim by the deployment when an unsortable field is requested.
SORTABLE_FIELDS = (
    "citation_count", "citation_normalized_percentile.value", "cited_by_percentile_year.max",
    "cited_by_percentile_year.min", "concepts.score", "fwci", "influential_citation_count",
    "primary_topic.score", "publication_published_date", "publication_published_year",
    "reference_count", "topics.score",
)

RELATIONS = ("CITATIONS", "REFERENCES", "RELATED_WORKS")
_COLLECTIONS = ("papers", "authors", "sources")

# Which hit fields are worth preserving in the evidence chain, per endpoint.  agentic-search hits
# carry no `doi` -- the previous projection recorded one and it was null on every row.
_CHUNK_FIELDS = (
    "doc_id", "chunk_id", "title", "offset", "page_no", "score", "lang", "recall_source",
    "model_name", "model_version", "publication_published_year", "publication_published_date",
    "publication_venue_name_unified", "citation_count",
)
_PAPER_FIELDS = (
    "unique_id", "doc_id", "title", "doi", "publication_published_year",
    "publication_venue_name_unified", "citation_count", "relevance_score",
    "is_content_accessible",
)


class SciverseError(RuntimeError):
    """A Sciverse call failed, carrying the server's own diagnosis rather than a flat string.

    ``code``, ``biz_code``, ``request_id`` and ``details`` are what make a failure actionable:
    ``details`` names the offending field for a validation error, and ``request_id`` is what the
    provider needs to trace a call.  Flattening them into the message discards exactly the part a
    caller can act on.
    """

    def __init__(
        self, message: str, *, status: int | None = None, code: str | None = None,
        biz_code: Any = None, request_id: str | None = None,
        details: list[Any] | None = None, method: str | None = None, path: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.biz_code = biz_code
        self.request_id = request_id
        self.details = details or []
        self.method = method
        self.path = path

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": str(self), "status": self.status, "code": self.code,
            "biz_code": self.biz_code, "request_id": self.request_id,
            "details": self.details, "method": self.method, "path": self.path,
        }


class SciverseEmptyResult(SciverseError):
    """The query was valid and matched nothing; the deployment reports this as HTTP 400."""


class SciverseNotFound(SciverseError):
    """A ``doc_id`` or ``unique_id`` is not in the corpus."""


class SciverseScopeTooLarge(SciverseError):
    """More than ``MAX_DOC_ID_SCOPE`` document ids were pushed into one semantic search."""


_ERROR_CLASSES: dict[str, type[SciverseError]] = {
    "EMPTY_RESULT": SciverseEmptyResult,
    "CONTENT_NOT_FOUND": SciverseNotFound,
    "SCOPE_TOO_LARGE": SciverseScopeTooLarge,
}


# --------------------------------------------------------------------------- request builders
def _numeric_range(low: Any = None, high: Any = None) -> dict[str, Any] | None:
    """Render an inclusive range the way semantic filters expect, or nothing if unbounded."""
    bounds = {}
    if low is not None:
        bounds["gte"] = low
    if high is not None:
        bounds["lte"] = high
    return bounds or None


def _normalise_doc_ids(doc_ids: Any) -> list[str]:
    """Deduplicate document ids, preserving order, and reject anything that cannot be one.

    The server answers ``SCOPE_TOO_LARGE`` past a thousand ids after its own deduplication, so
    the same limit is applied here where it costs nothing to discover.
    """
    seen: dict[str, None] = {}
    for value in doc_ids:
        text = str(value).strip().lower()
        if not _DOC_ID.match(text):
            raise SciverseError(f"not a Sciverse doc_id (expect 64 hex characters): {value!r}")
        seen[text] = None
    if len(seen) > MAX_DOC_ID_SCOPE:
        raise SciverseScopeTooLarge(
            f"{len(seen)} document ids exceed the {MAX_DOC_ID_SCOPE} the server accepts; "
            "narrow the candidate set with a metadata search first"
        )
    return list(seen)


def semantic_filters(
    *, lang: str | None = None, year_from: int | None = None, year_to: int | None = None,
    published_from: str | None = None, published_to: str | None = None,
    domain: str | None = None, topic: str | None = None, topic_logic: str = "and",
    venue: str | None = None, venue_type: str | None = None,
    metadata_type: str | None = None, min_citations: int | None = None,
    doc_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build the ``filters`` **object** that ``/agentic-search`` expects.

    Fields are ANDed; a list within one field is ORed.  ``doc_ids`` is the one hard constraint the
    endpoint offers, and an explicitly empty list is meaningful -- it returns nothing rather than
    degrading to a corpus-wide search -- so it is forwarded as ``[]`` and only omitted when
    ``None``.
    """
    built: dict[str, Any] = {}
    if lang:
        built["lang"] = lang
    years = _numeric_range(year_from, year_to)
    if years:
        built["publication_published_year"] = years
    dates = _numeric_range(published_from, published_to)
    if dates:
        built["publication_published_date"] = dates
    if venue:
        built["publication_venue_name_unified"] = venue
    if venue_type:
        built["publication_venue_type"] = venue_type
    if metadata_type:
        built["metadata_type"] = metadata_type
    if min_citations is not None:
        built["citation_count"] = {"gte": min_citations}
    dimensions = {}
    if topic:
        dimensions["primary_topic"] = topic
    if domain:
        dimensions["primary_topic_domain"] = domain
    if dimensions:
        built["topics"] = {"logic": topic_logic, "dimensions": dimensions}
    if doc_ids is not None:
        built["doc_id"] = _normalise_doc_ids(doc_ids)
    return built


def metadata_filters(
    *, lang: str | None = None, year_from: int | None = None, year_to: int | None = None,
    venue: str | None = None, venue_type: str | None = None, doi: str | None = None,
    subjects: list[str] | None = None, keywords: list[str] | None = None,
    author: str | None = None, min_citations: int | None = None,
    open_access: bool | None = None, cites: str | None = None,
    doc_ids: list[str] | None = None, require_full_text: bool = False,
    extra: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the ``filters`` **list** that ``/meta-search`` expects.

    Each element is ``{"field", "value", "operator"}`` with the operator defaulting to
    ``FILTER_OP_EQ`` server-side.  Note this is a different wire shape from
    :func:`semantic_filters` for the same conceptual filter -- the two endpoints genuinely differ,
    so the difference is made explicit rather than hidden behind one leaky abstraction.

    ``require_full_text`` is the only way to restrict a search to documents whose text can
    actually be read: ``is_content_accessible`` is reported ``false`` even for readable documents,
    while ``doc_id != ""`` selected rows carrying a ``doc_id`` on every observed page.

    ``cites`` performs the reverse citation lookup -- papers whose reference list contains the
    given ``unique_id``.  ``references_unique_id`` is filter-only: it cannot be sorted, projected
    or aggregated.
    """
    built: list[dict[str, Any]] = []

    def add(field: str, value: Any, operator: str | None = None) -> None:
        item: dict[str, Any] = {"field": field, "value": value}
        if operator is not None:
            if operator not in FILTER_OPERATORS:
                raise SciverseError(
                    f"unknown filter operator {operator!r}; expected one of {FILTER_OPERATORS}"
                )
            item["operator"] = operator
        built.append(item)

    if lang:
        add("language", lang)
    if year_from is not None:
        add("publication_published_year", year_from, "FILTER_OP_GTE")
    if year_to is not None:
        add("publication_published_year", year_to, "FILTER_OP_LTE")
    if venue:
        add("publication_venue_name_unified", venue)
    if venue_type:
        add("publication_venue_type", venue_type)
    if doi:
        add("doi", doi)
    if subjects:
        add("subjects", list(subjects), "FILTER_OP_IN")
    if keywords:
        add("keywords", list(keywords), "FILTER_OP_IN")
    if author:
        add("author", author, "FILTER_OP_MATCH")
    if min_citations is not None:
        add("citation_count", min_citations, "FILTER_OP_GTE")
    if open_access is not None:
        add("access_is_oa", open_access)
    if cites:
        add("references_unique_id", cites)
    if doc_ids is not None:
        ids = _normalise_doc_ids(doc_ids)
        add("doc_id", ids, "FILTER_OP_IN")
    if require_full_text:
        add("doc_id", "", "FILTER_OP_NE")
    if extra:
        for item in extra:
            if "field" not in item or "value" not in item:
                raise SciverseError(f"a metadata filter needs 'field' and 'value': {item!r}")
            add(str(item["field"]), item["value"], item.get("operator"))
    return built


def metadata_sort(*fields: str) -> list[dict[str, str]]:
    """Build a ``sort`` list; prefix a field with ``-`` for descending.

    Sorting is not free: with an explicit sort the server degrades ``query`` from a relevance
    ranking into an OR hit-filter, so sort only when order matters more than relevance.
    """
    built: list[dict[str, str]] = []
    for field in fields:
        descending = field.startswith("-")
        name = field.lstrip("+-")
        if name not in SORTABLE_FIELDS:
            raise SciverseError(
                f"{name!r} is not sortable; the server accepts {SORTABLE_FIELDS}"
            )
        built.append({
            "field": name,
            "order": "SORT_ORDER_DESC" if descending else "SORT_ORDER_ASC",
        })
    return built


class SciverseClient:
    """A faithful, audited transport for the six Sciverse endpoints."""

    def __init__(
        self, token: str | None = None, base_url: str | None = None,
        audit_log: str | None = None, timeout: float = 45.0, quiet: bool = False,
    ):
        self.token = token or os.environ.get("SCIVERSE_API_TOKEN", "")
        if not self.token:
            raise SciverseError(
                "missing SCIVERSE_API_TOKEN (environment variable or constructor argument)"
            )
        self.base_url = (
            base_url or os.environ.get("SCIVERSE_BASE_URL") or DEFAULT_BASE
        ).rstrip("/")
        self.audit_log = audit_log or os.environ.get("SCIVERSE_AUDIT_LOG") or ""
        self.timeout = timeout
        self.quiet = quiet

    # ------------------------------------------------------------------------------ transport
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _failure(
        self, error: urllib.error.HTTPError, method: str, path: str,
    ) -> SciverseError:
        """Turn an HTTP error into the most specific exception its payload supports.

        Two envelope shapes are in use.  Business errors answer ``{"code", "message",
        "request_id"}``, sometimes nested under ``error`` alongside a ``biz_code``; request
        validation answers Pydantic's ``{"details": [...]}`` naming each rejected field.  Both are
        preserved, because which field the server objected to is the whole diagnosis.
        """
        raw = error.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        nested = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = nested.get("code") or payload.get("code")
        details = payload.get("details") or payload.get("detail")
        message = nested.get("message") or payload.get("message") or raw[:300] or error.reason
        if isinstance(details, list) and details:
            fields = "; ".join(
                f"{'.'.join(str(part) for part in item.get('loc', [])[1:])}: {item.get('msg')}"
                for item in details if isinstance(item, dict)
            )
            message = f"{message} [{fields}]" if fields else message
        summary = " ".join(
            part for part in (f"HTTP {error.code} on {method} {path}:", code, str(message)) if part
        )
        return _ERROR_CLASSES.get(str(code), SciverseError)(
            summary, status=error.code, code=str(code) if code else None,
            biz_code=nested.get("biz_code") or payload.get("biz_code"),
            request_id=payload.get("request_id") or nested.get("request_id"),
            details=details if isinstance(details, list) else None,
            method=method, path=path,
        )

    def _request(
        self, method: str, path: str, *, body: dict | None = None,
        params: dict | None = None, max_retries: int = 4, raw: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            present = {key: value for key, value in params.items() if value is not None}
            if present:
                url = f"{url}?{urllib.parse.urlencode(present, doseq=True)}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        last: SciverseError | None = None
        for attempt in range(max(1, max_retries)):
            request = urllib.request.Request(
                url, data=data, headers=self._headers(), method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                    if raw:
                        return payload
                    return json.loads(payload) if payload.strip() else {}
            except urllib.error.HTTPError as error:
                failure = self._failure(error, method, path)
                retryable = error.code in _RETRY_STATUS or error.code == 429
                if not retryable or attempt == max(1, max_retries) - 1:
                    raise failure from error
                time.sleep(min(2 ** attempt * 3, 30) if error.code == 429 else 2 ** attempt)
                last = failure
            except urllib.error.URLError as error:
                last = SciverseError(
                    f"{method} {path} unreachable: {error.reason}", method=method, path=path,
                )
                if attempt == max(1, max_retries) - 1:
                    raise last from error
                time.sleep(2 ** attempt)
        raise last or SciverseError(f"{method} {path} failed without a diagnosis")

    # ---------------------------------------------------------------------------- audit chain
    def _audit(
        self, tool: str, request: dict, rows: list, *, fields: tuple[str, ...] = _CHUNK_FIELDS,
        extra: dict | None = None,
    ) -> None:
        """Append one call to the JSONL evidence chain that the submission has to defend."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 3,
            "request_id": str((extra or {}).get("request_id") or uuid.uuid4()),
            "tool": tool,
            "request": request,
            "n_hits": len(rows),
            "hits": [
                {key: row.get(key) for key in fields if row.get(key) is not None}
                for row in rows if isinstance(row, dict)
            ],
        }
        if extra:
            record.update(extra)
        if self.audit_log:
            target = extended_path(self.audit_log)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8", newline="\n") as handle:
                lock_exclusive(handle)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        if not self.quiet:
            query = str(request.get("query") or request.get("doc_id") or "")[:60]
            sys.stderr.write(f"[sciverse:{tool}] {len(rows)} rows  q={query!r}\n")

    @staticmethod
    def _digest(payload: Any) -> str:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    # ------------------------------------------------------------------------- semantic search
    def agentic_search(
        self, query: str, top_k: int = 10, *, filters: dict | None = None,
        mode: str | None = None, source_types: list[str] | None = None,
        request_id: str | None = None, max_retries: int = 4,
    ) -> list[dict[str, Any]]:
        """Semantic search over full-text chunks, returning citable evidence.

        Each hit carries ``doc_id`` and a byte ``offset`` that go straight into :meth:`content`.
        ``top_k`` is clamped to the documented 1..100, but the server saturates near
        ``SATURATION_TOP_K`` chunks over roughly a third fewer distinct papers, so asking for
        more than that buys nothing.  ``mode`` is accepted and forwarded but is currently inert on
        the deployment.  An empty match set comes back as an empty list, not an exception.
        """
        text = str(query)[:_MAX_QUERY_CHARS]
        if not text.strip():
            raise SciverseError("agentic-search needs a non-empty query")
        body: dict[str, Any] = {"query": text, "top_k": max(1, min(int(top_k), MAX_TOP_K))}
        if filters:
            # A filter block of {"doc_id": []} is truthy and must survive: it means "search
            # nothing", which the server honours, rather than "search everything".
            body["filters"] = filters
        if mode:
            body["mode"] = mode
        if source_types:
            body["source_types"] = list(source_types)
        started = time.monotonic()
        try:
            response = self._request(
                "POST", "/agentic-search", body=body, max_retries=max_retries,
            )
        except SciverseEmptyResult:
            response = {"hits": []}
        hits = [hit for hit in (response.get("hits") or []) if isinstance(hit, dict)]
        self._audit("agentic-search", body, hits, extra={
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "response_sha256": self._digest(hits),
            "saturated": len(hits) >= SATURATION_TOP_K,
            **({"request_id": request_id} if request_id else {}),
        })
        return hits

    # ------------------------------------------------------------------------- metadata search
    def meta_search(
        self, query: str = "", *, filters: list[dict] | None = None,
        sort: list[dict] | None = None, fields: list[str] | None = None,
        facets: list[dict] | None = None, page: int = 1, page_size: int = 10,
        cursor: str | None = None, request_id: str | None = None, max_retries: int = 4,
    ) -> dict[str, Any]:
        """Structured metadata search, returning the whole envelope rather than only the rows.

        The envelope is the point: ``total_count`` (saturating at ``_RESULT_WINDOW``),
        ``total_pages`` and ``next_cursor`` are what make paging possible, and the rows arrive
        under ``results`` -- not ``hits``.  An empty query is legal and means pure structured
        filtering.  Requesting a page beyond the result window is refused here, because the server
        refuses it too and :meth:`iter_meta_search` exists to cross that boundary with a cursor.

        ``fields`` projects the response, except that ``is_content_accessible`` and
        ``relevance_score`` are injected regardless, and naming a field the catalog does not know
        fails the whole request.
        """
        if page < 1 or page_size < 1:
            raise SciverseError("meta-search page and page_size start at 1")
        # Clamp before the window check so the bound that is enforced is the bound that is tested.
        size = min(int(page_size), _MAX_PAGE_SIZE)
        if cursor is None and page * size > _RESULT_WINDOW:
            raise SciverseError(
                f"page {page} x page_size {size} exceeds the {_RESULT_WINDOW}-result window; "
                "use iter_meta_search, which switches to cursor paging"
            )
        body: dict[str, Any] = {"query": str(query)[:_MAX_QUERY_CHARS], "page": page,
                                "page_size": size}
        if filters:
            body["filters"] = filters
        if sort:
            body["sort"] = sort
        if fields:
            body["fields"] = list(fields)
        if facets:
            body["facets"] = facets
        if cursor:
            body["cursor"] = cursor
        unknown = set(body) - set(_META_SEARCH_KEYS)
        if unknown:
            raise SciverseError(f"meta-search rejects these keys outright: {sorted(unknown)}")
        started = time.monotonic()
        try:
            response = self._request("POST", "/meta-search", body=body, max_retries=max_retries)
        except SciverseEmptyResult:
            response = {"results": [], "total_count": 0, "page": page, "page_size": size}
        rows = [row for row in (response.get("results") or []) if isinstance(row, dict)]
        response["results"] = rows
        self._audit("meta-search", body, rows, fields=_PAPER_FIELDS, extra={
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "response_sha256": self._digest(rows),
            "total_count": response.get("total_count"),
            "total_count_saturated": response.get("total_count") == _RESULT_WINDOW,
            **({"request_id": request_id} if request_id else {}),
        })
        return response

    def iter_meta_search(
        self, query: str = "", *, page_size: int = 50, limit: int | None = None,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        """Yield metadata rows across pages, switching to cursor paging at the window edge.

        ``page``-based paging stops working once ``page * page_size`` passes
        ``_RESULT_WINDOW``; ``next_cursor`` continues past it.  Iteration also stops when a page
        comes back short or a cursor repeats, so a deployment that ignores the cursor cannot spin
        this forever.
        """
        kwargs.pop("page", None)
        kwargs.pop("cursor", None)
        size = min(int(page_size), _MAX_PAGE_SIZE)
        produced = 0
        page = 1
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            envelope = self.meta_search(
                query, page=page, page_size=size, cursor=cursor, **kwargs,
            )
            rows = envelope.get("results") or []
            for row in rows:
                yield row
                produced += 1
                if limit is not None and produced >= limit:
                    return
            if len(rows) < size:
                return
            next_cursor = envelope.get("next_cursor")
            if (page + 1) * size > _RESULT_WINDOW:
                if not next_cursor or next_cursor in seen_cursors:
                    return
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            page += 1

    # -------------------------------------------------------------------------------- content
    def content(
        self, doc_id: str, offset: int = 0, limit: int = 4096,
        request_id: str | None = None, max_retries: int = 4,
    ) -> dict[str, Any]:
        """Read one slice of a document's text.

        ``offset`` and ``bytes_returned`` count **bytes** while ``text`` is a string, so the two
        differ wherever the text is not ASCII; continuation must follow ``next_offset`` rather
        than adding ``len(text)``.  ``limit`` is clamped to the documented maximum even though the
        deployment does not enforce it.
        """
        if offset < 0:
            raise SciverseError("content offset cannot be negative")
        params = {
            "doc_id": doc_id, "offset": int(offset),
            "limit": max(1, min(int(limit), MAX_CONTENT_LIMIT)),
        }
        started = time.monotonic()
        response = self._request("GET", "/content", params=params, max_retries=max_retries)
        text = str(response.get("text") or "")
        self._audit("content", params, [], extra={
            "bytes": len(text.encode("utf-8")),
            "bytes_returned": response.get("bytes_returned"),
            "more": response.get("more"),
            "next_offset": response.get("next_offset"),
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            **({"request_id": request_id} if request_id else {}),
        })
        return response

    def read_document(
        self, doc_id: str, *, offset: int = 0, max_bytes: int | None = None,
        chunk_limit: int = MAX_CONTENT_LIMIT, request_id: str | None = None,
        max_retries: int = 4,
    ) -> dict[str, Any]:
        """Assemble a document by following ``more``/``next_offset`` to the end.

        Structured extraction needs whole sections, not a single window, and one slice is capped
        at 16 kB.  The loop stops if the server stops advancing ``next_offset``, so a deployment
        that reports ``more`` without progress cannot turn this into an infinite spend.
        """
        start = max(0, int(offset))
        chunks: list[str] = []
        collected = 0
        cursor = start
        reached = start
        pages = 0
        truncated = False
        while True:
            window = min(chunk_limit, MAX_CONTENT_LIMIT)
            if max_bytes is not None:
                window = min(window, max(1, max_bytes - collected))
            slice_ = self.content(
                doc_id, offset=cursor, limit=window,
                request_id=request_id, max_retries=max_retries,
            )
            pages += 1
            text = str(slice_.get("text") or "")
            chunks.append(text)
            collected += len(text.encode("utf-8"))
            following = slice_.get("next_offset")
            if isinstance(following, int) and following > reached:
                reached = following
            if not slice_.get("more") or not isinstance(following, int) or following <= cursor:
                break
            cursor = following
            if max_bytes is not None and collected >= max_bytes:
                truncated = True
                break
        joined = "".join(chunks)
        return {
            "doc_id": doc_id, "text": joined, "pages": pages,
            "start_offset": start, "end_offset": reached,
            "bytes": len(joined.encode("utf-8")), "truncated": truncated,
            "content_sha256": hashlib.sha256(joined.encode("utf-8")).hexdigest(),
        }

    # --------------------------------------------------------------------------- introspection
    def meta_catalog(
        self, collection: str = "papers", *, include_sample_values: bool = False,
        include_field_stats: bool = False, max_retries: int = 4,
    ) -> dict[str, Any]:
        """Return the field catalog: which fields exist and which are filterable or sortable.

        Calling this once and keeping the answer is how a filter gets built from the schema rather
        than from a guess -- an unknown field name fails the whole search with ``INVALID_REQUEST``.
        ``filter_operators`` comes back without the ``FILTER_OP_`` prefix that requests require.
        """
        if collection not in _COLLECTIONS:
            raise SciverseError(f"collection must be one of {_COLLECTIONS}")
        response = self._request("GET", "/meta-catalog", params={
            "collection": collection,
            "include_sample_values": "true" if include_sample_values else None,
            "include_field_stats": "true" if include_field_stats else None,
        }, max_retries=max_retries)
        entries = [item for item in (response.get("fields") or []) if isinstance(item, dict)]
        self._audit("meta-catalog", {"collection": collection}, [], extra={
            "n_fields": len(entries),
            "filterable": sorted(e["name"] for e in entries if e.get("filterable")),
            "sortable": sorted(e["name"] for e in entries if e.get("sortable")),
        })
        return response

    def paper_relations(
        self, unique_id: str, relation: str = "REFERENCES", *, page: int = 1,
        page_size: int = 25, max_retries: int = 4,
    ) -> dict[str, Any]:
        """Walk the citation graph one page at a time.

        Keyed on ``unique_id``, not ``doc_id``.  Each neighbour carries its own ``id_type``, and
        the mix is a property of the paper rather than of the relation: ``sciverse``, ``openalex``
        and ``semantic_scholar`` have all been observed, and the same relation on two different
        seeds returned different types.  Only a ``sciverse`` id can be fed back in here or matched
        against ``unique_id``; the others are external handles and have to be resolved by doi or
        title first.  ``total_count`` counts only in-corpus neighbours, so it undercounts
        ``citation_count``.  Backward edges are the ones that work: a paper with 261,964 recorded
        citations returned 107 ``REFERENCES`` but ``CITATIONS`` 0, and a ``references_unique_id``
        filter against the same seed also returned 0, so forward citations should be treated as
        unavailable on this deployment rather than as evidence of an uncited paper.
        """
        if relation not in RELATIONS:
            raise SciverseError(f"relation must be one of {RELATIONS}")
        if page < 1 or page_size < 1 or page_size > _MAX_RELATION_PAGE_SIZE:
            raise SciverseError(
                f"relations paging: page >= 1 and 1 <= page_size <= {_MAX_RELATION_PAGE_SIZE}"
            )
        if page * page_size > _RESULT_WINDOW:
            raise SciverseError(
                f"page {page} x page_size {page_size} exceeds the {_RESULT_WINDOW} the server "
                "will page through; filter on references_unique_id instead"
            )
        body = {"unique_id": unique_id, "relation": relation,
                "page": page, "page_size": page_size}
        started = time.monotonic()
        response = self._request(
            "POST", "/meta-paper-relations", body=body, max_retries=max_retries,
        )
        items = [item for item in (response.get("items") or []) if isinstance(item, dict)]
        response["items"] = items
        self._audit("meta-paper-relations", body, items, fields=("id", "id_type", "title"), extra={
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "total_count": response.get("total_count"),
            "total_pages": response.get("total_pages"),
        })
        return response

    def resource(self, file_name: str, *, max_retries: int = 4) -> bytes:
        """Fetch a figure or table image referenced from a document's Markdown.

        ``file_name`` is the relative target of an ``![alt](file_name)`` link in
        :meth:`content` output.  Traversal is refused locally as well as server-side.  The
        endpoint answered 405 to the one probe available here, so treat a failure as the endpoint
        being unavailable on this deployment rather than as a bad argument.
        """
        name = str(file_name)
        if not name or name.startswith("/") or "\\" in name or ".." in name.split("/"):
            raise SciverseError(
                "resource file_name must be relative, with no '..' component and no backslash"
            )
        payload = self._request(
            "GET", "/resource", params={"file_name": name}, max_retries=max_retries, raw=True,
        )
        self._audit("resource", {"file_name": name}, [], extra={
            "bytes": len(payload),
            "content_sha256": hashlib.sha256(payload).hexdigest(),
        })
        return payload


# --------------------------------------------------------------------------------------- CLI
def _filters_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return semantic_filters(
        lang=args.lang, year_from=args.year_from, year_to=args.year_to,
        domain=args.domain, venue=args.venue,
        doc_ids=list(args.doc_ids) if args.doc_ids else None,
    )


def _cmd_search(client: SciverseClient, args: argparse.Namespace) -> dict[str, Any]:
    hits = client.agentic_search(
        args.query, top_k=args.top_k, filters=_filters_from_args(args) or None,
        mode=args.mode, source_types=args.source_types or None,
    )
    return {"tool": "agentic-search", "n": len(hits), "hits": hits}


def _cmd_meta(client: SciverseClient, args: argparse.Namespace) -> dict[str, Any]:
    envelope = client.meta_search(
        args.query,
        filters=metadata_filters(
            lang=args.lang, year_from=args.year_from, year_to=args.year_to,
            venue=args.venue, author=args.author, min_citations=args.min_citations,
            cites=args.cites, require_full_text=args.require_full_text,
        ) or None,
        sort=metadata_sort(*args.sort) if args.sort else None,
        fields=args.fields or None, page=args.page, page_size=args.page_size,
    )
    return {"tool": "meta-search", **envelope}


def _cmd_content(client: SciverseClient, args: argparse.Namespace) -> dict[str, Any]:
    return {"tool": "content", **client.content(args.doc_id, args.offset, args.limit)}


def _cmd_read(client: SciverseClient, args: argparse.Namespace) -> dict[str, Any]:
    return {"tool": "read", **client.read_document(args.doc_id, max_bytes=args.max_bytes)}


def _cmd_catalog(client: SciverseClient, args: argparse.Namespace) -> dict[str, Any]:
    return {"tool": "meta-catalog", **client.meta_catalog(
        args.collection, include_sample_values=args.sample_values,
        include_field_stats=args.field_stats,
    )}


def _cmd_relations(client: SciverseClient, args: argparse.Namespace) -> dict[str, Any]:
    return {"tool": "meta-paper-relations", **client.paper_relations(
        args.unique_id, args.relation, page=args.page, page_size=args.page_size,
    )}


def _cmd_resource(client: SciverseClient, args: argparse.Namespace) -> dict[str, Any]:
    payload = client.resource(args.file_name)
    destination = extended_path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "wb") as handle:
        handle.write(payload)
    return {"tool": "resource", "file_name": args.file_name, "bytes": len(payload),
            "written_to": str(destination),
            "content_sha256": hashlib.sha256(payload).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sciverse", description="Sciverse API CLI (standard library only)",
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--audit-log", default=None)
    parser.add_argument("--quiet", action="store_true", help="suppress the stderr call summary")
    subcommands = parser.add_subparsers(dest="cmd", required=True)

    def add_transport(target: argparse.ArgumentParser) -> None:
        # SUPPRESS so a value given to the root parser survives being redeclared here.
        target.add_argument("--base-url", default=argparse.SUPPRESS)
        target.add_argument("--audit-log", default=argparse.SUPPRESS)
        target.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)

    def add_scope(target: argparse.ArgumentParser) -> None:
        target.add_argument("--lang", default=None)
        target.add_argument("--year-from", type=int, default=None)
        target.add_argument("--year-to", type=int, default=None)
        target.add_argument("--venue", default=None)

    sub = subcommands.add_parser("search", help="agentic-search: semantic evidence retrieval")
    add_transport(sub)
    sub.add_argument("query")
    sub.add_argument("--top-k", type=int, default=10)
    sub.add_argument("--mode", choices=("fast", "balanced", "quality"), default=None,
                     help="forwarded for contract fidelity; currently inert on the deployment")
    sub.add_argument("--source-types", nargs="*", choices=("web", "pdf"), default=None)
    sub.add_argument("--domain", default=None, help="e.g. 'Physical Sciences'")
    sub.add_argument("--doc-ids", nargs="*", default=None,
                     help="the one hard constraint: restrict search to these documents")
    add_scope(sub)
    sub.set_defaults(func=_cmd_search)

    sub = subcommands.add_parser("meta", help="meta-search: structured metadata retrieval")
    add_transport(sub)
    sub.add_argument("query", nargs="?", default="")
    sub.add_argument("--page", type=int, default=1)
    sub.add_argument("--page-size", type=int, default=10)
    sub.add_argument("--sort", nargs="*", default=None,
                     help=f"prefix with '-' for descending; sortable: {', '.join(SORTABLE_FIELDS)}")
    sub.add_argument("--fields", nargs="*", default=None)
    sub.add_argument("--author", default=None)
    sub.add_argument("--min-citations", type=int, default=None)
    sub.add_argument("--cites", default=None, help="papers citing this unique_id")
    sub.add_argument("--require-full-text", action="store_true",
                     help="keep only rows whose text can be read")
    add_scope(sub)
    sub.set_defaults(func=_cmd_meta)

    sub = subcommands.add_parser("content", help="read one text slice by doc_id and byte offset")
    add_transport(sub)
    sub.add_argument("--doc-id", required=True)
    sub.add_argument("--offset", type=int, default=0)
    sub.add_argument("--limit", type=int, default=4096)
    sub.set_defaults(func=_cmd_content)

    sub = subcommands.add_parser("read", help="assemble a whole document by following next_offset")
    add_transport(sub)
    sub.add_argument("--doc-id", required=True)
    sub.add_argument("--max-bytes", type=int, default=None)
    sub.set_defaults(func=_cmd_read)

    sub = subcommands.add_parser("catalog", help="meta-catalog: the authoritative field schema")
    add_transport(sub)
    sub.add_argument("--collection", choices=_COLLECTIONS, default="papers")
    sub.add_argument("--sample-values", action="store_true")
    sub.add_argument("--field-stats", action="store_true")
    sub.set_defaults(func=_cmd_catalog)

    sub = subcommands.add_parser("relations", help="meta-paper-relations: walk the citation graph")
    add_transport(sub)
    sub.add_argument("--unique-id", required=True)
    sub.add_argument("-r", "--relation", choices=RELATIONS, default="REFERENCES")
    sub.add_argument("--page", type=int, default=1)
    sub.add_argument("--page-size", type=int, default=25)
    sub.set_defaults(func=_cmd_relations)

    sub = subcommands.add_parser("resource", help="fetch a figure referenced from document text")
    add_transport(sub)
    sub.add_argument("--file-name", required=True)
    sub.add_argument("--out", required=True)
    sub.set_defaults(func=_cmd_resource)

    args = parser.parse_args(argv)
    try:
        client = SciverseClient(
            base_url=args.base_url, audit_log=args.audit_log, quiet=args.quiet,
        )
        result = args.func(client, args)
    except SciverseError as error:
        print(json.dumps(error.as_dict(), ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
