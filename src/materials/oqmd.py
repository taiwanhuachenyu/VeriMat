"""Live OQMD adapter: turn REST responses into provider rows for cross-validation.

The Materials Project requires an account key, and NOMAD's API targets calculations rather than
static entries, so OQMD is the default composition oracle: no key, stable JSON, and formation
energy plus ``delta_e`` (energy above hull) are exactly the stability quantities a literature
claim about phase stability can be checked against.  The fetcher is injected into
:class:`~src.materials.providers.JsonMaterialsProvider`, which owns normalisation and hashing;
this module only speaks HTTP and shapes rows.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

OQMD_BASE = "https://oqmd.org/api/v1/"

#: Property names a thermoelectric/electrolyte claim may use that OQMD can actually speak to.
OQMD_PROPERTIES = {
    "formation energy": "formation_energy",
    "energy above hull": "delta_e",
    "band gap": "band_gap",
}


class OQMDFetchError(RuntimeError):
    """The OQMD endpoint is unreachable or returned an unusable payload."""


def fetch_oqmd(
    *, composition: str, property_name: str, operation_id: str,
    base_url: str = OQMD_BASE, timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Rows shaped for ``JsonMaterialsProvider.observations``; empty when nothing matches."""
    field = OQMD_PROPERTIES.get(str(property_name).strip().casefold())
    if field is None:
        return []
    query = urllib.parse.urlencode({
        "formula": composition, "limit": 10, "format": "json",
    })
    url = base_url.rstrip("/") + "/entries?" + query
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "VeriMat/0.1"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
    except Exception as exc:
        raise OQMDFetchError(f"OQMD request failed: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise OQMDFetchError(f"OQMD returned non-JSON: {exc}") from exc
    entries = payload.get("data") or []
    rows: list[dict[str, Any]] = []
    for entry in entries:
        value = entry.get(field)
        if value is None:
            continue
        body = json.dumps(entry, sort_keys=True).encode("utf-8")
        rows.append({
            "id": str(entry.get("entry_id", "")),
            "composition": str(entry.get("composition", composition)),
            "property": str(property_name),
            "value": float(value),
            "unit": {"formation_energy": "eV/atom", "delta_e": "eV/atom", "band_gap": "eV"}[field],
            "source": url,
            "content_sha256": hashlib.sha256(body).hexdigest(),
        })
    return rows
