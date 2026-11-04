"""Task input union and TaskRun projection shared by the executor.

The union mirrors ``shared/data-contracts.md`` §8.1/§9: each kind is a concrete
model, never arbitrary JSON.  Only implemented kinds appear; later modules add
their own members to the union.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, TypeAdapter, field_validator, model_validator

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


class CaptureInput(StrictModel):
    """One owned capture into a fixed segment/binding revision (04 §5).

    Only an adapter registered as verified may run; the segment and binding are
    fixed at submission and the lease is registered in the same transaction.
    """

    kind: Literal["capture"]
    session_id: ID
    segment_ref: BindingRef
    adapter_id: str
    adapter_version: str
    protocol_id: ID | None

    @field_validator("adapter_id", "adapter_version")
    @classmethod
    def names_are_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an adapter id and version must not be blank")
        return value


class DeviceScanInput(StrictModel):
    kind: Literal["device_scan"]
    game_context_id: ID | None
    field_paths: list[str]


class LogImportInput(StrictModel):
    """A managed capture log to attach to one fixed segment/binding revision.

    The client names a managed file and the exact revisions; it never supplies
    a local path, a column mapping or a time anchor.  Historical log anchors
    belong to the synchronous association entry (04 §4.3), not to this input.
    """

    kind: Literal["log_import"]
    file_id: ID
    session_id: ID
    segment_ref: BindingRef
    source_schema: str

    @field_validator("source_schema")
    @classmethod
    def schema_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_schema must not be blank")
        return value


class RecommendationInput(StrictModel):
    """The exact input snapshot of one recommendation (data §9).

    The input names fixed, immutable ids; it never carries a target FPS, a
    device field or a scope argument.  ``preference_ids`` is tolerated only as
    an empty list until 05 exists, and every id list is duplicate-free.
    """

    kind: Literal["recommendation"]
    game_context_id: ID
    device_snapshot_id: ID
    package_id: ID
    preference_ids: list[ID] = Field(default_factory=list)
    applied_id: ID | None = None
    history_summary_ids: list[ID] = Field(default_factory=list)

    @field_validator("preference_ids", "history_summary_ids")
    @classmethod
    def ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("each id appears once")
        return value


TaskInputPayload = Annotated[
    Union[
        KnowledgeInput,
        KnowledgeImportInput,
        AnalysisInput,
        CaptureInput,
        DeviceScanInput,
        LogImportInput,
        RecommendationInput,
    ],
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
    "CaptureInput",
    "DeviceScanInput",
    "LogImportInput",
    "RecommendationInput",
    "TaskInputPayload",
    "TaskInputAdapter",
    "DomainResult",
    "TaskStopReason",
    "TaskRun",
    "ResumeTaskInput",
]
