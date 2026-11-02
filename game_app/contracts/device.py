"""Game context and device contracts.

``GameContext`` is defined here because 02's selection flow produces it; DEV-T02
adds installation handling and DEV-T01 adds ``DeviceSnapshot``.  Fields follow
``shared/data-contracts.md`` §3.2.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from game_app.contracts.common import CheckResult, FieldValue, Provenance
from game_app.contracts.primitives import (
    ID,
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
    refresh_hz: FieldValue[float]


class GpuSnapshot(StrictModel):
    id: ID
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
]
