#!/usr/bin/env python3
"""Run the GOAI semifinal closed-loop experiment on thermoelectric materials.

Stages (each idempotent, resumable, and safe to run alone):

  freeze   build and snapshot the discovery-window corpus (SHA-256 sealed)
  extract  the shared, cached extraction pass over the discovery corpus
  claims   project admitted relations into claims and cap at the preregistered maximum
  verify   run every preregistered method variant over the shared claim set
  gaps     rule-based gap candidates + narrated gaps with novelty labels
  oracle   judge every claim and every new gap against the validation window
  score    metrics, calibration, cost, paired statistics
  report   summary.json + markdown report

Usage:
  python experiments/run_semifinal_v1.py --stage all --out results/semifinal_v1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.events import canonical_json
from src.evaluation.model_router import open_route
from src.experiments.budget import BudgetedTransport
from src.experiments.claims import Claim, VerifiedClaim
from src.experiments.methods import METHODS, MethodContext, run_method
from src.experiments.oracle import TimeSplitOracle
from src.experiments.scoring import (
    MethodScores, false_gap_rate, paired_comparison, score_method,
)
from src.survey.corpus import CorpusBuilder, coverage_report
from src.survey.extraction import RelationExtractor
from src.survey.gaps import GapNarrator, find_candidates
from src.survey.records import SurveyTopic
from src.tools.sciverse import SciverseClient

PREREG = ROOT / "preregistration" / "semifinal_v1.json"


class ThrottledSciverse:
    """Stay under the endpoint burst quota: minimum interval + 429 backoff.

    A transparent proxy over :class:`SciverseClient`: every call waits its turn, and a 429
    parks the caller for the observed quota window instead of failing the run.  Retrieval is
    infrastructure here, not a measured variable, so politeness costs nothing scientifically.
    """

    def __init__(self, inner, *, min_interval: float = 2.0, wait_on_429: float = 75.0,
                 max_429: int = 12):
        self._inner = inner
        self._min_interval = min_interval
        self._wait_on_429 = wait_on_429
        self._max_429 = max_429
        self._last = 0.0

    def _pacing(self) -> None:
        import time as _t
        gap = _t.monotonic() - self._last
        if gap < self._min_interval:
            _t.sleep(self._min_interval - gap)
        self._last = _t.monotonic()

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute
        def call(*args, **kwargs):
            import time as _t
            for attempt in range(self._max_429 + 1):
                self._pacing()
                try:
                    return attribute(*args, **kwargs)
                except Exception as exc:
                    if "429" not in str(exc) or attempt == self._max_429:
                        raise
                    wait = self._wait_on_429 * (attempt + 1)
                    print(f"[throttle] 429, waiting {wait:.0f}s ...", flush=True)
                    _t.sleep(wait)
        return call


def load_env() -> None:
    import os
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def build_topic(prereg: dict, window: str) -> SurveyTopic:
    cfg = prereg[window]
    return SurveyTopic(
        topic_id=f"thermoelectric-structure-property-{window}-{prereg['preregistration_id']}",
        title="Thermoelectric materials structure-property relationships",
        seed_queries=(
            "thermoelectric materials ZT doping nanostructure",
            "thermoelectric Seebeck coefficient thermal conductivity carrier concentration",
            "nanostructuring lattice thermal conductivity thermoelectric figure of merit",
            "thermoelectric power factor defect vacancy engineering ZT",
        ),
        probe_questions=(
            "thermoelectric ZT Seebeck thermal conductivity measured",
            "doping vacancy nanostructure power factor carrier concentration",
        ),
        year_from=cfg["year_from"], year_to=cfg["year_to"],
        language="en", domain="thermoelectric materials",
    )


def build_corpus(client, prereg: dict, out: Path) -> tuple[object, dict]:
    topic = build_topic(prereg, "discovery_window")
    cfg = prereg["corpus"]
    corpus = CorpusBuilder(
        source=client, max_candidates=cfg["max_candidates"],
        semantic_top_k=cfg["semantic_top_k"], min_passage_chars=cfg["min_passage_chars"],
        scope_shard_size=cfg["scope_shard_size"], citation_floor=cfg["citation_floor"],
        # Short keyword probes: title-length queries return zero hits on this deployment
        # even when the document's semantic index is live (recorded deployment divergence).
        document_probe_templates=("ZT doping", "thermal conductivity nanostructure"),
    ).build(topic)
    corpus.write_snapshot(out / "corpus_snapshot.json")
    (out / "corpus_manifest.json").write_text(
        canonical_json(coverage_report(corpus)) + "\n", encoding="utf-8",
    )
    return corpus, coverage_report(corpus)


def stage_freeze(prereg: dict, out: Path) -> None:
    client = ThrottledSciverse(
        SciverseClient(audit_log=out / "sciverse_audit.jsonl", quiet=True),
        min_interval=3.0,
    )
    _, manifest = build_corpus(client, prereg, out)
    print("corpus:", json.dumps(manifest, ensure_ascii=False)[:400])


def load_corpus(prereg: dict, out: Path):
    """Rebuild the corpus from the frozen snapshot so later stages never re-query."""
    from src.survey.records import SurveyCorpus
    snapshot = out / "corpus_snapshot.json"
    if not snapshot.exists():
        raise SystemExit("run --stage freeze first: corpus snapshot is missing")
    return SurveyCorpus.read_snapshot(snapshot)


def stage_extract(prereg: dict, out: Path, transport) -> None:
    corpus = load_corpus(prereg, out)
    skip_file = out / "skip_passages.json"
    if skip_file.exists():
        skip = set(json.loads(skip_file.read_text(encoding="utf-8"))["passage_ids"])
        for pid in skip:
            corpus.passages.pop(pid, None)
        print(f"skipping {len(skip)} disclosed unrunnable passages", flush=True)
    extractor = RelationExtractor(
        transport=transport, batch_size=8, prompt_profile="high_recall_v2",
        vocabulary=prereg["vocabulary"],
    )
    extraction = extractor.extract(corpus)
    (out / "extraction_manifest.json").write_text(
        canonical_json(extraction.manifest()) + "\n", encoding="utf-8",
    )
    (out / "relations.jsonl").write_text(
        "".join(canonical_json({
            "relation_id": r.relation_id, "passage_id": r.passage_id, "material": r.material,
            "structural_feature": r.structural_feature, "property_name": r.property_name,
            "direction": r.direction, "quote": r.quote, "composition": r.composition,
            "value": r.value, "unit": r.unit, "temperature_k": r.temperature_k,
            "method": r.method, "vocabulary": r.vocabulary,
        }) + "\n" for r in extraction.relations.values()),
        encoding="utf-8",
    )
    print("relations:", len(extraction.relations), "rejections:", len(extraction.rejections))


def stage_claims(prereg: dict, out: Path, claim_limit: int = 0) -> int:
    corpus = load_corpus(prereg, out)
    relations = load_extraction(out, corpus)
    claims: dict[str, Claim] = {}
    for relation in relations:
        claim = Claim.from_relation(relation)
        claims.setdefault(claim.claim_id, claim)
    cap = claim_limit or prereg["corpus"]["max_claims"]
    ordered = [claims[key] for key in sorted(claims)][:cap]
    (out / "claims.jsonl").write_text(
        "".join(canonical_json(c.as_dict()) + "\n" for c in ordered), encoding="utf-8",
    )
    print("claims:", len(ordered))
    return len(ordered)


def load_extraction(out: Path, corpus):
    from src.survey.extraction import ExtractionResult
    manifest = json.loads((out / "extraction_manifest.json").read_text(encoding="utf-8"))
    result = ExtractionResult()
    from src.survey.records import ExtractedRelation
    for line in (out / "relations.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        relation = ExtractedRelation(
            **{k: payload[k] for k in (
                "relation_id", "passage_id", "material", "structural_feature",
                "property_name", "direction", "quote", "composition", "value", "unit",
                "temperature_k", "method", "vocabulary")},
        )
        relation.validate()
        result.relations[relation.relation_id] = relation
    return list(result.relations.values())


def stage_verify(prereg: dict, out: Path, make_transport, corpus) -> None:
    claims = [Claim(**json.loads(line)) for line in
              (out / "claims.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    for method in prereg["methods"]:
        method_dir = out / method
        if (method_dir / "predictions.jsonl").exists():
            print(f"{method}: predictions already present, skipping (cache)")
            continue
        method_dir.mkdir(parents=True, exist_ok=True)
        inner = make_transport(method_dir / "model_operations.sqlite")
        transport = BudgetedTransport(inner, max_tokens=prereg["budget"]["max_tokens_per_method"])
        ctx = MethodContext(
            client=SciverseClient(audit_log=method_dir / "sciverse_audit.jsonl", quiet=True),
            transport=transport, discovery_year_to=prereg["discovery_window"]["year_to"],
            passages_by_id=corpus.passages, db_provider=None,
            mcts_iterations=prereg["mcts"]["iterations"],
        )
        predictions = run_method(method, claims, ctx)
        usage = transport.usage()
        (method_dir / "predictions.jsonl").write_text(
            "".join(p.line() + "\n" for p in predictions), encoding="utf-8",
        )
        (method_dir / "usage.json").write_text(
            canonical_json(usage) + "\n", encoding="utf-8",
        )
        transport.close()
        print(f"{method}: {len(predictions)} predictions, usage {usage}", flush=True)


def stage_gaps(prereg: dict, out: Path, make_transport, corpus) -> None:
    gaps_file = out / "gaps.jsonl"
    if gaps_file.exists():
        print("gaps already present, skipping (cache)")
        return
    relations = load_extraction(out, corpus)
    from src.survey.extraction import ExtractionResult
    extraction = ExtractionResult()
    for relation in relations:
        extraction.relations[relation.relation_id] = relation
    candidates = find_candidates(extraction, corpus)
    inner = make_transport(out / "gaps_model_operations.sqlite")
    transport = BudgetedTransport(inner, max_tokens=prereg["budget"]["max_tokens_per_method"])
    gap_result = GapNarrator(transport=transport, max_passages=6).narrate(
        candidates.candidates, result=extraction, corpus=corpus,
    )
    payload = [
        {
            "gap_id": gap.gap_id, "kind": gap.kind, "statement": gap.statement,
            "novelty": gap.novelty, "novelty_basis": gap.novelty_basis,
            "novelty_quote": gap.novelty_quote,
            "supporting_passages": list(gap.supporting_passages),
        }
        for gap in gap_result.gaps.values()
    ]
    gaps_file.write_text(
        "".join(canonical_json(item) + "\n" for item in payload) + "\n", encoding="utf-8",
    )
    (out / "gap_usage.json").write_text(canonical_json(transport.usage()) + "\n", encoding="utf-8")
    transport.close()
    print("gaps:", len(payload), "novel:", sum(1 for g in payload if g["novelty"] == "new"))


def stage_oracle(prereg: dict, out: Path, make_transport) -> None:
    inner = make_transport(out / "oracle_model_operations.sqlite")
    transport = BudgetedTransport(inner, max_tokens=prereg["budget"]["max_tokens_per_method"])
    oracle = TimeSplitOracle(
        client=SciverseClient(audit_log=out / "oracle_sciverse_audit.jsonl", quiet=True),
        transport=transport, year_from=prereg["validation_window"]["year_from"],
        year_to=prereg["validation_window"]["year_to"], cache_path=out / "oracle_cache.json",
        top_k=prereg["oracle"]["top_k"],
    )
    claims = [Claim(**json.loads(line)) for line in
              (out / "claims.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    results = {}
    for index, claim in enumerate(claims):
        verdict = oracle.judge_claim(claim)
        results[claim.claim_id] = verdict.state
        if (index + 1) % 10 == 0:
            print(f"oracle: {index + 1}/{len(claims)} claims")
    (out / "oracle_claims.json").write_text(
        canonical_json({"states": results, "detail": {
            c.claim_id: oracle.judge_claim(c).as_dict() for c in claims}}) + "\n",
        encoding="utf-8",
    )
    gaps_payload = [json.loads(line) for line in
                    (out / "gaps.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    from src.survey.records import ResearchGap
    addressed = {}
    for item in gaps_payload:
        if item["novelty"] != "new":
            continue
        gap = ResearchGap(
            gap_id=item["gap_id"], kind=item["kind"], statement=item["statement"],
            novelty=item["novelty"], novelty_basis=item["novelty_basis"],
            supporting_passages=tuple(item["supporting_passages"]),
            novelty_quote=item.get("novelty_quote", ""),
        )
        verdict = oracle.judge_gap(gap, item["statement"][:220])
        addressed[gap.gap_id] = verdict.addressed
    (out / "oracle_gaps.json").write_text(
        canonical_json(addressed) + "\n", encoding="utf-8",
    )
    transport.close()
    print("oracle done:", len(results), "claims,", len(addressed), "new gaps")


def stage_score(prereg: dict, out: Path, corpus) -> None:
    oracle_doc = json.loads((out / "oracle_claims.json").read_text(encoding="utf-8"))
    states = oracle_doc["states"]
    passage_text = {key: p.text for key, p in corpus.passages.items()}
    scores: dict[str, Any] = {}
    per_method_predictions = {}
    for method in prereg["methods"]:
        rows = [json.loads(line) for line in
                (out / method / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        rebuilt = []
        for row in rows:
            claim = Claim(**row["claim"])
            row.pop("claim")
            rebuilt.append(VerifiedClaim(claim=claim, **row))
        per_method_predictions[method] = rebuilt
        usage = json.loads((out / method / "usage.json").read_text(encoding="utf-8"))
        scores[method] = score_method(
            method, rebuilt, states, passage_text, tokens=usage.get("spent_tokens", 0),
        ).as_dict()
    novel = [item["gap_id"] for item in
             (json.loads(line) for line in
              (out / "gaps.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())
             if item["novelty"] == "new"]
    addressed_doc = json.loads((out / "oracle_gaps.json").read_text(encoding="utf-8"))
    false_gaps = false_gap_rate(novel, [g for g, ok in addressed_doc.items() if ok])
    comparisons = {}
    for pair in prereg["statistics"]["comparisons"]:
        left, right = pair.split(" vs ")
        key = f"{left}__vs__{right}"
        comparisons[key] = paired_comparison(
            per_method_predictions[left], per_method_predictions[right], states,
        )
    p_values = [v.get("p_value") for v in comparisons.values()]
    from src.experiments.scoring import holm
    if all(p is not None for p in p_values):
        adjusted = holm([float(p) for p in p_values])
        for key, value in zip(comparisons, adjusted):
            comparisons[key]["p_holm_adjusted"] = round(value, 5)
    summary = {
        "preregistration": prereg["preregistration_id"],
        "methods": scores,
        "already_known_false_gap_rate": None if false_gaps != false_gaps else round(false_gaps, 4),
        "n_novel_gaps": len(novel),
        "comparisons": comparisons,
        "scientific_result": "closed-loop automated oracles; not expert-attested",
    }
    (out / "summary.json").write_text(canonical_json(summary) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1)[:2000])


def stage_report(prereg: dict, out: Path) -> None:
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    lines = [
        "# 热电构效 v1 — 闭环自动评测结果",
        "",
        f"预注册：{summary['preregistration']}｜语料：热电材料 2000–2021，验证窗 2022–2025",
        "",
        "| 方法 | 决策准确率 | 反证召回 | 过度宣称 | 回放精度 | Brier | tokens/有效 |",
        "|---|---|---|---|---|---|---|",
    ]
    for method, s in summary["methods"].items():
        lines.append(
            f"| {method} | {s['decision_accuracy']} | {s['counterevidence_recall']} | "
            f"{s['overclaim_rate']} | {s['replay_precision']} | {s['brier']} | "
            f"{s['tokens_per_valid']} |"
        )
    fgr = summary.get("already_known_false_gap_rate")
    lines += ["", f"**already-known false-gap rate**: {fgr} "
              f"(novel gaps: {summary['n_novel_gaps']})", "", "## 配对比较（Holm 校正）", ""]
    for key, comp in summary["comparisons"].items():
        lines.append(f"- {key}: Δ={comp.get('mean_delta')} p={comp.get('p_value')} "
                     f"p_holm={comp.get('p_holm_adjusted', '—')} (n={comp.get('n_common')})")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("report:", out / "REPORT.md")


def main() -> None:
    parser = argparse.ArgumentParser(description="GOAI semifinal closed-loop experiment")
    parser.add_argument("--stage", default="all",
                        choices=["all", "freeze", "extract", "claims", "verify", "gaps",
                                 "oracle", "score", "report"])
    parser.add_argument("--out", default=str(ROOT / "results" / "semifinal_v1"))
    parser.add_argument("--claim-limit", type=int, default=0,
                        help="override the preregistered claim cap (pilot runs only)")
    args = parser.parse_args()
    load_env()
    prereg = json.loads(PREREG.read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def reconcile(db_path: Path) -> None:
        """Mark orphaned in-flight operations as retryable after a crashed run."""
        import sqlite3
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path))
        changed = conn.execute(
            "UPDATE model_operations SET status='RETRY_AUTHORIZED' WHERE status='PENDING'"
        ).rowcount
        conn.commit()
        conn.close()
        print(f"[supervisor] reconciled {changed} pending operation(s)", flush=True)

    def make_transport(operation_db: Path, timeout: int = 300):
        selection = open_route(
            prereg["model_route"]["route"], operation_db=operation_db,
            env={
                "VERIMAT_OPENCODE_BASE_URL": __import__("os").environ.get(
                    "VERIMAT_OPENCODE_BASE_URL", "http://127.0.0.1:4123"),
                "VERIMAT_OPENCODE_PROVIDER": prereg["model_route"]["provider"],
                "VERIMAT_OPENCODE_MODEL": prereg["model_route"]["model"],
                "VERIMAT_OPENCODE_AGENT": prereg["model_route"]["agent"],
                "VERIMAT_OPENCODE_API_KEY": __import__("os").environ.get(
                    "VERIMAT_OPENCODE_API_KEY", "held-by-server"),
            },
            timeout_seconds=timeout,
        )
        return selection.transport

    stages = (["freeze", "extract", "claims", "verify", "gaps", "oracle", "score", "report"]
              if args.stage == "all" else [args.stage])
    corpus = None
    for stage in stages:
        print(f"== stage: {stage} ==")
        if stage == "freeze":
            stage_freeze(prereg, out)
            corpus = load_corpus(prereg, out)
        elif stage == "extract":
            corpus = corpus or load_corpus(prereg, out)
            for attempt in range(20):
                inner = make_transport(out / "extract_model_operations.sqlite", timeout=600)
                try:
                    stage_extract(prereg, out, inner)
                    inner.close()
                    break
                except Exception as exc:
                    inner.close()
                    print(f"[supervisor] extract attempt {attempt + 1} failed: {str(exc)[:140]}",
                          flush=True)
                    reconcile(out / "extract_model_operations.sqlite")
                    if attempt == 19:
                        raise
                    # The circuit breaker opens for a cooldown after repeated failures; retrying
                    # inside the cooldown fails instantly, so wait it out instead.
                    import time as _t
                    _t.sleep(70 if "circuit" in str(exc) else 10)
        elif stage == "claims":
            corpus = corpus or load_corpus(prereg, out)
            stage_claims(prereg, out, claim_limit=args.claim_limit)
        elif stage == "verify":
            corpus = corpus or load_corpus(prereg, out)
            stage_verify(prereg, out, make_transport, corpus)
        elif stage == "gaps":
            corpus = corpus or load_corpus(prereg, out)
            for attempt in range(20):
                inner = make_transport(out / "gaps_model_operations.sqlite", timeout=600)
                try:
                    stage_gaps(prereg, out, make_transport, corpus)
                    inner.close()
                    break
                except Exception as exc:
                    inner.close()
                    print(f"[supervisor] gaps attempt {attempt + 1} failed: {str(exc)[:140]}",
                          flush=True)
                    reconcile(out / "gaps_model_operations.sqlite")
                    if attempt == 19:
                        raise
                    import time as _t
                    _t.sleep(70 if "circuit" in str(exc) else 10)
        elif stage == "oracle":
            for attempt in range(20):
                inner = make_transport(out / "oracle_model_operations.sqlite", timeout=600)
                try:
                    stage_oracle(prereg, out, make_transport)
                    inner.close()
                    break
                except Exception as exc:
                    inner.close()
                    print(f"[supervisor] oracle attempt {attempt + 1} failed: {str(exc)[:140]}",
                          flush=True)
                    reconcile(out / "oracle_model_operations.sqlite")
                    if attempt == 19:
                        raise
                    import time as _t
                    _t.sleep(70 if "circuit" in str(exc) else 10)
        elif stage == "score":
            corpus = corpus or load_corpus(prereg, out)
            stage_score(prereg, out, corpus)
        elif stage == "report":
            stage_report(prereg, out)
    print("all requested stages complete")


if __name__ == "__main__":
    main()
