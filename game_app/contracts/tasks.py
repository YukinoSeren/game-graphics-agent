"""Task input union and TaskRun projection shared by the executor.

The union mirrors ``shared/data-contracts.md`` §8.1/§9: each kind is a concrete
model, never arbitrary JSON.  Only implemented kinds appear; later modules add
their own members to the union.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, TypeAdapter, model_validator

from game_app.contracts.common import Budget, CallCounters, CheckResult
from game_app.contracts.enums import ExecutionStopCode, TaskState, TaskType
from game_app.contracts.primitives import (
    BindingRef,
    ID,
    ObjectRef,
    Revision,
    StrictModel,
    Timestamp,
)


class KnowledgeInput(StrictModel):
    kind: Literal["knowledge"]
    game_context_id: ID
    device_snapshot_id: ID | None = None
    prior_package_id: ID | None = None
    budget: Budget


class KnowledgeImportInput(StrictModel):
    """A collector file group already stored as managed files (02a §5).

    The client names frozen file ids; it never supplies a local directory, and
    an empty group is rejected rather than interpreted as "import nothing".
    """

    kind: Literal["knowledge_import"]
    file_ids: list[ID] = Field(min_length=1)
    game_context_id: ID
    mapping_version: str
    identity_mapping_ids: list[ID]


class AnalysisInput(StrictModel):
    kind: Literal["analysis"]
    artifact_id: ID
    bindings: list[BindingRef]
    analyzer_version: str
    cleaning_protocol_version: str


class DeviceScanInput(StrictModel):
    kind: Literal["device_scan"]
    game_context_id: ID | None
    field_paths: list[str]


TaskInputPayload = Annotated[
    Union[KnowledgeInput, KnowledgeImportInput, AnalysisInput, DeviceScanInput],
    Field(discriminator="kind"),
]
TaskInputAdapter = TypeAdapter(TaskInputPayload)


class DomainResult(StrictModel):
    domain: TaskType
    code: str
    input_revision: Revision
    checks: list[CheckResult]


class TaskStopReason(StrictModel):
    kind: Literal["collection", "execution"]
    code: str

    @model_validator(mode="after")
    def code_matches_kind(self) -> "TaskStopReason":
        if self.kind == "execution":
            allowed = {item.value for item in ExecutionStopCode}
        else:
            # Collection stop reasons are owned by the knowledge module; keep
            # the historical enum values here rather than inventing new ones.
            allowed = {
                "coverage_reached", "low_information_gain", "budget_exhausted",
                "access_blocked", "source_unavailable", "unresolved_conflict",
            }
        if self.code not in allowed:
            raise ValueError(f"{self.code} is not a {self.kind} stop reason")
        return self


class TaskRun(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    type: TaskType
    user_scope_id: ID
    parent_id: ID | None
    revision: Revision
    state: TaskState
    current_input_revision: Revision
    information_request_ids: list[ID]
    budget: Budget
    calls: CallCounters
    checkpoint_id: ID | None
    domain_result: DomainResult | None
    stop_reason: TaskStopReason | None
    result_refs: list[ObjectRef]
    cancel_requested: bool
    last_error_code: str | None
    active_elapsed_seconds: float


class ResumeTaskInput(StrictModel):
    parent_task_id: ID
    checkpoint_id: ID | None
    budget: Budget


__all__ = [
    "KnowledgeInput",
    "KnowledgeImportInput",
    "AnalysisInput",
    "DeviceScanInput",
    "TaskInputPayload",
    "TaskInputAdapter",
    "DomainResult",
    "TaskStopReason",
    "TaskRun",
    "ResumeTaskInput",
]
