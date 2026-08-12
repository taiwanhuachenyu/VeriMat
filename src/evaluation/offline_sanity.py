"""Gold-free deterministic components for plumbing tests, never scientific baselines."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date
from pathlib import Path

from .baseline_runner import (
    BaselineBackend, BlindTask, DecisionOutput, EvidenceSelection, MethodSpec,
    QueryPlan, RetrievalResult, RetrievedPassage, StrategyCandidate, Usage,
)

TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "a", "all", "alone", "and", "are", "as", "at", "be", "by", "can", "for",
    "from", "in", "is", "it", "no", "not", "of", "on", "or", "so", "that",
    "the", "their", "this", "to", "under", "using", "with", "without",
}


def _terms(text: str) -> list[str]:
    return [token for token in TOKEN.findall(text.lower())
            if token not in STOPWORDS and len(token) > 1]


class SnapshotCorpusRetriever:
    """Lexical search over public capsules; capsule rows contain no benchmark labels."""

    provider_id = "offline-snapshot-lexical-sanity"

    def __init__(self, snapshot_path: str | Path, *, top_k: int = 3):
        if top_k < 1:
            raise ValueError("top_k must be positive")
        self.top_k = top_k
        self.rows = [json.loads(line) for line in Path(snapshot_path).read_text(
            encoding="utf-8"
        ).splitlines() if line.strip()]
        self.document_frequency = Counter()
        for row in self.rows:
            self.document_frequency.update(set(_terms(row["content"])))

    def search(
        self, *, query_id: str, query: str, intent: str, cutoff_date: str,
        operation_id: str, reserve_call=lambda _suboperation: None,
    ) -> RetrievalResult:
        del intent, operation_id
        cutoff = date.fromisoformat(cutoff_date)
        query_terms = Counter(_terms(query))
        scored = []
        for row in self.rows:
            if date.fromisoformat(row["publication_date"]) > cutoff:
                continue
            document_terms = Counter(_terms(row["content"]))
            score = 0.0
            for term, query_count in query_terms.items():
                if term not in document_terms:
                    continue
                inverse_frequency = math.log(
                    (len(self.rows) + 1) / (self.document_frequency[term] + 1)
                ) + 1
                score += min(query_count, document_terms[term]) * inverse_frequency
            if score:
                scored.append((score, row["snapshot_id"], row))
        scored.sort(key=lambda value: (-value[0], value[1]))
        passages = tuple(
            RetrievedPassage(
                # A passage node represents one retrieval observation. The same immutable
                # capsule can be observed by support and counter queries, so its node identity
                # must include the query while its content hash remains stable.
                passage_id=f"{row['snapshot_id']}@{query_id}", query_id=query_id,
                doc_id=f"doi:{row['doi']}", text=str(row["content"]),
                locator={"offset": 0}, content_sha256=str(row["content_sha256"]),
                publication_date=str(row["publication_date"]),
            )
            for _, _, row in scored[:self.top_k]
        )
        return RetrievalResult(passages=passages, usage=Usage(calls=1, tokens=0))


class DeterministicPlumbingBackend:
    """A transparent rule fixture for end-to-end tests; it is not an AI baseline."""

    provider_id = "deterministic-plumbing-sanity"

    def plan_queries(
        self, *, task: BlindTask, intent: str, operation_id: str,
    ) -> QueryPlan:
        del operation_id
        suffix = (
            " limitation failure counterexample boundary precedent"
            if intent == "counterevidence" else " evidence result mechanism"
        )
        return QueryPlan((task.prompt + suffix,), Usage(calls=0, tokens=0))

    def decide(
        self, *, task: BlindTask, method: MethodSpec,
        support_passages: tuple[RetrievedPassage, ...],
        counter_passages: tuple[RetrievedPassage, ...],
        operation_id: str,
    ) -> DecisionOutput:
        del operation_id
        if not support_passages:
            return DecisionOutput(
                decision="UNRESOLVED", counterevidence_probability=0.5,
                evidence=(), reason="offline lexical fixture retrieved no passage",
                boundary="", usage=Usage(0, 0),
            )
        prompt = task.prompt.lower()
        narrow_control = "narrowly scoped claim" in prompt
        if narrow_control or not method.external_counter_retrieval:
            return DecisionOutput(
                decision="SURVIVED", counterevidence_probability=0.1,
                evidence=(EvidenceSelection(
                    support_passages[0].passage_id, "SUPPORTS",
                ),),
                reason="deterministic plumbing fixture selected top support passage",
                boundary=f"offline capsule corpus through {task.cutoff_date}",
                usage=Usage(0, 0),
            )
        if not counter_passages:
            return DecisionOutput(
                decision="UNRESOLVED", counterevidence_probability=0.5,
                evidence=(), reason="offline counter retrieval returned no passage",
                boundary="", usage=Usage(0, 0),
            )
        boundary_markers = (
            "all practically relevant", "is sufficient", "intrinsically stable",
        )
        bounded = any(marker in prompt for marker in boundary_markers)
        relation = "BOUNDS" if bounded else "CONTRADICTS"
        return DecisionOutput(
            decision="NARROWED" if bounded else "REFUTED",
            counterevidence_probability=0.9,
            evidence=(EvidenceSelection(counter_passages[0].passage_id, relation),),
            reason="deterministic plumbing fixture selected top counter-search passage",
            boundary=(f"offline capsule corpus through {task.cutoff_date}" if bounded else ""),
            usage=Usage(0, 0),
            strategy_candidates=(StrategyCandidate(
                kind="boundary_probe",
                pattern="search direct prior work, operating boundaries, and failure mechanisms",
            ),),
        )


def assert_sanity_only(backend: BaselineBackend, retriever) -> None:
    if not (
        getattr(backend, "provider_id", "") == "deterministic-plumbing-sanity"
        and getattr(retriever, "provider_id", "") == "offline-snapshot-lexical-sanity"
    ):
        raise ValueError("sanity marker used with non-sanity components")
