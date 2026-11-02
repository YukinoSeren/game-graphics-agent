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


__all__ = ["RequestedField", "InformationRequest"]
