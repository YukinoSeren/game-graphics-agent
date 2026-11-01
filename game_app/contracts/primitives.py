"""Strict identifiers, versions and references.

Transcribed from ``docs/technical-design/shared/data-contracts.md`` §1—§3 and
``shared/state-conventions.md`` §2.  Only names with a single shared definition
live here; domain DTOs belong to their owner modules.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union
from uuid import uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from game_app.contracts.enums import ENTITY_TYPES, StateDimension

ID = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")]
LegacyID = Annotated[str, StringConstraints(strict=True, min_length=1)]
Hash = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


def _finite_scalar(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NaN and Infinity are not contract values")
    return value


def _utc_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:  # pragma: no cover - message shape only
        raise ValueError("timestamp must be ISO 8601 with a UTC offset") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("timestamp must carry a zero UTC offset")
    return value


Scalar = Annotated[Union[str, int, float, bool], AfterValidator(_finite_scalar)]
Timestamp = Annotated[str, AfterValidator(_utc_timestamp)]


class StrictModel(BaseModel):
    """Every contract model rejects unknown fields and implicit coercion."""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid4().hex


class EntityHeader(StrictModel):
    id: ID
    schema_version: str = "product-contract/0.4"
    created_at: Timestamp
    created_by_task_id: ID | None = None

    @classmethod
    def create(cls, *, schema_version: str = "product-contract/0.4") -> "EntityHeader":
        return cls(id=new_id(), schema_version=schema_version, created_at=utc_now())


class VersionHeader(StrictModel):
    series_id: ID
    version: Revision


class ObjectRef(StrictModel):
    entity_type: str
    entity_id: ID

    @field_validator("entity_type")
    @classmethod
    def known_type(cls, value: str) -> str:
        if value not in ENTITY_TYPES:
            raise ValueError(f"unregistered entity type: {value}")
        return value


class VersionRef(StrictModel):
    entity_type: str
    entity_id: ID
    series_id: ID
    version: Revision

    @field_validator("entity_type")
    @classmethod
    def known_type(cls, value: str) -> str:
        if value not in ENTITY_TYPES:
            raise ValueError(f"unregistered entity type: {value}")
        return value


class InformationRequestRevisionRef(StrictModel):
    request_id: ID
    version: Revision


class TimeRange(StrictModel):
    start: float
    end: float
    time_basis: str
    unit: Literal["s", "ms"]

    @model_validator(mode="after")
    def ordered(self) -> "TimeRange":
        if not (math.isfinite(self.start) and math.isfinite(self.end)):
            raise ValueError("time range bounds must be finite")
        if self.end <= self.start:
            raise ValueError("time range end must be greater than start")
        return self


class BindingRange(StrictModel):
    start: float
    end: float | None
    time_basis: str
    unit: Literal["s", "ms"]

    @model_validator(mode="after")
    def ordered(self) -> "BindingRange":
        if not math.isfinite(self.start) or self.start < 0:
            raise ValueError("binding range start must be a finite non-negative value")
        if self.end is not None and (not math.isfinite(self.end) or self.end <= self.start):
            raise ValueError("binding range end must be greater than start")
        return self


class BindingRef(StrictModel):
    segment_id: ID
    segment_revision: Revision
    binding_revision: Revision


class StateEvent(StrictModel):
    target_ref: ObjectRef
    dimension: StateDimension
    sequence: Revision
    previous_state: str | None
    next_state: str
    cause: str
    related_refs: list[ObjectRef]
    header: EntityHeader


__all__ = [
    "ID",
    "LegacyID",
    "Hash",
    "Revision",
    "NonNegativeInt",
    "Scalar",
    "Timestamp",
    "StrictModel",
    "EntityHeader",
    "VersionHeader",
    "ObjectRef",
    "VersionRef",
    "InformationRequestRevisionRef",
    "TimeRange",
    "BindingRange",
    "BindingRef",
    "StateEvent",
    "utc_now",
    "new_id",
]
