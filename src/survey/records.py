"""The record types a literature survey is made of, each anchored so a reader can re-find it.

The competition checks cited claims against a full-text index and treats a fabricated citation as
a violation, so every type here carries the anchor a third party needs to land on the same words:
the document handle, the byte offset the retriever reported, the digest of the text that was read,
and the query that surfaced it.  A claim that cannot name those is not admissible, and the gates
in this package refuse it rather than emitting an unverifiable sentence.

Identifiers are digests of content rather than counters.  Two runs over the same corpus therefore
produce the same passage, relation and gap identifiers, which is what makes a survey diffable: a
changed identifier means the underlying text changed, not that the run order did.  It also means a
report can be regenerated without renumbering every citation.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.core.events import canonical_json
from src.evidence.graph import Passage

DATABASE_SCIVERSE = "Sciverse"

#: Extraction records are only useful if they aggregate, and they only aggregate if the property
#: name is drawn from a closed set.  Free-text property names produced a coverage matrix in which
#: "ZT", "zT" and "figure of merit" were three unrelated columns, which understates coverage and
#: manufactures gaps that do not exist.
PROPERTIES = (
    "ZT", "Seebeck coefficient", "electrical conductivity", "electrical resistivity",
    "thermal conductivity", "lattice thermal conductivity", "power factor",
    "carrier concentration", "carrier mobility", "band gap", "effective mass",
    "phonon lifetime", "thermal stability", "hardness",
)

#: The direction of effect is the whole content of a structure-property relation.  "Se vacancies
#: change ZT" is not a finding; "Se vacancies raise ZT" is.
DIRECTIONS = ("increase", "decrease", "non_monotonic", "unchanged", "unclear")

#: How the number was obtained bounds what it can be used for.  A DFT lattice thermal
#: conductivity and a measured one disagree routinely, and treating them as one population is how
#: a survey ends up reporting a contradiction that is only a difference in method.
METHODS = ("experiment", "dft", "simulation", "review", "unspecified")

#: A gap is only worth stating if its shape says what would close it.
GAP_KINDS = (
    "unexplored_combination",  # the corpus covers A and B separately but never together
    "contradictory_evidence",  # two sources disagree on the direction of the same effect
    "single_source",  # a relation rests on exactly one paper
    "missing_condition",  # a relation is reported without the condition it depends on
    "unvalidated_mechanism",  # a mechanism is asserted but never measured
)

#: The distinction the task statement asks for explicitly: is this gap news, or is it already
#: known and merely unaddressed?  Collapsing the two is what produces a survey that claims
#: novelty for a textbook fact.
NOVELTY = ("new", "known")

_WHITESPACE = re.compile(r"\s+")
_DOC_ID = re.compile(r"^[0-9a-f]{64}$")


class SurveyContractError(ValueError):
    """A survey record is not admissible as evidence."""


def digest_id(prefix: str, *parts: Any) -> str:
    """A short content-addressed identifier, stable across runs and platforms.

    ``canonical_json`` fixes key order and separators, so the digest does not change because a
    dictionary was built in a different order.  Sixteen hex characters leave the identifiers
    readable in a LaTeX ``\\cite`` key while keeping a collision implausible at survey scale.
    """
    body = canonical_json(list(parts)).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(body).hexdigest()[:16]}"


def normalise_quote(text: str) -> str:
    """Fold a quote to the form used for substring checking.

    Extracted PDF text carries line-wrap artefacts, so a quote copied out of a passage rarely
    matches it byte for byte: runs of whitespace differ and capitalisation follows the layout.
    Only whitespace and case are folded.  Punctuation and digits are left alone, because a gate
    that ignored them would accept "ZT of 2.6" as evidence for a passage reading "ZT of 1.2".
    """
    return _WHITESPACE.sub(" ", str(text)).strip().casefold()


def _require(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SurveyContractError(f"{name} is required")
    return text


def _one_of(value: Any, allowed: tuple[str, ...], name: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise SurveyContractError(f"{name} must be one of {allowed}, got {text!r}")
    return text


@dataclass(frozen=True)
class SurveyTopic:
    """The subfield under survey and the envelope that bounds the search.

    The envelope is part of the evidence, not a runtime detail.  "The corpus contains no work on
    X" is only a defensible statement alongside the year window, language and domain that were in
    force, so the fingerprint below travels with every report this topic produces.
    """

    topic_id: str
    title: str
    seed_queries: tuple[str, ...]
    probe_questions: tuple[str, ...]
    year_from: int | None = None
    year_to: int | None = None
    language: str = "en"
    domain: str = ""

    def validate(self) -> None:
        _require(self.topic_id, "topic_id")
        _require(self.title, "title")
        if not self.seed_queries:
            raise SurveyContractError("a topic needs at least one seed query")
        if not self.probe_questions:
            raise SurveyContractError("a topic needs at least one probe question")
        if any(not str(item).strip() for item in self.seed_queries + self.probe_questions):
            raise SurveyContractError("a topic query cannot be blank")
        if (
            self.year_from is not None and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise SurveyContractError("year_from cannot exceed year_to")

    def fingerprint(self) -> str:
        return digest_id("topic", {
            "title": self.title,
            "seed_queries": list(self.seed_queries),
            "probe_questions": list(self.probe_questions),
            "year_from": self.year_from, "year_to": self.year_to,
            "language": self.language, "domain": self.domain,
        })


@dataclass(frozen=True)
class DocumentRecord:
    """A bibliographic record, carrying what a checkable reference entry needs.

    ``doc_id`` is the full-text handle and ``unique_id`` the metadata handle; they are different
    namespaces on this deployment and only the second one walks the citation graph.  Both are kept
    because a reference the committee can verify needs the bibliographic side, while the evidence
    chain needs the side the text was actually read from.
    """

    doc_id: str
    unique_id: str
    title: str
    year: int | None
    venue: str
    doi: str
    citation_count: int = 0
    database: str = DATABASE_SCIVERSE

    def validate(self) -> None:
        _require(self.title, "title")
        _require(self.database, "database")
        if not self.doc_id and not self.unique_id:
            raise SurveyContractError("a document needs a doc_id or a unique_id")
        if self.doc_id and not _DOC_ID.match(self.doc_id):
            raise SurveyContractError("doc_id must be 64 hex characters")
        if self.year is not None and not 1600 <= int(self.year) <= 2100:
            # Year 0 occurs in this corpus, so an implausible year is dropped rather than trusted:
            # a reference entry printed with it would look fabricated to a reviewer.
            raise SurveyContractError(f"implausible publication year {self.year}")

    def is_citable(self) -> bool:
        """Whether this record can be printed as a reference a reader could look up.

        A title alone is not enough to find a paper, and a reviewer checking a bibliography against
        a full-text index needs either the doi or the venue and year.
        """
        return bool(self.title.strip()) and bool(
            self.doi.strip() or (self.venue.strip() and self.year)
        )

    def bib_key(self) -> str:
        """A BibTeX key that is stable, unique and free of characters BibTeX rejects."""
        stem = re.sub(r"[^A-Za-z0-9]+", "", (self.title or "").title())[:24] or "Ref"
        return f"{stem}{self.year or 0}{digest_id('', self.doc_id, self.unique_id)[1:9]}"


@dataclass(frozen=True)
class QueryRecord:
    """One executed search, kept because coverage claims rest on what was asked, not what was found.

    ``n_hits`` is recorded even when zero.  A query that returned nothing is the only evidence a
    survey can offer for "the corpus does not cover this", and dropping empty queries would leave
    every absence unsupported.
    """

    query_id: str
    text: str
    stage: str
    intent: str
    n_hits: int
    #: How many rows matched, against how many were read.  The metadata endpoint reports this and
    #: saturates it at ten thousand, so it is the difference between "this is the literature" and
    #: "this is the part of the literature the survey looked at", the second being the only one
    #: the corpus can actually support.
    total_matched: int | None = None
    filters_fingerprint: str = ""
    saturated: bool = False
    database: str = DATABASE_SCIVERSE

    def validate(self) -> None:
        _require(self.query_id, "query_id")
        _one_of(self.stage, ("metadata", "semantic"), "stage")
        _require(self.intent, "intent")
        _require(self.database, "database")
        if self.n_hits < 0:
            raise SurveyContractError("n_hits cannot be negative")
        if self.total_matched is not None and self.total_matched < self.n_hits:
            raise SurveyContractError(
                f"query {self.query_id} read {self.n_hits} rows out of a reported "
                f"{self.total_matched}, which cannot be right"
            )
        if self.stage == "semantic" and not str(self.text).strip():
            raise SurveyContractError("a semantic query cannot be blank")


@dataclass(frozen=True)
class SurveyPassage:
    """A retrieved span of text, anchored so a third party can land on the same words.

    ``content_sha256`` digests the text as it was read.  If the corpus is re-fetched later and the
    digest differs, the citation is stale rather than false, and the difference is visible instead
    of silent.
    """

    passage_id: str
    doc_id: str
    query_id: str
    text: str
    content_sha256: str
    offset: int | None = None
    page_no: int | None = None
    score: float | None = None
    retrieved_by: str = "agentic-search"
    database: str = DATABASE_SCIVERSE

    @classmethod
    def build(
        cls, *, doc_id: str, query_id: str, text: str, offset: int | None = None,
        page_no: int | None = None, score: float | None = None,
        retrieved_by: str = "agentic-search", database: str = DATABASE_SCIVERSE,
    ) -> "SurveyPassage":
        body = str(text or "")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        # The identifier covers the document, the offset and the digest, so the same chunk reached
        # by two different queries is one passage rather than two, and a chunk whose text changed
        # under a stable offset is a different passage rather than a silent overwrite.
        return cls(
            passage_id=digest_id("psg", doc_id, offset, digest), doc_id=doc_id,
            query_id=query_id, text=body, content_sha256=digest, offset=offset,
            page_no=page_no, score=score, retrieved_by=retrieved_by, database=database,
        )

    def validate(self) -> None:
        _require(self.passage_id, "passage_id")
        _require(self.doc_id, "doc_id")
        _require(self.query_id, "query_id")
        _require(self.database, "database")
        if not str(self.text).strip():
            raise SurveyContractError("a passage with no text cannot support a claim")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.content_sha256:
            raise SurveyContractError(f"passage {self.passage_id} digest does not match its text")
        if self.offset is not None and self.offset < 0:
            raise SurveyContractError("a passage offset cannot be negative")

    def supports_quote(self, quote: str) -> bool:
        """Whether ``quote`` really occurs in this passage, after folding layout noise."""
        needle = normalise_quote(quote)
        return bool(needle) and needle in normalise_quote(self.text)

    def as_graph_passage(self) -> Passage:
        """Project into the ledger's passage type so a survey citation joins the decision graph."""
        return Passage(
            passage_id=self.passage_id, doc_id=self.doc_id, query_id=self.query_id,
            content_sha256=self.content_sha256, offset=self.offset, page_no=self.page_no,
        )


