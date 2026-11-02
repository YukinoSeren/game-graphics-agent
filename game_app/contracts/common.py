"""Shared value wrappers and call-boundary DTOs.

FieldValue/provenance/evidence shapes come from ``shared/data-contracts.md`` §2
and §9; they are the only definitions other modules may consume.
"""

from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import Field, StringConstraints, field_validator, model_validator

from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    TaskState,
    ValueState,
)
from game_app.contracts.primitives import (
    Hash,
    ID,
    NonNegativeInt,
    ObjectRef,
    Revision,
    Scalar,
    StrictModel,
    TimeRange,
    Timestamp,
)

LegacyUnknownReason = Literal[
    "not_provided",
    "scope_ambiguous",
    "parse_failed",
    "conflicting",
    "unresolved_choice",
]
LegacyScope = Literal["page", "section", "table", "row", "cell", "run"]
LocalEvidenceType = Literal[
    "InformationResponse",
    "PlayerDeclaration",
    "ConfigurationObservation",
    "ConfigurationBinding",
    "PerformanceSummary",
    "Feedback",
    "AppliedConfiguration",
]

T = TypeVar("T")


class KnowledgeRecordRef(StrictModel):
    package_id: ID
    record_snapshot_id: ID
    source_revision_id: ID
    capture_scope_id: Annotated[str, StringConstraints(strict=True, min_length=1)]
    local_record_id: Annotated[str, StringConstraints(strict=True, min_length=1)]
    record_type: str
    payload_hash: Hash


class EvidenceRef(StrictModel):
    """Union-shaped evidence reference keyed by ``kind``.

    Only the fields for the chosen kind may be populated; the validator below
    makes that rule explicit instead of relying on the reader.
    """

    kind: Literal["knowledge", "source_location", "local", "artifact"]
    record: KnowledgeRecordRef | None = None
    source_revision_id: ID | None = None
    block_id: str | None = None
    scope: LegacyScope | None = None
    row: int | None = Field(default=None, ge=0)
    column: int | None = Field(default=None, ge=0)
    quote: str | None = None
    quote_hash: Hash | None = None
    entity_type: LocalEvidenceType | None = None
    entity_id: ID | None = None
    field_path: str | None = None
    file_id: ID | None = None
    content_hash: Hash | None = None
    locator: str | None = None
    time_range: TimeRange | None = None

    @model_validator(mode="after")
    def shape_matches_kind(self) -> "EvidenceRef":
        if self.kind == "knowledge" and self.record is None:
            raise ValueError("knowledge evidence requires record")
        if self.kind == "source_location":
            missing = [
                name for name in ("source_revision_id", "block_id", "scope", "quote", "quote_hash")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"source_location evidence missing {missing}")
        if self.kind == "local":
            if self.entity_type is None or self.entity_id is None:
                raise ValueError("local evidence requires entity_type and entity_id")
        if self.kind == "artifact":
            if self.file_id is None or self.content_hash is None or self.locator is None:
                raise ValueError("artifact evidence requires file_id, content_hash and locator")
        return self


class Provenance(StrictModel):
    method: AcquisitionMethod
    observed_at: Timestamp | None = None
    recorded_at: Timestamp
    evidence_refs: list[EvidenceRef]
    note: str | None = None

    @field_validator("evidence_refs")
    @classmethod
    def non_empty(cls, value: list[EvidenceRef]) -> list[EvidenceRef]:
        if not value:
            raise ValueError("provenance requires at least one evidence reference")
        return value


class CheckResult(StrictModel):
    check_id: str
    definition_version: str
    status: CheckStatus
    input_refs: list[ObjectRef] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    checked_at: Timestamp | None
    method: str
    note: str | None = None

    @model_validator(mode="after")
    def checked_at_matches_status(self) -> "CheckResult":
        if self.status in (CheckStatus.pass_, CheckStatus.fail):
            if self.checked_at is None:
                raise ValueError("a pass/fail check records when it ran")
        if self.status is CheckStatus.not_run and self.checked_at is not None:
            raise ValueError("a not_run check has no checked_at")
        return self


