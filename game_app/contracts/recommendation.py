"""Recommendation contracts (REC-T01..T02, 03 §4—§7, data §5.1).

The models here are the only durable shape of a recommendation: a version, its
actions and alternatives, an optional target proposal and the review events.
Candidate assessments are a module-local, read-only result and never a stored
fact.  Everything is strict; unknown stays unknown and nothing here invents a
default frame rate, a refresh rate or an applicability claim.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from game_app.contracts.common import (
    CheckResult,
    EvidenceRef,
    FieldValue,
    KnowledgeRecordRef,
)
from game_app.contracts.configuration import SettingRef
from game_app.contracts.enums import (
    FrameStreamKind,
    RecommendationActionKind,
    RecommendationReadiness,
    ReviewState,
)
from game_app.contracts.performance import NumericValueV01
from game_app.contracts.primitives import (
    ID,
    Revision,
    Scalar,
    StrictModel,
    Timestamp,
)

MatchClass = Literal["direct", "conditional", "neighbor", "general"]

# A legacy reason string is free text; the contract only requires non-blank.
Reason = Annotated[str, StringConstraints(strict=True, min_length=1)]


class BranchV01(StrictModel):
    """One conditional branch of a setting value (legacy ``Branch`` shape)."""

    condition: Annotated[str, StringConstraints(strict=True, min_length=1)]
    value: Scalar


class SettingValueV01(StrictModel):
    """The legacy ``SettingValue`` shape as a product contract.

    The collector module is deliberately not imported: the product layer parses
    the payload into this shape and never executes it.
    """

    value_kind: Literal["fixed", "alternatives", "conditional", "unknown"]
    value: Scalar | None = None
    alternatives: list[Scalar] = Field(default_factory=list)
    branches: list[BranchV01] = Field(default_factory=list)
    unknown_reason: Reason | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def shape_matches_kind(self) -> "SettingValueV01":
        shapes = {
            "fixed": (
                self.value is not None and not self.alternatives
                and not self.branches and self.unknown_reason is None
            ),
            "alternatives": (
                self.value is None and bool(self.alternatives)
                and not self.branches and self.unknown_reason is None
            ),
            "conditional": (
                self.value is None and bool(self.branches)
                and not self.alternatives and self.unknown_reason is None
            ),
            "unknown": (
                self.value is None and self.unknown_reason is not None
                and not self.alternatives and not self.branches
            ),
        }
        if not shapes[self.value_kind]:
            raise ValueError("value_kind and payload disagree")
        return self


class AssignmentV01(StrictModel):
    """One setting assignment inside a package configuration record."""

    setting_id: Annotated[str, StringConstraints(strict=True, min_length=1)]
    value: SettingValueV01
    evidence_ids: list[Annotated[str, StringConstraints(strict=True, min_length=1)]] = (
        Field(min_length=1)
    )


class CandidateAssessment(StrictModel):
    """A deterministic judgement about one package configuration (03 §4).

    ``match_class`` is a purpose-match layer, never a confidence probability.
    """

    configuration_ref: KnowledgeRecordRef
    match_class: MatchClass
    condition_checks: list[CheckResult]
    visual_basis: list[EvidenceRef]
    performance_basis: list[EvidenceRef]
    independence_checks: list[CheckResult]
    selection_reason: str
    limitations: list[str]


class RecommendationAction(StrictModel):
    """One executable (or explicitly non-executable) action (data §5.1)."""

    action_id: ID
    setting_ref: SettingRef
    setting_identity_ref: ID | None = None
    action: RecommendationActionKind
    value: SettingValueV01 | None
    current_value: FieldValue[Scalar]
    menu_path: list[str]
    reason: str
    evidence_refs: list[EvidenceRef]
    dependency_group_id: ID | None
    restart_required: FieldValue[bool]
    checks: list[CheckResult]

    @model_validator(mode="after")
    def value_shape_matches_action(self) -> "RecommendationAction":
        if self.action is RecommendationActionKind.set_value:
            if self.value is None or self.value.value_kind != "fixed":
                raise ValueError("set_value requires a fixed value")
        elif self.action is RecommendationActionKind.conditional_strategy:
            if self.value is None or self.value.value_kind not in (
                "conditional", "alternatives"
            ):
                raise ValueError(
                    "conditional_strategy needs a conditional or alternatives value"
                )
        elif self.action in (
            RecommendationActionKind.keep_current,
            RecommendationActionKind.check_in_game,
            RecommendationActionKind.defer,
        ):
            if self.value is not None and self.value.value_kind not in (
                "unknown", "fixed", "alternatives", "conditional"
            ):
                raise ValueError("non-executable actions carry a known value shape")
        return self


class RecommendationAlternative(StrictModel):
    """A recommended alternative, not a ChangeSet and not an adoption record."""

    id: ID
    label: str
    base_recommendation_id: ID
    action_overrides: list[RecommendationAction]
    checks: list[CheckResult]


class TargetProposal(StrictModel):
    """An immutable target proposal; a later acceptance never rewrites it."""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, frozen=True)

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    value: NumericValueV01
    basis_refs: list[EvidenceRef]
    frame_stream_kind: FrameStreamKind
    note: str

    @model_validator(mode="after")
    def has_a_basis(self) -> "TargetProposal":
        if not self.basis_refs:
            raise ValueError("a target proposal states its basis")
        return self


class RecommendationVersion(StrictModel):
    """One immutable recommendation version (data §5.1)."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    parent_id: ID | None
    input: "RecommendationInput"
    base_configuration_ref: KnowledgeRecordRef | None
    actions: list[RecommendationAction]
    alternatives: list[RecommendationAlternative]
    target_proposal: TargetProposal | None
    reference_results: list[KnowledgeRecordRef]
    declared_scope: str
    readiness: RecommendationReadiness
    checks: list[CheckResult]
    candidate_manifest_file_id: ID

    @model_validator(mode="after")
    def identifiers_are_distinct(self) -> "RecommendationVersion":
        action_ids = [item.action_id for item in self.actions]
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("action ids are unique inside one recommendation")
        alternative_ids = [item.id for item in self.alternatives]
        if len(set(alternative_ids)) != len(alternative_ids):
            raise ValueError("alternative ids are unique inside one recommendation")
        return self


class RecommendationReviewEvent(StrictModel):
    """An append-only review transition; the recommendation is never rewritten."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    recommendation_id: ID
    previous_state: ReviewState
    next_state: ReviewState
    affected_refs: list[KnowledgeRecordRef]
    reason: str
    new_package_id: ID | None

    @model_validator(mode="after")
    def transition_is_legal(self) -> "RecommendationReviewEvent":
        legal = {
            (ReviewState.current, ReviewState.needs_review),
            (ReviewState.needs_review, ReviewState.current),
            (ReviewState.current, ReviewState.superseded),
            (ReviewState.needs_review, ReviewState.superseded),
        }
        if (self.previous_state, self.next_state) not in legal:
            raise ValueError("illegal recommendation review transition")
        if not self.reason.strip():
            raise ValueError("a review event states its reason")
        return self


# Imported late: ``contracts.tasks`` imports the enums and common models only,
# and this module must not create an import cycle through the task union.
from game_app.contracts.tasks import RecommendationInput  # noqa: E402

RecommendationVersion.model_rebuild()


__all__ = [
    "MatchClass",
    "BranchV01",
    "SettingValueV01",
    "AssignmentV01",
    "CandidateAssessment",
    "RecommendationAction",
    "RecommendationAlternative",
    "TargetProposal",
    "RecommendationVersion",
    "RecommendationReviewEvent",
]
