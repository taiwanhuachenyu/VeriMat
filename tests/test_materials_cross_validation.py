import hashlib

import pytest

from src.materials.cross_validation import (
    CrossValidationError,
    MatchPolicy,
    MaterialObservation,
    compare_observations,
    normalise_composition,
)


def observation(*, provider, provider_id, composition="Bi2Te3", property_name="band gap",
                value=0.15, unit="eV", temperature_k=None, method="dft"):
    return MaterialObservation(
        provider=provider,
        provider_id=provider_id,
        composition=composition,
        property_name=property_name,
        value=value,
        unit=unit,
        temperature_k=temperature_k,
        method=method,
        source_locator=f"https://example.test/{provider}/{provider_id}",
        content_sha256=hashlib.sha256(f"{provider}:{provider_id}".encode()).hexdigest(),
    )


def test_composition_is_order_independent_and_reduced():
    assert normalise_composition("Te3Bi2") == "Bi2Te3"
    assert normalise_composition("Bi4 Te6") == "Bi2Te3"
    with pytest.raises(CrossValidationError, match="flat neutral"):
        normalise_composition("Bi2(Te3)")


def test_comparison_converts_units_and_reports_numeric_metrics():
    report = compare_observations(
        [observation(provider="Sciverse", provider_id="paper", value=0.2, unit="eV/atom")],
        [observation(provider="OQMD", provider_id="entry", value=205, unit="meV/atom")],
        policy=MatchPolicy(tolerance_relative=0.03),
    )
    assert report.metrics["n_pairs"] == 1
    assert report.metrics["mae"] == pytest.approx(0.005)
    assert report.metrics["rmse"] == pytest.approx(0.005)
    assert report.metrics["bias"] == pytest.approx(0.005)
    assert report.metrics["r_squared"] is None
    assert report.metrics["tolerance_pass_rate"] == 1
    assert report.comparisons[0].canonical_unit_dimension == "energy_per_atom"


def test_temperature_and_method_constraints_are_audited_as_unmatched():
    report = compare_observations(
        [observation(provider="Sciverse", provider_id="paper", temperature_k=700, method="experiment")],
        [observation(provider="Materials Project", provider_id="mp-1", temperature_k=0, method="dft")],
    )
    assert report.comparisons == ()
    assert report.unmatched[0]["reason"] == "method_mismatch"


def test_best_temperature_match_is_selected_once_and_r_squared_is_defined():
    literature = [
        observation(provider="Sciverse", provider_id="a", composition="Bi2Te3", value=1.0, unit="1", temperature_k=300),
        observation(provider="Sciverse", provider_id="b", composition="Sb2Te3", value=2.0, unit="1", temperature_k=300),
    ]
    database = [
        observation(provider="NOMAD", provider_id="far", value=1.1, unit="1", temperature_k=320),
        observation(provider="NOMAD", provider_id="near", value=1.05, unit="1", temperature_k=301),
        observation(provider="NOMAD", provider_id="sb", composition="Te3Sb2", value=1.9, unit="1", temperature_k=300),
    ]
    report = compare_observations(literature, database)
    assert [item.database_id for item in report.comparisons] == [
        observation(provider="NOMAD", provider_id="near", value=1.05, unit="1", temperature_k=301).observation_id(),
        observation(provider="NOMAD", provider_id="sb", composition="Te3Sb2", value=1.9, unit="1", temperature_k=300).observation_id(),
    ]
    assert report.metrics["r_squared"] == pytest.approx(0.975)


def test_unsupported_unit_fails_before_any_comparison():
    with pytest.raises(CrossValidationError, match="unsupported unit"):
        compare_observations(
            [observation(provider="Sciverse", provider_id="paper", unit="K")], [],
        )
