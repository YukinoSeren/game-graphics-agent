"""Performance capture contracts owned by 04.

Shapes follow ``shared/data-contracts.md`` §6.1.  ``NormalizedFrame`` is an
in-memory analyzer value: it is never persisted, and an unknown measurement is
``None`` rather than a fabricated number.
"""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator, model_validator

from game_app.contracts.common import CheckResult
from game_app.contracts.device import ProcessIdentity
from game_app.contracts.enums import FrameStreamKind, LogQuality
from game_app.contracts.primitives import (
    ID,
    Scalar,
    StrictModel,
    Timestamp,
)

class ColumnMapping(StrictModel):
    """How one source column becomes a normalized field.

    ``conversion`` is a registered deterministic transform name (``identity``
    or ``none``), never executable input.  ``source_unit`` is the unit the
    registered sample actually carried; a file stating another unit is not
    converted with it.
    """

    source_column: str
    target_field: str
    source_unit: str
    target_unit: str
    conversion: str

    @field_validator("source_column", "target_field", "source_unit", "target_unit", "conversion")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("column mapping fields must not be blank")
        return value

class TimeMapping(StrictModel):
    """Source clock to session clock.

    ``offset`` is a finite anchor offset in source units; ``scale`` converts
    one source unit to one target unit.  Without both, the mapping is not
    bindable: a log keeps its own basis and is never attached to a historical
    segment on a guess.
    """

    source_basis: str
    target_basis: str
    source_unit: str
    target_unit: str
    offset: float | None
    scale: float | None
    checks: list[CheckResult]

    @model_validator(mode="after")
    def scale_is_positive(self) -> "TimeMapping":
        if self.scale is not None and self.scale <= 0:
            raise ValueError("a time scale must be positive")
        return self

    @property
    def bindable(self) -> bool:
        return self.offset is not None and self.scale is not None

class NormalizedFrame(StrictModel):
    """One frame as the analyzer sees it; never stored as a product object."""

    event_index: int
    timestamp: float
    time_basis: str
    process: ProcessIdentity
    swapchain_id: str | None
    frame_stream_kind: FrameStreamKind
    interval_ms: float | None
    frame_type: str | None
    gpu_busy_ms: float | None
    cpu_busy_ms: float | None
    validity: Literal["valid", "break", "invalid"]

class CaptureArtifact(StrictModel):
    """A published original plus the verified schema it was read with."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    session_id: ID
    segment_ids: list[ID]
    file_id: ID
    tool_name: str
    tool_version: str | None
    options: dict[str, Scalar]
    source_schema: str
    source_column_map: list[ColumnMapping]
    time_mapping: TimeMapping
    quality: LogQuality
    checks: list[CheckResult]

    @field_validator("segment_ids")
    @classmethod
    def has_segments(cls, value: list[ID]) -> list[ID]:
        if not value:
            raise ValueError("a capture artifact belongs to at least one segment")
        return value

__all__ = [
    "ColumnMapping",
    "TimeMapping",
    "NormalizedFrame",
    "CaptureArtifact",
]
