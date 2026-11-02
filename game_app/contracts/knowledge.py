"""Public knowledge contracts owned by 02.

Transcribed from ``shared/data-contracts.md`` §3.1 and §4.2.  KNW-T02 adds the
coverage evaluator and package builder; the shapes here are the shared ones.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from game_app.contracts.common import CheckResult, EvidenceRef, KnowledgeRecordRef
from game_app.contracts.enums import (
    CheckStatus,
    CollectionResult,
    CollectionStopReason,
    CoverageStatus,
    GameIdentityStatus,
    GapPriority,
    GapStatus,
    PublicationStatus,
)
from game_app.contracts.primitives import (
    ID,
    LegacyID,
    NonNegativeInt,
    ObjectRef,
    Revision,
    Scalar,
    StrictModel,
    Timestamp,
)

# Operation scopes are a stable vocabulary owned by 02; models never invent one.
OPERATION_SCOPES = frozenset({
    "collection.public_graphics",
    "recommendation.configuration",
    "recommendation.target",
    "comparison.settings_effect",
})


class GameAlias(StrictModel):
    name: str
    platform: str | None
    evidence_refs: list[EvidenceRef]
    status: CheckStatus

    @field_validator("name")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("alias names must not be blank")
        return value


class Game(StrictModel):
    """Versioned public game identity; the series id is the stable game id."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    canonical_name: str
    aliases: list[GameAlias]
    platform_scope: list[str]
    identity_status: GameIdentityStatus
    identity_checks: list[CheckResult]
    declaring_user_scope_id: ID | None

    @model_validator(mode="after")
    def identity_shape(self) -> "Game":
        if not self.canonical_name.strip():
            raise ValueError("canonical_name must not be blank")
        if self.identity_status is GameIdentityStatus.verified:
            if not self.platform_scope:
                raise ValueError("a verified game declares at least one platform")
            if not self.identity_checks:
                raise ValueError("a verified game records its identity checks")
            if self.declaring_user_scope_id is not None:
                raise ValueError("a verified game is not scoped to one declaring user")
        elif self.identity_status is GameIdentityStatus.provisional:
            if self.declaring_user_scope_id is None:
                raise ValueError("a provisional game belongs to its declaring user")
        elif self.declaring_user_scope_id is not None:
            raise ValueError("legacy read views are not owned by a declaring user")
        return self


class IdentityMapping(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    legacy_namespace: str
    legacy_value: LegacyID
    target_game_id: ID | None
    checks: list[CheckResult]


class CoverageCell(StrictModel):
    key: str
    dimensions: dict[str, Scalar]
    operation_scope: str
    status: CoverageStatus
    evidence_refs: list[EvidenceRef]
    note: str

    @model_validator(mode="after")
    def covered_needs_basis(self) -> "CoverageCell":
        if self.status in (CoverageStatus.covered, CoverageStatus.not_applicable):
            if not self.evidence_refs:
                raise ValueError("covered/not_applicable coverage requires evidence")
        return self


class KnowledgeGap(StrictModel):
    id: ID
    legacy_gap_id: LegacyID | None
    operation_scope: str
    priority: GapPriority
    question: str
    record_refs: list[KnowledgeRecordRef]
    diagnostic_record_ids: list[ID]
    status: GapStatus
    stop_reason: CollectionStopReason | None
    evidence_refs: list[EvidenceRef]
    checks: list[CheckResult]


class KnowledgePackage(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    game_id: ID
    game_revision_id: ID
    platform_scope: list[str]
    parent_package_id: ID | None
    import_snapshot_ids: list[ID]
    source_revision_ids: list[ID]
    record_snapshot_ids: list[ID]
    setting_catalog: list[KnowledgeRecordRef]
    configuration_templates: list[KnowledgeRecordRef]
    measurement_evidence: list[KnowledgeRecordRef]
    constraints: list[KnowledgeRecordRef]
    diagnostic_record_ids: list[ID]
    coverage_matrix: list[CoverageCell]
    critical_gaps: list[KnowledgeGap]
    noncritical_gaps: list[KnowledgeGap]
    checks: list[CheckResult]
    relation_ids: list[ID]
    collection_result: CollectionResult | None
    stop_reason: CollectionStopReason | None
    collection_execution_ref: ObjectRef | None
    publication_status: PublicationStatus
    declared_scope: str
    checked_at: Timestamp | None

    @model_validator(mode="after")
    def platform_and_scope(self) -> "KnowledgePackage":
        if not self.platform_scope:
            raise ValueError("a knowledge package declares a non-empty platform scope")
        if self.declared_scope not in OPERATION_SCOPES:
            raise ValueError(f"unregistered operation scope: {self.declared_scope}")
        return self


class SettingIdentityMapping(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    game_id: ID
    platform_scope: list[str]
    source_setting_refs: list[KnowledgeRecordRef]
    display_name: str
    checks: list[CheckResult]


# -- D-P01 catalog and selection ---------------------------------------------


class GameCatalogQuery(StrictModel):
    query: str
    cursor: str | None
    limit: NonNegativeInt = 20

    @field_validator("limit")
    @classmethod
    def bounded(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("limit must be between 1 and 100")
        return value


class GameChoice(StrictModel):
    game_id: ID
    game_revision_id: ID
    canonical_name: str
    platform_scope: list[str]
    identity_status: GameIdentityStatus


class GameChoicePage(StrictModel):
    choices: list[GameChoice]
    next_cursor: str | None


class GameSelectionRequest(StrictModel):
    raw_name: str
    selected_game_revision_id: ID | None
    platform: str | None
    installation_id: ID | None
    expected_installation_revision: Revision | None

    @field_validator("raw_name")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("raw_name must not be blank")
        return value

    @model_validator(mode="after")
    def installation_fields_pair(self) -> "GameSelectionRequest":
        if (self.installation_id is None) != (self.expected_installation_revision is None):
            raise ValueError(
                "installation_id and expected_installation_revision are both null or both set"
            )
        return self


class GameSelection(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    input: GameSelectionRequest
    state: Literal["selected", "needs_choice"]
    game_revision_id: ID | None
    game_context_id: ID | None
    candidate_revision_ids: list[ID]
    declaration_file_id: ID

    @model_validator(mode="after")
    def state_shape(self) -> "GameSelection":
        if self.state == "selected":
            if self.game_revision_id is None or self.game_context_id is None:
                raise ValueError("a selected result carries both ids")
        else:
            if self.game_revision_id is not None or self.game_context_id is not None:
                raise ValueError("needs_choice carries no game or context id")
            if len(self.candidate_revision_ids) < 2:
                raise ValueError("needs_choice lists at least two candidates")
        return self


class ReuseDecision(StrictModel):
    package_id: ID | None
    mode: Literal["reuse", "followup", "discover"]
    checks: list[CheckResult]
    gap_keys: list[str]
    reason: str


class IdentityResolution(StrictModel):
    """Result of ``resolve_game_identity``; no version is written without evidence."""

    game_ref: ObjectRef | None
    checks: list[CheckResult]
    reason: str


__all__ = [
    "OPERATION_SCOPES",
    "GameAlias",
    "Game",
    "IdentityMapping",
    "CoverageCell",
    "KnowledgeGap",
    "KnowledgePackage",
    "SettingIdentityMapping",
    "GameCatalogQuery",
    "GameChoice",
    "GameChoicePage",
    "GameSelectionRequest",
    "GameSelection",
    "ReuseDecision",
    "IdentityResolution",
]
