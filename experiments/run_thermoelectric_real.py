#!/usr/bin/env python3
"""Run one real thermoelectric literature-agent experiment."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.events import canonical_json
from src.evaluation.model_router import open_route
from src.survey.corpus import CorpusBuilder, coverage_report
from src.survey.extraction import RelationExtractor
from src.survey.gaps import GapNarrator, find_candidates
from src.survey.records import SurveyTopic
from src.survey.report import build_report
from src.tools.sciverse import SciverseClient

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "_vm_scratch" / "thermoelectric_targeted_20260826"
ENV = ROOT / ".env"


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    load_env(ENV)
    OUT.mkdir(parents=True, exist_ok=True)
    topic = SurveyTopic(
        topic_id="thermoelectric-structure-property-v1",
        title="Thermoelectric materials structure-property relationships",
        seed_queries=(
            "thermoelectric materials ZT doping nanostructure",
            "thermoelectric Seebeck thermal conductivity carrier concentration",
        ),
        probe_questions=(
            "thermoelectric structure property relation measured ZT Seebeck thermal conductivity",
        ),
        year_from=2000,
        year_to=2025,
        language="en",
        domain="thermoelectric materials",
    )
    client = SciverseClient(audit_log=OUT / "sciverse_audit.jsonl", quiet=True)
    corpus = CorpusBuilder(
        source=client,
        max_candidates=4,
        semantic_top_k=8,
        min_passage_chars=240,
        scope_shard_size=4,
        citation_floor=20,
        document_probe_templates=(
            "{title} {domain} doping co-doping vacancy defect ZT Seebeck thermal conductivity",
            "{title} grain size nanostructure carrier concentration power factor ZT",
        ),
    ).build(topic)
    snapshot_digest = corpus.write_snapshot(OUT / "corpus_snapshot.json")
    (OUT / "corpus_manifest.json").write_text(
        canonical_json(coverage_report(corpus)) + "\n", encoding="utf-8",
    )
    with open_route(
        "claude-code",
        operation_db=OUT / "model_operations.sqlite",
        usage_log=OUT / "model_usage.jsonl",
        request_response_log=OUT / "model_request_response.jsonl",
        operator_declared_backend="claude-code-session",
        timeout_seconds=900,
    ) as route:
        extractor = RelationExtractor(
            transport=route.transport, batch_size=2, prompt_profile="high_recall_v2",
        )
        extraction = extractor.extract(corpus)
        candidates = find_candidates(extraction, corpus)
        gaps = GapNarrator(transport=route.transport, max_passages=6).narrate(
            candidates.candidates, result=extraction, corpus=corpus,
        )
        report = build_report(
            corpus=corpus, extraction=extraction, candidates=candidates, gaps=gaps,
            title="Thermoelectric Structure--Property Literature Survey",
            author="taiwanhuachenyu", report_date="2026-08-26",
            model_route=route.route,
        )
        report.write(OUT / "report")
        manifest = {
            "experiment": "thermoelectric-real-v1",
            "topic": topic.__dict__,
            "corpus": coverage_report(corpus),
            "corpus_snapshot": {
                "path": "corpus_snapshot.json", "payload_sha256": snapshot_digest,
            },
            "extraction": extraction.manifest(),
            "candidates": candidates.manifest(),
            "gaps": gaps.manifest(),
            "report_audit": report.audit,
            "route": route.manifest(),
            "extraction_prompt_profile": extractor.prompt_profile,
            "model_request_response_log": "model_request_response.jsonl",
        }
        (OUT / "experiment_manifest.json").write_text(
            canonical_json(manifest) + "\n", encoding="utf-8",
        )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
