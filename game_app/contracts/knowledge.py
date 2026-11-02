"""Public knowledge contracts owned by 02.

Transcribed from ``shared/data-contracts.md`` §3.1 and §4.2.  KNW-T02 adds the
coverage evaluator and package builder; the shapes here are the shared ones.
"""

from __future__ import annotations

from typing import Any, Literal

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
    LegacyRecordState,
    PublicationStatus,
)
from game_app.contracts.primitives import (
    Hash,
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
DIAGNOSIS_SCOPE_PREFIX = "diagnosis."


def is_registered_scope(scope: str) -> bool:
    """Fixed scopes plus the registered ``diagnosis.<symptom>`` family."""
    if scope in OPERATION_SCOPES:
        return True
    return (
        scope.startswith(DIAGNOSIS_SCOPE_PREFIX)
        and len(scope) > len(DIAGNOSIS_SCOPE_PREFIX)
        and scope[len(DIAGNOSIS_SCOPE_PREFIX):].strip() == scope[len(DIAGNOSIS_SCOPE_PREFIX):]
        and bool(scope[len(DIAGNOSIS_SCOPE_PREFIX):])
    )


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


# -- IMP-T02: capture-scoped import records ----------------------------------


class SourceMetadata(StrictModel):
    """The collector's own metadata block, preserved field for field."""

    author: str | None
    published_date: str | None
    updated_date: str | None
    test_date: str | None
    original_source_url: str | None


class Source(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    original_url: str
    canonical_url: str
    identity_checks: list[CheckResult]

    @field_validator("original_url", "canonical_url")
    @classmethod
    def url_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a source url must not be blank")
        return value


class SourceRevision(StrictModel):
    """One captured revision.  A failed capture may have neither document nor hash."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    source_id: ID
    retrieved_at: Timestamp
    upstream_revision_id: LegacyID | None
    content_hash: Hash | None
    document_file_id: ID | None
    capture_method: str
    capture_scope: str
    metadata: SourceMetadata
    truncated: bool
    checks: list[CheckResult]

    @field_validator("capture_method", "capture_scope")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("capture method and scope must not be blank")
        return value


class BoundarySemantics(StrictModel):
    """Strictness of a legacy numeric bound; null means the artifact is silent."""

    lower_inclusive: bool | None
    upper_inclusive: bool | None
    evidence_refs: list[EvidenceRef]
    status: CheckStatus


class RecordExtension(StrictModel):
    boundary_semantics: BoundarySemantics | None
    setting_identity_ref: ID | None
    checks: list[CheckResult]


class ImportedRecord(StrictModel):
    """One read-only record in a capture scope; it is never a package fact."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    record_snapshot_id: ID
    import_snapshot_id: ID
    capture_scope_id: LegacyID
    local_record_id: LegacyID
    legacy_record_id: LegacyID
    source_revision_id: ID
    record_type: str
    payload: Any
    payload_hash: Hash
    upstream_status: LegacyRecordState
    upstream_envelope: dict[str, Any]
    checks: list[CheckResult]
    extension: RecordExtension

    @model_validator(mode="after")
    def one_identity(self) -> "ImportedRecord":
        if self.record_snapshot_id != self.id:
            raise ValueError("record_snapshot_id is the entity id; it is never a second identity")
        return self


class ImportedExtraction(StrictModel):
    """A legacy extraction read-only view.

    It is deliberately not an ``ExternalCallRecord``: the old output proves
    which model was *requested*, never which one answered, so ``returned_model``
    can only be null.
    """

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    import_snapshot_id: ID
    source_revision_id: ID | None
    legacy_source_id: LegacyID
    status: str | None
    model_requested: str | None
    returned_model: Literal[None]
    upstream_envelope: dict[str, Any]
    checks: list[CheckResult]
    limitations: list[str]


class ImportValidation(StrictModel):
    """Local staging result (02a §5); all fields R."""

    accepted_record_ids: list[ID]
    diagnostic_record_ids: list[ID]
    checks: list[CheckResult]


class SnapshotFileEntry(StrictModel):
    """Mirror of the shared ``FileEntry`` shape, so contracts stay independent."""

    relative_name: str
    byte_length: NonNegativeInt
    sha256: Hash
    media_type: str


class SnapshotManifest(StrictModel):
    entries: list[SnapshotFileEntry]

    @field_validator("entries")
    @classmethod
    def non_empty(cls, value: list[SnapshotFileEntry]) -> list[SnapshotFileEntry]:
        if not value:
            raise ValueError("a published import manifest is non-empty")
        return value


class ImportSnapshot(StrictModel):
    """One published import of one frozen byte group (02a §4/§6)."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    origin: Literal["collector_run", "research_json"]
    legacy_run_id: LegacyID | None
    manifest: SnapshotManifest
    import_fingerprint: Hash
    import_status: PublicationStatus
    mapping_version: str
    mapping_context_hash: Hash
    parent_import_snapshot_id: ID | None
    identity_mapping_ids: list[ID]
    limitations: list[str]

    @field_validator("identity_mapping_ids")
    @classmethod
    def mapping_ids_are_unique(cls, value: list[ID]) -> list[ID]:
        if len(set(value)) != len(value):
            raise ValueError("an import names each identity mapping once")
        return value

    @field_validator("mapping_version")
    @classmethod
    def mapping_version_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("mapping_version must not be blank")
        return value


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
        if not is_registered_scope(self.declared_scope):
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
    "DIAGNOSIS_SCOPE_PREFIX",
    "is_registered_scope",
    "GameAlias",
    "Game",
    "IdentityMapping",
    "SourceMetadata",
    "Source",
    "SourceRevision",
    "BoundarySemantics",
    "RecordExtension",
    "ImportedRecord",
    "ImportedExtraction",
    "ImportValidation",
    "ImportSnapshot",
    "SnapshotFileEntry",
    "SnapshotManifest",
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