@dataclass(frozen=True)
class ExtractedRelation:
    """One structure-property statement, bound to the words that assert it.

    This is the unit the advanced route searches over and the unit the basic task reports, so it
    is deliberately narrow: a material, a structural handle on it, one property, and the direction
    the property moves.  Anything broader stops being checkable against a single passage.
    """

    relation_id: str
    passage_id: str
    material: str
    structural_feature: str
    property_name: str
    direction: str
    quote: str
    composition: str = ""
    value: str = ""
    unit: str = ""
    temperature_k: str = ""
    method: str = "unspecified"

    @classmethod
    def build(cls, *, passage_id: str, material: str, structural_feature: str,
              property_name: str, direction: str, quote: str, composition: str = "",
              value: str = "", unit: str = "", temperature_k: str = "",
              method: str = "unspecified") -> "ExtractedRelation":
        return cls(
            relation_id=digest_id(
                "rel", passage_id, material, composition, structural_feature,
                property_name, direction,
            ),
            passage_id=passage_id, material=material, structural_feature=structural_feature,
            property_name=property_name, direction=direction, quote=quote,
            composition=composition, value=value, unit=unit, temperature_k=temperature_k,
            method=method,
        )

    def validate(self) -> None:
        _require(self.relation_id, "relation_id")
        _require(self.passage_id, "passage_id")
        _require(self.material, "material")
        _require(self.structural_feature, "structural_feature")
        _require(self.quote, "quote")
        _one_of(self.property_name, PROPERTIES, "property_name")
        _one_of(self.direction, DIRECTIONS, "direction")
        _one_of(self.method, METHODS, "method")
        if self.value and not self.unit and self.property_name != "ZT":
            # ZT is dimensionless; every other property here has a unit, and a bare number is not
            # comparable across papers.
            raise SurveyContractError(
                f"{self.property_name} value {self.value!r} needs a unit to be comparable"
            )

    def coverage_key(self) -> tuple[str, str, str]:
        """The cell this relation occupies in the coverage matrix the gap finder reads."""
        return (self.material.casefold(), self.structural_feature.casefold(), self.property_name)


