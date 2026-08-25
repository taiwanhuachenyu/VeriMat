"""Deterministic numeric cross-validation for literature and materials databases.

The competition requires a finding to be distinguished from a database-validated result.
This module does not blur those concepts: it pairs explicitly anchored numeric observations,
records why a candidate could not be paired, and reports numerical agreement separately from
the evidence gate that admits a discovery.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol, Sequence

from src.core.events import canonical_json

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORMULA_PART = re.compile(r"([A-Z][a-z]?)([0-9]+(?:\.[0-9]+)?)?")

_PROPERTY_ALIASES = {
    "zt": "ZT",
    "figure of merit": "ZT",
    "band gap": "band_gap",
    "bandgap": "band_gap",
    "formation energy": "formation_energy_per_atom",
    "formation energy per atom": "formation_energy_per_atom",
    "energy above hull": "energy_above_hull",
    "thermal conductivity": "thermal_conductivity",
    "lattice thermal conductivity": "lattice_thermal_conductivity",
    "seebeck coefficient": "seebeck_coefficient",
    "electrical conductivity": "electrical_conductivity",
    "power factor": "power_factor",
}

_UNITS = {
    "1": ("dimensionless", 1.0),
    "": ("dimensionless", 1.0),
    "ev": ("energy", 1.0),
    "mev": ("energy", 0.001),
    "ev/atom": ("energy_per_atom", 1.0),
    "mev/atom": ("energy_per_atom", 0.001),
    "w/(m*k)": ("thermal_conductivity", 1.0),
    "w/mk": ("thermal_conductivity", 1.0),
    "mw/(m*k)": ("thermal_conductivity", 0.001),
    "uv/k": ("seebeck", 1.0),
    "mv/k": ("seebeck", 1000.0),
    "v/k": ("seebeck", 1_000_000.0),
    "s/m": ("conductivity", 1.0),
    "s/cm": ("conductivity", 100.0),
    "w/(m*k^2)": ("power_factor", 1.0),
    "uw/(cm*k^2)": ("power_factor", 0.0001),
}


class CrossValidationError(ValueError):
    """An observation or validation request violates the numeric evidence contract."""


def _required(value: str, name: str) -> str:
    text = str(value).strip()
    if not text:
        raise CrossValidationError(f"{name} is required")
    return text


def normalise_composition(value: str) -> str:
    """Canonicalize a flat chemical formula without claiming to parse crystal chemistry."""

    compact = re.sub(r"\s+", "", _required(value, "composition"))
    if any(character in compact for character in "()[]+-"):
        raise CrossValidationError("composition must be a flat neutral formula")
    parts: list[tuple[str, Fraction]] = []
    offset = 0
    for match in _FORMULA_PART.finditer(compact):
        if match.start() != offset:
            raise CrossValidationError("composition contains an unsupported formula token")
        element, raw_amount = match.groups()
        parts.append((element, Fraction(raw_amount or "1")))
        offset = match.end()
    if offset != len(compact) or not parts:
        raise CrossValidationError("composition contains an unsupported formula token")
    totals: dict[str, Fraction] = {}
    for element, amount in parts:
        totals[element] = totals.get(element, Fraction()) + amount
    denominator = math.lcm(*(amount.denominator for amount in totals.values()))
    integers = [int(amount * denominator) for amount in totals.values()]
    divisor = math.gcd(*integers)
    rendered = []
    for element in sorted(totals):
        amount = totals[element] * denominator / divisor
        number = "" if amount == 1 else str(float(amount)).rstrip("0").rstrip(".")
        rendered.append(element + number)
    return "".join(rendered)


def normalise_property(value: str) -> str:
    folded = re.sub(r"\s+", " ", _required(value, "property_name").casefold()).strip()
    return _PROPERTY_ALIASES.get(folded, folded.replace(" ", "_"))


def _normalise_unit(value: str) -> str:
    return re.sub(r"\s+", "", str(value).casefold()).replace("·", "*")


@dataclass(frozen=True)
class MaterialObservation:
    """One numeric observation whose provenance identifies the exact source record."""

    provider: str
    provider_id: str
    composition: str
    property_name: str
    value: float
    unit: str
    source_locator: str
    content_sha256: str
    temperature_k: float | None = None
    method: str = "unspecified"
    uncertainty: float | None = None

    def validate(self) -> None:
        for field in ("provider", "provider_id", "composition", "property_name", "source_locator"):
            _required(getattr(self, field), field)
        if not _SHA256.fullmatch(self.content_sha256):
            raise CrossValidationError("content_sha256 must be 64 lowercase hex characters")
        if not math.isfinite(self.value):
            raise CrossValidationError("value must be finite")
        if self.temperature_k is not None and (
            not math.isfinite(self.temperature_k) or self.temperature_k < 0
        ):
            raise CrossValidationError("temperature_k must be finite and non-negative")
        if self.uncertainty is not None and (
            not math.isfinite(self.uncertainty) or self.uncertainty < 0
        ):
            raise CrossValidationError("uncertainty must be finite and non-negative")
        if _normalise_unit(self.unit) not in _UNITS:
            raise CrossValidationError(f"unsupported unit {self.unit!r}")

    @property
    def normalized_composition(self) -> str:
        return normalise_composition(self.composition)

    @property
    def normalized_property(self) -> str:
        return normalise_property(self.property_name)

    @property
    def canonical_value(self) -> tuple[str, float]:
        self.validate()
        dimension, scale = _UNITS[_normalise_unit(self.unit)]
        return dimension, self.value * scale

    def observation_id(self) -> str:
        payload = {
            "provider": self.provider,
            "provider_id": self.provider_id,
            "composition": self.normalized_composition,
            "property_name": self.normalized_property,
            "value": self.value,
            "unit": self.unit,
            "source_locator": self.source_locator,
            "content_sha256": self.content_sha256,
            "temperature_k": self.temperature_k,
            "method": self.method,
            "uncertainty": self.uncertainty,
        }
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        return "observation-" + digest[:16]


class MaterialsProvider(Protocol):
    """A provider adapter returning normalized, provenance-bearing observations."""

    provider_id: str

    def observations(
        self, *, composition: str, property_name: str, operation_id: str,
    ) -> Sequence[MaterialObservation]: ...


@dataclass(frozen=True)
class MatchPolicy:
    temperature_tolerance_k: float = 25.0
    require_same_method: bool = True
    tolerance_absolute: float = 0.0
    tolerance_relative: float = 0.1

    def validate(self) -> None:
        for field in ("temperature_tolerance_k", "tolerance_absolute", "tolerance_relative"):
            value = getattr(self, field)
            if not math.isfinite(value) or value < 0:
                raise CrossValidationError(f"{field} must be finite and non-negative")


@dataclass(frozen=True)
class NumericComparison:
    literature_id: str
    database_id: str
    composition: str
    property_name: str
    literature_value: float
    database_value: float
    canonical_unit_dimension: str
    residual: float
    temperature_delta_k: float | None
    within_tolerance: bool


@dataclass(frozen=True)
class CrossValidationReport:
    comparisons: tuple[NumericComparison, ...]
    unmatched: tuple[dict[str, str], ...]
    metrics: dict[str, float | int | None]


def _pair_reason(
    literature: MaterialObservation, database: MaterialObservation, policy: MatchPolicy,
) -> tuple[bool, str, float | None]:
    if literature.normalized_composition != database.normalized_composition:
        return False, "composition_mismatch", None
    if literature.normalized_property != database.normalized_property:
        return False, "property_mismatch", None
    if policy.require_same_method and literature.method != database.method:
        return False, "method_mismatch", None
    left_dimension, _ = literature.canonical_value
    right_dimension, _ = database.canonical_value
    if left_dimension != right_dimension:
        return False, "unit_dimension_mismatch", None
    if literature.temperature_k is None and database.temperature_k is None:
        return True, "matched", None
    if literature.temperature_k is None or database.temperature_k is None:
        return False, "temperature_unavailable", None
    delta = abs(literature.temperature_k - database.temperature_k)
    if delta > policy.temperature_tolerance_k:
        return False, "temperature_mismatch", delta
    return True, "matched", delta


def _metrics(comparisons: Sequence[NumericComparison]) -> dict[str, float | int | None]:
    if not comparisons:
        return {
            "n_pairs": 0,
            "mae": None,
            "rmse": None,
            "bias": None,
            "r_squared": None,
            "tolerance_pass_rate": None,
        }
    residuals = [item.residual for item in comparisons]
    expected = [item.literature_value for item in comparisons]
    count = len(comparisons)
    mean = sum(expected) / count
    total = sum((value - mean) ** 2 for value in expected)
    residual_sum = sum(value ** 2 for value in residuals)
    return {
        "n_pairs": count,
        "mae": sum(abs(value) for value in residuals) / count,
        "rmse": math.sqrt(residual_sum / count),
        "bias": sum(residuals) / count,
        "r_squared": None if total == 0 else 1 - residual_sum / total,
        "tolerance_pass_rate": sum(item.within_tolerance for item in comparisons) / count,
    }


def compare_observations(
    literature: Sequence[MaterialObservation], database: Sequence[MaterialObservation],
    *, policy: MatchPolicy = MatchPolicy(),
) -> CrossValidationReport:
    """Match each literature observation once and report non-comparable records explicitly."""

    policy.validate()
    for observation in tuple(literature) + tuple(database):
        observation.validate()
    remaining = sorted(
        database, key=lambda item: (item.provider, item.provider_id, item.observation_id()),
    )
    comparisons: list[NumericComparison] = []
    unmatched: list[dict[str, str]] = []
    for item in sorted(literature, key=lambda observation: observation.observation_id()):
        candidates: list[tuple[float, MaterialObservation, float | None]] = []
        reasons: list[str] = []
        for candidate in remaining:
            admitted, reason, temperature_delta = _pair_reason(item, candidate, policy)
            if admitted:
                distance = temperature_delta if temperature_delta is not None else -1.0
                candidates.append((distance, candidate, temperature_delta))
            else:
                reasons.append(reason)
        if not candidates:
            unmatched.append({
                "observation_id": item.observation_id(),
                "reason": sorted(set(reasons))[0] if reasons else "no_provider_observation",
            })
            continue
        _, candidate, temperature_delta = min(
            candidates, key=lambda value: (value[0], value[1].provider, value[1].provider_id),
        )
        remaining.remove(candidate)
        dimension, left = item.canonical_value
        _, right = candidate.canonical_value
        residual = right - left
        tolerance = max(policy.tolerance_absolute, abs(left) * policy.tolerance_relative)
        comparisons.append(NumericComparison(
            literature_id=item.observation_id(),
            database_id=candidate.observation_id(),
            composition=item.normalized_composition,
            property_name=item.normalized_property,
            literature_value=left,
            database_value=right,
            canonical_unit_dimension=dimension,
            residual=residual,
            temperature_delta_k=temperature_delta,
            within_tolerance=abs(residual) <= tolerance,
        ))
    return CrossValidationReport(tuple(comparisons), tuple(unmatched), _metrics(comparisons))
