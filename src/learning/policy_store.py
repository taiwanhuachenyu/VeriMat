"""Delayed-credit policy memory that never learns from model self-approval."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from src.operations.runtime_migrations import (
    POLICY_SPEC, assert_runtime_compatibility, prepare_runtime_database, schema_script,
)


class PolicyStoreError(RuntimeError):
    pass


class CreditConflict(PolicyStoreError):
    pass


class StrategyStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


ALLOWED_EVALUATORS = {"known_answer", "expert_blind", "temporal_holdout"}
ALLOWED_KINDS = {"counter_query_template", "boundary_probe", "precedent_probe"}

SCHEMA = schema_script(POLICY_SPEC)


@dataclass(frozen=True)
class StrategyScore:
    strategy_id: str
    kind: str
    pattern: str
    status: StrategyStatus
    evaluations: int
    successes: int
    success_rate: float
    wilson_lower: float
    mean_calls: float
    mean_tokens: float


@dataclass(frozen=True)
class PolicyEvent:
    event_id: str
    tenant_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    idempotency_key: str
    created_at: float


def _wilson_lower(successes: int, trials: int, z: float = 1.959963984540054) -> float:
    if trials <= 0:
        return 0.0
    proportion = successes / trials
    denominator = 1 + z * z / trials
    centre = proportion + z * z / (2 * trials)
    margin = z * math.sqrt(
        proportion * (1 - proportion) / trials + z * z / (4 * trials * trials)
    )
    return max(0.0, (centre - margin) / denominator)


class PolicyStore:
    """Persist strategy applications first, then accept independent delayed outcomes.

    A candidate strategy cannot be recalled until it has enough outcomes from task families
    different from the task that produced it. Only known-answer, blinded-expert, or temporal-
    holdout evaluation can create credit; model self-ratings are rejected by construction.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        prepare_runtime_database(self.path, POLICY_SPEC)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA journal_mode=WAL")
        assert_runtime_compatibility(self.conn, POLICY_SPEC)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PolicyStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @contextmanager
    def _write_transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _enqueue(
        self, *, tenant_id: str, event_type: str, aggregate_type: str,
        aggregate_id: str, payload: dict[str, Any], idempotency_key: str,
    ) -> None:
        rendered = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
        self.conn.execute(
            "INSERT INTO policy_outbox VALUES (?,?,?,?,?,?,?,?,NULL)",
            (str(uuid.uuid4()), tenant_id, event_type, aggregate_type, aggregate_id,
             rendered, idempotency_key, time.time()),
        )

    @staticmethod
    def _strategy_key(kind: str, pattern: str) -> str:
        normalized = " ".join(pattern.lower().split())
        return hashlib.sha256(f"{kind}|{normalized}".encode()).hexdigest()

    def propose_strategy(
        self, *, tenant_id: str, kind: str, pattern: str, source_job_id: str,
        source_task_family: str, strategy_id: str | None = None,
    ) -> str:
        if kind not in ALLOWED_KINDS:
            raise PolicyStoreError(f"unsupported strategy kind {kind!r}")
        if (
            not tenant_id.strip() or not pattern.strip() or not source_job_id.strip()
            or not source_task_family.strip()
        ):
            raise PolicyStoreError(
                "tenant_id, pattern, source_job_id, and source_task_family are required"
            )
        key, now = self._strategy_key(kind, pattern), time.time()
        identifier = strategy_id or str(uuid.uuid4())
        with self._write_transaction():
            existing = self.conn.execute(
                "SELECT * FROM strategies WHERE tenant_id=? AND strategy_key=?",
                (tenant_id, key),
            ).fetchone()
            if existing is not None:
                return str(existing["strategy_id"])
            self.conn.execute(
                "INSERT INTO strategies VALUES (?,?,?,?,?,?,?,?,?,?)",
                (identifier, tenant_id, key, kind, pattern, source_job_id,
                 source_task_family, StrategyStatus.CANDIDATE.value, now, now),
            )
            self._enqueue(
                tenant_id=tenant_id, event_type="policy.strategy_proposed",
                aggregate_type="strategy", aggregate_id=identifier,
                payload={
                    "strategy_id": identifier, "kind": kind,
                    "pattern_sha256": hashlib.sha256(pattern.encode()).hexdigest(),
                    "source_job_id": source_job_id,
                    "source_task_family": source_task_family,
                },
                idempotency_key=f"strategy:{identifier}:proposed",
            )
        return identifier

    def record_application(
        self, *, tenant_id: str, strategy_id: str, target_job_id: str,
        target_task_family: str,
        rendered_query: str, idempotency_key: str,
        application_id: str | None = None,
    ) -> str:
        strategy = self.conn.execute(
            "SELECT * FROM strategies WHERE strategy_id=? AND tenant_id=?",
            (strategy_id, tenant_id),
        ).fetchone()
        if strategy is None:
            raise PolicyStoreError(f"unknown strategy {strategy_id!r}")
        if strategy["status"] == StrategyStatus.RETIRED.value:
            raise PolicyStoreError("retired strategy cannot be applied")
        if not target_job_id.strip() or not target_task_family.strip() or not rendered_query.strip():
            raise PolicyStoreError("target and rendered query fields are required")
        query_hash = hashlib.sha256(rendered_query.encode()).hexdigest()
        identifier = application_id or str(uuid.uuid4())
        with self._write_transaction():
            # Recheck under the write lock; another process may have raced the first lookup.
            strategy = self.conn.execute(
                "SELECT * FROM strategies WHERE strategy_id=? AND tenant_id=?",
                (strategy_id, tenant_id),
            ).fetchone()
            if strategy is None:
                raise PolicyStoreError(f"unknown strategy {strategy_id!r}")
            if strategy["status"] == StrategyStatus.RETIRED.value:
                raise PolicyStoreError("retired strategy cannot be applied")
            existing = self.conn.execute(
                """SELECT * FROM applications
                   WHERE tenant_id=? AND idempotency_key=?""",
                (tenant_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["strategy_id"] == strategy_id
                    and existing["target_job_id"] == target_job_id
                    and existing["target_task_family"] == target_task_family
                    and existing["rendered_query_sha256"] == query_hash
                )
                if not same:
                    raise CreditConflict(
                        "application idempotency key has different semantics"
                    )
                return str(existing["application_id"])
            self.conn.execute(
                "INSERT INTO applications VALUES (?,?,?,?,?,?,?,?)",
                (identifier, strategy_id, tenant_id, target_job_id,
                 target_task_family, query_hash, idempotency_key, time.time()),
            )
            self._enqueue(
                tenant_id=tenant_id, event_type="policy.strategy_applied",
                aggregate_type="strategy", aggregate_id=strategy_id,
                payload={
                    "strategy_id": strategy_id, "application_id": identifier,
                    "target_job_id": target_job_id,
                    "target_task_family": target_task_family,
                    "rendered_query_sha256": query_hash,
                },
                idempotency_key=f"application:{identifier}:recorded",
            )
        return identifier

    def record_outcome(
        self, *, tenant_id: str, application_id: str, evaluator_kind: str, success: bool,
        false_gap_avoided: bool, valid_finding_delta: float, calls: int, tokens: int,
        evidence_ref: str,
    ) -> None:
        if evaluator_kind not in ALLOWED_EVALUATORS:
            raise PolicyStoreError(
                "credit must come from known_answer, expert_blind, or temporal_holdout"
            )
        if min(calls, tokens) < 0 or not math.isfinite(valid_finding_delta):
            raise PolicyStoreError("invalid outcome costs or delta")
        if not evidence_ref.strip():
            raise PolicyStoreError("outcome requires a replayable evaluation evidence_ref")
        application = self.conn.execute(
            "SELECT * FROM applications WHERE application_id=? AND tenant_id=?",
            (application_id, tenant_id),
        ).fetchone()
        if application is None:
            raise PolicyStoreError(f"unknown application {application_id!r}")
        semantic = (
            evaluator_kind, int(success), int(false_gap_avoided), float(valid_finding_delta),
            calls, tokens, evidence_ref,
        )
        with self._write_transaction():
            application = self.conn.execute(
                "SELECT * FROM applications WHERE application_id=? AND tenant_id=?",
                (application_id, tenant_id),
            ).fetchone()
            if application is None:
                raise PolicyStoreError(f"unknown application {application_id!r}")
            existing = self.conn.execute(
                "SELECT * FROM outcomes WHERE application_id=?", (application_id,)
            ).fetchone()
            if existing is not None:
                current = (
                    existing["evaluator_kind"], existing["success"],
                    existing["false_gap_avoided"], existing["valid_finding_delta"],
                    existing["calls"], existing["tokens"], existing["evidence_ref"],
                )
                if current != semantic:
                    raise CreditConflict("application outcome already recorded differently")
                return
            self.conn.execute(
                "INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?)",
                (application_id, *semantic, time.time()),
            )
            self._enqueue(
                tenant_id=tenant_id, event_type="policy.outcome_credited",
                aggregate_type="strategy", aggregate_id=str(application["strategy_id"]),
                payload={
                    "strategy_id": str(application["strategy_id"]),
                    "application_id": application_id,
                    "target_job_id": str(application["target_job_id"]),
                    "evaluator_kind": evaluator_kind, "success": bool(success),
                    "false_gap_avoided": bool(false_gap_avoided),
                    "valid_finding_delta": float(valid_finding_delta),
                    "calls": calls, "tokens": tokens,
                    "evidence_ref_sha256": hashlib.sha256(
                        evidence_ref.encode()
                    ).hexdigest(),
                },
                idempotency_key=f"application:{application_id}:outcome",
            )

    def application_has_outcome(self, *, tenant_id: str, application_id: str) -> bool:
        row = self.conn.execute(
            """SELECT 1 FROM outcomes o JOIN applications a
               ON a.application_id=o.application_id
               WHERE a.tenant_id=? AND o.application_id=?""",
            (tenant_id, application_id),
        ).fetchone()
        return row is not None

    def _score_rows(self, *, tenant_id: str) -> list[StrategyScore]:
        rows = self.conn.execute(
            """SELECT s.strategy_id,s.kind,s.pattern,s.status,s.source_task_family,
                      COUNT(o.application_id) evaluations,
                      COALESCE(SUM(o.success),0) successes,
                      COALESCE(AVG(o.calls),0) mean_calls,
                      COALESCE(AVG(o.tokens),0) mean_tokens
               FROM strategies s
               LEFT JOIN applications a ON a.strategy_id=s.strategy_id
                 AND a.target_task_family<>s.source_task_family
               LEFT JOIN outcomes o ON o.application_id=a.application_id
               WHERE s.tenant_id=?
               GROUP BY s.strategy_id ORDER BY s.strategy_id""",
            (tenant_id,),
        ).fetchall()
        scores: list[StrategyScore] = []
        for row in rows:
            evaluations, successes = int(row["evaluations"]), int(row["successes"])
            scores.append(StrategyScore(
                strategy_id=str(row["strategy_id"]), kind=str(row["kind"]),
                pattern=str(row["pattern"]), status=StrategyStatus(row["status"]),
                evaluations=evaluations, successes=successes,
                success_rate=round(successes / evaluations, 6) if evaluations else 0.0,
                wilson_lower=round(_wilson_lower(successes, evaluations), 6),
                mean_calls=round(float(row["mean_calls"]), 6),
                mean_tokens=round(float(row["mean_tokens"]), 6),
            ))
        return scores

    def refresh_statuses(
        self, *, tenant_id: str, min_evaluations: int = 3,
        activation_lower_bound: float = 0.2,
        suspension_lower_bound: float = 0.05,
    ) -> list[StrategyScore]:
        if min_evaluations < 1 or not (0 <= suspension_lower_bound <= activation_lower_bound <= 1):
            raise PolicyStoreError("invalid promotion thresholds")
        now = time.time()
        with self._write_transaction():
            scores = self._score_rows(tenant_id=tenant_id)
            for score in scores:
                if score.status == StrategyStatus.RETIRED:
                    continue
                if score.evaluations < min_evaluations:
                    target = StrategyStatus.CANDIDATE
                elif score.wilson_lower >= activation_lower_bound:
                    target = StrategyStatus.ACTIVE
                elif score.wilson_lower < suspension_lower_bound:
                    target = StrategyStatus.SUSPENDED
                else:
                    target = StrategyStatus.CANDIDATE
                if target == score.status:
                    continue
                self.conn.execute(
                    """UPDATE strategies SET status=?,updated_at=?
                       WHERE strategy_id=? AND tenant_id=?""",
                    (target.value, now, score.strategy_id, tenant_id),
                )
                transition = {
                    "strategy_id": score.strategy_id,
                    "from_status": score.status.value,
                    "to_status": target.value,
                    "evaluations": score.evaluations,
                    "successes": score.successes,
                    "wilson_lower": score.wilson_lower,
                }
                transition_hash = hashlib.sha256(
                    json.dumps(transition, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                self._enqueue(
                    tenant_id=tenant_id, event_type="policy.status_changed",
                    aggregate_type="strategy", aggregate_id=score.strategy_id,
                    payload=transition,
                    idempotency_key=(
                        f"strategy:{score.strategy_id}:transition:{transition_hash}"
                    ),
                )
        return self._score_rows(tenant_id=tenant_id)

    def recall_active(
        self, *, tenant_id: str, target_task_family: str, limit: int = 5,
    ) -> list[StrategyScore]:
        if limit < 1:
            return []
        source = {row["strategy_id"]: row["source_task_family"] for row in self.conn.execute(
            "SELECT strategy_id,source_task_family FROM strategies WHERE tenant_id=?",
            (tenant_id,),
        )}
        scores = [
            score for score in self._score_rows(tenant_id=tenant_id)
            if score.status == StrategyStatus.ACTIVE
            and source[score.strategy_id] != target_task_family
        ]
        return sorted(
            scores,
            key=lambda score: (score.wilson_lower, score.success_rate, -score.mean_calls),
            reverse=True,
        )[:limit]

    def recall_uncredited(
        self, *, tenant_id: str, target_task_family: str, limit: int = 5,
    ) -> list[StrategyScore]:
        """Replay recent non-retired strategies without interpreting outcomes as credit."""
        if limit < 1:
            return []
        scores = {score.strategy_id: score for score in self._score_rows(tenant_id=tenant_id)}
        rows = self.conn.execute(
            """SELECT strategy_id FROM strategies
               WHERE tenant_id=? AND status<>? AND source_task_family<>?
               ORDER BY created_at DESC,strategy_id LIMIT ?""",
            (tenant_id, StrategyStatus.RETIRED.value, target_task_family, limit),
        ).fetchall()
        return [scores[str(row["strategy_id"])] for row in rows]

    def recall_candidate_for_evaluation(
        self, *, tenant_id: str, target_task_family: str,
    ) -> StrategyScore | None:
        """Choose one cross-family candidate for attributable, delayed evaluation.

        Selection is deterministic and favors the candidate with the fewest independently
        credited applications. A strategy is never evaluated twice in one task family.
        """
        row = self.conn.execute(
            """SELECT s.strategy_id,COUNT(o.application_id) evaluations,s.created_at
               FROM strategies s
               LEFT JOIN applications a ON a.strategy_id=s.strategy_id
                 AND a.target_task_family<>s.source_task_family
               LEFT JOIN outcomes o ON o.application_id=a.application_id
               WHERE s.tenant_id=? AND s.status=? AND s.source_task_family<>?
                 AND NOT EXISTS (
                   SELECT 1 FROM applications prior
                   WHERE prior.strategy_id=s.strategy_id
                     AND prior.target_task_family=?
                 )
               GROUP BY s.strategy_id
               ORDER BY evaluations ASC,s.created_at ASC,s.strategy_id ASC
               LIMIT 1""",
            (tenant_id, StrategyStatus.CANDIDATE.value, target_task_family,
             target_task_family),
        ).fetchone()
        if row is None:
            return None
        scores = {score.strategy_id: score for score in self._score_rows(tenant_id=tenant_id)}
        return scores[str(row["strategy_id"])]

    def bind_sequence_intervention(
        self, *, tenant_id: str, run_id: str, method_id: str, sequence_index: int,
        challenge_id: str, order_sha256: str, memory_mode: str,
        strategy_ids: list[str],
    ) -> list[StrategyScore]:
        """Persist the exact pre-task memory intervention before any task execution."""
        if (
            not all(value.strip() for value in (
                tenant_id, run_id, method_id, challenge_id, order_sha256, memory_mode,
            ))
            or sequence_index < 0
            or len(order_sha256) != 64
            or len(strategy_ids) != len(set(strategy_ids))
        ):
            raise PolicyStoreError("invalid ordered intervention identity")
        rows = self.conn.execute(
            """SELECT strategy_id FROM strategies
               WHERE tenant_id=? AND strategy_id IN ({})""".format(
                ",".join("?" for _ in strategy_ids) or "NULL"
            ),
            (tenant_id, *strategy_ids),
        ).fetchall()
        if {str(row["strategy_id"]) for row in rows} != set(strategy_ids):
            raise PolicyStoreError("ordered intervention references an unknown strategy")
        rendered = json.dumps(strategy_ids, separators=(",", ":"))
        semantic = (
            challenge_id, order_sha256, memory_mode, rendered,
        )
        with self._write_transaction():
            existing = self.conn.execute(
                """SELECT * FROM sequence_interventions
                   WHERE tenant_id=? AND run_id=? AND method_id=? AND sequence_index=?""",
                (tenant_id, run_id, method_id, sequence_index),
            ).fetchone()
            if existing is not None:
                current = (
                    str(existing["challenge_id"]), str(existing["order_sha256"]),
                    str(existing["memory_mode"]), str(existing["strategy_ids"]),
                )
                if current != semantic:
                    raise CreditConflict("ordered intervention already has different semantics")
            else:
                self.conn.execute(
                    "INSERT INTO sequence_interventions VALUES (?,?,?,?,?,?,?,?,?)",
                    (tenant_id, run_id, method_id, sequence_index, challenge_id,
                     order_sha256, memory_mode, rendered, time.time()),
                )
                self._enqueue(
                    tenant_id=tenant_id,
                    event_type="policy.sequence_intervention_bound",
                    aggregate_type="policy_sequence",
                    aggregate_id=f"{run_id}:{method_id}",
                    payload={
                        "run_id": run_id, "method_id": method_id,
                        "sequence_index": sequence_index,
                        "challenge_id": challenge_id,
                        "order_sha256": order_sha256,
                        "memory_mode": memory_mode,
                        "strategy_ids": strategy_ids,
                    },
                    idempotency_key=(
                        f"sequence:{run_id}:{method_id}:{sequence_index}:bound"
                    ),
                )
        scores = {score.strategy_id: score for score in self._score_rows(tenant_id=tenant_id)}
        return [scores[strategy_id] for strategy_id in strategy_ids]

    def sequence_intervention(
        self, *, tenant_id: str, run_id: str, method_id: str, sequence_index: int,
    ) -> list[StrategyScore] | None:
        row = self.conn.execute(
            """SELECT strategy_ids FROM sequence_interventions
               WHERE tenant_id=? AND run_id=? AND method_id=? AND sequence_index=?""",
            (tenant_id, run_id, method_id, sequence_index),
        ).fetchone()
        if row is None:
            return None
        identifiers = json.loads(str(row["strategy_ids"]))
        scores = {score.strategy_id: score for score in self._score_rows(tenant_id=tenant_id)}
        return [scores[strategy_id] for strategy_id in identifiers]

    def audit_snapshot(self, *, tenant_id: str) -> dict[str, Any]:
        scores = self._score_rows(tenant_id=tenant_id)
        return {
            "schema_version": 1,
            "allowed_evaluators": sorted(ALLOWED_EVALUATORS),
            "strategies": [
                {**score.__dict__, "status": score.status.value} for score in scores
            ],
            "applications": self.conn.execute(
                "SELECT COUNT(*) FROM applications WHERE tenant_id=?", (tenant_id,)
            ).fetchone()[0],
            "credited_outcomes": self.conn.execute(
                """SELECT COUNT(*) FROM outcomes o JOIN applications a
                   ON a.application_id=o.application_id WHERE a.tenant_id=?""",
                (tenant_id,),
            ).fetchone()[0],
            "undispatched_events": self.conn.execute(
                """SELECT COUNT(*) FROM policy_outbox
                   WHERE tenant_id=? AND dispatched_at IS NULL""",
                (tenant_id,),
            ).fetchone()[0],
            "sequence_interventions": self.conn.execute(
                """SELECT COUNT(*) FROM sequence_interventions
                   WHERE tenant_id=?""", (tenant_id,),
            ).fetchone()[0],
        }

    def pending_events(self, *, tenant_id: str, limit: int = 100) -> list[PolicyEvent]:
        if limit < 1:
            return []
        rows = self.conn.execute(
            """SELECT * FROM policy_outbox
               WHERE tenant_id=? AND dispatched_at IS NULL
               ORDER BY created_at,event_id LIMIT ?""",
            (tenant_id, limit),
        ).fetchall()
        return [
            PolicyEvent(
                event_id=str(row["event_id"]), tenant_id=str(row["tenant_id"]),
                event_type=str(row["event_type"]),
                aggregate_type=str(row["aggregate_type"]),
                aggregate_id=str(row["aggregate_id"]),
                payload=json.loads(row["payload"]),
                idempotency_key=str(row["idempotency_key"]),
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    def mark_dispatched(self, *, tenant_id: str, event_id: str) -> None:
        cursor = self.conn.execute(
            """UPDATE policy_outbox SET dispatched_at=COALESCE(dispatched_at,?)
               WHERE tenant_id=? AND event_id=?""",
            (time.time(), tenant_id, event_id),
        )
        if cursor.rowcount != 1:
            raise PolicyStoreError(f"unknown policy event {event_id!r}")
