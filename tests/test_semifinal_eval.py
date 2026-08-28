"""Offline tests for the semifinal closed-loop evaluation package."""
import pytest

from src.experiments.budget import BudgetExhausted, BudgetedTransport
from src.experiments.claims import Claim, VerifiedClaim
from src.experiments.methods import _cedg_label, _confidence
from src.experiments.oracle import OracleVerdict, oracle_state
from src.experiments.scoring import (
    agreement_score, false_gap_rate, holm, paired_comparison, replay_precision, score_method,
)
from src.survey.records import ExtractedRelation, SurveyContractError


def make_claim(claim_id: str = "claim-1") -> Claim:
    return Claim(
        claim_id=claim_id, relation_id="rel-1", material="Bi2Te3",
        structural_feature="Se vacancy", property_name="ZT", direction="increase",
        quote="Se vacancies raise ZT substantially", passage_id="psg-1",
        composition="Bi2Te3", value="1.2", unit="dimensionless",
    )


def make_prediction(method: str, claim: Claim, label: str, confidence: float = 0.9):
    return VerifiedClaim(
        method=method, claim=claim, label=label, confidence=confidence,
    )


# ------------------------------------------------------------------ oracle aggregation

def verdict(v: str, scope: bool = False) -> OracleVerdict:
    return OracleVerdict(passage_ref="p", verdict=v, scope_limitation=scope, quote="q",
                         template="t")


def test_oracle_contradiction_dominates():
    assert oracle_state([verdict("supported"), verdict("contradicted")]) == "contradicted"


def test_oracle_scope_bound_outranks_support():
    assert oracle_state([verdict("supported", scope=True), verdict("supported")]) == "narrowed"


def test_oracle_unresolved_without_admissible_verdicts():
    assert oracle_state([]) == "unresolved"
    assert oracle_state([verdict("unrelated")]) == "unresolved"


# ------------------------------------------------------------------- vocabulary routing

def test_solid_electrolyte_relation_validates_against_its_own_vocabulary():
    relation = ExtractedRelation.build(
        passage_id="psg-1", material="Li7La3Zr2O12",
        structural_feature="Al doping", property_name="ionic conductivity",
        direction="increase", quote="Al doping raises the ionic conductivity",
        vocabulary="solid_electrolyte",
    )
    relation.validate()


def test_unknown_vocabulary_is_refused():
    relation = ExtractedRelation.build(
        passage_id="psg-1", material="M", structural_feature="F", property_name="ZT",
        direction="increase", quote="a quote that is long enough", vocabulary="nonsense",
    )
    with pytest.raises(SurveyContractError):
        relation.validate()


# ------------------------------------------------------------------------------ scoring

ORACLE = {"c1": "supported", "c2": "contradicted", "c3": "narrowed", "c4": "unresolved"}


def predictions(method: str):
    return [
        make_prediction(method, make_claim("c1"), "ACCEPTED"),
        make_prediction(method, make_claim("c2"), "REFUTED"),
        make_prediction(method, make_claim("c3"), "NARROWED"),
        make_prediction(method, make_claim("c4"), "UNRESOLVED"),
    ]


def test_perfect_predictions_score_one():
    scores = score_method("m", predictions("m"), ORACLE, passage_text={}, tokens=100)
    assert scores.decision_accuracy == 1.0
    assert scores.counterevidence_recall == 1.0
    assert scores.overclaim_rate == 0.0
    assert scores.tokens_per_valid == 25.0


def test_overclaim_detected_when_accepting_a_contradicted_claim():
    preds = predictions("m")
    preds[1] = make_prediction("m", make_claim("c2"), "ACCEPTED")
    scores = score_method("m", preds, ORACLE, passage_text={})
    assert scores.overclaim_rate == 0.25
    assert scores.counterevidence_recall == 0.0


def test_narrowed_earns_half_credit_against_supported_oracle():
    assert agreement_score("NARROWED", "supported") == 0.5
    assert agreement_score("ACCEPTED", "contradicted") == 0.0


