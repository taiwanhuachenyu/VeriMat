"""Offline tests for the retrieval cache and discovery packages."""
import json

import pytest

from src.experiments.discovery_pack import DiscoveryPackage, build_packages
from src.experiments.retrieval_cache import CachedThrottledSciverse
from src.experiments.claims import Claim, VerifiedClaim
from src.survey.records import SurveyCorpus, SurveyContractError


class FakeClient:
    def __init__(self):
        self.calls = 0

    def agentic_search(self, query, *, filters=None, top_k=3):
        self.calls += 1
        return [{"doc_id": f"d{self.calls}", "abstract": f"hit for {query}"}]

    def meta_search(self, query, **kwargs):
        self.calls += 1
        return {"results": [], "total_count": 0}


def test_cache_hits_never_touch_the_network(tmp_path):
    inner = FakeClient()
    cache = CachedThrottledSciverse(
        inner, cache_path=tmp_path / "cache.jsonl", min_interval=0.0,
    )
    first = cache.agentic_search("ZT", filters={"a": 1}, top_k=3)
    second = cache.agentic_search("ZT", filters={"a": 1}, top_k=3)
    assert inner.calls == 1
    assert first == second
    assert cache.misses == 1 and cache.hits == 1


def test_cache_keys_distinguish_year_windows(tmp_path):
    inner = FakeClient()
    cache = CachedThrottledSciverse(
        inner, cache_path=tmp_path / "cache.jsonl", min_interval=0.0,
    )
    cache.agentic_search("ZT", filters={"year": 2020})
    cache.agentic_search("ZT", filters={"year": 2024})
    assert inner.calls == 2


def test_cache_reloads_from_disk(tmp_path):
    inner = FakeClient()
    cache1 = CachedThrottledSciverse(
        inner, cache_path=tmp_path / "cache.jsonl", min_interval=0.0,
    )
    cache1.agentic_search("ZT", filters={"a": 1})
    cache2 = CachedThrottledSciverse(
        inner, cache_path=tmp_path / "cache.jsonl", min_interval=0.0,
    )
    cache2.agentic_search("ZT", filters={"a": 1})
    assert inner.calls == 1
    assert cache2.hits == 1


def make_claim(claim_id="c1"):
    return Claim(
        claim_id=claim_id, relation_id="r1", material="Bi2Te3",
        structural_feature="Se vacancy", property_name="ZT", direction="increase",
        quote="Se vacancies raise ZT", passage_id="psg-1", composition="Bi2Te3",
    )


class FakeTransport:
    def __init__(self, text):
        self.text = text

    def complete(self, **kwargs):
        class R:
            pass
        r = R()
        r.text = self.text
        return r


def corpus_with(text):
    from src.survey.records import SurveyPassage, SurveyTopic, QueryRecord, DocumentRecord
    c = SurveyCorpus(topic=SurveyTopic(
        topic_id="t", title="t", seed_queries=("q",), probe_questions=("p",),
    ))
    c.add_document(DocumentRecord(
        doc_id="a"*64, unique_id="u1", title="T", year=2020, venue="V", doi="10.1/x",
    ))
    c.add_query(QueryRecord(query_id="q1", text="q", stage="metadata", intent="i", n_hits=1))
    passage = SurveyPassage.build(doc_id="a"*64, query_id="q1", text=text)
    c.add_passage(passage)
    return c, passage.passage_id


def test_package_requires_surviving_status():
    with pytest.raises(SurveyContractError):
        DiscoveryPackage(
            claim_id="c", material="m", statement="s", boundary="b", status="REFUTED",
            confidence=0.9, evidence=[{"passage_id": "p"}],
            counterevidence_considered=3, pack={"falsifiable_statement": "x",
                                                "minimal_verification_experiment": "y"},
        ).validate()


def test_build_packages_grounded_flow(tmp_path):
    corpus, psg_id = corpus_with("Se vacancies raise ZT in Bi2Te3 at room temperature.")
    claim = make_claim()
    claim = Claim(**{**claim.as_dict(), "passage_id": psg_id})
    good = VerifiedClaim(method="V3-full", claim=claim, label="ACCEPTED", confidence=0.9,
                         counter_queries_executed=3)
    bad_status = VerifiedClaim(method="V3-full", claim=make_claim("c2"), label="REFUTED",
                               confidence=0.5)
    transport = FakeTransport(json.dumps({
        "falsifiable_statement": "Removing Se vacancies lowers ZT",
        "minimal_verification_experiment": "Measure ZT versus Se vacancy concentration",
        "observable": "ZT", "expected_result_if_true": "ZT decreases with vacancy density",
    }))
    packages, refused = build_packages([good, bad_status], corpus=corpus, transport=transport)
    assert len(packages) == 1 and packages[0].claim_id == "c1"
    assert packages[0].evidence[0]["content_sha256"]
    assert refused == []  # REFUTED claims are skipped by label before any lookup
