"""Installation, local-catalog, context and device contracts (01).

``GameContext`` is defined here because 02's selection flow produces it; DEV-T02
adds installation registration and the local setting directory, DEV-T01 adds
``DeviceSnapshot``.  Fields follow ``shared/data-contracts.md`` §3.2 and §4.3.
"""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator, model_validator

from game_app.contracts.common import CheckResult, EvidenceRef, FieldValue, Provenance
from game_app.contracts.primitives import (
    ID,
    NonNegativeInt,
    Revision,
    Scalar,
    StrictModel,
    Timestamp,
)

class BuildIdentifier(StrictModel):
    scheme: str
    raw_value: str
    platform: str
    provenance: list[Provenance]

    @model_validator(mode="after")
    def has_evidence(self) -> "BuildIdentifier":
        if not self.provenance:
            raise ValueError("a build identifier requires provenance")
        return self


class ProcessIdentity(StrictModel):
    pid: int
    started_at: Timestamp | None
    executable_path_ref: ID | None
    provenance: list[Provenance]

    @model_validator(mode="after")
    def valid_pid(self) -> "ProcessIdentity":
        if self.pid <= 0:
            raise ValueError("process ids are positive")
        if not self.provenance:
            raise ValueError("a process identity requires provenance")
        return self


class AdapterCapabilities(StrictModel):
    can_identify: CheckResult
    can_read_config: CheckResult
    can_bind_capture: CheckResult


