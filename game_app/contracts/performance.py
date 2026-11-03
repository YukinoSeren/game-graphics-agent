"""Performance capture contracts owned by 04.

Shapes follow ``shared/data-contracts.md`` §6.1.  ``NormalizedFrame`` is an
in-memory analyzer value: it is never persisted, and an unknown measurement is
``None`` rather than a fabricated number.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import Field, field_validator, model_validator

from game_app.contracts.common import (
    CheckResult,
    EvidenceRef,
    FieldValue,
    LegacyUnknownReason,
)
from game_app.contracts.device import ProcessIdentity, Resolution
from game_app.contracts.enums import (
    CheckStatus,
    ComparabilityLevel,
    ConfigurationState,
    FrameStreamKind,
    LogQuality,
    SessionLifecycle,
    ThresholdMode,
)
from game_app.contracts.primitives import (
    BindingRange,
    BindingRef,
    Hash,
    ID,
    NonNegativeInt,
    ObjectRef,
    Revision,
    Scalar,
    StrictModel,
    TimeRange,
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

# --- sessions, segments and configuration bindings (04 §4, data §6.1) --------


class RunConditions(StrictModel):
    """What the player declared about one run; unknown stays unknown.

    ``scene_conditions`` is keyed by the registered ``scene.*`` paths.  A
    missing declaration is ``unknown/not_provided/not_attempted`` — no default
    resolution, frame-generation mode or upscaling setting is filled in.
    """

    output_resolution: FieldValue[Resolution]
    render_resolution: FieldValue[Resolution]
    frame_generation: FieldValue[bool]
    upscaling: FieldValue[str]
    scene_conditions: dict[str, FieldValue[Scalar]]


class RunDeclarationInput(StrictModel):
    """The player's own words and declared values, saved before interpretation."""

    installation_id: ID
    fields: dict[str, Scalar]
    raw_text: str

    @field_validator("raw_text")
    @classmethod
    def raw_text_is_a_string(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("raw_text must be a string")
        return value


class CreateSessionRequest(StrictModel):
    installation_id: ID
    device_snapshot_id: ID
    game_context_id: ID
    applied_id: ID | None
    configuration_state: ConfigurationState
    run_declaration: RunDeclarationInput
    test_protocol_id: ID | None
    recommendation_id: ID | None


class EndSegmentRequest(StrictModel):
    segment_id: ID
    expected_segment_revision: Revision
    expected_binding_revision: Revision


class SplitSegmentRequest(StrictModel):
    ending: EndSegmentRequest
    new_applied_id: ID | None
    configuration_state: ConfigurationState
    run_declaration: RunDeclarationInput


class EndSessionRequest(StrictModel):
    session_id: ID
    expected_session_revision: Revision
    active_segments: list[EndSegmentRequest]


class BindConfigurationRequest(StrictModel):
    """A historical binding: evidence for a past period, never today's pointer."""

    segment_id: ID
    expected_segment_revision: Revision
    expected_binding_revision: Revision
    applied_id: ID
    effective_range: TimeRange
    evidence_refs: list[EvidenceRef]


class ExcludedRange(StrictModel):
    range: TimeRange
    reason: str
    evidence_refs: list[EvidenceRef]


class StreamSelection(StrictModel):
    process: ProcessIdentity | None
    swapchain_id: str | None
    frame_stream_kind: FrameStreamKind
    checks: list[CheckResult]


class PlaySession(StrictModel):
    """An aggregate projection; the current lifecycle is the latest revision."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    installation_id: ID
    game_id: ID
    device_snapshot_id: ID
    game_context_id: ID
    started_at: Timestamp
    ended_at: Timestamp | None
    revision: Revision
    lifecycle: SessionLifecycle
    initial_applied_id: ID | None
    recommendation_id: ID | None
    configuration_state: ConfigurationState
    scenario: FieldValue[str]
    target_proposal_ref: ID | None
    test_protocol_id: ID | None
    run_conditions: RunConditions

    @model_validator(mode="after")
    def ended_after_started(self) -> "PlaySession":
        if self.lifecycle is SessionLifecycle.active and self.ended_at is not None:
            raise ValueError("an active session has no end time")
        if self.lifecycle is SessionLifecycle.ended and self.ended_at is None:
            raise ValueError("an ended session records its end time")
        if self.ended_at is not None:
            started = datetime.fromisoformat(self.started_at)
            ended = datetime.fromisoformat(self.ended_at)
            if ended < started:
                raise ValueError("a session cannot end before it starts")
        return self


class SessionSegment(StrictModel):
    """A run interval on the session clock, closed by a later revision."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    session_id: ID
    start_s: float
    end_s: float | None
    time_basis: str
    revision: Revision
    lifecycle: SessionLifecycle
    applied_id: ID | None
    configuration_state: ConfigurationState
    binding_revision: Revision
    scenario: FieldValue[str]
    excluded_ranges: list[ExcludedRange]
    stream_selection: StreamSelection
    run_conditions: RunConditions

    @model_validator(mode="after")
    def closed_range_matches_lifecycle(self) -> "SessionSegment":
        if not math.isfinite(self.start_s) or self.start_s < 0:
            raise ValueError("a segment starts at a finite non-negative second")
        if not self.time_basis.strip():
            raise ValueError("a segment names its time basis")
        if self.lifecycle is SessionLifecycle.active:
            if self.end_s is not None:
                raise ValueError("an active segment has no end")
        else:
            if self.end_s is None:
                raise ValueError("an ended segment records its end")
            if not math.isfinite(self.end_s) or self.end_s <= self.start_s:
                raise ValueError("a segment ends after it starts")
        return self


class ConfigurationBinding(StrictModel):
    """One immutable binding revision; closing appends, it never rewrites."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    segment_id: ID
    binding_revision: Revision
    applied_id: ID | None
    configuration_state: ConfigurationState
    effective_range: BindingRange
    evidence_refs: list[EvidenceRef]
    supersedes_revision: Revision | None


# --- metric catalog and test protocols (04 §4.4, data §7) -------------------


class MetricCatalogQuery(StrictModel):
    frame_stream_kind: FrameStreamKind | None


class MetricChoice(StrictModel):
    key: str
    label: str
    unit: str
    frame_stream_kind: FrameStreamKind
    definition_id: ID | None


class MetricChoicePage(StrictModel):
    choices: list[MetricChoice]
    registry_version: str


class MetricDefinition(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    metric_key: str
    definition_version: str
    unit: str
    algorithm_ref: str
    frame_stream_kind: FrameStreamKind
    minimum_sample_rule: str


class CreateTestProtocolRequest(StrictModel):
    """A plan, not an execution: no field may claim Controlled or executed."""

    game_context_id: ID
    parent_protocol_id: ID | None
    scenario: str | None
    condition_fields: dict[str, Scalar]
    controlled_variables: list[str]
    metric_registry_version: str
    metric_keys: list[str]
    warmup_seconds: float | None
    sample_duration_seconds: float | None
    repeat_count: int | None
    note: str | None

    @field_validator("scenario")
    @classmethod
    def scenario_is_null_or_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("a scenario is null or non-blank")
        return value

    @field_validator("warmup_seconds")
    @classmethod
    def warmup_is_finite_and_non_negative(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("warmup_seconds is null or finite and >= 0")
        return value

    @field_validator("sample_duration_seconds")
    @classmethod
    def sample_duration_is_positive(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("sample_duration_seconds is null or finite and > 0")
        return value

    @field_validator("repeat_count")
    @classmethod
    def repeat_count_is_a_strict_integer(cls, value: int | None) -> int | None:
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("repeat_count is null or a strict integer >= 1")
        return value


class TestProtocol(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    game_id: ID
    scenario: FieldValue[str]
    conditions: RunConditions
    controlled_variables: list[str]
    metric_definition_ids: list[ID]
    warmup_seconds: float | None
    sample_duration_seconds: float | None
    repeat_count: int | None
    note: str | None
    parent_protocol_id: ID | None
    declaration_file_id: ID | None


# --- historical log preview and association (04 §4.3) -----------------------


class LogStreamCandidate(StrictModel):
    stream_key: str
    process: ProcessIdentity | None
    swapchain_id: str | None
    frame_stream_kind: FrameStreamKind

    @field_validator("stream_key")
    @classmethod
    def key_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a stream key must not be blank")
        return value


class LogInspection(StrictModel):
    file_id: ID
    content_hash: Hash
    source_schema: str
    source_ranges: list[TimeRange]
    streams: list[LogStreamCandidate]
    limitations: list[str]


class ExistingLogTarget(StrictModel):
    mode: Literal["existing_segment"]
    session_id: ID
    expected_session_revision: Revision
    segment_ref: BindingRef
    close_at_s: float | None

    @field_validator("close_at_s")
    @classmethod
    def close_point_is_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("close_at_s is null or finite")
        return value


class HistoricalLogTarget(StrictModel):
    mode: Literal["historical_session"]
    installation_id: ID
    device_snapshot_id: ID
    game_context_id: ID
    started_at: Timestamp
    run_declaration: RunDeclarationInput
    test_protocol_id: ID | None


LogTarget = Annotated[
    Union[ExistingLogTarget, HistoricalLogTarget],
    Field(discriminator="mode"),
]


class LogAssociationRequest(StrictModel):
    file_id: ID
    content_hash: Hash
    source_schema: str
    source_range: TimeRange
    stream_key: str
    source_anchor: float
    target_anchor_s: float
    target: LogTarget
    evidence_refs: list[EvidenceRef]
    raw_text: str

    @field_validator("source_anchor")
    @classmethod
    def anchor_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("a source anchor is finite")
        return value

    @field_validator("target_anchor_s")
    @classmethod
    def target_anchor_is_non_negative(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("a target anchor is finite and >= 0")
        return value

    @field_validator("source_schema", "stream_key")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("this field must not be blank")
        return value


# --- deterministic analysis and summaries (04 §6, data §6.2) ----------------


class NumericValueV01(StrictModel):
    """The legacy numeric shape, reused verbatim for measured statistics.

    An unavailable statistic is ``number_kind=unknown`` with a legacy reason;
    a finite value is never silently omitted and unknown is never stored as 0.
    """

    number_kind: Literal[
        "exact", "approximate", "interval", "lower_bound", "upper_bound", "unknown"
    ]
    value: int | float | None
    lower: int | float | None
    upper: int | float | None
    unknown_reason: LegacyUnknownReason | None

    @model_validator(mode="after")
    def shape_matches_kind(self) -> "NumericValueV01":
        if self.number_kind in {"exact", "approximate"}:
            valid = (
                self.value is not None and self.lower is None and self.upper is None
                and self.unknown_reason is None
            )
        elif self.number_kind == "interval":
            valid = (
                self.value is None and self.lower is not None and self.upper is not None
                and self.lower <= self.upper and self.unknown_reason is None
            )
        elif self.number_kind == "lower_bound":
            valid = (
                self.value is None and self.lower is not None and self.upper is None
                and self.unknown_reason is None
            )
        elif self.number_kind == "upper_bound":
            valid = (
                self.value is None and self.upper is not None and self.lower is None
                and self.unknown_reason is None
            )
        else:
            valid = (
                self.value is None and self.lower is None and self.upper is None
                and self.unknown_reason is not None
            )
        if not valid:
            raise ValueError("the numeric value shape does not match its kind")
        return self


class MetricDenominator(StrictModel):
    value: float
    unit: str
    input_refs: list[ObjectRef]

    @field_validator("value")
    @classmethod
    def non_negative(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("a denominator is finite and >= 0")
        return value

    @field_validator("unit")
    @classmethod
    def unit_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a denominator names its unit")
        return value


class MetricValue(StrictModel):
    """One defined statistic; insufficient input stays unknown with a reason."""

    definition_id: ID
    numeric: NumericValueV01
    unit: str
    frame_stream_kind: FrameStreamKind
    input_refs: list[ObjectRef]
    denominator: MetricDenominator | None
    method: str


class AdaptiveLongFrame(StrictModel):
    """One continuous valid run and the segment-adaptive threshold it used.

    ``threshold_ms`` is ``max(50, 2 × the run's median interval)``.  A run with
    no usable statistics records nulls and a non-blank reason instead of 0.
    """

    range: TimeRange
    threshold_ms: float | None
    cluster_count: int | None
    interval_count: int | None
    long_frame_duration_ms: float | None
    reason: str | None

    @model_validator(mode="after")
    def shape_matches_data(self) -> "AdaptiveLongFrame":
        statistics = (
            self.threshold_ms, self.cluster_count, self.interval_count,
            self.long_frame_duration_ms,
        )
        if any(item is None for item in statistics):
            if any(item is not None for item in statistics):
                raise ValueError("adaptive long-frame statistics are all present or all null")
            if not self.reason or not self.reason.strip():
                raise ValueError("missing adaptive statistics need a reason")
        elif self.reason and not self.reason.strip():
            raise ValueError("a reason must not be blank")
        if self.threshold_ms is not None and (
            not math.isfinite(self.threshold_ms) or self.threshold_ms <= 0
        ):
            raise ValueError("a long-frame threshold is finite and > 0")
        if self.interval_count is not None and self.interval_count < 0:
            raise ValueError("an interval count is >= 0")
        if self.cluster_count is not None and self.cluster_count < 0:
            raise ValueError("a cluster count is >= 0")
        if self.long_frame_duration_ms is not None and self.long_frame_duration_ms < 0:
            raise ValueError("a long-frame duration is >= 0")
        return self


class PerformanceSummary(StrictModel):
    """An immutable analysis result; a second run creates a new summary."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    artifact_id: ID
    bindings: list[BindingRef]
    analyzed_ranges: list[TimeRange]
    analyzer_version: str
    cleaning_protocol_version: str
    manifest_file_id: ID
    valid_interval_count: NonNegativeInt
    effective_duration_s: float
    metrics: list[MetricValue]
    adaptive_long_frames: list[AdaptiveLongFrame]
    quality: LogQuality
    checks: list[CheckResult]

    @field_validator("bindings")
    @classmethod
    def has_bindings(cls, value: list[BindingRef]) -> list[BindingRef]:
        if not value:
            raise ValueError("a summary names at least one closed binding")
        return value

    @field_validator("effective_duration_s")
    @classmethod
    def duration_is_finite(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("the effective duration is finite and >= 0")
        return value




# --- comparison protocols and comparisons (04 §4.2/§6, data §6.2) -----------


class CreateComparisonProtocolRequest(StrictModel):
    """A common long-frame threshold, frozen before the adjusted result is read."""

    threshold_mode: ThresholdMode
    threshold_ms: float | None
    baseline_summary_ids: list[ID]
    baseline_binding_refs: list[BindingRef]
    cleaning_definition: str
    clustering_definition: str
    denominator_definition: str
    exploratory: bool

    @field_validator("threshold_ms")
    @classmethod
    def threshold_is_finite_and_positive(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("threshold_ms is null or finite and > 0")
        return value

    @field_validator("cleaning_definition", "clustering_definition", "denominator_definition")
    @classmethod
    def definition_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a comparison definition names a registered rule version")
        return value


class ComparisonProtocol(StrictModel):
    """An immutable frozen protocol; a changed rule means a new protocol id."""

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    series_id: ID
    version: Revision
    threshold_mode: ThresholdMode
    threshold_ms: float
    baseline_summary_ids: list[ID]
    baseline_binding_refs: list[BindingRef]
    frozen_at: Timestamp
    exploratory: bool
    cleaning_definition: str
    clustering_definition: str
    denominator_definition: str

    @field_validator("threshold_ms")
    @classmethod
    def threshold_is_positive(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("a frozen threshold is finite and > 0")
        return value


class CompareInput(StrictModel):
    summary_ids: list[ID]
    protocol_id: ID

    @field_validator("summary_ids")
    @classmethod
    def exactly_two_distinct_summaries(cls, value: list[ID]) -> list[ID]:
        if len(value) != 2:
            raise ValueError("a comparison names exactly two summaries")
        if value[0] == value[1]:
            raise ValueError("a comparison needs two different summaries")
        return value


class MetricComparison(StrictModel):
    metric_definition_id: ID
    status: CheckStatus
    left: MetricValue | None
    right: MetricValue | None
    delta: MetricValue | None
    reason: str | None


class PerformanceComparison(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    summary_ids: list[ID]
    protocol_id: ID
    level: ComparabilityLevel
    per_metric: list[MetricComparison]
    checks: list[CheckResult]
    limitations: list[str]

    @field_validator("summary_ids")
    @classmethod
    def two_distinct_summaries(cls, value: list[ID]) -> list[ID]:
        if len(value) != 2 or value[0] == value[1]:
            raise ValueError("a comparison references two different summaries")
        return value




# --- owned capture and its recovery lease (04 §4.3/§5) ----------------------


class CaptureLease(StrictModel):
    """One local recovery log row per capture task; state changes append events.

    ``prepared`` is registered with the task, ``owned`` is committed before the
    tool is launched, ``finalized`` requires a published original and a
    registered file status, and an unprovable ownership stays ``unresolved``.
    """

    id: ID
    schema_version: str
    created_at: Timestamp
    created_by_task_id: ID | None
    task_id: ID
    session_id: ID
    native_name: str
    process_pid: int | None
    process_started_at: Timestamp | None
    file_id: ID
    state: Literal["prepared", "owned", "finalized", "unresolved"]

    @field_validator("native_name")
    @classmethod
    def native_name_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a capture lease names the tool's native session")
        return value

    @field_validator("process_pid")
    @classmethod
    def pid_is_positive(cls, value: int | None) -> int | None:
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("a recorded pid is a positive integer")
        return value


class StopCaptureInput(StrictModel):
    task_id: ID


__all__ = [
    "ColumnMapping",
    "TimeMapping",
    "NormalizedFrame",
    "CaptureArtifact",
    "RunConditions",
    "RunDeclarationInput",
    "CreateSessionRequest",
    "EndSegmentRequest",
    "SplitSegmentRequest",
    "EndSessionRequest",
    "BindConfigurationRequest",
    "ExcludedRange",
    "StreamSelection",
    "PlaySession",
    "SessionSegment",
    "ConfigurationBinding",
    "MetricCatalogQuery",
    "MetricChoice",
    "MetricChoicePage",
    "MetricDefinition",
    "CreateTestProtocolRequest",
    "TestProtocol",
    "LogStreamCandidate",
    "LogInspection",
    "ExistingLogTarget",
    "HistoricalLogTarget",
    "LogTarget",
    "LogAssociationRequest",
    "NumericValueV01",
    "MetricDenominator",
    "MetricValue",
    "AdaptiveLongFrame",
    "PerformanceSummary",
    "CreateComparisonProtocolRequest",
    "ComparisonProtocol",
    "CompareInput",
    "MetricComparison",
    "PerformanceComparison",
    "CaptureLease",
    "StopCaptureInput",
]
