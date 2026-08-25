"""Recoverable stage worker with stable side-effect identities and fail-closed outcomes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.core.events import EventValidationError
from src.core.portability import extended_path
from src.evidence.graph import GraphInvariantError
from src.evidence.ledger import EventLedger, LedgerIntegrityError

from .job_store import (
    BudgetExceeded, IdempotencyConflict, IllegalTransition, Job, JobStatus, JobStore,
    LeaseConflict, Stage,
)
from .runtime import ExecutionContext, FinalizationReport

EXECUTION_STAGES = tuple(stage for stage in Stage if stage != Stage.VALIDATE)


class RetryableStageError(RuntimeError):
    pass


class FatalStageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StageInvocation:
    job: Job
    stage: Stage
    operation_id: str


@dataclass(frozen=True)
class EventSpec:
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str


@dataclass(frozen=True)
class ChargeSpec:
    charge_key: str
    provider: str
    calls: int = 0
    tokens: int = 0
    cost_microunits: int = 0


@dataclass(frozen=True)
class StageResult:
    events: tuple[EventSpec, ...] = ()
    charges: tuple[ChargeSpec, ...] = ()
    checkpoint: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerReport:
    claimed: bool
    job_id: str = ""
    status: str = "IDLE"
    stages_committed: tuple[str, ...] = ()
    error_code: str = ""
    finalization: FinalizationReport | None = None


StageHandler = Callable[[StageInvocation], StageResult]
Finalizer = Callable[[ExecutionContext], FinalizationReport]


class ExecutionWorker:
    """Execute one queued/retryable job and resume only from committed checkpoints.

    A handler must use ``operation_id`` as the idempotency key for any external request. The worker
    can make ledger writes, charges, and checkpoints idempotent after the handler returns, but no
    local mechanism can make an upstream API call exactly-once without upstream idempotency.
    """

    def __init__(
        self, *, store: JobStore, ledger_root: str | Path, worker_id: str,
        handlers: dict[Stage, StageHandler], lease_seconds: float = 60,
        max_attempts: int = 3, finalizer: Finalizer | None = None,
        job_id: str | None = None,
    ):
        if not worker_id.strip() or lease_seconds <= 0 or max_attempts < 1:
            raise ValueError("worker id, positive lease, and max_attempts are required")
        missing = [stage.value for stage in EXECUTION_STAGES if stage not in handlers]
        if missing:
            raise ValueError("missing stage handlers: " + ", ".join(missing))
        self.store = store
        self.ledger_root = extended_path(ledger_root)
        self.worker_id = worker_id
        self.handlers = handlers
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.finalizer = finalizer or (lambda context: context.finalize())
        self.job_id = job_id

    @staticmethod
    def _checkpoint_key(stage: Stage) -> str:
        return f"stage:{stage.value}:v1"

    def _claim(self) -> Job | None:
        candidates = (
            [self.job_id] if self.job_id is not None
            else self.store.runnable_job_ids(limit=100)
        )
        for job_id in candidates:
            try:
                return self.store.acquire_lease(
                    job_id, worker_id=self.worker_id, lease_seconds=self.lease_seconds,
                )
            except LeaseConflict:
                continue
        return None

    def _fail(self, job: Job, *, retryable: bool, code: str) -> Job:
        current = self.store.get(job.job_id)
        if current.status in {JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.SUCCEEDED}:
            return current
        target = (
            JobStatus.RETRY_WAIT
            if retryable and current.attempts < self.max_attempts
            else JobStatus.FAILED
        )
        return self.store.transition(current.job_id, target=target, error_code=code)

    def run_once(self) -> WorkerReport:
        job = self._claim()
        if job is None:
            return WorkerReport(claimed=False)
        ledger = EventLedger(
            self.ledger_root / job.tenant_id / job.job_id / "events.jsonl"
        )
        context = ExecutionContext(
            store=self.store, job=job, worker_id=self.worker_id, ledger=ledger,
        )
        committed = {
            row["checkpoint_key"] for row in self.store.checkpoints(job.job_id)
        }
        stages_committed: list[str] = []
        try:
            for stage in EXECUTION_STAGES:
                checkpoint_key = self._checkpoint_key(stage)
                if checkpoint_key in committed:
                    continue
                current = self.store.get(job.job_id)
                if current.status == JobStatus.CANCELLED:
                    return WorkerReport(
                        claimed=True, job_id=job.job_id, status=current.status.value,
                        stages_committed=tuple(stages_committed),
                    )
                if current.status != JobStatus.RUNNING:
                    raise FatalStageError(f"job left RUNNING state: {current.status.value}")
                invocation = StageInvocation(
                    job=current, stage=stage,
                    operation_id=f"{job.job_id}:{stage.value}:v1",
                )
                result = self.handlers[stage](invocation)
                if not isinstance(result, StageResult):
                    raise FatalStageError(f"{stage.value} handler returned invalid result")
                for event in result.events:
                    context.emit(**event.__dict__)
                for charge in result.charges:
                    context.charge(**charge.__dict__)
                context.checkpoint(
                    stage=stage, checkpoint_key=checkpoint_key,
                    payload={
                        **result.checkpoint, "operation_id": invocation.operation_id,
                    },
                )
                stages_committed.append(stage.value)
            finalization = self.finalizer(context)
            return WorkerReport(
                claimed=True, job_id=job.job_id, status=finalization.job.status.value,
                stages_committed=tuple(stages_committed), finalization=finalization,
                error_code="" if finalization.ok else "EVIDENCE_INVARIANT_FAILED",
            )
        except RetryableStageError:
            failed = self._fail(job, retryable=True, code="RETRYABLE_STAGE_ERROR")
            return WorkerReport(
                claimed=True, job_id=job.job_id, status=failed.status.value,
                stages_committed=tuple(stages_committed),
                error_code="RETRYABLE_STAGE_ERROR",
            )
        except BudgetExceeded:
            failed = self._fail(job, retryable=False, code="BUDGET_EXCEEDED")
            return WorkerReport(
                claimed=True, job_id=job.job_id, status=failed.status.value,
                stages_committed=tuple(stages_committed), error_code="BUDGET_EXCEEDED",
            )
        except (
            EventValidationError, FatalStageError, GraphInvariantError,
            IdempotencyConflict, IllegalTransition, LedgerIntegrityError,
        ):
            failed = self._fail(job, retryable=False, code="FATAL_STAGE_ERROR")
            return WorkerReport(
                claimed=True, job_id=job.job_id, status=failed.status.value,
                stages_committed=tuple(stages_committed), error_code="FATAL_STAGE_ERROR",
            )
        except LeaseConflict:
            current = self.store.get(job.job_id)
            if current.status == JobStatus.CANCELLED:
                return WorkerReport(
                    claimed=True, job_id=job.job_id, status=current.status.value,
                    stages_committed=tuple(stages_committed),
                )
            failed = self._fail(job, retryable=True, code="LEASE_LOST")
            return WorkerReport(
                claimed=True, job_id=job.job_id, status=failed.status.value,
                stages_committed=tuple(stages_committed), error_code="LEASE_LOST",
            )
        except Exception:
            failed = self._fail(job, retryable=True, code="UNHANDLED_STAGE_ERROR")
            return WorkerReport(
                claimed=True, job_id=job.job_id, status=failed.status.value,
                stages_committed=tuple(stages_committed),
                error_code="UNHANDLED_STAGE_ERROR",
            )