@dataclass(frozen=True)
class ResearchGap:
    """A stated gap, with the passages that make it a gap rather than an opinion.

    ``novelty`` carries the distinction the task statement asks for outright.  A gap the corpus
    itself already names is ``known``; only a gap that follows from the evidence without any
    source saying so is ``new``, and ``novelty_basis`` has to say which passages establish that.
    """

    gap_id: str
    kind: str
    statement: str
    novelty: str
    novelty_basis: str
    supporting_passages: tuple[str, ...]
    supporting_relations: tuple[str, ...] = ()
    #: For a ``known`` gap, the words in the corpus that say it is known.  Without this the
    #: distinction the task statement asks for is a label anyone can assert; with it, "already
    #: known" is checkable by the same substring gate every other quote passes through.
    novelty_quote: str = ""

    @classmethod
    def build(cls, *, kind: str, statement: str, novelty: str, novelty_basis: str,
              supporting_passages: Iterable[str], supporting_relations: Iterable[str] = (),
              novelty_quote: str = "") -> "ResearchGap":
        passages = tuple(dict.fromkeys(supporting_passages))
        relations = tuple(dict.fromkeys(supporting_relations))
        return cls(
            gap_id=digest_id("gap", kind, statement, list(passages)),
            kind=kind, statement=statement, novelty=novelty, novelty_basis=novelty_basis,
            supporting_passages=passages, supporting_relations=relations,
            novelty_quote=novelty_quote,
        )

    def validate(self) -> None:
        _require(self.gap_id, "gap_id")
        _require(self.statement, "statement")
        _require(self.novelty_basis, "novelty_basis")
        _one_of(self.kind, GAP_KINDS, "kind")
        _one_of(self.novelty, NOVELTY, "novelty")
        if not self.supporting_passages:
            # An unsourced gap is the failure mode the task statement calls out by name, so it is
            # refused at construction rather than filtered later.
            raise SurveyContractError(f"gap {self.gap_id} cites no passage")
        if self.novelty == "known" and not self.novelty_quote.strip():
            raise SurveyContractError(
                f"gap {self.gap_id} claims the corpus already knows this, but quotes nothing that "
                "says so, which makes the claim unfalsifiable"
            )
        if self.novelty == "new" and self.novelty_quote.strip():
            # A quote saying the gap is recognised is exactly the evidence that it is not new, so
            # carrying one under "new" is a contradiction rather than extra support.
            raise SurveyContractError(
                f"gap {self.gap_id} is marked new yet quotes a source recognising it"
            )


