"""Information request contracts (07-owned lifecycle, 01-owned field rules).

Shapes follow ``shared/data-contracts.md`` §8.2.  DEV-T04 implements answering;
this slice only creates the request that a failed probe produces.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from game_app.contracts.common import FieldValue
from game_app.contracts.enums import (
    AnswerState,
    InformationContinuationMode,
    InformationRequestState,
)
from game_app.contracts.primitives import (
    ID,
    InformationRequestRevisionRef,
    ObjectRef,
    Revision,
    Scalar,
    StrictModel,
    Timestamp,
    VersionRef,
)


class RequestedField(StrictModel):
    field_path: str
    purpose: str
    required_for: list[str]
    candidates: list[Scalar]
    answer_state: AnswerState
    value: FieldValue[Scalar]


class InformationRequest(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    source_task_id: ID
    version: Revision
    context_refs: list[VersionRef]
    expected_context_revision: Revision
    fields: list[RequestedField]
    state: InformationRequestState
    result_refs: list[ObjectRef]
    superseded_by_request_id: ID | None
    continuation_mode: InformationContinuationMode
    amends_request_id: ID | None
    amendment_root_ref: InformationRequestRevisionRef | None

    @model_validator(mode="after")
    def continuation_shape(self) -> "InformationRequest":
        if not self.fields:
            raise ValueError("an information request asks for at least one field")
        paths = [field.field_path for field in self.fields]
        if len(paths) != len(set(paths)):
            raise ValueError("an information request asks for each field once")
        if self.continuation_mode is InformationContinuationMode.source_task:
            if self.amendment_root_ref is not None:
                raise ValueError("source_task requests carry no amendment root")
        else:
            if self.amends_request_id is None or self.amendment_root_ref is None:
                raise ValueError("facts_only requests name their source and fixed root")
        return self


__all__ = [
    "RequestedField",
    "InformationRequest",
    "InformationResponseInput",
    "InformationResponse",
    "CreateInformationAmendmentRequest",
    "InformationAmendment",
]


class InformationResponseInput(StrictModel):
    """A submitted answer set; the two expected revisions are both checked."""

    request_id: ID
    expected_request_version: Revision
    expected_context_revision: Revision
    answers: dict[str, Scalar]
    skipped_fields: list[str]
    raw_text: str
    conflict_selections: dict[str, int]

    @model_validator(mode="after")
    def sets_do_not_cross(self) -> "InformationResponseInput":
        overlap = set(self.answers) & set(self.skipped_fields)
        if overlap:
            raise ValueError(
                "a field is either answered or skipped, never both: "
                + ", ".join(sorted(overlap))
            )
        extra = set(self.conflict_selections) - set(self.answers)
        if extra:
            raise ValueError(
                "a conflict selection belongs to an answered field: "
                + ", ".join(sorted(extra))
            )
        if any(index < 0 for index in self.conflict_selections.values()):
            raise ValueError("conflict selection indexes are non-negative")
        return self


class InformationResponse(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    request_id: ID
    expected_request_version: Revision
    expected_context_revision: Revision
    answers: dict[str, Scalar]
    skipped_fields: list[str]
    raw_text: str
    conflict_selections: dict[str, int]
    answered_fields: list[str]
    unresolved_fields: list[str]
    answered_values: dict[str, Scalar]
    submitted_at: Timestamp
    result_refs: list[ObjectRef]
    post_request_version: Revision
    post_context_revision: Revision

    @model_validator(mode="after")
    def answered_shape(self) -> "InformationResponse":
        """The three background fields describe this response, not a projection."""
        if len(set(self.answered_fields)) != len(self.answered_fields):
            raise ValueError("a response answers each field once")
        if len(set(self.unresolved_fields)) != len(self.unresolved_fields):
            raise ValueError("a response leaves each field unresolved once")
        if set(self.answered_fields) & set(self.unresolved_fields):
            raise ValueError("a field is answered or unresolved, never both")
        if set(self.answered_values) != set(self.answered_fields):
            raise ValueError("answered_values has exactly the answered fields")
        if set(self.answered_fields) & set(self.skipped_fields):
            raise ValueError("a skipped field is not an answered field")
        if set(self.unresolved_fields) & set(self.skipped_fields):
            raise ValueError("a skipped field is not an unresolved field")
        return self


class CreateInformationAmendmentRequest(StrictModel):
    source_request_id: ID
    expected_request_version: Revision
    expected_context_revision: Revision
    field_paths: list[str]
    reason: str

    @model_validator(mode="after")
    def fields_are_distinct_and_a_reason_is_given(self) -> "CreateInformationAmendmentRequest":
        if not self.field_paths:
            raise ValueError("an amendment names at least one field")
        if len(set(self.field_paths)) != len(self.field_paths):
            raise ValueError("an amendment names each field once")
        if not self.reason.strip():
            raise ValueError("an amendment states a reason")
        return self


class InformationAmendment(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    input: CreateInformationAmendmentRequest
    new_request_id: ID
    declaration_file_id: ID
