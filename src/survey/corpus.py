"""Assemble a survey corpus under a recorded protocol, so its coverage is a claim with evidence.

"Targeted coverage" is scored, and a pile of search results cannot demonstrate it.  What can is a
protocol: a metadata stage that pins a candidate set with hard constraints, then a semantic stage
restricted to exactly that set.  Both stages are recorded -- including the queries that returned
nothing -- because an absence is the only support a survey can offer for "the literature does not
cover this", and an unrecorded empty query leaves every such statement unsourced.

The order matters and is not interchangeable.  Semantic search on this deployment honours exactly
one hard constraint, the document scope, so anything else asked of it is a preference rather than a
filter.  Establishing the scope first with metadata filters, which are hard, is what makes the
year window and the language a property of the corpus rather than a hope.
"""
from __future__ import annotations

from typing import Any, Protocol

from src.tools.sciverse import (
    MAX_DOC_ID_SCOPE, SATURATION_TOP_K, metadata_filters, semantic_filters,
)

from .records import (
    DATABASE_SCIVERSE, DocumentRecord, QueryRecord, SurveyContractError, SurveyCorpus,
    SurveyPassage, SurveyTopic, digest_id,
)

#: Fields the metadata stage projects.  Requested explicitly because the default projection is a
#: subset of the catalogue and omits the doi, without which a reference entry is not checkable.
METADATA_FIELDS = (
    "unique_id", "doc_id", "title", "doi", "publication_published_year",
    "publication_venue_name_unified", "citation_count",
)

#: A page of metadata results.  The endpoint clamps to 50, so asking for more only hides the clamp.
METADATA_PAGE_SIZE = 50

#: Semantic hits per probe question.  The deployment saturates near 50 chunks however much more is
#: asked for, so requesting beyond the saturation point buys nothing and misreports recall.
SEMANTIC_TOP_K = SATURATION_TOP_K

#: Documents per semantic slice.  See :meth:`CorpusBuilder._shards` for why the scope is sliced.
SCOPE_SHARD_SIZE = 25

#: Citation floor for the anchoring pass.  A disclosed parameter, not a fact about the field: it
#: buys the seminal work a phrasing-sensitive relevance ranking can miss, at the cost of a bias
#: towards older papers, which is why it runs as a second pass rather than as a global filter.
CITATION_FLOOR = 50


class LiteratureSource(Protocol):
    """The slice of the Sciverse client this package needs, so tests can script it."""

    def meta_search(
        self, query: str = "", *, filters: list[dict] | None = None,
        sort: list[dict] | None = None, fields: list[str] | None = None,
        facets: list[dict] | None = None, page: int = 1, page_size: int = 10,
        cursor: str | None = None, request_id: str | None = None, max_retries: int = 4,
    ) -> dict[str, Any]: ...

    def agentic_search(
        self, query: str, top_k: int = 10, *, filters: dict | None = None,
        mode: str | None = None, source_types: list[str] | None = None,
        request_id: str | None = None, max_retries: int = 4,
    ) -> list[dict[str, Any]]: ...