class GameContext(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    game_id: ID
    game_revision_id: ID
    installation_id: ID | None
    platform: FieldValue[str]
    build_identifiers: list[BuildIdentifier]
    process_candidates: list[ProcessIdentity]
    selected_process: ProcessIdentity | None
    adapter_id: str | None
    adapter_version: str | None
    capabilities: AdapterCapabilities
    checks: list[CheckResult]

    @model_validator(mode="after")
    def adapter_fields_travel_together(self) -> "GameContext":
        if (self.adapter_id is None) != (self.adapter_version is None):
            raise ValueError("adapter_id and adapter_version are both null or both set")
        return self


class Resolution(StrictModel):
    width: int
    height: int

    @model_validator(mode="after")
    def positive(self) -> "Resolution":
        if self.width <= 0 or self.height <= 0:
            raise ValueError("resolution dimensions must be positive")
        return self


class DisplayContext(StrictModel):
    desktop_resolution: FieldValue[Resolution]
    # Each axis is kept on its own: a pair is only known when both are, and a
    # later answer must be able to complete a half-known pair.
    desktop_resolution_axes: dict[str, FieldValue[NonNegativeInt]]
    refresh_hz: FieldValue[float]

    @field_validator("desktop_resolution_axes")
    @classmethod
    def axis_keys(cls, value: dict[str, FieldValue[NonNegativeInt]]) -> dict:
        if set(value) - {"width", "height"}:
            raise ValueError("desktop resolution axes are width and/or height")
        return value


class GpuSnapshot(StrictModel):
    id: ID
    probe_index: NonNegativeInt
    raw_name: FieldValue[str]
    vram_bytes: FieldValue[int]
    driver_version: FieldValue[str]


class DeviceSnapshot(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    device_series_id: ID
    user_scope_id: ID
    captured_at: Timestamp
    gpus: list[GpuSnapshot]
    selected_gpu_id: ID | None
    cpu: FieldValue[str]
    memory_bytes: FieldValue[int]
    device_form: FieldValue[str]
    os_version: FieldValue[str]
    display: DisplayContext
    power_context: dict[str, FieldValue[Scalar]]
    checks: list[CheckResult]

    @model_validator(mode="after")
    def selected_gpu_belongs_to_snapshot(self) -> "DeviceSnapshot":
        if self.selected_gpu_id is not None:
            if self.selected_gpu_id not in {gpu.id for gpu in self.gpus}:
                raise ValueError("selected_gpu_id must belong to this snapshot")
        return self


__all__ = [
    "BuildIdentifier",
    "ProcessIdentity",
    "AdapterCapabilities",
    "GameContext",
    "Resolution",
    "DisplayContext",
    "GpuSnapshot",
    "DeviceSnapshot",
    "GameInstallation",
    "InstallationCatalogQuery",
    "InstallationChoice",
    "InstallationChoicePage",
    "CreateInstallationRequest",
    "InstallationRegistration",
    "LocalSettingDefinition",
    "RegisterLocalSettingRequest",
    "LocalSettingRegistration",
    "LocalSettingChoice",
]


# -- D-P01 installation registration -----------------------------------------

class GameInstallation(StrictModel):
    """One local installation; a second store or directory is another series.

    ``series_id`` is the stable installation id.  Revisions are separate
    objects in the same series, so an old context keeps a readable reference.
    """

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    revision: Revision
    user_scope_id: ID
    game_id: ID
    origin: Literal["system_probe", "user_provided"]
    platform: FieldValue[str]
    private_path_ref: ID | None

    @model_validator(mode="after")
    def revision_matches_version(self) -> "GameInstallation":
        if self.revision != self.version:
            raise ValueError("installation revision and object version are the same value")
        return self


class InstallationCatalogQuery(StrictModel):
    game_id: ID
    cursor: str | None
    limit: NonNegativeInt = 20

    @field_validator("limit")
    @classmethod
    def bounded(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("limit must be between 1 and 100")
        return value


class InstallationChoice(StrictModel):
    installation_id: ID
    revision: Revision
    display_label: str
    game_context_id: ID


class InstallationChoicePage(StrictModel):
    choices: list[InstallationChoice]
    next_cursor: str | None


class CreateInstallationRequest(StrictModel):
    game_context_id: ID
    display_label: str | None

    @field_validator("display_label")
    @classmethod
    def label_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("a display label is either null or non-blank")
        return value


class InstallationRegistration(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    input: CreateInstallationRequest
    installation_id: ID
    game_context_id: ID
    declaration_file_id: ID


# -- D-P02 local setting directory -------------------------------------------

class LocalSettingDefinition(StrictModel):
    """Versioned local catalog entry; it names a menu entry, never a value."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    parent_definition_id: ID | None
    installation_id: ID
    namespace: str
    raw_name: str
    menu_path: list[str]
    evidence_refs: list[EvidenceRef]

    @field_validator("raw_name")
    @classmethod
    def name_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("raw_name must not be blank")
        return value

    @field_validator("menu_path")
    @classmethod
    def path_parts_are_text(cls, value: list[str]) -> list[str]:
        if any(not isinstance(part, str) or not part for part in value):
            raise ValueError("menu_path holds non-empty strings")
        return value


class RegisterLocalSettingRequest(StrictModel):
    installation_id: ID
    raw_name: str
    menu_path: list[str]
    identity_mode: Literal["auto", "reuse", "distinct"]
    reuse_local_setting_id: ID | None
    supersedes_local_setting_id: ID | None
    raw_text: str

    @field_validator("raw_name")
    @classmethod
    def name_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("raw_name must not be blank")
        return value

    @field_validator("menu_path")
    @classmethod
    def path_parts_are_text(cls, value: list[str]) -> list[str]:
        if any(not isinstance(part, str) or not part for part in value):
            raise ValueError("menu_path holds non-empty strings")
        return value


class LocalSettingRegistration(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    input: RegisterLocalSettingRequest
    definition_id: ID
    declaration_file_id: ID


class LocalSettingChoice(StrictModel):
    definition_id: ID
    series_id: ID
    raw_name: str
    menu_path: list[str]
    location_known: bool

    @model_validator(mode="after")
    def location_flag_is_derived(self) -> "LocalSettingChoice":
        if self.location_known != bool(self.menu_path):
            raise ValueError("location_known is exactly whether a menu path is known")
        return self
