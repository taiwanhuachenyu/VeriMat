import pytest

from src.survey.extraction import ExtractionResult
from src.survey.gaps import CandidateSet, GapResult
from src.survey.records import (
    DocumentRecord,
    ExtractedRelation,
    QueryRecord,
    SurveyCorpus,
    SurveyPassage,
    SurveyTopic,
)
from src.survey.report import build_bibliography, citation_keys, verify

DOC_ID = "a" * 64


def corpus_and_relation():
    topic = SurveyTopic(
        topic_id="thermoelectric",
        title="Thermoelectric materials",
        seed_queries=("thermoelectric",),
        probe_questions=("structure property",),
        year_from=2000,
        year_to=2025,
    )
    corpus = SurveyCorpus(topic=topic)
    corpus.add_document(DocumentRecord(
        doc_id=DOC_ID,
        unique_id="uid-a",
        title="A thermoelectric study",
        year=2024,
        venue="Materials Journal",
        doi="10.1000/example",
    ))
    query = QueryRecord(
        query_id="query-a",
        text="structure property",
        stage="semantic",
        intent="evidence",
        n_hits=1,
    )
    corpus.add_query(query)
    passage = SurveyPassage.build(
        doc_id=DOC_ID,
        query_id=query.query_id,
        text="Doping increased ZT to 2.6 at 700 K.",
    )
    corpus.add_passage(passage)
    relation = ExtractedRelation.build(
        passage_id=passage.passage_id,
        material="sample",
        structural_feature="doping",
        property_name="ZT",
        direction="increase",
        quote="Doping increased ZT to 2.6 at 700 K.",
        value="2.6",
        temperature_k="700",
        method="experiment",
    )
    relation.validate()
    extraction = ExtractionResult(relations={relation.relation_id: relation})
    return corpus, extraction


def test_verify_accepts_matching_citation_and_bibliography():
    corpus, extraction = corpus_and_relation()
    keys = citation_keys(corpus.documents)
    bib = build_bibliography(corpus.documents, keys)
    tex = r"\cite{" + keys[DOC_ID] + "}"
    result = verify(
        corpus=corpus,
        extraction=extraction,
        gaps=GapResult(),
        tex=tex,
        bib=bib,
        keys=keys,
    )
    assert result["verified"]
    assert result["citations_match_bibliography"]


def test_verify_rejects_body_citation_without_bibliography_entry():
    corpus, extraction = corpus_and_relation()
    keys = citation_keys(corpus.documents)
    result = verify(
        corpus=corpus,
        extraction=extraction,
        gaps=GapResult(),
        tex=r"\cite{missing-key}",
        bib="",
        keys=keys,
    )
    assert not result["verified"]
    assert {item["check"] for item in result["failures"]} == {"citation_has_entry"}


def test_verify_rejects_orphan_bibliography_entry():
    corpus, extraction = corpus_and_relation()
    keys = citation_keys(corpus.documents)
    bib = build_bibliography(corpus.documents, keys)
    result = verify(
        corpus=corpus,
        extraction=extraction,
        gaps=GapResult(),
        tex="",
        bib=bib,
        keys=keys,
    )
    assert not result["verified"]
    assert any(item["check"] == "entry_is_cited" for item in result["failures"])


def test_report_write_requires_a_verified_audit(tmp_path):
    from src.survey.report import SurveyReport

    report = SurveyReport(files={"survey.tex": "x"}, audit={"verified": False})
    with pytest.raises(ValueError, match="verification"):
        report.write(tmp_path)


def test_corpus_snapshot_round_trip_and_tamper_detection(tmp_path):
    import json
    from src.survey.records import SurveyCorpus, SurveyPassage

    corpus, _ = corpus_and_relation()
    path = tmp_path / "corpus.json"
    digest = corpus.write_snapshot(path)
    restored = SurveyCorpus.read_snapshot(path)
    assert restored.snapshot() == corpus.snapshot()
    assert digest == corpus.snapshot()["payload_sha256"]
    value = json.loads(path.read_text(encoding="utf-8"))
    value["passages"][0]["text"] = "tampered"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        SurveyCorpus.read_snapshot(path)


def test_high_recall_extractor_admits_unclear_relation():
    from src.evaluation.model_backend import ModelResponse
    from src.survey.extraction import RelationExtractor

    corpus, _ = corpus_and_relation()
    passage = next(iter(corpus.passages.values()))
    payload = {
        "relations": [{
            "passage_id": passage.passage_id, "material": "sample",
            "structural_feature": "doping", "property_name": "ZT",
            "direction": "unclear", "quote": passage.text, "composition": "",
            "value": "", "unit": "", "temperature_k": "", "method": "experiment",
        }]
    }

    class Transport:
        def complete(self, **kwargs):
            return ModelResponse(
                text=__import__("json").dumps(payload), input_tokens=1,
                output_tokens=1, request_id="request-1",
            )

    result = RelationExtractor(transport=Transport(), batch_size=1).extract(corpus)
    assert len(result.relations) == 1


def test_gap_prompt_profile_changes_durable_operation_identity():
    from src.evaluation.model_backend import ModelResponse
    from src.survey.gaps import GapNarrator, find_candidates

    class Transport:
        def __init__(self):
            self.calls = []

        def complete(self, **kwargs):
            self.calls.append(kwargs)
            return ModelResponse(
                text=(
                    '{"verdict":"not_a_gap","statement":"This candidate is not a '
                    'research gap.","novelty":"known","novelty_quote":"",'
                    '"novelty_basis":"The evidence is already established."}'
                ),
                input_tokens=1, output_tokens=1, request_id="request-1",
            )

    corpus, extraction = corpus_and_relation()
    candidates = find_candidates(extraction, corpus)
    candidate = candidates.candidates[0]
    first, second = Transport(), Transport()
    GapNarrator(transport=first, prompt_profile="schema_v1").narrate(
        [candidate], result=extraction, corpus=corpus,
    )
    GapNarrator(transport=second, prompt_profile="schema_v2").narrate(
        [candidate], result=extraction, corpus=corpus,
    )
    assert first.calls[0]["operation_id"] != second.calls[0]["operation_id"]
