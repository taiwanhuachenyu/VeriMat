"""Small, injectable adapters for provider JSON envelopes.

Network policy and credentials belong to the caller. These adapters only normalize provider
records, which keeps live calls and offline fixtures on exactly the same parsing path.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .cross_validation import MaterialObservation, _required

JsonFetcher = Callable[..., Sequence[Mapping[str, Any]]]


class JsonMaterialsProvider:
    """Normalize records from a provider-specific JSON fetcher."""

    def __init__(self, *, provider_id: str, fetch: JsonFetcher):
        self.provider_id = _required(provider_id, "provider_id")
        self._fetch = fetch

    def observations(
        self, *, composition: str, property_name: str, operation_id: str,
    ) -> tuple[MaterialObservation, ...]:
        rows = self._fetch(
            composition=composition, property_name=property_name, operation_id=operation_id,
        )
        result = tuple(self._observation(row) for row in rows)
        for item in result:
            item.validate()
        return result

    def _observation(self, row: Mapping[str, Any]) -> MaterialObservation:
        required = ("id", "composition", "property", "value", "unit", "source", "content_sha256")
        missing = [field for field in required if field not in row]
        if missing:
            raise ValueError(f"{self.provider_id} observation missing fields: {', '.join(missing)}")
        return MaterialObservation(
            provider=self.provider_id,
            provider_id=str(row["id"]),
            composition=str(row["composition"]),
            property_name=str(row["property"]),
            value=float(row["value"]),
            unit=str(row["unit"]),
            source_locator=str(row["source"]),
            content_sha256=str(row["content_sha256"]),
            temperature_k=None if row.get("temperature_k") is None else float(row["temperature_k"]),
            method=str(row.get("method", "unspecified")),
            uncertainty=None if row.get("uncertainty") is None else float(row["uncertainty"]),
        )


class MaterialsProjectProvider(JsonMaterialsProvider):
    """Materials Project adapter; the fetcher owns API version and authentication details."""

    def __init__(self, *, fetch: JsonFetcher):
        super().__init__(provider_id="Materials Project", fetch=fetch)


class OQMDProvider(JsonMaterialsProvider):
    """OQMD adapter; the fetcher owns API version and authentication details."""

    def __init__(self, *, fetch: JsonFetcher):
        super().__init__(provider_id="OQMD", fetch=fetch)


class NOMADProvider(JsonMaterialsProvider):
    """NOMAD adapter; the fetcher owns API version and authentication details."""

    def __init__(self, *, fetch: JsonFetcher):
        super().__init__(provider_id="NOMAD", fetch=fetch)


__all__ = [
    "JsonMaterialsProvider",
    "MaterialsProjectProvider",
    "NOMADProvider",
    "OQMDProvider",
]

# Provider classes intentionally depend only on the normalized observation contract.
