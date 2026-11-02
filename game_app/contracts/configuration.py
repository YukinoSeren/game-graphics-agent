"""Configuration adoption and historical facts (01 §5.2, data §5.2, state §4).

Every object here is a declaration.  Nothing writes a game or driver file, and
a confirmed setting keeps the method that produced it: a player statement is
never relabelled as a ``system_probe``.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, field_validator, model_validator

from game_app.contracts.common import (
    CheckResult,
    EvidenceRef,
    FieldValue,
    KnowledgeRecordRef,
    LegacyUnknownReason,
)
from game_app.contracts.enums import (
    ActivationState,
    AcquisitionMethod,
    AppliedOrigin,
    ConfigurationRegistrationKind,
)
from game_app.contracts.primitives import (
    BindingRef,
    ID,
    Revision,
    Scalar,
    StrictModel,
    TimeRange,
    Timestamp,
)


class SettingRef(StrictModel):
    """A public knowledge record or one local catalog definition."""

    kind: Literal["knowledge", "local"]
    record: KnowledgeRecordRef | None = None
    local_setting_id: ID | None = None

    @model_validator(mode="after")
    def shape_matches_kind(self) -> "SettingRef":
        if self.kind == "knowledge":
            if self.record is None:
                raise ValueError("a knowledge setting reference needs its record")
            if self.local_setting_id is not None:
                raise ValueError("a knowledge setting reference carries no local id")
        else:
            if self.local_setting_id is None:
                raise ValueError("a local setting reference needs its definition id")
            if self.record is not None:
                raise ValueError("a local setting reference carries no knowledge record")
        return self

    @property
    def key(self) -> str:
        if self.kind == "knowledge":
            return f"knowledge:{self.record.record_snapshot_id}"
        return f"local:{self.local_setting_id}"


class SettingDeclaration(StrictModel):
    setting_ref: SettingRef
    value: Scalar | None
    unknown_reason: LegacyUnknownReason | None
    unit: str | None
    claimed_activation: ActivationState
    activation_note: str | None
    observation_id: ID | None

    @model_validator(mode="after")
    def value_and_reason_are_exclusive(self) -> "SettingDeclaration":
        if self.value is None and self.unknown_reason is None:
            raise ValueError("an undeclared value needs a legacy unknown reason")
        if self.value is not None and self.unknown_reason is not None:
            raise ValueError("a declared value carries no unknown reason")
        return self


class ConfirmedSetting(StrictModel):
    setting_ref: SettingRef
    setting_identity_ref: ID | None
    value: FieldValue[Scalar]
    unit: str | None
    confirmation_method: AcquisitionMethod
    activation_state: ActivationState

    @model_validator(mode="after")
    def method_is_a_player_method(self) -> "ConfirmedSetting":
        if self.confirmation_method not in (
            AcquisitionMethod.user_provided,
            AcquisitionMethod.player_confirmed,
        ):
            raise ValueError("an adopted value is declared by the player, not probed")
        return self


class SettingRefReplacement(StrictModel):
    old_local_setting_id: ID
    new_local_setting_id: ID
    expected_old_value: Scalar
    expected_old_unit: str | None

    @model_validator(mode="after")
    def different_definitions(self) -> "SettingRefReplacement":
        if self.old_local_setting_id == self.new_local_setting_id:
            raise ValueError("a replacement names two different definitions")
        return self


class AdoptConfigurationInput(StrictModel):
    """One adoption request; it never carries a local path or a probe claim."""

    kind: Literal["adopt_configuration"]
    origin: AppliedOrigin
    installation_id: ID
    device_snapshot_id: ID
    game_context_id: ID
    expected_current_applied_id: ID | None
    recommendation_id: ID | None
    base_applied_id: ID | None
    change_set_id: ID | None
    restored_from_applied_id: ID | None
    declared_settings: list[SettingDeclaration]
    preset_reference: str | None
    claimed_activation: ActivationState
    setting_ref_replacements: list[SettingRefReplacement] = Field(default_factory=list)

    @field_validator("preset_reference")
    @classmethod
    def preset_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("a preset reference is either null or non-blank")
        return value

    @model_validator(mode="after")
    def replacements_only_for_manual_edit(self) -> "AdoptConfigurationInput":
        if self.setting_ref_replacements and self.origin is not AppliedOrigin.manual_edit:
            raise ValueError("explicit setting reference replacements exist only for manual_edit")
        keys = [declaration.setting_ref.key for declaration in self.declared_settings]
        if len(set(keys)) != len(keys):
            raise ValueError("a declaration names each setting once")
        return self


class HistoricalConfigurationInput(StrictModel):
    kind: Literal["historical_configuration"]
    installation_id: ID
    device_snapshot_id: ID
    game_context_id: ID
    segment_ref: BindingRef
    effective_range: TimeRange
    adopted_at: Timestamp | None
    declared_settings: list[SettingDeclaration]
    preset_reference: str | None
    claimed_activation: ActivationState
    evidence_refs: list[EvidenceRef]

    @model_validator(mode="after")
    def evidence_is_declared(self) -> "HistoricalConfigurationInput":
        if not self.evidence_refs:
            raise ValueError("a historical record needs evidence for its time period")
        keys = [declaration.setting_ref.key for declaration in self.declared_settings]
        if len(set(keys)) != len(keys):
            raise ValueError("a declaration names each setting once")
        return self


class ContextDeclarationInput(StrictModel):
    """A player's own statement about a run, saved before it is interpreted.

    It records declared fields only; it never claims a probe, an activation or
    provenance.  01 owns the declaration path, 04 builds RunConditions from the
    stored values afterwards.
    """

    kind: Literal["context_declaration"]
    installation_id: ID | None
    game_id: ID
    fields: dict[str, Scalar]


DeclarationInput = Annotated[
    Union[AdoptConfigurationInput, HistoricalConfigurationInput, ContextDeclarationInput],
    Field(discriminator="kind"),
]


class PlayerDeclaration(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    installation_id: ID | None
    input: DeclarationInput
    raw_text: str
    submitted_at: Timestamp
    declaration_file_id: ID


class AppliedConfiguration(StrictModel):
    """One immutable adopted (or historically recorded) configuration snapshot."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    user_scope_id: ID
    installation_id: ID
    game_id: ID
    registration_kind: ConfigurationRegistrationKind = (
        ConfigurationRegistrationKind.current_adoption
    )
    device_snapshot_id: ID
    game_context_id: ID
    origin: AppliedOrigin
    recommendation_id: ID | None
    parent_applied_id: ID | None
    change_set_id: ID | None
    restored_from_applied_id: ID | None
    settings: list[ConfirmedSetting]
    preset_reference: str | None
    adopted_at: Timestamp | None
    activation_state: ActivationState
    checks: list[CheckResult]
    declaration_id: ID

    @model_validator(mode="after")
    def settings_are_unambiguous(self) -> "AppliedConfiguration":
        keys = [setting.setting_ref.key for setting in self.settings]
        if len(set(keys)) != len(keys):
            raise ValueError("a configuration has one executable value per setting")
        identities = [
            setting.setting_identity_ref
            for setting in self.settings
            if setting.setting_identity_ref is not None
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("two settings claim the same public identity")
        if not self.settings and not self.preset_reference:
            raise ValueError("a configuration needs settings or an explicit preset reference")
        if self.registration_kind is ConfigurationRegistrationKind.historical_record:
            if self.origin is not AppliedOrigin.baseline:
                raise ValueError("a historical record is always a baseline fact")
            if any((
                self.recommendation_id,
                self.parent_applied_id,
                self.change_set_id,
                self.restored_from_applied_id,
            )):
                raise ValueError("a historical record links no adoption chain")
            if self.version != 1:
                raise ValueError("a historical record starts its own version family")
        return self


class CurrentAppliedPointer(StrictModel):
    user_scope_id: ID
    installation_id: ID
    applied_id: ID | None
    revision: Revision


class CurrentAppliedEvent(StrictModel):
    user_scope_id: ID
    installation_id: ID
    pointer_revision: Revision
    previous_applied_id: ID | None
    next_applied_id: ID


class AdoptionCatalogQuery(StrictModel):
    installation_id: ID
    cursor: str | None
    limit: int = 20


__all__ = [
    "SettingRef",
    "SettingDeclaration",
    "ConfirmedSetting",
    "SettingRefReplacement",
    "AdoptConfigurationInput",
    "HistoricalConfigurationInput",
    "ContextDeclarationInput",
    "DeclarationInput",
    "PlayerDeclaration",
    "AppliedConfiguration",
    "CurrentAppliedPointer",
    "CurrentAppliedEvent",
    "AdoptionCatalogQuery",
]
