"""The claim unit: one extracted relation carried through verification into a scored decision.

Every method variant in the comparison consumes the same claims built from the same shared,
cached extraction pass, so differences between methods are attributable to verification alone
rather than to different candidate generation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.core.events import canonical_json
from src.survey.records import ExtractedRelation, SurveyContractError, digest_id

#: Final labels the scorer compares.  ``ACCEPTED``/``REFUTED``/``NARROWED``/``UNRESOLVED`` map
#: onto the CEDG states; ``ACCEPTED`` is the label a method without a state machine emits when it
#: lets a claim through.
LABELS = ("ACCEPTED", "REFUTED", "NARROWED", "UNRESOLVED")

#: Query templates for verification searches inside the discovery window.  Fixed at
#: preregistration so no method can tune its own searches; ``{material}``, ``{feature}`` and
#: ``{property}`` are filled from the claim.
COUNTEREVIDENCE_TEMPLATES = (
    "{material} {feature} {property} decrease contrary unexpected",
    "{material} {property} degradation failure limit drawback",
    "{material} {feature} {property} discrepancy inconsistency between studies",
)


@dataclass(frozen=True)
class Claim:
    """One checkable structure-property statement, method-independent."""

    claim_id: str
    relation_id: str
    material: str
    structural_feature: str
    property_name: str
    direction: str
    quote: str
    passage_id: str
    composition: str = ""
    value: str = ""
    unit: str = ""

    @classmethod
    def from_relation(cls, relation: ExtractedRelation) -> "Claim":
        return cls(
            claim_id=digest_id(
                "claim", relation.material, relation.composition, relation.structural_feature,
                relation.property_name, relation.direction,
            ),
            relation_id=relation.relation_id,
            material=relation.material, structural_feature=relation.structural_feature,
            property_name=relation.property_name, direction=relation.direction,
            quote=relation.quote, passage_id=relation.passage_id,
            composition=relation.composition, value=relation.value, unit=relation.unit,
        )

    def search_fragments(self) -> dict[str, str]:
        return {
            "material": self.material, "feature": self.structural_feature,
            "property": self.property_name,
        }

    def as_dict(self) -> dict[str, Any]:
        payload = {
            key: getattr(self, key)
            for key in ("claim_id", "relation_id", "material", "structural_feature",
                        "property_name", "direction", "quote", "passage_id", "composition",
                        "value", "unit")
        }
        return payload


@dataclass
class VerifiedClaim:
    """One claim under one method: the label the method outputs, and what it cost."""

    method: str
    claim: Claim
    label: str
    confidence: float
    counter_queries_executed: int = 0
    counter_passages_read: int = 0
    db_checked: bool = False
    boundary: str = ""
    notes: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.label not in LABELS:
            raise SurveyContractError(f"label must be one of {LABELS}, got {self.label!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise SurveyContractError("confidence must lie in [0, 1]")

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method, "claim": self.claim.as_dict(), "label": self.label,
            "confidence": round(self.confidence, 4),
            "counter_queries_executed": self.counter_queries_executed,
            "counter_passages_read": self.counter_passages_read,
            "db_checked": self.db_checked, "boundary": self.boundary, "notes": self.notes,
        }

    def line(self) -> str:
        return canonical_json(self.as_dict())
