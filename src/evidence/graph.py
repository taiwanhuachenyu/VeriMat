"""Deterministic projection of ledger events into a Claim–Evidence Decision Graph."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from src.core.events import EventEnvelope


class GraphInvariantError(ValueError):
    """An event would create an invalid scientific decision state."""


class ClaimState(str, Enum):
    PROPOSED = "PROPOSED"
    SUPPORTED = "SUPPORTED"
    CHALLENGED = "CHALLENGED"
    SURVIVED = "SURVIVED"
    NARROWED = "NARROWED"
    REFUTED = "REFUTED"
    UNRESOLVED = "UNRESOLVED"


class EvidenceRelation(str, Enum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    BOUNDS = "BOUNDS"
    PRECEDENT_FOR = "PRECEDENT_FOR"
    CHECKED = "CHECKED"


TERMINAL_STATES = {
    ClaimState.SURVIVED, ClaimState.NARROWED, ClaimState.REFUTED,
    ClaimState.UNRESOLVED,
}

ALLOWED_TRANSITIONS = {
    ClaimState.PROPOSED: {
        ClaimState.SUPPORTED, ClaimState.CHALLENGED, ClaimState.UNRESOLVED,
    },
    ClaimState.SUPPORTED: {ClaimState.CHALLENGED, ClaimState.UNRESOLVED},
    ClaimState.CHALLENGED: TERMINAL_STATES,
}


@dataclass(frozen=True)
class Passage:
    passage_id: str
    doc_id: str
    query_id: str
    content_sha256: str
    offset: int | None = None
    page_no: int | None = None


@dataclass(frozen=True)
class Query:
    query_id: str
    claim_id: str
    text: str
    intent: str
    n_hits: int


@dataclass
class Claim:
    claim_id: str
    text: str
    scope: str
    state: ClaimState = ClaimState.PROPOSED
    boundary: str = ""
    reason: str = ""
    version: int = 1


@dataclass(frozen=True)
class EvidenceEdge:
    claim_id: str
    passage_id: str
    relation: EvidenceRelation


@dataclass
class DecisionGraph:
    claims: dict[str, Claim] = field(default_factory=dict)
    queries: dict[str, Query] = field(default_factory=dict)
    passages: dict[str, Passage] = field(default_factory=dict)
    edges: list[EvidenceEdge] = field(default_factory=list)
    applied_event_ids: set[str] = field(default_factory=set)

    @classmethod
    def project(cls, events: Iterable[EventEnvelope]) -> "DecisionGraph":
        graph = cls()
        for event in events:
            graph.apply(event)
        return graph

    @staticmethod
    def _required(payload: dict, *keys: str) -> None:
        missing = [key for key in keys if payload.get(key) in (None, "")]
        if missing:
            raise GraphInvariantError("missing payload fields: " + ", ".join(missing))

    def apply(self, event: EventEnvelope) -> None:
        if event.event_id in self.applied_event_ids:
            return
        handlers = {
            "claim.proposed": self._claim_proposed,
            "query.executed": self._query_executed,
            "passage.observed": self._passage_observed,
            "evidence.linked": self._evidence_linked,
            "claim.transitioned": self._claim_transitioned,
        }
        handler = handlers.get(event.event_type)
        if handler is not None:
            handler(event.payload)
        self.applied_event_ids.add(event.event_id)

    def _claim_proposed(self, payload: dict) -> None:
        self._required(payload, "claim_id", "text", "scope")
        claim_id = str(payload["claim_id"])
        if claim_id in self.claims:
            raise GraphInvariantError(f"claim {claim_id!r} already exists")
        self.claims[claim_id] = Claim(
            claim_id=claim_id, text=str(payload["text"]), scope=str(payload["scope"])
        )

    def _query_executed(self, payload: dict) -> None:
        self._required(payload, "query_id", "claim_id", "text", "intent")
        query_id, claim_id = str(payload["query_id"]), str(payload["claim_id"])
        if claim_id not in self.claims:
            raise GraphInvariantError(f"query references unknown claim {claim_id!r}")
        if query_id in self.queries:
            raise GraphInvariantError(f"query {query_id!r} already exists")
        intent = str(payload["intent"])
        if intent not in {"support", "counterevidence"}:
            raise GraphInvariantError("query intent must be support or counterevidence")
        n_hits = payload.get("n_hits", 0)
        if not isinstance(n_hits, int) or n_hits < 0:
            raise GraphInvariantError("n_hits must be a non-negative integer")
        self.queries[query_id] = Query(
            query_id=query_id, claim_id=claim_id, text=str(payload["text"]),
            intent=intent, n_hits=n_hits,
        )

    def _passage_observed(self, payload: dict) -> None:
        self._required(payload, "passage_id", "doc_id", "query_id", "content_sha256")
        passage_id, query_id = str(payload["passage_id"]), str(payload["query_id"])
        if query_id not in self.queries:
            raise GraphInvariantError(f"passage references unknown query {query_id!r}")
        if passage_id in self.passages:
            raise GraphInvariantError(f"passage {passage_id!r} already exists")
        offset, page_no = payload.get("offset"), payload.get("page_no")
        if offset is None and page_no is None:
            raise GraphInvariantError("passage requires offset or page_no")
        if offset is not None and (not isinstance(offset, int) or offset < 0):
            raise GraphInvariantError("offset must be a non-negative integer")
        if page_no is not None and (not isinstance(page_no, int) or page_no < 0):
            raise GraphInvariantError("page_no must be a non-negative integer")
        content_hash = str(payload["content_sha256"])
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise GraphInvariantError("content_sha256 must be a SHA-256 hex digest")
        self.passages[passage_id] = Passage(
            passage_id=passage_id, doc_id=str(payload["doc_id"]), query_id=query_id,
            content_sha256=content_hash, offset=offset, page_no=page_no,
        )

    def _evidence_linked(self, payload: dict) -> None:
        self._required(payload, "claim_id", "passage_id", "relation")
        claim_id, passage_id = str(payload["claim_id"]), str(payload["passage_id"])
        if claim_id not in self.claims:
            raise GraphInvariantError(f"edge references unknown claim {claim_id!r}")
        if passage_id not in self.passages:
            raise GraphInvariantError(f"edge references unknown passage {passage_id!r}")
        passage = self.passages[passage_id]
        if self.queries[passage.query_id].claim_id != claim_id:
            raise GraphInvariantError("passage query belongs to a different claim")
        try:
            relation = EvidenceRelation(str(payload["relation"]))
        except ValueError as exc:
            raise GraphInvariantError(f"unknown evidence relation {payload['relation']!r}") from exc
        edge = EvidenceEdge(claim_id=claim_id, passage_id=passage_id, relation=relation)
        query_intent = self.queries[passage.query_id].intent
        if relation == EvidenceRelation.SUPPORTS and query_intent != "support":
            raise GraphInvariantError("SUPPORTS evidence must originate from a support query")
        if relation in {
            EvidenceRelation.CONTRADICTS, EvidenceRelation.BOUNDS,
            EvidenceRelation.PRECEDENT_FOR,
        } and query_intent != "counterevidence":
            raise GraphInvariantError(
                f"{relation.value} evidence must originate from a counterevidence query"
            )
        if edge in self.edges:
            raise GraphInvariantError("duplicate evidence edge")
        self.edges.append(edge)

    def _relations(self, claim_id: str) -> set[EvidenceRelation]:
        return {edge.relation for edge in self.edges if edge.claim_id == claim_id}

    def _has_counter_query(self, claim_id: str) -> bool:
        return any(
            query.claim_id == claim_id and query.intent == "counterevidence"
            for query in self.queries.values()
        )

    def _claim_transitioned(self, payload: dict) -> None:
        self._required(payload, "claim_id", "to_state", "reason")
        claim_id = str(payload["claim_id"])
        if claim_id not in self.claims:
            raise GraphInvariantError(f"transition references unknown claim {claim_id!r}")
        claim = self.claims[claim_id]
        try:
            target = ClaimState(str(payload["to_state"]))
        except ValueError as exc:
            raise GraphInvariantError(f"unknown claim state {payload['to_state']!r}") from exc
        if target not in ALLOWED_TRANSITIONS.get(claim.state, set()):
            raise GraphInvariantError(f"illegal transition {claim.state.value} -> {target.value}")
        relations = self._relations(claim_id)
        if target == ClaimState.SUPPORTED and EvidenceRelation.SUPPORTS not in relations:
            raise GraphInvariantError("SUPPORTED requires at least one SUPPORTS edge")
        if target == ClaimState.CHALLENGED and not self._has_counter_query(claim_id):
            raise GraphInvariantError("CHALLENGED requires an executed counterevidence query")
        if target == ClaimState.SURVIVED:
            if EvidenceRelation.SUPPORTS not in relations or not self._has_counter_query(claim_id):
                raise GraphInvariantError("SURVIVED requires support and a counterevidence query")
            if not str(payload.get("boundary", "")).strip():
                raise GraphInvariantError("SURVIVED requires a non-empty boundary")
        if target == ClaimState.NARROWED:
            if EvidenceRelation.BOUNDS not in relations:
                raise GraphInvariantError("NARROWED requires a BOUNDS edge")
            if not str(payload.get("boundary", "")).strip():
                raise GraphInvariantError("NARROWED requires a non-empty boundary")
        if target == ClaimState.REFUTED and not (
            {EvidenceRelation.CONTRADICTS, EvidenceRelation.PRECEDENT_FOR} & relations
        ):
            raise GraphInvariantError("REFUTED requires CONTRADICTS or PRECEDENT_FOR evidence")
        claim.state = target
        claim.reason = str(payload["reason"])
        claim.boundary = str(payload.get("boundary", claim.boundary))
        claim.version += 1

    def validate_for_publication(self) -> list[str]:
        issues: list[str] = []
        if not self.claims:
            issues.append("graph contains no claims")
        for claim in self.claims.values():
            if claim.state not in TERMINAL_STATES:
                issues.append(f"{claim.claim_id}: non-terminal state {claim.state.value}")
            if claim.state in {ClaimState.SURVIVED, ClaimState.NARROWED} and not claim.boundary:
                issues.append(f"{claim.claim_id}: surviving decision has no boundary")
        return issues

    def metrics(self) -> dict[str, int]:
        counts = {state.value: 0 for state in ClaimState}
        for claim in self.claims.values():
            counts[claim.state.value] += 1
        return {
            "claims": len(self.claims),
            "queries": len(self.queries),
            "passages": len(self.passages),
            "evidence_edges": len(self.edges),
            **{f"claims_{key.lower()}": value for key, value in counts.items()},
        }
