import hashlib

from src.materials import MaterialsProjectProvider, NOMADProvider, OQMDProvider


def row(identifier, provider="fixture", value=0.15):
    return {
        "id": identifier,
        "composition": "Bi2Te3",
        "property": "band gap",
        "value": value,
        "unit": "eV",
        "source": f"https://example.test/{provider}/{identifier}",
        "content_sha256": hashlib.sha256(identifier.encode()).hexdigest(),
        "method": "dft",
    }


def test_provider_adapters_share_one_fixture_parser():
    calls = []

    def fetch(**request):
        calls.append(request)
        return [row("mp-1", value=0.2)]

    provider = MaterialsProjectProvider(fetch=fetch)
    observations = provider.observations(
        composition="Bi2Te3", property_name="band gap", operation_id="op-1",
    )
    assert provider.provider_id == "Materials Project"
    assert observations[0].provider_id == "mp-1"
    assert observations[0].normalized_property == "band_gap"
    assert calls == [{
        "composition": "Bi2Te3", "property_name": "band gap", "operation_id": "op-1",
    }]


def test_provider_ids_are_explicit_for_cross_database_reports():
    assert OQMDProvider(fetch=lambda **_: [row("oqmd-1")]).provider_id == "OQMD"
    assert NOMADProvider(fetch=lambda **_: [row("nomad-1")]).provider_id == "NOMAD"