def test_replay_precision_checks_quotes_against_snapshot_text():
    claim = make_claim()
    preds = [make_prediction("m", claim, "ACCEPTED")]
    assert replay_precision(preds, {claim.passage_id: "... Se vacancies raise ZT substantially ..."
                                   }) == 1.0
    assert replay_precision(preds, {claim.passage_id: "unrelated words"}) == 0.0


def test_brier_and_ece_computed_on_decided_claims():
    preds = [
        make_prediction("m", make_claim("c1"), "ACCEPTED", confidence=1.0),
        make_prediction("m", make_claim("c2"), "ACCEPTED", confidence=1.0),
    ]
    scores = score_method("m", preds, ORACLE, passage_text={})
    assert scores.brier == 0.5
    assert scores.ece == 0.5


def test_false_gap_rate():
    assert false_gap_rate(["g1", "g2", "g3"], ["g1"]) == pytest.approx(1 / 3)
    assert false_gap_rate([], []) != false_gap_rate([], [])  # NaN when nothing declared new


# --------------------------------------------------------------------------- statistics

def test_paired_comparison_and_holm():
    oracle = {f"c{i}": "supported" for i in range(6)}
    left = [make_prediction("A", make_claim(f"c{i}"), "ACCEPTED") for i in range(6)]
    right = [make_prediction("B", make_claim(f"c{i}"), "ACCEPTED") for i in range(5)]
    right.append(make_prediction("B", make_claim("c5"), "REFUTED"))
    result = paired_comparison(left, right, oracle)
    assert result["n_common"] == 6
    assert result["mean_delta"] == pytest.approx(1 / 6, abs=1e-3)
    adjusted = holm([0.03, 0.02, 0.5])
    assert adjusted == pytest.approx([0.06, 0.06, 0.5], abs=1e-9)


# ------------------------------------------------------------------------------ budget

class FakeInner:
    def __init__(self, rows):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE model_operations (operation_id TEXT, status TEXT,"
            " input_tokens INT, output_tokens INT)"
        )
        self.conn.executemany("INSERT INTO model_operations VALUES (?,?,?,?)", rows)
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        return "response"

    def close(self):
        pass


def test_budget_refuses_when_spent(tmp_path):
    inner = FakeInner([("op", "COMPLETED", 700, 300)])
    transport = BudgetedTransport(inner, max_tokens=1000)
    assert transport.usage() == {"spent_tokens": 1000, "max_tokens": 1000}
    with pytest.raises(BudgetExhausted):
        transport.complete(operation_id="x")


def test_budget_discards_response_that_crosses_the_cap():
    inner = FakeInner([("op", "COMPLETED", 0, 0)])
    transport = BudgetedTransport(inner, max_tokens=5000)
    inner.conn.execute(
        "UPDATE model_operations SET input_tokens=4800, output_tokens=400"
        " WHERE operation_id='op'"
    )
    with pytest.raises(BudgetExhausted):
        transport.complete(operation_id="x")


# ------------------------------------------------------------------ verification helpers

def test_cedg_label_mapping():
    supported = [{"verdict": "supported", "scope_limitation": False, "quote": "q", "ref": "r"}]
    contradicted = [{"verdict": "contradicted", "scope_limitation": False, "quote": "q", "ref": "r"}]
    narrowed = [{"verdict": "supported", "scope_limitation": True, "quote": "q", "ref": "r"}]
    assert _cedg_label(supported, counter_executed=True)[0] == "ACCEPTED"
    assert _cedg_label(contradicted, counter_executed=True)[0] == "REFUTED"
    assert _cedg_label(narrowed, counter_executed=True)[0] == "NARROWED"
    assert _cedg_label([], counter_executed=True)[0] == "UNRESOLVED"
    assert _cedg_label(supported, counter_executed=False)[0] == "UNRESOLVED"


def test_confidence_policy():
    verdicts = [{"verdict": "contradicted"}] * 2 + [{"verdict": "supported"}] * 2
    assert _confidence(verdicts, n_read=4, prior=0.8) == 0.5
    assert _confidence([], n_read=0, prior=0.8) == 0.8
