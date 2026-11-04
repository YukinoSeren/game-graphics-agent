"""Feedback, preference and target-acceptance contracts (05 §4—§5, data §7).

The player's own words, the model's structured reading and the player's
explicit declarations are three separate facts.  Nothing here lets a model
declare a preference, lets an interpretation overwrite the original text, or
lets an acceptance rewrite the target proposal it accepted.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from game_app.contracts.common import EvidenceRef, FieldValue
from game_app.contracts.enums import (
    InterpretationStatus,
    PreferenceExplicitness,
    PreferenceLifecycle,
    PreferenceScope,
)
from game_app.contracts.primitives import ID, Revision, Scalar, StrictModel, Timestamp

NonBlank = Annotated[str, StringConstraints(strict=True, min_length=1)]


class Symptom(StrictModel):
    """One structured reading of the player's words.

    ``severity``/``range`` stay unknown unless the reading supplied them, and
    a symptom without evidence is not a symptom: it is a guess.
    """

    kind: NonBlank
    severity: FieldValue[str]
    range: FieldValue[str]
    interpretation: InterpretationStatus
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class Feedback(StrictModel):
    """An immutable feedback fact; a later parse appends, it never edits."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    session_id: ID
    segment_id: ID | None
    raw_text: str
    selections: dict[str, Scalar]
    symptoms: list[Symptom]
    interpretation_status: InterpretationStatus

    @model_validator(mode="after")
    def keeps_something_the_player_said(self) -> "Feedback":
        if not self.raw_text.strip() and not self.selections:
            raise ValueError("a feedback carries text or at least one selection")
        if self.interpretation_status is InterpretationStatus.unparsed and self.symptoms:
            raise ValueError("an unparsed feedback has no structured symptoms")
        return self


class FeedbackInput(StrictModel):
    """The exact input of one feedback registration (05 §5)."""

    kind: Literal["feedback"]
    session_id: ID
    segment_id: ID | None = None
    raw_text: str
    selections: dict[str, Scalar] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keeps_something_the_player_said(self) -> "FeedbackInput":
        if not self.raw_text.strip() and not self.selections:
            raise ValueError("a feedback carries text or at least one selection")
        return self


class FeedbackInterpretationLink(StrictModel):
    """Local immutable relation: one original, one derived interpretation."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    source_feedback_id: ID
    derived_feedback_id: ID
    task_id: ID

    @model_validator(mode="after")
    def is_not_a_self_link(self) -> "FeedbackInterpretationLink":
        if self.source_feedback_id == self.derived_feedback_id:
            raise ValueError("an interpretation is a new feedback, not the original")
        return self


class Preference(StrictModel):
    """One immutable preference version (data §7).

    ``global`` has no scope id; ``game`` and ``experiment`` name the game or
    change set they apply to.  Withdrawal appends a version; old snapshots of
    a recommendation keep reading the old id.
    """

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    parent_id: ID | None
    scope: PreferenceScope
    scope_id: ID | None
    content: NonBlank
    explicitness: PreferenceExplicitness
    lifecycle: PreferenceLifecycle
    feedback_id: ID

    @model_validator(mode="after")
    def scope_matches_its_id(self) -> "Preference":
        if self.scope is PreferenceScope.global_ and self.scope_id is not None:
            raise ValueError("a global preference has no scope id")
        if self.scope is not PreferenceScope.global_ and self.scope_id is None:
            raise ValueError("a game or experiment preference names its scope")
        if (self.version == 1) != (self.parent_id is None):
            raise ValueError("only the first version has no parent")
        return self


class PreferenceDeclarationInput(StrictModel):
    """An explicit declaration, correction or withdrawal (05 §5).

    A new preference leaves both ids null; a correction or withdrawal names
    the series and the version it was made against.
    """

    series_id: ID | None = None
    expected_current_id: ID | None = None
    feedback_id: ID
    scope: PreferenceScope
    scope_id: ID | None = None
    content: NonBlank
    lifecycle: PreferenceLifecycle

    @model_validator(mode="after")
    def new_or_corrected(self) -> "PreferenceDeclarationInput":
        if (self.series_id is None) != (self.expected_current_id is None):
            raise ValueError("a correction names both the series and its current version")
        if self.scope is PreferenceScope.global_ and self.scope_id is not None:
            raise ValueError("a global preference has no scope id")
        if self.scope is not PreferenceScope.global_ and self.scope_id is None:
            raise ValueError("a game or experiment preference names its scope")
        if self.series_id is None and self.lifecycle is PreferenceLifecycle.withdrawn:
            raise ValueError("a new preference cannot start withdrawn")
        return self


class PreferenceDeclaration(StrictModel):
    """The local, immutable record of one explicit player action."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    input: PreferenceDeclarationInput
    submitted_at: Timestamp
    source_feedback_id: ID

    @model_validator(mode="after")
    def quotes_the_input_feedback(self) -> "PreferenceDeclaration":
        if self.source_feedback_id != self.input.feedback_id:
            raise ValueError("the declaration's source feedback is its input's feedback")
        return self


class TargetAcceptanceInput(StrictModel):
    """The player's acceptance or rejection of a target proposal (05 §5)."""

    proposal_id: ID
    session_id: ID
    accepted: bool
    feedback_id: ID
    supersedes_id: ID | None = None


class TargetAcceptance(StrictModel):
    """An immutable acceptance fact; the current projection points at one."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    proposal_id: ID
    session_id: ID
    accepted: bool
    feedback_id: ID
    supersedes_id: ID | None


__all__ = [
    "Symptom",
    "Feedback",
    "FeedbackInput",
    "FeedbackInterpretationLink",
    "Preference",
    "PreferenceDeclarationInput",
    "PreferenceDeclaration",
    "TargetAcceptanceInput",
    "TargetAcceptance",
]
