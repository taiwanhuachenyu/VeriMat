"""Cutoff-safe Sciverse retrieval with indeterminate-call quarantine and response replay."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Protocol

from src.core.events import canonical_json
from src.core.portability import extended_path
from src.operations.runtime_migrations import (
    RETRIEVAL_OPERATION_SPEC, assert_runtime_compatibility, prepare_runtime_database,
    schema_script,
)
from src.tools.sciverse import MAX_CONTENT_LIMIT, SciverseClient, semantic_filters

from .baseline_runner import (
    BaselineContractError, RetrievalResult, RetrievedPassage, Usage,
)
from .circuit_breaker import PersistentCircuitBreaker

CACHE_SCHEMA = schema_script(RETRIEVAL_OPERATION_SPEC)


class IndeterminateRetrievalOperation(BaselineContractError):
    pass


class SciverseAPI(Protocol):
    def agentic_search(
        self, query: str, top_k: int = 10, *, filters: dict | None = None,
        mode: str | None = None, source_types: list[str] | None = None,
        request_id: str | None = None, max_retries: int = 4,
    ) -> list: ...

    def content(
        self, doc_id: str, offset: int = 0, limit: int = 4096,
        request_id: str | None = None, max_retries: int = 4,
    ) -> dict: ...


class CachedSciverseTransport:
    """Cache exact Sciverse responses and quarantine uncertain paid/network calls."""

    def __init__(
        self, *, client: SciverseAPI, operation_db: str | Path,
        circuit_failure_threshold: int = 3,
        circuit_recovery_seconds: float = 60.0,
    ):
        self.client = client
        path = extended_path(operation_db)
        path.parent.mkdir(parents=True, exist_ok=True)
        prepare_runtime_database(path, RETRIEVAL_OPERATION_SPEC)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA journal_mode=WAL")
        assert_runtime_compatibility(self.conn, RETRIEVAL_OPERATION_SPEC)
        self.circuit = PersistentCircuitBreaker(
            database=path, circuit_id="retrieval:sciverse",
            database_spec=RETRIEVAL_OPERATION_SPEC,
            failure_threshold=circuit_failure_threshold,
            recovery_timeout_seconds=circuit_recovery_seconds,
            probe_timeout_seconds=180.0,
        )

    def close(self) -> None:
        self.circuit.close()
        self.conn.close()

    @staticmethod
    def _hash(kind: str, request: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json({
            "kind": kind, "request": request,
        }).encode()).hexdigest()

    def _lookup(self, operation_id: str, request_hash: str) -> Any | None:
        row = self.conn.execute(
            "SELECT * FROM retrieval_operations WHERE operation_id=?", (operation_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_hash:
            raise BaselineContractError(
                "retrieval operation id was reused with different semantics"
            )
        if row["status"] in {"PENDING", "ABANDONED"}:
            raise IndeterminateRetrievalOperation(
                f"retrieval operation is {row['status']}; operator reconciliation required"
            )
        if row["status"] == "RETRY_AUTHORIZED":
            return None
        if row["status"] != "COMPLETED":
            raise BaselineContractError("retrieval operation cache has invalid status")
        return json.loads(str(row["response_json"]))

    def _reserve(self, operation_id: str, request_hash: str) -> Any | None:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT * FROM retrieval_operations WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if row is not None:
                if row["request_sha256"] != request_hash:
                    self.conn.execute("COMMIT")
                    raise BaselineContractError(
                        "retrieval operation id was reused with different semantics"
                    )
                if row["status"] == "RETRY_AUTHORIZED":
                    cursor = self.conn.execute(
                        """UPDATE retrieval_operations SET status='PENDING',created_at=?
                           WHERE operation_id=? AND request_sha256=?
                             AND status='RETRY_AUTHORIZED'""",
                        (time.time(), operation_id, request_hash),
                    )
                    if cursor.rowcount != 1:
                        raise IndeterminateRetrievalOperation(
                            "retry authorization was consumed concurrently"
                        )
                    self.conn.execute("COMMIT")
                    return None
                self.conn.execute("COMMIT")
                cached = self._lookup(operation_id, request_hash)
                if cached is not None:
                    return cached
                raise IndeterminateRetrievalOperation(
                    "retrieval operation changed during reservation"
                )
            self.conn.execute(
                "INSERT INTO retrieval_operations VALUES (?,?,?,NULL,?,NULL)",
                (operation_id, request_hash, "PENDING", time.time()),
            )
            self.conn.execute("COMMIT")
            return None
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _complete(self, operation_id: str, request_hash: str, response: Any) -> Any:
        rendered = canonical_json(response)
        cursor = self.conn.execute(
            """UPDATE retrieval_operations SET status='COMPLETED',response_json=?,
                      completed_at=?
               WHERE operation_id=? AND request_sha256=? AND status='PENDING'""",
            (rendered, time.time(), operation_id, request_hash),
        )
        if cursor.rowcount != 1:
            raise IndeterminateRetrievalOperation("retrieval cache commit failed")
        return json.loads(rendered)

    def search(
        self, *, operation_id: str, query: str, top_k: int, filters: dict,
    ) -> list[dict[str, Any]]:
        request = {"query": query, "top_k": top_k, "filters": filters}
        request_hash = self._hash("search", request)
        cached = self._lookup(operation_id, request_hash)
        if cached is not None:
            return cached
        self.circuit.before_call(operation_id=operation_id)
        try:
            raced = self._reserve(operation_id, request_hash)
            if raced is not None:
                self.circuit.record_success(operation_id=operation_id)
                return raced
            response = self.client.agentic_search(
                query, top_k=top_k, filters=filters,
                request_id=operation_id, max_retries=1,
            )
            if not isinstance(response, list):
                raise IndeterminateRetrievalOperation(
                    "Sciverse search returned an invalid response"
                )
        except Exception as exc:
            self.circuit.record_failure(
                operation_id=operation_id, reason_code="retrieval_search_failed",
            )
            if isinstance(exc, IndeterminateRetrievalOperation):
                raise
            raise IndeterminateRetrievalOperation(
                "Sciverse search state is indeterminate; automatic retry is disabled"
            ) from exc
        self.circuit.record_success(operation_id=operation_id)
        return self._complete(operation_id, request_hash, response)

    def content(
        self, *, operation_id: str, doc_id: str, offset: int, limit: int,
    ) -> dict[str, Any]:
        request = {"doc_id": doc_id, "offset": offset, "limit": limit}
        request_hash = self._hash("content", request)
        cached = self._lookup(operation_id, request_hash)
        if cached is not None:
            return cached
        self.circuit.before_call(operation_id=operation_id)
        try:
            raced = self._reserve(operation_id, request_hash)
            if raced is not None:
                self.circuit.record_success(operation_id=operation_id)
                return raced
            response = self.client.content(
                doc_id, offset=offset, limit=limit,
                request_id=operation_id, max_retries=1,
            )
            if not isinstance(response, dict):
                raise IndeterminateRetrievalOperation(
                    "Sciverse content returned an invalid response"
                )
        except Exception as exc:
            self.circuit.record_failure(
                operation_id=operation_id, reason_code="retrieval_content_failed",
            )
            if isinstance(exc, IndeterminateRetrievalOperation):
                raise
            raise IndeterminateRetrievalOperation(
                "Sciverse content state is indeterminate; automatic retry is disabled"
            ) from exc
        self.circuit.record_success(operation_id=operation_id)
        return self._complete(operation_id, request_hash, response)


def _year_of(*candidates: Any) -> int | None:
    """Read a publication year out of whichever numeric shape the endpoint used.

    Semantic search reports this field as an integer while metadata search reports the same field
    as a float (``1999.0``), so an int-only check silently discarded every metadata row.  Values
    outside a plausible range are treated as unknown rather than converted: the corpus uses ``0``
    as a missing-year sentinel and ``date(0, ...)`` is not constructible.
    """
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                continue
        if isinstance(value, (int, float)):
            year = int(value)
            if 1000 <= year <= 9999:
                return year
    return None


def _publication_date(hit: dict[str, Any]) -> date | None:
    for key in ("publication_date", "publication_published_date", "published_at"):
        value = hit.get(key)
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                return None
    year = _year_of(hit.get("publication_published_year"), hit.get("year"))
    if year is None:
        return None
    # December 31 is conservative: a same-year hit is included only for a year-end cutoff.
    return date(year, 12, 31)


class SciverseBenchmarkRetriever:
    """Retrieve and re-read passages under a conservative publication cutoff."""

    def __init__(
        self, *, transport: CachedSciverseTransport, index_snapshot_id: str,
        index_snapshot_attested: bool = False, top_k: int = 3,
        content_limit: int = 4000,
    ):
        if (
            not index_snapshot_id.strip() or top_k < 1 or top_k > 20
            or content_limit < 256 or content_limit > MAX_CONTENT_LIMIT
        ):
            raise ValueError("retrieval snapshot identity or bounds are invalid")
        self.transport = transport
        self.index_snapshot_id = index_snapshot_id
        self.index_snapshot_attested = index_snapshot_attested
        self.top_k = top_k
        self.content_limit = content_limit
        self.provider_id = f"sciverse:{index_snapshot_id}"

    def provenance_manifest(self) -> dict[str, Any]:
        return {
            "provider": "sciverse",
            "index_snapshot_id": self.index_snapshot_id,
            "index_snapshot_attested": self.index_snapshot_attested,
            "publication_ready": self.index_snapshot_attested,
            "top_k": self.top_k,
            "content_limit": self.content_limit,
            "cutoff_policy": "exact date when available; unknown same-year dates excluded",
        }

    def search(
        self, *, query_id: str, query: str, intent: str, cutoff_date: str,
        operation_id: str, reserve_call: Callable[[str], None],
    ) -> RetrievalResult:
        del intent
        cutoff = date.fromisoformat(cutoff_date)
        # The year bound is load-bearing for the cutoff claim, and it does hold: a `lte` bound
        # was observed to return only years at or below it, with no hit missing a year.  Every
        # hit is still re-checked against the cutoff below, so a provider that quietly loosened
        # this would cost recall rather than turn into a false provenance claim.
        filters = semantic_filters(
            lang="en", year_to=cutoff.year, domain="Physical Sciences",
        )
        reserve_call("search")
        hits = self.transport.search(
            operation_id=f"{operation_id}:search", query=query,
            top_k=self.top_k, filters=filters,
        )
        calls = 1
        passages: list[RetrievedPassage] = []
        seen: set[tuple[str, int]] = set()
        for index, hit in enumerate(hits[:self.top_k]):
            if not isinstance(hit, dict):
                continue
            doc_id = str(hit.get("doc_id") or "").strip()
            offset = hit.get("offset")
            published = _publication_date(hit)
            if (
                not doc_id or isinstance(offset, bool) or not isinstance(offset, int)
                or offset < 0 or published is None or published > cutoff
                or (doc_id, offset) in seen
            ):
                continue
            seen.add((doc_id, offset))
            suboperation = f"content:{index}:{hashlib.sha256(doc_id.encode()).hexdigest()[:16]}"
            reserve_call(suboperation)
            calls += 1
            content = self.transport.content(
                operation_id=f"{operation_id}:{suboperation}",
                doc_id=doc_id, offset=offset, limit=self.content_limit,
            )
            text = str(content.get("text") or "").strip()
            if not text:
                continue
            digest = hashlib.sha256(text.encode()).hexdigest()
            passage_id = "obs-" + hashlib.sha256(
                f"{query_id}|{doc_id}|{offset}|{digest}".encode()
            ).hexdigest()[:32]
            passages.append(RetrievedPassage(
                passage_id=passage_id, query_id=query_id, doc_id=doc_id,
                text=text, locator={"offset": offset}, content_sha256=digest,
                publication_date=published.isoformat(),
            ))
        result = RetrievalResult(tuple(passages), Usage(calls=calls, tokens=0))
        result.validate()
        return result
