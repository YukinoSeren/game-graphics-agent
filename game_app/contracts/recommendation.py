"""Recommendation contracts (REC-T01, 03 §4—§7, data §5.1).

The models here are the only durable shape of a recommendation input: the
legacy setting-value shape and the deterministic candidate assessment.
Candidate assessments are a module-local, read-only result and never a stored
fact.  Everything is strict; unknown stays unknown and nothing here invents a
default frame rate, a refresh rate or an applicability claim.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from game_app.contracts.common import CheckResult, EvidenceRef, KnowledgeRecordRef
from game_app.contracts.primitives import Scalar, StrictModel

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


__all__ = [
    "MatchClass",
    "BranchV01",
    "SettingValueV01",
    "AssignmentV01",
    "CandidateAssessment",
]