class AcquisitionAttempt(StrictModel):
    id: ID
    method: AcquisitionMethod
    status: AcquisitionStatus
    attempted_at: Timestamp
    error_code: str | None = None
    information_response_id: ID | None = None

    @model_validator(mode="after")
    def status_is_an_attempt(self) -> "AcquisitionAttempt":
        if self.status is AcquisitionStatus.not_attempted:
            raise ValueError("an acquisition attempt cannot be not_attempted")
        if self.status is AcquisitionStatus.failed and not self.error_code:
            raise ValueError("failed acquisition attempts require an error_code")
        return self


class FieldResolution(StrictModel):
    selected_provenance_indexes: list[int]
    reason: str
    prior_object_ref: ObjectRef

    @field_validator("selected_provenance_indexes")
    @classmethod
    def non_empty(cls, value: list[int]) -> list[int]:
        if not value or any(index < 0 for index in value):
            raise ValueError("a resolution must select at least one non-negative provenance index")
        return value


class FieldCandidate(StrictModel, Generic[T]):
    value: T
    provenance: list[Provenance]

    @field_validator("provenance")
    @classmethod
    def non_empty(cls, value: list[Provenance]) -> list[Provenance]:
        if not value:
            raise ValueError("a field candidate requires provenance")
        return value


class FieldValue(StrictModel, Generic[T]):
    value_state: ValueState
    value: T | None
    unknown_reason: LegacyUnknownReason | None = None
    candidates: list[FieldCandidate[T]] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    acquisition_status: AcquisitionStatus
    attempts: list[AcquisitionAttempt] = Field(default_factory=list)
    applicability_note: str | None = None
    resolution: FieldResolution | None = None

    @model_validator(mode="after")
    def consistent_shape(self) -> "FieldValue[T]":
        if self.value_state is ValueState.known:
            if self.value is None:
                raise ValueError("known values cannot be null")
            if self.unknown_reason is not None:
                raise ValueError("known values cannot carry an unknown_reason")
            if not self.provenance:
                raise ValueError("known values require at least one provenance record")
        elif self.value_state is ValueState.unknown:
            if self.value is not None:
                raise ValueError("unknown values must be null")
            if self.unknown_reason is None:
                raise ValueError("unknown values require a legacy unknown_reason")
        elif self.value_state is ValueState.conflicting:
            if self.value is not None:
                raise ValueError("conflicting values must be null until resolved")
            if len(self.candidates) < 2:
                raise ValueError("conflicting values need at least two candidates")
        elif self.value_state is ValueState.not_applicable:
            if self.value is not None:
                raise ValueError("not_applicable values must be null")
            if not self.applicability_note:
                raise ValueError("not_applicable requires an applicability_note")
        if self.acquisition_status is AcquisitionStatus.not_attempted and self.attempts:
            raise ValueError("not_attempted acquisitions cannot have attempts")
        if self.value_state is ValueState.unknown and self.acquisition_status is AcquisitionStatus.succeeded:
            raise ValueError("a succeeded acquisition cannot leave the value unknown")
        return self


class Budget(StrictModel):
    max_urls: NonNegativeInt = 12
    max_searches: NonNegativeInt = 12
    max_followup_rounds: NonNegativeInt = 2
    max_queries_per_gap: NonNegativeInt = 2
    max_model_calls: NonNegativeInt = 16
    max_read_calls: NonNegativeInt = 36
    max_seconds: float | None = None

    @field_validator("max_seconds")
    @classmethod
    def non_negative(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("max_seconds must be non-negative")
        return value


class CallCounters(StrictModel):
    search: NonNegativeInt = 0
    read: NonNegativeInt = 0
    model: NonNegativeInt = 0


class MutationContext(StrictModel):
    user_scope_id: ID
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1)]
    request_id: ID


class ResourceResult(StrictModel):
    refs: list[ObjectRef]

    @field_validator("refs")
    @classmethod
    def non_empty(cls, value: list[ObjectRef]) -> list[ObjectRef]:
        if not value:
            raise ValueError("a resource result requires at least one reference")
        return value


class TaskAccepted(StrictModel):
    task_id: ID
    input_revision: Revision
    state: TaskState = TaskState.queued


__all__ = [
    "LegacyUnknownReason",
    "LegacyScope",
    "LocalEvidenceType",
    "KnowledgeRecordRef",
    "EvidenceRef",
    "Provenance",
    "CheckResult",
    "AcquisitionAttempt",
    "FieldResolution",
    "FieldCandidate",
    "FieldValue",
    "Budget",
    "CallCounters",
    "MutationContext",
    "ResourceResult",
    "TaskAccepted",
]
