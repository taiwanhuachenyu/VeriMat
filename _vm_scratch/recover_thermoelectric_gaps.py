#!/usr/bin/env python3
"""Finish one interrupted gap stage without mutating its original operation ledger."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.events import canonical_json
from src.evaluation.baseline_runner import Usage
from src.evaluation.model_router import open_route
from src.survey.corpus import coverage_report
from src.survey.extraction import ExtractionResult, RelationExtractor
from src.survey.gaps import GAP_PROMPT_PROFILE, GapNarrator, GapResult, find_candidates
from src.survey.records import SurveyCorpus, digest_id
from src.survey.report import build_report

SOURCE = ROOT / "_vm_scratch" / "thermoelectric_targeted_20260826"
OUT = ROOT / "_vm_scratch" / "thermoelectric_targeted_gap_recovery_20260826"


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def read_audit(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def rebuild_extraction(corpus: SurveyCorpus, audit: list[dict]) -> ExtractionResult:
    extractor = RelationExtractor(transport=None, batch_size=2, prompt_profile="high_recall_v2")
    result = ExtractionResult()
    calls = tokens = 0
    for record in audit:
        response = record.get("response_text")
        if not response or '"relations"' not in response:
            continue
        payload = json.loads(record["user"])
        exposed = {
            item["passage_id"]: corpus.passages[item["passage_id"]]
            for item in payload["passages"]
        }
        for proposal in extractor._parse(response):
            extractor._admit(proposal, exposed, result)
        calls += 1
        tokens += int(record["input_tokens"]) + int(record["output_tokens"])
    result.usage = Usage(calls, tokens)
    result.usage.validate()
    return result


def replay_completed_gaps(
    narrator: GapNarrator, candidates, corpus: SurveyCorpus, extraction: ExtractionResult, audit: list[dict],
) -> tuple[GapResult, tuple]:
    outcome = GapResult()
    fingerprint = corpus.topic.fingerprint()
    completed = {item["operation_id"]: item for item in audit if item.get("response_text")}
    calls = tokens = 0
    pending = []
    for candidate in sorted(candidates, key=lambda item: item.candidate_id()):
        exposed = narrator._exposed(candidate, corpus)
        old_id = digest_id("op", "gap", fingerprint, candidate.candidate_id())
        record = completed.get(old_id)
        if record is None:
            pending.append(candidate)
            continue
        narrator._admit(candidate, narrator._parse(record["response_text"]), exposed, outcome)
        calls += 1
        tokens += int(record["input_tokens"]) + int(record["output_tokens"])
    outcome.usage = Usage(calls, tokens)
    outcome.usage.validate()
    return outcome, tuple(pending)


def main() -> None:
    load_env(ROOT / ".env")
    if OUT.exists():
        raise SystemExit(f"recovery directory already exists: {OUT}")
    OUT.mkdir(parents=True)
    corpus = SurveyCorpus.read_snapshot(SOURCE / "corpus_snapshot.json")
    audit = read_audit(SOURCE / "model_request_response.jsonl")
    extraction = rebuild_extraction(corpus, audit)
    all_candidates = find_candidates(extraction, corpus)
    # The original run reached candidate five. Preserve that exact bounded prefix rather than
    # claiming the later 22 candidates were evaluated after the interruption.
    attempted_candidates = tuple(sorted(
        all_candidates.candidates, key=lambda item: item.candidate_id()
    )[:5])
    candidates = type(all_candidates)(
        candidates=attempted_candidates, suppressed={
            **all_candidates.suppressed,
            "not_started_after_interruption": len(all_candidates.candidates) - len(attempted_candidates),
        },
    )
    narrator = GapNarrator(transport=None, max_passages=6, prompt_profile=GAP_PROMPT_PROFILE)
    gaps, pending = replay_completed_gaps(narrator, candidates.candidates, corpus, extraction, audit)
    if len(pending) != 1:
        raise SystemExit(f"expected one unresolved gap candidate, found {len(pending)}")
    with open_route(
        "claude-code", operation_db=OUT / "model_operations.sqlite",
        usage_log=OUT / "model_usage.jsonl",
        request_response_log=OUT / "model_request_response.jsonl",
        operator_declared_backend="claude-code-session", timeout_seconds=900,
    ) as route:
        narrator.transport = route.transport
        candidate = pending[0]
        exposed = narrator._exposed(candidate, corpus)
        response = route.transport.complete(
            operation_id=digest_id(
                "op", "gap", narrator.prompt_profile, corpus.topic.fingerprint(),
                candidate.candidate_id(),
            ),
            system=__import__("src.survey.gaps", fromlist=["SYSTEM_PROMPT"]).SYSTEM_PROMPT,
            user=narrator._payload(candidate, exposed, extraction),
            response_schema=__import__("src.survey.gaps", fromlist=["GAP_SCHEMA"]).GAP_SCHEMA,
        )
        narrator._admit(candidate, narrator._parse(response.text), exposed, gaps)
        prior_usage = gaps.usage
        new_usage = response.usage()
        gaps.usage = Usage(
            prior_usage.calls + new_usage.calls, prior_usage.tokens + new_usage.tokens,
        )
        report = build_report(
            corpus=corpus, extraction=extraction, candidates=candidates, gaps=gaps,
            title="Thermoelectric Structure--Property Literature Survey",
            author="taiwanhuachenyu", report_date="2026-08-26", model_route=route.route,
        )
        report.write(OUT / "report")
        manifest = {
            "experiment": "thermoelectric-real-v1-gap-recovery",
            "source_experiment": str(SOURCE.name),
            "recovery_protocol": {
                "source_pending_operation_is_immutable": True,
                "replayed_completed_gap_calls": prior_usage.calls,
                "new_gap_calls": new_usage.calls,
                "gap_prompt_profile": narrator.prompt_profile,
                "unresolved_candidate_ids": [item.candidate_id() for item in pending],
                "not_started_candidate_count": len(all_candidates.candidates) - len(attempted_candidates),
            },
            "corpus": coverage_report(corpus),
            "corpus_snapshot": {"path": "../thermoelectric_targeted_20260826/corpus_snapshot.json"},
            "extraction": extraction.manifest(),
            "candidates": candidates.manifest(),
            "gaps": gaps.manifest(),
            "report_audit": report.audit,
            "route": route.manifest(),
        }
    (OUT / "experiment_manifest.json").write_text(
        canonical_json(manifest) + "\n", encoding="utf-8", newline="\n",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
