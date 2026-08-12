#!/usr/bin/env python3
"""
Sciverse API 客户端（纯 Python 标准库，无第三方依赖）。

设计目标（VeriMat 材料文献研究智能体）：
  1. 直连 https://api.sciverse.space（Bearer token），不走 cursor sdk / node / docker。
  2. 每一次调用都写入「审计链」（JSONL），记录 query / 参数 / 命中 doc_id / offset /
     page_no / score —— 这正是赛题要求的「可审计证据链」。
  3. 既可作为 Python 库被 harness 复用，也可作为 CLI 被 opencode agent 通过 `bash` 调用。

环境变量：
  SCIVERSE_API_TOKEN   必填，形如 sci_xxx
  SCIVERSE_BASE_URL    可选，默认 https://api.sciverse.space
  SCIVERSE_AUDIT_LOG   可选，审计链 JSONL 路径；不设则不落盘（stderr 仍打印摘要）

CLI 用法：
  python -m tools.sciverse search  "lithium cathode voltage" --top-k 10 [--year-from 2018] [--domain "Physical Sciences"]
  python -m tools.sciverse content --doc-id <id> [--offset 0] [--limit 4000]
  python -m tools.sciverse meta    "graphene anode"  --top-k 10
  所有命令输出 JSON 到 stdout（agent 可直接解析）。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE = "https://api.sciverse.space"
_RETRY_CODES = {500, 502, 503}


class SciverseError(RuntimeError):
    pass


class SciverseClient:
    def __init__(
        self,
        token: str | None = None,
        base_url: str | None = None,
        audit_log: str | None = None,
        timeout: float = 45.0,
    ):
        self.token = token or os.environ.get("SCIVERSE_API_TOKEN", "")
        if not self.token:
            raise SciverseError(
                "缺少 SCIVERSE_API_TOKEN（环境变量或构造参数），无法调用 Sciverse。"
            )
        self.base_url = (base_url or os.environ.get("SCIVERSE_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.audit_log = audit_log or os.environ.get("SCIVERSE_AUDIT_LOG") or ""
        self.timeout = timeout

    # ---------- 底层 HTTP ----------
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 params: dict | None = None, max_retries: int = 4) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last_err: Exception | None = None
        for attempt in range(max_retries):
            req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                code = e.code
                detail = e.read().decode("utf-8", "ignore")[:500]
                if code in _RETRY_CODES and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    last_err = SciverseError(f"HTTP {code}: {detail}")
                    continue
                if code == 429 and attempt < max_retries - 1:
                    time.sleep(min(2 ** attempt * 3, 30))
                    last_err = SciverseError(f"HTTP 429 rate limited: {detail}")
                    continue
                raise SciverseError(f"HTTP {code} on {method} {path}: {detail}") from e
            except urllib.error.URLError as e:
                last_err = SciverseError(f"URLError on {method} {path}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise last_err from e
        raise last_err or SciverseError("unknown request failure")

    # ---------- 审计链 ----------
    def _audit(self, tool: str, request: dict, hits: list, extra: dict | None = None) -> None:
        request_id = str((extra or {}).get("request_id") or uuid.uuid4())
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 2,
            "request_id": request_id,
            "tool": tool,
            "request": request,
            "n_hits": len(hits),
            "hits": [
                {
                    "doc_id": h.get("doc_id"),
                    "chunk_id": h.get("chunk_id"),
                    "title": h.get("title"),
                    "offset": h.get("offset"),
                    "page_no": h.get("page_no"),
                    "score": h.get("score"),
                    "doi": h.get("doi"),
                    "year": h.get("publication_published_year"),
                    "venue": h.get("publication_venue_name_unified"),
                }
                for h in hits
            ],
        }
        if extra:
            record.update(extra)
        if self.audit_log:
            os.makedirs(os.path.dirname(os.path.abspath(self.audit_log)), exist_ok=True)
            with open(self.audit_log, "a", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        sys.stderr.write(f"[sciverse:{tool}] {record['n_hits']} hits  q={request.get('query','')[:60]!r}\n")

    # ---------- 高层能力 ----------
    def agentic_search(self, query: str, top_k: int = 10, sub_queries: int = 0,
                       filters: dict | None = None, request_id: str | None = None,
                       max_retries: int = 4) -> list:
        """语义检索，返回可引用证据块（含 doc_id/offset/page_no/score）。"""
        body: dict = {"query": query, "top_k": top_k}
        if sub_queries:
            body["sub_queries"] = sub_queries
        if filters:
            body["filters"] = filters
        started = time.monotonic()
        resp = self._request(
            "POST", "/agentic-search", body=body, max_retries=max_retries,
        )
        hits = resp.get("hits") or []
        response_hash = hashlib.sha256(
            json.dumps(hits, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self._audit("agentic-search", body, hits, extra={
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "response_sha256": response_hash,
            **({"request_id": request_id} if request_id else {}),
        })
        return hits

    def content(
        self, doc_id: str, offset: int = 0, limit: int = 4096,
        request_id: str | None = None, max_retries: int = 4,
    ) -> dict:
        """按 doc_id + offset 读原文切片（扩展上下文）。"""
        started = time.monotonic()
        resp = self._request(
            "GET", "/content",
            params={"doc_id": doc_id, "offset": offset, "limit": limit},
            max_retries=max_retries,
        )
        text = resp.get("text") or ""
        self._audit("content", {"doc_id": doc_id, "offset": offset, "limit": limit}, [],
                    extra={
                        "bytes": len(text.encode("utf-8")),
                        "latency_ms": round((time.monotonic() - started) * 1000, 3),
                        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        **({"request_id": request_id} if request_id else {}),
                    })
        return resp

    def meta_search(self, query: str, top_k: int = 10, filters: dict | None = None) -> list:
        """结构化元数据检索（精确过滤/排序/DOI）。"""
        body: dict = {"query": query, "top_k": top_k}
        if filters:
            body["filters"] = filters
        started = time.monotonic()
        resp = self._request("POST", "/meta-search", body=body)
        hits = resp.get("hits") or resp.get("data") or []
        response_hash = hashlib.sha256(
            json.dumps(hits, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self._audit("meta-search", body, hits, extra={
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "response_sha256": response_hash,
        })
        return hits


# ---------- 过滤器构造 helper ----------
def build_filters(lang: str | None = None, year_from: int | None = None,
                  year_to: int | None = None, domain: str | None = None,
                  venue: str | None = None) -> dict:
    f: dict = {}
    if lang:
        f["lang"] = lang
    if year_from or year_to:
        rng: dict = {}
        if year_from:
            rng["gte"] = year_from
        if year_to:
            rng["lte"] = year_to
        f["publication_published_year"] = rng
    if venue:
        f["publication_venue_name_unified"] = venue
    if domain:
        f["topics"] = {"logic": "and", "dimensions": {"primary_topic_domain": domain}}
    return f


# ---------- CLI ----------
def _cmd_search(client: SciverseClient, a) -> dict:
    filters = build_filters(a.lang, a.year_from, a.year_to, a.domain, a.venue) or None
    hits = client.agentic_search(a.query, top_k=a.top_k, sub_queries=a.sub_queries, filters=filters)
    return {"tool": "agentic-search", "n": len(hits), "hits": hits}


def _cmd_content(client: SciverseClient, a) -> dict:
    return {"tool": "content", **client.content(a.doc_id, offset=a.offset, limit=a.limit)}


def _cmd_meta(client: SciverseClient, a) -> dict:
    filters = build_filters(a.lang, a.year_from, a.year_to, a.domain, a.venue) or None
    hits = client.meta_search(a.query, top_k=a.top_k, filters=filters)
    return {"tool": "meta-search", "n": len(hits), "hits": hits}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sciverse", description="Sciverse API CLI (stdlib only)")
    p.add_argument("--base-url", default=None)
    p.add_argument("--audit-log", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        # Accept runtime options after the subcommand as well as before it.  Using
        # SUPPRESS preserves a value supplied to the root parser.
        sp.add_argument("--base-url", default=argparse.SUPPRESS)
        sp.add_argument("--audit-log", default=argparse.SUPPRESS)
        sp.add_argument("--top-k", type=int, default=10)
        sp.add_argument("--lang", default=None)
        sp.add_argument("--year-from", type=int, default=None)
        sp.add_argument("--year-to", type=int, default=None)
        sp.add_argument("--domain", default=None, help="e.g. 'Physical Sciences'")
        sp.add_argument("--venue", default=None)

    sp = sub.add_parser("search", help="agentic-search 语义检索")
    sp.add_argument("query")
    sp.add_argument("--sub-queries", type=int, default=0)
    add_common(sp)
    sp.set_defaults(func=_cmd_search)

    sp = sub.add_parser("content", help="按 doc_id 读原文切片")
    sp.add_argument("--base-url", default=argparse.SUPPRESS)
    sp.add_argument("--audit-log", default=argparse.SUPPRESS)
    sp.add_argument("--doc-id", required=True)
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--limit", type=int, default=4096)
    sp.set_defaults(func=_cmd_content)

    sp = sub.add_parser("meta", help="meta-search 结构化检索")
    sp.add_argument("query")
    add_common(sp)
    sp.set_defaults(func=_cmd_meta)

    a = p.parse_args(argv)
    try:
        client = SciverseClient(base_url=a.base_url, audit_log=a.audit_log)
        out = a.func(client, a)
    except SciverseError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