def _int_or_none(value: Any) -> int | None:
    """Coerce a numeric, which the metadata endpoint returns as a float (``1999.0``, ``136.0``)."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _year_or_none(value: Any) -> int | None:
    """Coerce a publication year, dropping the values a reference entry cannot use.

    The corpus contains rows with year 0.  Printing that in a bibliography would look fabricated,
    and arithmetic on it produces date errors, so it becomes "unknown" here rather than a number
    the rest of the pipeline has to keep second-guessing.  The bound is deliberately not shared
    with the other numerics: a citation count of 42 is not an implausible year, it is a count.
    """
    number = _int_or_none(value)
    return number if number is not None and 1600 <= number <= 2100 else None


def _non_negative(value: Any) -> int | None:
    number = _int_or_none(value)
    return number if number is not None and number >= 0 else None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def document_from_row(row: dict[str, Any]) -> DocumentRecord | None:
    """Build a bibliographic record from one metadata row, or nothing if it cannot be cited.

    A row without a ``doc_id`` has no full-text handle, so no passage can ever be anchored in it
    and it cannot contribute evidence.  Keeping it would inflate the corpus count with documents
    the survey never read.
    """
    doc_id = _text(row.get("doc_id"))
    if not doc_id:
        return None
    record = DocumentRecord(
        doc_id=doc_id,
        unique_id=_text(row.get("unique_id")),
        title=_text(row.get("title")),
        year=_year_or_none(row.get("publication_published_year")),
        venue=_text(row.get("publication_venue_name_unified")),
        doi=_text(row.get("doi")),
        citation_count=_non_negative(row.get("citation_count")) or 0,
        database=DATABASE_SCIVERSE,
    )
    try:
        record.validate()
    except SurveyContractError:
        return None
    return record


class CorpusBuilder:
    """Run the two-stage protocol and return a corpus that satisfies its own invariants."""

    def __init__(
        self, *, source: LiteratureSource, max_candidates: int = 120,
        semantic_top_k: int = SEMANTIC_TOP_K, min_passage_chars: int = 200,
        scope_shard_size: int = SCOPE_SHARD_SIZE, citation_floor: int = CITATION_FLOOR,
        document_probe_templates: tuple[str, ...] = (),
    ):
        if max_candidates < 1:
            raise SurveyContractError("a survey needs at least one candidate document")
        if max_candidates > MAX_DOC_ID_SCOPE:
            # The scope is sent to the semantic endpoint as a single filter, which refuses more
            # than this. Failing here names the real limit instead of surfacing a server error
            # after the metadata stage has already been paid for.
            raise SurveyContractError(
                f"max_candidates cannot exceed the {MAX_DOC_ID_SCOPE}-document semantic scope"
            )
        self.source = source
        self.max_candidates = max_candidates
        self.semantic_top_k = semantic_top_k
        self.min_passage_chars = min_passage_chars
        self.scope_shard_size = scope_shard_size
        self.citation_floor = citation_floor
        self.document_probe_templates = tuple(document_probe_templates)

    # ------------------------------------------------------------------- stage one: candidates
    def _passes(self, topic: SurveyTopic) -> tuple[tuple[str, list[dict[str, Any]]], ...]:
        """The metadata passes, as ``(intent, filters)`` pairs.

        Neither pass sorts.  An explicit sort degrades ``query`` from a relevance ranking into an
        OR hit-filter on this deployment, so a citation-sorted search returns the most-cited
        papers containing any one of the query words: the opposite of targeted coverage.  The
        anchoring pass gets the same effect from a citation floor, which is a hard filter and
        leaves the ranking intact, and it is recorded under its own intent so the bias it
        introduces stays visible instead of being folded into one undifferentiated corpus.

        ``require_full_text`` selects on ``doc_id`` rather than on the accessibility flag, which
        reads false on this deployment even for documents whose text loads.
        """
        common: dict[str, Any] = {
            "lang": topic.language or None, "year_from": topic.year_from,
            "year_to": topic.year_to, "require_full_text": True,
        }
        passes = [("coverage", metadata_filters(**common))]
        if self.citation_floor > 0:
            passes.append(
                ("seminal", metadata_filters(**common, min_citations=self.citation_floor))
            )
        return tuple(passes)

    def _candidate_stage(self, topic: SurveyTopic, corpus: SurveyCorpus) -> None:
        for intent, filters in self._passes(topic):
            fingerprint = digest_id("flt", filters)
            for query in topic.seed_queries:
                remaining = self.max_candidates - len(corpus.documents)
                if remaining <= 0:
                    return
                envelope = self.source.meta_search(
                    query, filters=filters, fields=list(METADATA_FIELDS),
                    page_size=min(METADATA_PAGE_SIZE, remaining),
                )
                rows = [row for row in (envelope.get("results") or []) if isinstance(row, dict)]
                for row in rows:
                    record = document_from_row(row)
                    if record is None:
                        continue
                    key = record.doc_id or record.unique_id
                    if key in corpus.documents:
                        continue
                    if len(corpus.documents) >= self.max_candidates:
                        break
                    corpus.add_document(record)
                corpus.add_query(QueryRecord(
                    query_id=digest_id("qry", "metadata", intent, query, fingerprint),
                    text=query, stage="metadata", intent=intent, n_hits=len(rows),
                    total_matched=_non_negative(envelope.get("total_count")),
                    filters_fingerprint=fingerprint,
                ))

    # -------------------------------------------------------------------- stage two: evidence
    def _shards(self, scope: list[str]) -> list[list[str]]:
        """Split the candidate scope into slices the semantic endpoint ranks within.

        One call over the whole scope returns the fifty best-matching chunks anywhere in it, which
        on a scope of a hundred papers leaves most papers unread and every negative claim resting
        on a corpus the survey only skimmed.  Ranking within slices spends more calls to make the
        per-document read rate a property of the protocol rather than of how quotable one paper
        happened to be.
        """
        size = max(1, self.scope_shard_size)
        return [scope[index:index + size] for index in range(0, len(scope), size)]

    def _evidence_stage(self, topic: SurveyTopic, corpus: SurveyCorpus) -> None:
        scope = sorted(corpus.documents)
        if not scope:
            return
        for question in topic.probe_questions:
            for shard in self._shards(scope):
                hits = self.source.agentic_search(
                    question, top_k=self.semantic_top_k,
                    filters=semantic_filters(doc_ids=shard),
                )
                query_id = digest_id("qry", "semantic", question, shard)
                for hit in hits:
                    doc_id = _text(hit.get("doc_id"))
                    body = _text(hit.get("chunk") or hit.get("text") or hit.get("content"))
                    if doc_id not in corpus.documents:
                        # The scope is a hard constraint on this endpoint, so this should not
                        # occur; if it ever does the passage is dropped rather than admitted,
                        # because a document outside the scope has no bibliography entry.
                        continue
                    if len(body) < self.min_passage_chars:
                        # A chunk too short to carry a quotable sentence produces relations that
                        # cannot be checked against it, which is worse than no relation at all.
                        continue
                    score = hit.get("score")
                    corpus.add_passage(SurveyPassage.build(
                        doc_id=doc_id, query_id=query_id, text=body,
                        offset=_non_negative(hit.get("offset")),
                        page_no=_non_negative(hit.get("page_no")),
                        score=float(score) if isinstance(score, (int, float)) else None,
                    ))
                corpus.add_query(QueryRecord(
                    query_id=query_id, text=question, stage="semantic", intent="evidence",
                    n_hits=len(hits), filters_fingerprint=digest_id("flt", shard),
                    # The endpoint saturates near fifty chunks, so a full return is the only
                    # honest signal that more matched in this slice than was looked at.
                    saturated=len(hits) >= SATURATION_TOP_K,
                ))

    def _document_probe_stage(self, topic: SurveyTopic, corpus: SurveyCorpus) -> None:
        if not self.document_probe_templates:
            return
        for doc_id in sorted(corpus.documents):
            record = corpus.documents[doc_id]
            for template in self.document_probe_templates:
                question = str(template).format(
                    title=record.title, year=record.year or "", venue=record.venue,
                    doi=record.doi, unique_id=record.unique_id, domain=topic.domain,
                )
                hits = self.source.agentic_search(
                    question, top_k=self.semantic_top_k,
                    filters=semantic_filters(doc_ids=[doc_id]),
                )
                query_id = digest_id("qry", "semantic-document", question, doc_id)
                for hit in hits:
                    hit_doc_id = _text(hit.get("doc_id"))
                    body = _text(hit.get("chunk") or hit.get("text") or hit.get("content"))
                    if hit_doc_id != doc_id or len(body) < self.min_passage_chars:
                        continue
                    score = hit.get("score")
                    corpus.add_passage(SurveyPassage.build(
                        doc_id=doc_id, query_id=query_id, text=body,
                        offset=_non_negative(hit.get("offset")),
                        page_no=_non_negative(hit.get("page_no")),
                        score=float(score) if isinstance(score, (int, float)) else None,
                    ))
                corpus.add_query(QueryRecord(
                    query_id=query_id, text=question, stage="semantic",
                    intent="document_evidence", n_hits=len(hits),
                    filters_fingerprint=digest_id("flt", [doc_id]),
                    saturated=len(hits) >= SATURATION_TOP_K,
                ))

    def build(self, topic: SurveyTopic) -> SurveyCorpus:
        topic.validate()
        corpus = SurveyCorpus(topic=topic)
        self._candidate_stage(topic, corpus)
        self._evidence_stage(topic, corpus)
        self._document_probe_stage(topic, corpus)
        corpus.validate()
        return corpus


def coverage_report(corpus: SurveyCorpus) -> dict[str, Any]:
    """State the corpus's reach and, more importantly, where it stops.

    The unread fraction is reported because it bounds every negative claim the survey makes.  A
    candidate set of a hundred papers of which sixty produced no passage supports far less than a
    reader would assume from the headline count, and hiding that is how a survey overstates itself.
    """
    corpus.validate()
    documents_with_evidence = {passage.doc_id for passage in corpus.passages.values()}
    unread = sorted(set(corpus.documents) - documents_with_evidence)
    per_document: dict[str, int] = {}
    for passage in corpus.passages.values():
        per_document[passage.doc_id] = per_document.get(passage.doc_id, 0) + 1
    empty_queries = sorted(
        record.text for record in corpus.queries.values() if record.n_hits == 0
    )
    document_queries = [
        record for record in corpus.queries.values() if record.intent == "document_evidence"
    ]
    return {
        **corpus.manifest(),
        "n_documents_with_evidence": len(documents_with_evidence),
        "n_documents_unread": len(unread),
        "unread_document_ids": unread,
        "passages_per_document": {
            "min": min(per_document.values()) if per_document else 0,
            "max": max(per_document.values()) if per_document else 0,
        },
        "queries_returning_nothing": empty_queries,
        "n_document_probes": len(document_queries),
        "n_empty_document_probes": sum(1 for record in document_queries if record.n_hits == 0),
        "protocol": {
            "stage_one": "relevance-ranked metadata search, hard filters, no sort",
            "stage_one_passes": ["coverage", "seminal"],
            "anchoring_bias": (
                "the seminal pass applies a citation floor, so the corpus is not a uniform "
                "sample of the field and leans towards work that has had time to be cited"
            ),
            "stage_two": (
                f"semantic search scoped to the candidate doc_ids in slices of "
                f"{SCOPE_SHARD_SIZE}, top_k={SEMANTIC_TOP_K} per slice"
            ),
            "scope_is_enforced_server_side": True,
            "document_probes": bool(document_queries),
            "document_probe_count": len(document_queries),
        },
    }
