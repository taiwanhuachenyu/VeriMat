#!/usr/bin/env python3
"""
tools.jsonl_ops —— reduce 用代码做，别烧 token（对齐 Agent Graph Engineering）。

「很多 Agent 系统消耗的 token 不是花在推理上，而是花在本可由代码完成的数据处理上。」
合并 / 去重 / 筛选 / schema 校验这类确定性操作，交给本工具，不要调 Agent。

CLI:
  python -m src.tools.jsonl_ops merge a.jsonl b.jsonl -o out.jsonl
  python -m src.tools.jsonl_ops dedup in.jsonl --key doc_id -o out.jsonl     # 针对所有见过的
  python -m src.tools.jsonl_ops filter in.jsonl --where "tag=novel" -o out.jsonl
  python -m src.tools.jsonl_ops validate in.jsonl --schema schemas/xxx.schema.json
  python -m src.tools.jsonl_ops stats in.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys

from src.core.portability import extended_path


def read_jsonl(path: str) -> list[dict]:
    rows = []
    for line in extended_path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                sys.stderr.write(f"[jsonl_ops] 跳过非法行: {e}\n")
    return rows


def write_jsonl(rows: list[dict], path: str | None) -> None:
    out = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    if path:
        extended_path(path).write_text(out + "\n", encoding="utf-8")
    else:
        print(out)


def _get(row: dict, dotted: str):
    cur = row
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


# ---------- schema 校验（极简，纯 stdlib，支持 required/type/enum + 数组 items）----------
_TYPES = {"string": str, "number": (int, float), "integer": int,
          "boolean": bool, "object": dict, "array": list}


def _validate_obj(obj, schema, path="") -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    expected = _TYPES.get(t)
    type_ok = expected is None or isinstance(obj, expected)
    if t in ("integer", "number") and isinstance(obj, bool):
        type_ok = False
    if not type_ok:
        errs.append(f"{path or '<root>'}: 期望 {t}，实得 {type(obj).__name__}")
        return errs

    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path or '<root>'}={obj!r} 不在 enum {schema['enum']}")

    if "anyOf" in schema:
        alternatives = [_validate_obj(obj, sub, path) for sub in schema["anyOf"]]
        if all(alt for alt in alternatives):
            errs.append(f"{path or '<root>'}: 不符合 anyOf 中任一约束")

    if t == "object" or (isinstance(obj, dict) and
                          ("required" in schema or "properties" in schema)):
        for req in schema.get("required", []):
            if req not in obj:
                errs.append(f"{path}.{req} 缺失(required)")
        props = schema.get("properties", {})
        for k, sub in props.items():
            if k in obj:
                errs += _validate_obj(obj[k], sub, f"{path}.{k}")
    elif t == "array":
        if len(obj) < schema.get("minItems", 0):
            errs.append(f"{path or '<root>'}: 数组长度 {len(obj)} 小于 minItems")
        item_schema = schema.get("items")
        if item_schema:
            for i, it in enumerate(obj):
                errs += _validate_obj(it, item_schema, f"{path}[{i}]")
    return errs


def cmd_merge(a) -> int:
    rows: list[dict] = []
    for f in a.files:
        rows += read_jsonl(f)
    write_jsonl(rows, a.output)
    sys.stderr.write(f"[merge] {len(rows)} 行\n")
    return 0


def cmd_dedup(a) -> int:
    rows = read_jsonl(a.files[0])
    seen: set = set()
    out = []
    for r in rows:
        k = _get(r, a.key)
        if k is None:
            out.append(r)  # 无 key 的保留
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    write_jsonl(out, a.output)
    sys.stderr.write(f"[dedup] {len(rows)} → {len(out)}（key={a.key}）\n")
    return 0


def cmd_filter(a) -> int:
    rows = read_jsonl(a.files[0])
    field, _, val = a.where.partition("=")
    out = [r for r in rows if str(_get(r, field.strip())) == val.strip()]
    write_jsonl(out, a.output)
    sys.stderr.write(f"[filter] {len(rows)} → {len(out)}（{a.where}）\n")
    return 0


def cmd_validate(a) -> int:
    rows = read_jsonl(a.files[0])
    schema = json.loads(extended_path(a.schema).read_text(encoding="utf-8"))
    item_schema = schema.get("items", schema)  # 允许传数组 schema 或单条 schema
    bad = 0
    for i, r in enumerate(rows):
        errs = _validate_obj(r, item_schema, f"row[{i}]")
        if errs:
            bad += 1
            for e in errs[:5]:
                sys.stderr.write(f"[validate] {e}\n")
    ok = bad == 0
    print(json.dumps({"rows": len(rows), "invalid": bad, "ok": ok}, ensure_ascii=False))
    return 0 if ok else 1


def cmd_stats(a) -> int:
    rows = read_jsonl(a.files[0])
    keys: dict = {}
    for r in rows:
        for k in r:
            keys[k] = keys.get(k, 0) + 1
    print(json.dumps({"rows": len(rows), "field_coverage": keys}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jsonl_ops")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("merge", "dedup", "filter", "validate", "stats"):
        sp = sub.add_parser(name)
        sp.add_argument("files", nargs="+")
        sp.add_argument("-o", "--output", default=None)
        if name == "dedup":
            sp.add_argument("--key", default="doc_id")
        if name == "filter":
            sp.add_argument("--where", required=True, help='形如 "tag=novel"')
        if name == "validate":
            sp.add_argument("--schema", required=True)
    a = p.parse_args(argv)
    return {"merge": cmd_merge, "dedup": cmd_dedup, "filter": cmd_filter,
            "validate": cmd_validate, "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
