"""Durable task registration, idempotent submission, one execution slot, resume.

Storage and network boundaries follow ``07-data-and-architecture.md`` §5/§6 and
``state-conventions.md`` §3/§6/§7: inputs, first event and the original response
commit together; a provider call is audited as ``prepared`` and then
``in_flight`` before it may be sent; nothing is re-sent after an unknown outcome.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from pydantic import ValidationError

from game_app.contracts.common import (
    Budget,
    CallCounters,
    MutationContext,
    ResourceResult,
    TaskAccepted,
)
from game_app.contracts.enums import (
    TERMINAL_TASK_STATES,
    ExternalCallState,
    TaskState,
    TaskType,
)
from game_app.contracts.primitives import ObjectRef, new_id, utc_now
from game_app.contracts.tasks import (
    DomainResult,
    ResumeTaskInput,
    TaskInputAdapter,
    TaskRun,
    TaskStopReason,
)
from game_app.errors import ContractError, ErrorCode, ErrorEnvelope, FieldError
from game_app.repository import ProductRepository, UnitOfWork, canonical_json

PRODUCT_CONTRACT_VERSION = "product-contract/0.4"
DEFAULT_ROUTES = {
    "knowledge": "/api/knowledge-tasks",
    "analysis": "/api/analyses",
}
_COUNTER_LIMITS = {
    "model": "max_model_calls",
    "read": "max_read_calls",
    "search": "max_searches",
}
_BLOCKING_CALL_STATES = {
    ExternalCallState.in_flight.value,
    ExternalCallState.outcome_unknown.value,
}


class _SuspendExecution(Exception):
    """Internal signal: the handler parked the task on a player answer."""


class _TaskCancelled(Exception):
    """Internal signal: a cancel request won before a new provider call."""


class BudgetExhausted(Exception):
    """The task cannot start another audited call within its budget."""


class UnknownCall(Exception):
    """The provider call may have been delivered; its outcome is unknown."""

    def __init__(self, call_id: str) -> None:
        super().__init__(f"call {call_id} has an unknown outcome")
        self.call_id = call_id


@dataclass
class HandlerOutcome:
    state: TaskState = TaskState.completed
    domain_result: DomainResult | None = None
    stop_reason: TaskStopReason | None = None
    error_code: str | None = None
    result_refs: list[ObjectRef] = field(default_factory=list)

    @classmethod
    def completed(cls) -> "HandlerOutcome":
        return cls(state=TaskState.completed)


@dataclass(frozen=True)
class RunOutcome:
    task_id: str
    state: TaskState


class _NullProvider:
    sends = 0

    def send(self, request: Mapping[str, Any]) -> dict[str, Any]:
        raise ContractError(
            ErrorCode.PROVIDER_NOT_CONFIGURED,
            "no provider is configured; no call was attempted",
        )


@dataclass(frozen=True)
class TaskProgress:
    phase: str
    last_activity_at: str | None
    completed_steps: list[str]


@dataclass(frozen=True)
class TaskViewResult:
    task: TaskRun
    error: ErrorEnvelope | None
    result_refs: list[ObjectRef]
    progress: TaskProgress


class TaskContext:
    """What a domain handler may do; it never writes task state directly."""

    def __init__(self, service: "TaskService", task_id: str) -> None:
        self.service = service
        self.task_id = task_id
        self.suspended = False

    @property
    def task_input(self) -> Any:
        return self.service._load_input(self.task_id)

    @property
    def budget(self) -> Budget:
        return self.service._load_budget(self.task_id)

    def suspend(self, *, information_request_id: str) -> None:
        self.service._suspend(self.task_id, information_request_id)
        self.suspended = True
        raise _SuspendExecution()

    def invoke_provider(
        self,
        *,
        kind: str = "model",
        provider_name: str = "stub",
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.service._invoke(
            self.task_id, kind=kind, provider_name=provider_name, payload=payload or {}
        )

    def checkpoint(self, completed_steps: Sequence[str]) -> str:
        return self.service.record_checkpoint(self.task_id, completed_steps)


class TaskService:
    def __init__(
        self,
        repository: ProductRepository,
        *,
        provider: Any | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = repository
        self.provider = provider or _NullProvider()
        self.clock = clock
        self.handlers: dict[str, Callable[[TaskContext], HandlerOutcome | None]] = {}
        self._lock = threading.Lock()

    # -- registration -----------------------------------------------------
    def register_task(
        self,
        task_input: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        canonical_route: str | None = None,
        method: str = "POST",
        budget: Budget | None = None,
    ) -> TaskAccepted:
        typed = _validate_task_input(task_input)
        route = canonical_route or DEFAULT_ROUTES[typed.kind]
        effective_budget = getattr(typed, "budget", None) or budget or Budget()
        request_hash = _request_hash(request_schema_version, method, route, typed, effective_budget)

        with self.repository.unit_of_work() as uow:
            replay = self._replay_if_registered(
                uow, context, method, route, request_schema_version, request_hash
            )
            if replay is not None:
                return replay
            task_id = new_id()
            now = self.clock()
            uow.execute(
                "INSERT INTO tasks(task_id, type, user_scope_id, parent_id, revision, state,"
                " current_input_revision, budget, calls, checkpoint_id, domain_result, stop_reason,"
                " result_refs, cancel_requested, last_error_code, active_elapsed_seconds,"
                " resume_blocked_unknown_call, created_at, updated_at)"
                " VALUES(?, ?, ?, NULL, 1, 'queued', 1, ?, ?, NULL, NULL, NULL, '[]', 0, NULL,"
                " 0, 0, ?, ?)",
                (
                    task_id,
                    typed.kind,
                    context.user_scope_id,
                    effective_budget.model_dump_json(),
                    CallCounters().model_dump_json(),
                    now,
                    now,
                ),
            )
            uow.execute(
                "INSERT INTO task_inputs(task_id, input_revision, input, supersedes_revision,"
                " cause_ref, created_at) VALUES(?, 1, ?, NULL, NULL, ?)",
                (task_id, canonical_json(typed.model_dump(mode="json")).decode("utf-8"), now),
            )
            self._append_event(uow, task_id, 1, 1, None, TaskState.queued, "registered")
            accepted = TaskAccepted(task_id=task_id, input_revision=1, state=TaskState.queued)
            uow.insert_idempotency(
                user_scope_id=context.user_scope_id,
                method=method,
                canonical_route=route,
                key=context.idempotency_key,
                request_schema_version=request_schema_version,
                request_hash=request_hash,
                response_status=202,
                response_body=canonical_json(accepted.model_dump(mode="json")),
                task_id=task_id,
                result_refs=[{"entity_type": "TaskRun", "entity_id": task_id}],
            )
            return accepted

    # -- reads -------------------------------------------------------------
    def get_task(self, task_id: str) -> TaskViewResult:
        with self.repository.unit_of_work() as uow:
            row = self._task_row(uow, task_id)
            return self._view(uow, row)

    def _view(self, uow: UnitOfWork, row: Any) -> TaskViewResult:
        task = self._task_from_row(row)
        last = uow.query(
            "SELECT created_at FROM task_events WHERE task_id=? ORDER BY sequence DESC LIMIT 1",
            (task.id,),
        )
        completed_steps: list[str] = []
        if task.checkpoint_id is not None:
            checkpoint = uow.query(
                "SELECT completed_steps FROM task_checkpoints WHERE checkpoint_id=?",
                (task.checkpoint_id,),
            )
            if checkpoint:
                completed_steps = json.loads(checkpoint[0]["completed_steps"])
        return TaskViewResult(
            task=task,
            error=None,
            result_refs=task.result_refs,
            progress=TaskProgress(
                phase=_phase_for(task.state),
                last_activity_at=last[0]["created_at"] if last else None,
                completed_steps=completed_steps,
            ),
        )

    # -- control -----------------------------------------------------------
    def cancel_task(self, task_id: str, context: MutationContext) -> ResourceResult:
        with self.repository.unit_of_work() as uow:
            row = self._task_row(uow, task_id)
            reference = ObjectRef(entity_type="TaskRun", entity_id=task_id)
            state = TaskState(row["state"])
            if state in TERMINAL_TASK_STATES:
                return ResourceResult(refs=[reference])
            now = self.clock()
            revision = row["revision"] + 1
            if state is TaskState.running:
                uow.execute(
                    "UPDATE tasks SET cancel_requested=1, revision=?, updated_at=? WHERE task_id=?",
                    (revision, now, task_id),
                )
                self._append_event(
                    uow, task_id, revision, row["current_input_revision"],
                    state, state, "cancel_requested",
                    active_elapsed=row["active_elapsed_seconds"],
                )
            else:
                stop = TaskStopReason(kind="execution", code="user_cancelled")
                uow.execute(
                    "UPDATE tasks SET cancel_requested=1, state='cancelled', revision=?,"
                    " stop_reason=?, updated_at=? WHERE task_id=?",
                    (revision, stop.model_dump_json(), now, task_id),
                )
                self._append_event(
                    uow, task_id, revision, row["current_input_revision"],
                    state, TaskState.cancelled, "cancelled",
                    active_elapsed=row["active_elapsed_seconds"],
                )
            return ResourceResult(refs=[reference])

    # -- startup recovery ---------------------------------------------------
    def recover_on_startup(self) -> list[str]:
        """Mark in-flight calls unknown and unfinished tasks interrupted."""
        interrupted: list[str] = []
        with self.repository.unit_of_work() as uow:
            now = self.clock()
            uow.execute(
                "UPDATE external_calls SET call_state='outcome_unknown', finished_at=?,"
                " error_code='CALL_OUTCOME_UNKNOWN'"
                " WHERE call_state IN ('in_flight')",
                (now,),
            )
            rows = uow.query(
                "SELECT * FROM tasks WHERE state IN ('queued', 'running')"
                " ORDER BY created_at, rowid"
            )
            for row in rows:
                revision = row["revision"] + 1
                uow.execute(
                    "UPDATE tasks SET state='interrupted', revision=?, updated_at=? WHERE task_id=?",
                    (revision, now, row["task_id"]),
                )
                self._append_event(
                    uow, row["task_id"], revision, row["current_input_revision"],
                    TaskState(row["state"]), TaskState.interrupted, "restart_detected",
                    active_elapsed=row["active_elapsed_seconds"],
                )
                interrupted.append(row["task_id"])
        return interrupted

    # -- resume --------------------------------------------------------------
    def resume_task(
        self,
        resume_input: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        method: str = "POST",
    ) -> TaskAccepted:
        try:
            resume = ResumeTaskInput.model_validate(resume_input)
        except ValidationError as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the resume input is invalid",
                field_errors=[
                    FieldError(path=".".join(str(part) for part in item["loc"]),
                               code="type", message=item["msg"])
                    for item in error.errors()
                ],
            ) from error
        route = f"/api/tasks/{resume.parent_task_id}/resume"
        request_hash = hashlib.sha256(canonical_json({
            "schema_version": request_schema_version,
            "method": method,
            "route": route,
            "input": resume.model_dump(mode="json"),
        })).hexdigest()

        with self.repository.unit_of_work() as uow:
            replay = self._replay_if_registered(
                uow, context, method, route, request_schema_version, request_hash
            )
            if replay is not None:
                return replay
            parent = self._task_row(uow, resume.parent_task_id)
            if TaskState(parent["state"]) not in TERMINAL_TASK_STATES:
                raise ContractError(
                    ErrorCode.TASK_STATE_CONFLICT,
                    "only a terminal task can be resumed",
                    field_errors=[FieldError(
                        path="parent_task_id", code="conflict",
                        message=f"task is {parent['state']}",
                    )],
                )
            checkpoint_steps: list[str] = []
            if resume.checkpoint_id is not None:
                checkpoint = uow.query(
                    "SELECT * FROM task_checkpoints WHERE checkpoint_id=?",
                    (resume.checkpoint_id,),
                )
                if (
                    not checkpoint
                    or checkpoint[0]["task_id"] != resume.parent_task_id
                    or checkpoint[0]["input_revision"] != parent["current_input_revision"]
                ):
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID,
                        "the checkpoint does not belong to this task and input revision",
                        field_errors=[FieldError(
                            path="checkpoint_id", code="reference",
                            message="foreign or incompatible checkpoint",
                        )],
                    )
                checkpoint_steps = json.loads(checkpoint[0]["completed_steps"])
            elif parent["checkpoint_id"] is not None:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "the parent has a checkpoint; resuming with null must not hide it",
                    field_errors=[FieldError(
                        path="checkpoint_id", code="required",
                        message="pass the existing checkpoint or start a new task",
                    )],
                )

            blocked = self._chain_has_blocking_calls(uow, resume.parent_task_id)
            input_row = uow.query(
                "SELECT input FROM task_inputs WHERE task_id=? AND input_revision=?",
                (resume.parent_task_id, parent["current_input_revision"]),
            )
            if not input_row:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "the parent has no durable input to resume from",
                )
            task_id = new_id()
            now = self.clock()
            uow.execute(
                "INSERT INTO tasks(task_id, type, user_scope_id, parent_id, revision, state,"
                " current_input_revision, budget, calls, checkpoint_id, domain_result, stop_reason,"
                " result_refs, cancel_requested, last_error_code, active_elapsed_seconds,"
                " resume_blocked_unknown_call, created_at, updated_at)"
                " VALUES(?, ?, ?, ?, 1, 'queued', 1, ?, ?, ?, NULL, NULL, '[]', 0, NULL, 0, ?, ?, ?)",
                (
                    task_id,
                    parent["type"],
                    context.user_scope_id,
                    resume.parent_task_id,
                    resume.budget.model_dump_json(),
                    CallCounters().model_dump_json(),
                    resume.checkpoint_id,
                    int(blocked),
                    now,
                    now,
                ),
            )
            uow.execute(
                "INSERT INTO task_inputs(task_id, input_revision, input, supersedes_revision,"
                " cause_ref, created_at) VALUES(?, 1, ?, NULL, ?, ?)",
                (
                    task_id,
                    input_row[0]["input"],
                    canonical_json({"entity_type": "TaskRun", "entity_id": resume.parent_task_id}).decode(),
                    now,
                ),
            )
            self._append_event(
                uow, task_id, 1, 1, None, TaskState.queued,
                "resumed_from_parent", related=[ObjectRef(
                    entity_type="TaskRun", entity_id=resume.parent_task_id,
                )],
            )
            accepted = TaskAccepted(task_id=task_id, input_revision=1, state=TaskState.queued)
            uow.insert_idempotency(
                user_scope_id=context.user_scope_id,
                method=method,
                canonical_route=route,
                key=context.idempotency_key,
                request_schema_version=request_schema_version,
                request_hash=request_hash,
                response_status=202,
                response_body=canonical_json(accepted.model_dump(mode="json")),
                task_id=task_id,
                result_refs=[{"entity_type": "TaskRun", "entity_id": task_id}],
            )
            return accepted

    # -- checkpoints ---------------------------------------------------------
    def record_checkpoint(self, task_id: str, completed_steps: Sequence[str]) -> str:
        with self.repository.unit_of_work() as uow:
            row = self._task_row(uow, task_id)
            checkpoint_id = new_id()
            calls = uow.query(
                "SELECT call_id FROM external_calls WHERE task_id=? AND call_state='succeeded'",
                (task_id,),
            )
            uow.execute(
                "INSERT INTO task_checkpoints(checkpoint_id, task_id, input_revision,"
                " completed_steps, artifact_refs, call_ids, created_at)"
                " VALUES(?, ?, ?, ?, '[]', ?, ?)",
                (
                    checkpoint_id,
                    task_id,
                    row["current_input_revision"],
                    canonical_json(list(completed_steps)).decode("utf-8"),
                    canonical_json([item["call_id"] for item in calls]).decode("utf-8"),
                    self.clock(),
                ),
            )
            uow.execute(
                "UPDATE tasks SET checkpoint_id=?, revision=?, updated_at=? WHERE task_id=?",
                (checkpoint_id, row["revision"] + 1, self.clock(), task_id),
            )
            return checkpoint_id

    # -- execution -----------------------------------------------------------
    def run_once(self) -> RunOutcome | None:
        """Claim at most one queued task; waiting tasks never hold the slot."""
        claimed = self._claim_next()
        if claimed is None:
            return None
        task_id, task_type, blocked = claimed
        if blocked:
            state = self._finish(
                task_id,
                TaskState.partial,
                stop_reason=TaskStopReason(kind="execution", code="process_exit"),
                error_code=ErrorCode.CALL_OUTCOME_UNKNOWN.value,
            )
            return RunOutcome(task_id=task_id, state=state)

        handler = self.handlers.get(task_type.value) or (lambda ctx: HandlerOutcome.completed())
        context = TaskContext(self, task_id)
        try:
            outcome = handler(context)
        except _SuspendExecution:
            return RunOutcome(task_id=task_id, state=TaskState.waiting_input)
        except _TaskCancelled:
            state = self._finish(task_id, TaskState.cancelled)
            return RunOutcome(task_id=task_id, state=state)
        except UnknownCall:
            state = self._finish(
                task_id,
                TaskState.partial,
                stop_reason=TaskStopReason(kind="execution", code="process_exit"),
                error_code=ErrorCode.CALL_OUTCOME_UNKNOWN.value,
            )
            return RunOutcome(task_id=task_id, state=state)
        except BudgetExhausted:
            state = self._finish(
                task_id,
                TaskState.partial,
                stop_reason=TaskStopReason(kind="collection", code="budget_exhausted"),
            )
            return RunOutcome(task_id=task_id, state=state)
        except ContractError as error:
            state = self._finish(
                task_id,
                TaskState.failed,
                stop_reason=TaskStopReason(kind="execution", code="validation_failed"),
                error_code=error.code.value,
            )
            return RunOutcome(task_id=task_id, state=state)
        except Exception:  # noqa: BLE001 - never leak a traceback into a task state
            state = self._finish(
                task_id,
                TaskState.failed,
                stop_reason=TaskStopReason(kind="execution", code="provider_error"),
                error_code=ErrorCode.INTERNAL_ERROR.value,
            )
            return RunOutcome(task_id=task_id, state=state)

        if context.suspended:
            return RunOutcome(task_id=task_id, state=TaskState.waiting_input)
        final = outcome or HandlerOutcome.completed()
        state = self._finish(
            task_id,
            final.state,
            domain_result=final.domain_result,
            stop_reason=final.stop_reason,
            error_code=final.error_code,
            result_refs=final.result_refs,
        )
        return RunOutcome(task_id=task_id, state=state)

    def _claim_next(self) -> tuple[str, TaskType, bool] | None:
        with self.repository.unit_of_work() as uow:
            if uow.query("SELECT 1 FROM tasks WHERE state='running' LIMIT 1"):
                return None
            rows = uow.query(
                "SELECT * FROM tasks WHERE state='queued' ORDER BY created_at, rowid LIMIT 1"
            )
            if not rows:
                return None
            row = rows[0]
            next_sequence = self._next_sequence(uow, row["task_id"])
            revision = row["revision"] + 1
            uow.execute(
                "UPDATE tasks SET state='running', revision=?, updated_at=? WHERE task_id=?",
                (revision, self.clock(), row["task_id"]),
            )
            self._append_event(
                uow, row["task_id"], next_sequence, row["current_input_revision"],
                TaskState.queued, TaskState.running, "started",
                active_elapsed=row["active_elapsed_seconds"],
            )
            return (
                row["task_id"],
                TaskType(row["type"]),
                bool(row["resume_blocked_unknown_call"]),
            )

    def _finish(
        self,
        task_id: str,
        state: TaskState,
        *,
        domain_result: DomainResult | None = None,
        stop_reason: TaskStopReason | None = None,
        error_code: str | None = None,
        result_refs: Sequence[ObjectRef] = (),
    ) -> TaskState:
        with self.repository.unit_of_work() as uow:
            row = self._task_row(uow, task_id)
            current = TaskState(row["state"])
            if current in TERMINAL_TASK_STATES:
                return current
            if row["cancel_requested"]:
                state = TaskState.cancelled
                stop_reason = TaskStopReason(kind="execution", code="user_cancelled")
            revision = row["revision"] + 1
            now = self.clock()
            uow.execute(
                "UPDATE tasks SET state=?, domain_result=?, stop_reason=?, result_refs=?,"
                " last_error_code=?, revision=?, updated_at=? WHERE task_id=?",
                (
                    state.value,
                    None if domain_result is None else domain_result.model_dump_json(),
                    None if stop_reason is None else stop_reason.model_dump_json(),
                    canonical_json([ref.model_dump(mode="json") for ref in result_refs]).decode(),
                    error_code,
                    revision,
                    now,
                    task_id,
                ),
            )
            self._append_event(
                uow, task_id, self._next_sequence(uow, task_id), row["current_input_revision"],
                current, state, "finished" if state is not TaskState.cancelled else "cancelled",
                active_elapsed=row["active_elapsed_seconds"],
            )
            return state

    def _suspend(self, task_id: str, information_request_id: str) -> None:
        with self.repository.unit_of_work() as uow:
            row = self._task_row(uow, task_id)
            if TaskState(row["state"]) is not TaskState.running:
                raise ContractError(
                    ErrorCode.TASK_STATE_CONFLICT,
                    "only a running task can wait for input",
                )
            revision = row["revision"] + 1
            uow.execute(
                "UPDATE tasks SET state='waiting_input', revision=?, updated_at=? WHERE task_id=?",
                (revision, self.clock(), task_id),
            )
            self._append_event(
                uow, task_id, self._next_sequence(uow, task_id), row["current_input_revision"],
                TaskState.running, TaskState.waiting_input, "awaiting_input",
                related=[ObjectRef(
                    entity_type="InformationRequest", entity_id=information_request_id,
                )],
                active_elapsed=row["active_elapsed_seconds"],
            )

    # -- audited provider calls ------------------------------------------------
    def _invoke(
        self,
        task_id: str,
        *,
        kind: str,
        provider_name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if kind not in _COUNTER_LIMITS:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                f"unknown external call kind: {kind}",
            )
        call_id = new_id()
        request = {
            "call_id": call_id,
            "task_id": task_id,
            "kind": kind,
            "payload": dict(payload),
        }
        input_hash = hashlib.sha256(canonical_json(request)).hexdigest()
        with self.repository.unit_of_work() as uow:
            row = self._task_row(uow, task_id)
            if row["cancel_requested"]:
                raise _TaskCancelled()
            if TaskState(row["state"]) is not TaskState.running:
                raise ContractError(ErrorCode.TASK_STATE_CONFLICT, "the task is not running")
            budget = Budget.model_validate_json(row["budget"])
            counters = CallCounters.model_validate_json(row["calls"])
            limit = getattr(budget, _COUNTER_LIMITS[kind])
            if getattr(counters, kind) >= limit:
                raise BudgetExhausted()
            now = self.clock()
            uow.execute(
                "INSERT INTO external_calls(call_id, task_id, input_revision, kind, call_state,"
                " provider, requested_model, returned_model, prompt_version, output_schema_version,"
                " input_hash, intent_saved_at, sent_at, finished_at, provider_request_id,"
                " response_file_id, usage, billing_state, error_code)"
                " VALUES(?, ?, ?, ?, 'prepared', ?, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL,"
                " NULL, NULL, NULL, 'not_reported', NULL)",
                (call_id, task_id, row["current_input_revision"], kind, provider_name, input_hash, now),
            )
            updated = counters.model_copy(update={kind: getattr(counters, kind) + 1})
            uow.execute(
                "UPDATE tasks SET calls=?, updated_at=? WHERE task_id=?",
                (updated.model_dump_json(), now, task_id),
            )
            uow.execute(
                "UPDATE external_calls SET call_state='in_flight', sent_at=? WHERE call_id=?",
                (now, call_id),
            )

        try:
            reply = self.provider.send(request)
        except (TimeoutError, ConnectionError) as error:
            with self.repository.unit_of_work() as uow:
                uow.execute(
                    "UPDATE external_calls SET call_state='outcome_unknown', finished_at=?,"
                    " error_code='CALL_OUTCOME_UNKNOWN' WHERE call_id=?",
                    (self.clock(), call_id),
                )
            raise UnknownCall(call_id) from error
        except Exception as error:
            with self.repository.unit_of_work() as uow:
                uow.execute(
                    "UPDATE external_calls SET call_state='failed_known', finished_at=?,"
                    " error_code='PROVIDER_FAILED' WHERE call_id=?",
                    (self.clock(), call_id),
                )
            raise

        with self.repository.unit_of_work() as uow:
            uow.execute(
                "UPDATE external_calls SET call_state='succeeded', finished_at=?,"
                " provider_request_id=?, returned_model=?, usage=?, billing_state='not_reported'"
                " WHERE call_id=?",
                (
                    self.clock(),
                    reply.get("provider_request_id"),
                    reply.get("returned_model"),
                    canonical_json(reply.get("usage") or {}).decode("utf-8"),
                    call_id,
                ),
            )
        return dict(reply)

    # -- helpers ---------------------------------------------------------------
    def _task_row(self, uow: UnitOfWork, task_id: str) -> Any:
        rows = uow.query("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if not rows:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the requested task is not available"
            )
        return rows[0]

    def _task_from_row(self, row: Any) -> TaskRun:
        domain_result = None
        if row["domain_result"]:
            raw = json.loads(row["domain_result"])
            raw["domain"] = TaskType(raw["domain"])
            domain_result = DomainResult.model_validate(raw)
        stop_reason = None
        if row["stop_reason"]:
            stop_reason = TaskStopReason.model_validate_json(row["stop_reason"])
        return TaskRun(
            id=row["task_id"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=row["created_at"],
            created_by_task_id=None,
            type=TaskType(row["type"]),
            user_scope_id=row["user_scope_id"],
            parent_id=row["parent_id"],
            revision=row["revision"],
            state=TaskState(row["state"]),
            current_input_revision=row["current_input_revision"],
            information_request_ids=[],
            budget=Budget.model_validate_json(row["budget"]),
            calls=CallCounters.model_validate_json(row["calls"]),
            checkpoint_id=row["checkpoint_id"],
            domain_result=domain_result,
            stop_reason=stop_reason,
            result_refs=[ObjectRef.model_validate(item) for item in json.loads(row["result_refs"] or "[]")],
            cancel_requested=bool(row["cancel_requested"]),
            last_error_code=row["last_error_code"],
            active_elapsed_seconds=float(row["active_elapsed_seconds"]),
        )

    def _load_input(self, task_id: str) -> Any:
        with self.repository.unit_of_work() as uow:
            row = self._task_row(uow, task_id)
            input_row = uow.query(
                "SELECT input FROM task_inputs WHERE task_id=? AND input_revision=?",
                (task_id, row["current_input_revision"]),
            )
        return TaskInputAdapter.validate_python(json.loads(input_row[0]["input"]))

    def _load_budget(self, task_id: str) -> Budget:
        with self.repository.unit_of_work() as uow:
            return Budget.model_validate_json(self._task_row(uow, task_id)["budget"])

    def _next_sequence(self, uow: UnitOfWork, task_id: str) -> int:
        row = uow.query(
            "SELECT COALESCE(MAX(sequence), 0) AS last FROM task_events WHERE task_id=?",
            (task_id,),
        )[0]
        return int(row["last"]) + 1

    def _append_event(
        self,
        uow: UnitOfWork,
        task_id: str,
        sequence: int,
        input_revision: int,
        from_state: TaskState | None,
        to_state: TaskState,
        cause: str,
        *,
        related: Sequence[ObjectRef] = (),
        active_elapsed: float = 0.0,
    ) -> None:
        uow.execute(
            "INSERT INTO task_events(task_id, sequence, input_revision, from_state, to_state,"
            " cause, related_refs, active_elapsed_seconds, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                sequence,
                input_revision,
                None if from_state is None else from_state.value,
                to_state.value,
                cause,
                canonical_json([ref.model_dump(mode="json") for ref in related]).decode(),
                active_elapsed,
                self.clock(),
            ),
        )

    def _chain_has_blocking_calls(self, uow: UnitOfWork, task_id: str) -> bool:
        seen: set[str] = set()
        current: str | None = task_id
        while current and current not in seen:
            seen.add(current)
            states = uow.query(
                "SELECT call_state FROM external_calls WHERE task_id=?", (current,)
            )
            if any(row["call_state"] in _BLOCKING_CALL_STATES for row in states):
                return True
            parent = uow.query("SELECT parent_id FROM tasks WHERE task_id=?", (current,))
            current = parent[0]["parent_id"] if parent else None
        return False

    def _replay_if_registered(
        self,
        uow: UnitOfWork,
        context: MutationContext,
        method: str,
        route: str,
        request_schema_version: str,
        request_hash: str,
    ) -> TaskAccepted | None:
        existing = uow.find_idempotency(
            context.user_scope_id, method, route, context.idempotency_key
        )
        if existing is None:
            return None
        if existing.lifecycle.value == "tombstoned":
            raise ContractError(
                ErrorCode.RESOURCE_GONE,
                "this submission was cleaned up; start a new logical submission",
            )
        if existing.request_schema_version != request_schema_version:
            raise ContractError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "this key is registered under another contract version",
                field_errors=[FieldError(
                    path="X-Product-Contract-Version", code="conflict",
                    message=f"registered as {existing.request_schema_version}",
                )],
            )
        if existing.request_hash != request_hash:
            raise ContractError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "the same key was already used with different business content",
                field_errors=[FieldError(
                    path="idempotency_key", code="conflict",
                    message="different request hash",
                )],
            )
        body = json.loads(existing.response_body)
        body["state"] = TaskState(body["state"])
        return TaskAccepted.model_validate(body)


def _validate_task_input(task_input: Any) -> Any:
    try:
        return TaskInputAdapter.validate_python(task_input)
    except ValidationError as error:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            "the task input is invalid",
            field_errors=[
                FieldError(
                    path=".".join(str(part) for part in item["loc"]),
                    code="type",
                    message=item["msg"],
                )
                for item in error.errors()
            ],
        ) from error


def _request_hash(
    schema_version: str,
    method: str,
    route: str,
    typed: Any,
    budget: Budget,
) -> str:
    return hashlib.sha256(canonical_json({
        "schema_version": schema_version,
        "method": method,
        "route": route,
        "input": typed.model_dump(mode="json"),
        "budget": budget.model_dump(mode="json"),
    })).hexdigest()


def _phase_for(state: TaskState) -> str:
    if state is TaskState.queued:
        return "queued"
    if state is TaskState.waiting_input:
        return "waiting"
    if state in TERMINAL_TASK_STATES:
        return "terminal"
    return "acquire"


__all__ = [
    "BudgetExhausted",
    "HandlerOutcome",
    "RunOutcome",
    "TaskContext",
    "TaskProgress",
    "TaskService",
    "TaskViewResult",
    "UnknownCall",
]