@dataclass
class SurveyCorpus:
    """Everything one survey run retrieved, keyed for lookup and closed under its own references.

    The invariants are checked here rather than at the point of use, because every consumer needs
    the same ones: a passage must belong to a document that was recorded, and to a query that was
    executed.  A corpus that satisfies them can be handed to the extractor, the gap finder and the
    report writer without any of them re-deriving the join.
    """

    topic: SurveyTopic
    documents: dict[str, DocumentRecord] = field(default_factory=dict)
    passages: dict[str, SurveyPassage] = field(default_factory=dict)
    queries: dict[str, QueryRecord] = field(default_factory=dict)

    def add_document(self, record: DocumentRecord) -> None:
        record.validate()
        key = record.doc_id or record.unique_id
        self.documents[key] = record

    def add_query(self, record: QueryRecord) -> None:
        record.validate()
        self.queries[record.query_id] = record

    def add_passage(self, passage: SurveyPassage) -> None:
        passage.validate()
        self.passages[passage.passage_id] = passage

    def document_for(self, passage: SurveyPassage) -> DocumentRecord | None:
        return self.documents.get(passage.doc_id)

    def validate(self) -> None:
        self.topic.validate()
        for passage in self.passages.values():
            passage.validate()
            if passage.doc_id not in self.documents:
                raise SurveyContractError(
                    f"passage {passage.passage_id} cites document {passage.doc_id}, which was "
                    "never recorded, so its reference entry cannot be built"
                )
            if passage.query_id not in self.queries:
                raise SurveyContractError(
                    f"passage {passage.passage_id} came from query {passage.query_id}, which was "
                    "never recorded, so its provenance cannot be shown"
                )

    def citable_documents(self) -> dict[str, DocumentRecord]:
        return {key: record for key, record in self.documents.items() if record.is_citable()}

    def manifest(self) -> dict[str, Any]:
        """A summary that states the survey's reach, including what it could not cite."""
        citable = self.citable_documents()
        years = sorted({record.year for record in self.documents.values() if record.year})
        return {
            "topic_id": self.topic.topic_id,
            "topic_fingerprint": self.topic.fingerprint(),
            "databases": sorted({record.database for record in self.documents.values()}),
            "n_queries": len(self.queries),
            "n_documents": len(self.documents),
            "n_citable_documents": len(citable),
            "n_passages": len(self.passages),
            "n_empty_queries": sum(
                1 for record in self.queries.values() if record.n_hits == 0
            ),
            "n_saturated_queries": sum(
                1 for record in self.queries.values() if record.saturated
            ),
            "year_span": [years[0], years[-1]] if years else [],
        }
