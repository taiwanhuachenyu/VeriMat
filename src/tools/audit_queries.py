#!/usr/bin/env python3
"""Check that every declared counter-query is copied from the immutable search audit."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from src.harness.validator import _artifact_after_replays, _counter_replay_coverage


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _rows(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: row is not an object")
        rows.append(value)
    return rows


def audited_queries(audit_path: Path) -> set[str]:
    observed = set()
    for row in _rows(audit_path):
        if row.get("tool") not in ("agentic-search", "meta-search"):
            continue
        query = (row.get("request") or {}).get("query")
        if query:
            observed.add(_norm(query))
    return observed


def declared_queries(path: Path) -> list[tuple[str, str]]:
    output = []
    for row in _rows(path):
        identity = str(row.get("finding_id") or row.get("candidate_id") or "?")
        if isinstance(row.get("counterevidence_check"), dict):
            values = row["counterevidence_check"].get("queries") or []
        else:
            values = row.get("counter_queries") or []
        output.extend((identity, str(value)) for value in values if str(value).strip())
    return output


def check(audit_path: Path, output_paths: list[Path]) -> list[dict]:
    observed = audited_queries(audit_path)
    missing = []
    for path in output_paths:
        if not path.exists():
            missing.append({"file": str(path), "id": "?", "query": "<FILE_MISSING>"})
            continue
        for identity, query in declared_queries(path):
            if _norm(query) not in observed:
                missing.append({"file": str(path), "id": identity, "query": query})
    return missing


def missing_replays(audit_path: Path, output_paths: list[Path]) -> list[dict]:
    audit = _rows(audit_path)
    missing = []
    for path in output_paths:
        if not path.exists():
            continue
        for row in _rows(path):
            identity = str(row.get("finding_id") or row.get("candidate_id") or "?")
            field = ("counterevidence_check" if isinstance(
                row.get("counterevidence_check"), dict) else "counter_queries")
            if _counter_replay_coverage([row], field, audit) < 1.0:
                missing.append({"file": str(path), "id": identity})
    return missing


def invalid_counter_subsets(output_paths: list[Path]) -> list[dict]:
    invalid = []
    for path in output_paths:
        if not path.exists():
            continue
        for row in _rows(path):
            if not row.get("candidate_id"):
                continue
            checked = {item.get("doc_id") for item in row.get("checked_evidence") or []
                       if isinstance(item, dict)}
            counter = {item.get("doc_id") for item in row.get("counter_evidence") or []
                       if isinstance(item, dict)}
            if not counter.issubset(checked):
                invalid.append({"file": str(path), "id": str(row["candidate_id"])})
    return invalid


def stale_artifacts(audit_path: Path, output_paths: list[Path]) -> list[dict]:
    audit = _rows(audit_path)
    stale = []
    for path in output_paths:
        if not path.exists():
            continue
        rows = _rows(path)
        if not rows:
            continue
        field = ("counterevidence_check" if any(
            isinstance(row.get("counterevidence_check"), dict) for row in rows)
                 else "counter_queries")
        if not _artifact_after_replays(path, rows, field, audit):
            stale.append({"file": str(path)})
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("outputs", nargs="+", type=Path)
    args = parser.parse_args(argv)
    missing = check(args.audit, args.outputs)
    replay_missing = missing_replays(args.audit, args.outputs)
    subset_invalid = invalid_counter_subsets(args.outputs)
    stale = stale_artifacts(args.audit, args.outputs)
    if missing:
        for item in missing:
            print(f"MISSING\t{item['file']}\t{item['id']}\t{item['query']}")
    for item in replay_missing:
        print(f"MISSING_REPLAY\t{item['file']}\t{item['id']}\t"
              "counter-query hit was not followed by a content read")
    for item in subset_invalid:
        print(f"INVALID_COUNTER_SUBSET\t{item['file']}\t{item['id']}\t"
              "counter_evidence must be included in checked_evidence")
    for item in stale:
        print(f"STALE_DECISION_ARTIFACT\t{item['file']}\t"
              "rewrite the final JSONL after qualifying content reads")
    if missing or replay_missing or subset_invalid or stale:
        return 1
    print("AUDIT_QUERY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
