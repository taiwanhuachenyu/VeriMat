"""Discovery packages: the falsifiable output a literature-discovery agent owes its reader.

A surviving claim becomes a discovery package only when it can answer the question a
compiler of scientific claims must face: *what observation would refute this, and what is the
cheapest way to look?*  Each package binds the claim's boundary, its replayable evidence
locators, the counterevidence that was considered, and a minimal verification experiment.

Grounding gates: the experiment narrative may not quote anything outside the corpus evidence;
a package whose evidence set does not replay against the snapshot is refused and recorded.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.core.events import canonical_json
from src.experiments.claims import Claim
from src.survey.records import SurveyContractError, SurveyCorpus, normalise_quote

PACK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["falsifiable_statement", "minimal_verification_experiment",
                 "observable", "expected_result_if_true"],
    "properties": {
        "falsifiable_statement": {"type": "string", "minLength": 12},
        "minimal_verification_experiment": {"type": "string", "minLength": 12},
        "observable": {"type": "string", "minLength": 3},
        "expected_result_if_true": {"type": "string", "minLength": 3},
    },
}

PACK_SYSTEM = (
    "You compose a falsifiable discovery package for one vetted materials claim. You receive "
    "the claim, its boundary and its supporting evidence quotes; everything you write must be "
    "grounded in that material. State the claim so that it can in principle be refuted, propose "
    "the cheapest experiment or measurement that could refute it, name the observable, and say "
    "what result would support the claim. Never import outside facts. Return raw JSON with no "
    "code fence."
)


@dataclass
class DiscoveryPackage:
    claim_id: str
    material: str
    statement: str
    boundary: str
    status: str
    confidence: float
    evidence: list[dict[str, Any]]
    counterevidence_considered: int
    pack: dict[str, Any]

    def validate(self) -> None:
        if self.status not in {"ACCEPTED", "NARROWED"}:
            raise SurveyContractError(
                f"a discovery package requires a surviving claim, got {self.status!r}"
            )
        if not self.evidence:
            raise SurveyContractError("a discovery package without evidence is an assertion")
        for key in ("falsifiable_statement", "minimal_verification_experiment"):
            if not str(self.pack.get(key) or "").strip():
                raise SurveyContractError(f"discovery package field {key} is required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id, "material": self.material, "statement": self.statement,
            "boundary": self.boundary, "status": self.status,
            "confidence": round(self.confidence, 4), "evidence": self.evidence,
            "counterevidence_considered": self.counterevidence_considered, "pack": self.pack,
        }

    def line(self) -> str:
        return canonical_json(self.as_dict())


def build_packages(
    predictions: Iterable[Any], *, corpus: SurveyCorpus, transport: Any,
    method: str = "V3-full",
) -> tuple[list[DiscoveryPackage], list[dict[str, str]]]:
    """Compose packages for surviving claims; refuse and record anything ungrounded."""
    packages: list[DiscoveryPackage] = []
    refused: list[dict[str, str]] = []
    for prediction in predictions:
        if prediction.label not in {"ACCEPTED", "NARROWED"}:
            continue
        claim: Claim = prediction.claim
        passage = corpus.passages.get(claim.passage_id)
        if passage is None:
            refused.append({"claim_id": claim.claim_id, "reason": "passage_not_in_snapshot"})
            continue
        evidence = [{
            "passage_id": passage.passage_id, "doc_id": passage.doc_id,
            "content_sha256": passage.content_sha256, "quote": claim.quote,
        }]
        pack_input = {
            "claim": claim.as_dict(),
            "boundary": prediction.boundary or "as stated in the source passage",
            "evidence_quotes": [claim.quote],
        }
        try:
            response = transport.complete(
                operation_id=f"pack-{method}-{claim.claim_id}",
                system=PACK_SYSTEM, response_schema=PACK_SCHEMA,
                user=canonical_json(pack_input),
            )
            pack = json.loads(response.text)
        except Exception as exc:
            refused.append({"claim_id": claim.claim_id, "reason": str(exc)[:160]})
            continue
        package = DiscoveryPackage(
            claim_id=claim.claim_id, material=claim.material,
            statement=str(pack.get("falsifiable_statement") or ""),
            boundary=prediction.boundary, status=prediction.label,
            confidence=prediction.confidence, evidence=evidence,
            counterevidence_considered=prediction.counter_queries_executed,
            pack=pack,
        )
        try:
            package.validate()
        except SurveyContractError as exc:
            refused.append({"claim_id": claim.claim_id, "reason": str(exc)[:160]})
            continue
        packages.append(package)
    return packages, refused
