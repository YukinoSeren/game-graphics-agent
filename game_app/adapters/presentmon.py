"""Versioned capture-log formats, unit/clock/stream checks and bounded parsing.

No real capture tool has been sampled (G-FORMAT has no evidence), so the only
registered format is the offline fixture shape used by ``test_log_formats.py``.
Registering it does **not** claim support for PresentMon or any other tool: the
registration carries a ``format.real_tool_support`` check that was never run.

The parser refuses to invent anything.  An unregistered column, a column whose
stated unit differs from the verified one, a file that never names its frame
stream, an unknown frame-generation state or an unanchored clock all downgrade
the result and are listed in ``limitations`` — none of them is silently
converted, defaulted or merged.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from game_app.contracts.common import CheckResult, EvidenceRef, FieldValue, Provenance
from game_app.contracts.device import ProcessIdentity
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    FrameStreamKind,
    LogQuality,
    ValueState,
)
from game_app.contracts.performance import ColumnMapping, NormalizedFrame, TimeMapping
from game_app.contracts.primitives import TimeRange, utc_now
from game_app.errors import ContractError, ErrorCode

OFFLINE_FORMAT_ID = "offline-frame-csv/v1"
UNITLESS = "none"
DEFAULT_DEADLINE_SECONDS = 120.0
TIME_BASIS = "log_relative"
TARGET_TIME_BASIS = "session_relative_s"
TIME_COLUMN = "Time"
INTERVAL_COLUMN = "MsBetweenPresents"
STREAM_COLUMN = "FrameStream"
FRAME_GENERATION_COLUMN = "FrameGeneration"

_UNIT_TOKEN = re.compile(r"^(?P<name>[^()]+?)\s*\((?P<unit>[^()]*)\)$")
_TRUE_TOKENS = frozenset({"on", "true", "yes", "1"})
_FALSE_TOKENS = frozenset({"off", "false", "no", "0"})
_STREAM_KINDS = {
    "application_present": FrameStreamKind.application_present,
    "displayed": FrameStreamKind.displayed,
    "unknown": FrameStreamKind.unknown,
}


# --- registration ------------------------------------------------------------


@dataclass(frozen=True)
class LogColumn:
    """One verified source column.  ``required`` columns identify the format."""

    mapping: ColumnMapping
    required: bool


@dataclass(frozen=True)
class LogFormat:
    """An immutable, sample-verified schema registration (04 §5).

    Two headers that differ only in their columns are two formats, even for the
    same tool version: a column set that was never verified must not borrow the
    evidence of another.
    """

    format_id: str
    tool_name: str
    tool_version: str
    header: tuple[str, ...]
    columns: tuple[LogColumn, ...]
    time_basis: str
    time_mapping_rule: str
    frame_stream_kind: FrameStreamKind
    options_fingerprint: str
    fixture_hashes: tuple[str, ...]
    checks: tuple[CheckResult, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(_split_token(token)[0] for token in self.header)

    @property
    def column_map(self) -> tuple[ColumnMapping, ...]:
        return tuple(column.mapping for column in self.columns)

    @property
    def column_names(self) -> set[str]:
        return {column.mapping.source_column for column in self.columns}


class FormatRegistry:
    """Only formats present here may be parsed; everything else is refused."""

    def __init__(self, formats: Sequence[LogFormat] = ()) -> None:
        self._formats: list[LogFormat] = list(formats)

    def register(self, format_: LogFormat) -> None:
        if any(item.format_id == format_.format_id for item in self._formats):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                f"format {format_.format_id} is already registered",
            )
        self._formats.append(format_)

    @property
    def formats(self) -> tuple[LogFormat, ...]:
        return tuple(self._formats)

    def match(self, names: Sequence[str]) -> LogFormat:
        """Exact header match first; a unique superset second; else refuse.

        Matching is by column *names*, never by file name or extension.
        """
        actual = set(names)
        exact = [item for item in self._formats if set(item.names) == actual]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ContractError(
                ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
                "the header matches more than one registered format; nothing is guessed",
            )
        candidates = [
            item
            for item in self._formats
            if {column.mapping.source_column for column in item.columns if column.required}
            <= actual
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ContractError(
                ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
                "the header is ambiguous between registered formats; nothing is guessed",
            )
        raise ContractError(
            ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
            "no registered format matches these columns; the original is retained unparsed",
        )


def _split_token(token: str) -> tuple[str, str | None]:
    """``"Time (ms)"`` -> ``("Time", "ms")``; ``"EventNumber"`` -> ``("EventNumber", None)``."""
    match = _UNIT_TOKEN.match(token.strip())
    if match is None:
        return token.strip(), None
    return match["name"].strip(), match["unit"].strip()


# --- sample intake (G-FORMAT evidence) ---------------------------------------
#
# Registering a real tool format needs a fixed tool version/options, an original
# CSV, and its columns/units/stream/clock/anomalies (09 §7, G-FORMAT).  This
# reader only *describes* a sample: it never registers a format, never parses
# frames and never claims support.  A human reads the report, then decides
# whether a LogFormat may be written with this sample's hash.

_TIME_NAME_HINT = "time"
_PROCESS_NAME_HINTS = ("processid", "process_id", "pid")
_INTERVAL_NAME_HINTS = ("msbetweenpresents", "msbetweendisplaychange")
_STREAM_NAME_HINTS = ("framestream", "presentmode", "stream")
_FRAME_GENERATION_HINTS = ("framegeneration",)


@dataclass(frozen=True)
class SampleColumnFinding:
    """One header token as the file actually states it."""

    header_token: str
    name: str
    unit: str | None
    role: str


@dataclass(frozen=True)
class SampleInspection:
    """What one capture sample states about itself.

    ``blockers`` are structural problems that make the file unusable as
    evidence.  An empty tuple means the sample can be reviewed for
    registration; it does **not** mean any tool is supported yet.
    """

    source_name: str
    byte_length: int
    sha256: str
    header: tuple[str, ...]
    columns: tuple[SampleColumnFinding, ...]
    row_count: int
    malformed_rows: int
    process_ids: tuple[str, ...]
    stream_values: tuple[str, ...]
    frame_generation_values: tuple[str, ...]
    time_column: str | None
    time_monotonic: bool | None
    interval_column: str | None
    blockers: tuple[str, ...]

    @property
    def registration_ready(self) -> bool:
        return not self.blockers


def _role_of(name: str) -> str:
    lowered = name.lower()
    if _TIME_NAME_HINT in lowered:
        return "time"
    if lowered in _PROCESS_NAME_HINTS or lowered.startswith("process"):
        return "process"
    if lowered in _INTERVAL_NAME_HINTS:
        return "interval"
    if lowered in _STREAM_NAME_HINTS:
        return "stream"
    if lowered in _FRAME_GENERATION_HINTS:
        return "frame_generation"
    return "other"


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def inspect_sample(data: bytes, *, source_name: str) -> SampleInspection:
    """Describe one capture original; register nothing and parse no frame."""

    digest = hashlib.sha256(data).hexdigest()
    blockers: list[str] = []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return SampleInspection(
            source_name=source_name, byte_length=len(data), sha256=digest,
            header=(), columns=(), row_count=0, malformed_rows=0,
            process_ids=(), stream_values=(), frame_generation_values=(),
            time_column=None, time_monotonic=None, interval_column=None,
            blockers=("the file is not UTF-8 text",),
        )

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return SampleInspection(
            source_name=source_name, byte_length=len(data), sha256=digest,
            header=(), columns=(), row_count=0, malformed_rows=0,
            process_ids=(), stream_values=(), frame_generation_values=(),
            time_column=None, time_monotonic=None, interval_column=None,
            blockers=("the file has no header or rows",),
        )

    try:
        header_tokens = tuple(_header_tokens(lines[0]))
    except ContractError as error:
        return SampleInspection(
            source_name=source_name, byte_length=len(data), sha256=digest,
            header=(), columns=(), row_count=0, malformed_rows=0,
            process_ids=(), stream_values=(), frame_generation_values=(),
            time_column=None, time_monotonic=None, interval_column=None,
            blockers=(error.message,),
        )
    columns = tuple(
        SampleColumnFinding(
            header_token=token,
            name=_split_token(token)[0],
            unit=_split_token(token)[1],
            role=_role_of(_split_token(token)[0]),
        )
        for token in header_tokens
    )
    positions = {column.name: index for index, column in enumerate(columns)}
    time_columns = [column for column in columns if column.role == "time"]
    interval_columns = [column for column in columns if column.role == "interval"]
    stream_columns = [column for column in columns if column.role == "stream"]
    fg_columns = [column for column in columns if column.role == "frame_generation"]
    process_columns = [column for column in columns if column.role == "process"]

    rows = [line for line in lines[1:] if line.strip()]
    malformed = 0
    times: list[float] = []
    process_ids: set[str] = set()
    stream_values: set[str] = set()
    fg_values: set[str] = set()
    time_index = positions.get(time_columns[0].name) if time_columns else None
    for line in rows:
        fields = next(csv.reader([line]))
        if len(fields) != len(columns):
            malformed += 1
            continue
        if time_index is not None:
            value = _number(fields[time_index])
            if value is not None:
                times.append(value)
        for column in process_columns:
            value = fields[positions[column.name]].strip()
            if value:
                process_ids.add(value)
        for column in stream_columns:
            value = fields[positions[column.name]].strip()
            if value:
                stream_values.add(value)
        for column in fg_columns:
            value = fields[positions[column.name]].strip()
            if value:
                fg_values.add(value)

    if not rows:
        blockers.append("the file states a header but no data rows")
    if malformed:
        blockers.append(
            f"{malformed} row(s) do not have the header's column count"
        )
    if not time_columns:
        blockers.append("no column states a time/clock value")
    elif not times:
        blockers.append("the time column has no numeric value")
    time_monotonic = None
    if len(times) >= 2:
        time_monotonic = all(
            later >= earlier for earlier, later in zip(times, times[1:])
        )
        if not time_monotonic:
            blockers.append("the time column is not monotonic")

    return SampleInspection(
        source_name=source_name,
        byte_length=len(data),
        sha256=digest,
        header=header_tokens,
        columns=columns,
        row_count=len(rows),
        malformed_rows=malformed,
        process_ids=tuple(sorted(process_ids)),
        stream_values=tuple(sorted(stream_values)),
        frame_generation_values=tuple(sorted(fg_values)),
        time_column=time_columns[0].name if time_columns else None,
        time_monotonic=time_monotonic,
        interval_column=interval_columns[0].name if interval_columns else None,
        blockers=tuple(blockers),
    )


def _check(
    check_id: str,
    status: CheckStatus,
    method: str,
    *,
    note: str | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version="log-format-checks/0.4",
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else utc_now(),
        method=method,
        note=note,
    )


def _column(source: str, target: str, unit: str, *, required: bool = False) -> LogColumn:
    return LogColumn(
        mapping=ColumnMapping(
            source_column=source,
            target_field=target,
            source_unit=unit,
            target_unit=unit,
            conversion="identity",
        ),
        required=required,
    )


def _offline_format() -> LogFormat:
    header = (
        "EventNumber",
        "ProcessId",
        "Time (ms)",
        "MsBetweenPresents (ms)",
        "FrameType",
        "MsGPUActive (ms)",
        "FrameStream",
        "FrameGeneration",
    )
    columns = (
        _column("EventNumber", "event_index", UNITLESS, required=True),
        _column("ProcessId", "process_pid", UNITLESS, required=True),
        _column("Time", "timestamp", "ms", required=True),
        _column(INTERVAL_COLUMN, "interval_ms", "ms"),
        _column("FrameType", "frame_type", UNITLESS),
        _column("MsGPUActive", "gpu_busy_ms", "ms"),
        _column(STREAM_COLUMN, "frame_stream", UNITLESS),
        _column(FRAME_GENERATION_COLUMN, "frame_generation", UNITLESS),
    )
    return LogFormat(
        format_id=OFFLINE_FORMAT_ID,
        tool_name="offline-fixture",
        tool_version="1",
        header=header,
        columns=columns,
        time_basis=TIME_BASIS,
        time_mapping_rule="unit_scale_only;offset_requires_anchor",
        frame_stream_kind=FrameStreamKind.application_present,
        options_fingerprint=hashlib.sha256(
            "\n".join(header).encode("utf-8") + b"\nunit=ms;options=none"
        ).hexdigest(),
        fixture_hashes=(
            # basic.csv: the sample this registration was verified against.
            "5bbb043e9a942525f4617a359fa25d89252715f4b422cb4bc7409fa9217fece4",
        ),
        checks=(
            _check(
                "format.fixture_verified",
                CheckStatus.pass_,
                "offline_fixture sample comparison; evidence level offline_fixture",
                note="verified against a hand-authored synthetic CSV only",
            ),
            _check(
                "format.real_tool_support",
                CheckStatus.not_run,
                "no real capture tool sample; G-FORMAT has no evidence",
                note="this registration claims no support for any real tool",
            ),
        ),
    )


def default_registry() -> FormatRegistry:
    return FormatRegistry([_offline_format()])


# --- parse result ------------------------------------------------------------


@dataclass(frozen=True)
class StreamObservation:
    stream_key: str
    frame_stream_kind: FrameStreamKind
    row_count: int


@dataclass(frozen=True)
class LogParseResult:
    """Read-only parse outcome; publishing a CaptureArtifact is a later step."""

    format_id: str
    source_schema: str
    tool_name: str
    tool_version: str | None
    column_map: list[ColumnMapping]
    time_mapping: TimeMapping
    quality: LogQuality
    frames: list[NormalizedFrame]
    valid_intervals_ms: list[float]
    observed_streams: list[StreamObservation]
    source_ranges: list[TimeRange]
    frame_stream_kind: FrameStreamKind
    frame_generation: FieldValue[bool]
    limitations: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    provider_send_count: int = 0


@dataclass(frozen=True)
class _ColumnState:
    """How one declared column resolved against this file's header."""

    column: LogColumn
    present: bool
    unit: str | None
    unit_ok: bool

    @property
    def source(self) -> str:
        return self.column.mapping.source_column

    @property
    def measurement_usable(self) -> bool:
        """A measurement is only readable when its unit matches the verified one."""
        return self.present and self.unit_ok


# --- parsing ----------------------------------------------------------------


def parse_log(
    data: bytes,
    *,
    registry: FormatRegistry,
    evidence: EvidenceRef,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> LogParseResult:
    """Parse one log original outside any write transaction.

    The bytes are only read: the caller keeps the managed original whatever the
    outcome, including ``UNSUPPORTED_ARTIFACT_FORMAT``.
    """
    started = clock()
    text = _decode(data)
    lines = text.splitlines()
    if not lines:
        raise ContractError(
            ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
            "the log is empty; the original is retained",
        )
    header_tokens = _header_tokens(lines[0])
    positions = {_split_token(token)[0]: index for index, token in enumerate(header_tokens)}
    format_ = registry.match(list(positions))

    limitations: list[str] = []
    states = _resolve_columns(format_, header_tokens, limitations)
    column_map = _resolved_column_map(states)

    if clock() - started > deadline_seconds:
        return _budget_exhausted(format_, column_map)

    rows = _Rows(
        format_=format_,
        states=states,
        lines=lines[1:],
        positions=positions,
        evidence=evidence,
    )
    rows.read(clock=clock, started=started, deadline_seconds=deadline_seconds)
    if clock() - started > deadline_seconds:
        return _budget_exhausted(format_, column_map)

    limitations.extend(rows.limitations)
    if rows.process_missing:
        limitations.append(
            "the log does not identify a process, so no frame can be attributed"
        )
    frame_generation, fg_limitations = _frame_generation(rows)
    limitations.extend(fg_limitations)
    observed = _observed_streams(rows)
    stream_kind, stream_limitations = _resolve_stream(
        format_, states, observed, frame_generation
    )
    limitations.extend(stream_limitations)

    frames = [
        frame.model_copy(update={"frame_stream_kind": stream_kind}) for frame in rows.frames
    ]
    valid_intervals = _continuous_intervals(frames)
    if not valid_intervals and frames:
        limitations.append(
            "no pair of adjacent valid frames yields an interval; no frame-rate statistic"
            " is claimed"
        )
    time_mapping = _time_mapping(format_, states)
    if time_mapping.scale is None:
        limitations.append(
            "the time column states no unit this format was verified with;"
            " the clock keeps its own basis and no scale is assumed"
        )
    source_ranges = _source_ranges(states, frames)
    if not source_ranges and frames:
        limitations.append(
            "no source time range can be expressed without a verified time unit"
        )

    quality = _quality(frames, observed, limitations)
    checks = list(format_.checks) + [
        _check(
            "log.header",
            CheckStatus.pass_,
            f"matched the registered header of {format_.format_id}",
        ),
        _check(
            "log.quality",
            _quality_check_status(quality),
            f"derived from {len(frames)} parsed frame(s) and {len(limitations)} limitation(s)",
        ),
    ]
    return LogParseResult(
        format_id=format_.format_id,
        source_schema=format_.format_id,
        tool_name=format_.tool_name,
        tool_version=format_.tool_version,
        column_map=column_map,
        time_mapping=time_mapping,
        quality=quality,
        frames=frames,
        valid_intervals_ms=valid_intervals,
        observed_streams=observed,
        source_ranges=source_ranges,
        frame_stream_kind=stream_kind,
        frame_generation=frame_generation,
        limitations=limitations,
        checks=checks,
    )


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ContractError(
            ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
            "the log is not UTF-8 text; the original is retained",
        ) from error


def _header_tokens(line: str) -> list[str]:
    tokens = [token.strip() for token in next(csv.reader(io.StringIO(line)), [])]
    tokens = [token for token in tokens if token]
    if not tokens:
        raise ContractError(
            ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT, "the log has no header row"
        )
    return tokens


def _resolve_columns(
    format_: LogFormat, header_tokens: Sequence[str], limitations: list[str]
) -> list[_ColumnState]:
    units = {_split_token(token)[0]: _split_token(token)[1] for token in header_tokens}
    states: list[_ColumnState] = []
    for column in format_.columns:
        source = column.mapping.source_column
        expected = column.mapping.source_unit
        if source not in units:
            states.append(_ColumnState(column, present=False, unit=None, unit_ok=False))
            limitations.append(
                f"column {source} is absent; its target field stays unknown"
            )
            continue
        unit = units[source]
        unit_ok = unit is None if expected == UNITLESS else unit == expected
        if not unit_ok:
            limitations.append(
                f"column {source} declares unit {unit or 'none'} but the format was"
                f" verified with {expected or 'no unit'}; no conversion is applied"
            )
        states.append(_ColumnState(column, present=True, unit=unit, unit_ok=unit_ok))

    registered = format_.column_names
    for name in units:
        if name not in registered:
            limitations.append(
                f"column {name} is not registered for {format_.format_id}; it was not parsed"
            )
    return states


def _resolved_column_map(states: Sequence[_ColumnState]) -> list[ColumnMapping]:
    """The mapping actually used for this file, not the registered one.

    A column read with the verified unit keeps ``identity``; a column whose
    unit differs is recorded with the unit the file states and ``none`` as the
    conversion, so the artifact never claims a conversion that did not happen.
    An absent column has no mapping at all and is reported as a limitation.
    """
    resolved: list[ColumnMapping] = []
    for state in states:
        if not state.present:
            continue
        mapping = state.column.mapping
        if state.unit_ok:
            resolved.append(mapping)
        else:
            resolved.append(mapping.model_copy(update={
                "source_unit": state.unit or "unknown",
                "conversion": "none",
            }))
    return resolved


def _state(states: Sequence[_ColumnState], source: str) -> _ColumnState | None:
    return next((state for state in states if state.source == source), None)


class _Rows:
    """One reading pass over the data rows; never re-reads or guesses."""

    def __init__(
        self,
        *,
        format_: LogFormat,
        states: Sequence[_ColumnState],
        lines: Sequence[str],
        positions: dict[str, int],
        evidence: EvidenceRef,
    ) -> None:
        self.format = format_
        self.states = states
        self.lines = lines
        self.positions = positions
        self.evidence = evidence
        self.frames: list[NormalizedFrame] = []
        self.stream_values: list[str | None] = []
        self.frame_generation_values: list[bool | None] = []
        self.limitations: list[str] = []
        self.process_missing = False
        self._process: ProcessIdentity | None = None
        self._broken = 0

    # -- one pass ----------------------------------------------------------
    def read(
        self,
        *,
        clock: Callable[[], float],
        started: float,
        deadline_seconds: float,
    ) -> None:
        usable = {state.source: state.measurement_usable for state in self.states}
        present = {state.source: state.present for state in self.states}
        previous_valid: float | None = None
        index = 0
        for values in csv.reader(self.lines):
            if clock() - started > deadline_seconds:
                return
            if not any(value.strip() for value in values):
                continue
            index += 1
            cell = self._cells(values)

            if present.get("ProcessId"):
                pid = _parse_int(cell.get("ProcessId"))
                if pid is not None and self._process is None:
                    self._process = _process_identity(pid, self.evidence)
            if self._process is None:
                self.process_missing = True
                return

            timestamp = _parse_float(cell.get(TIME_COLUMN)) if present.get(TIME_COLUMN) else None
            if timestamp is None:
                # An unplaceable row is reported, never emitted as a fake frame.
                self._broken += 1
                continue

            raw_interval = cell.get(INTERVAL_COLUMN) if usable.get(INTERVAL_COLUMN) else None
            interval = _parse_float(raw_interval)
            if raw_interval is not None and interval is None:
                validity = "break"
            elif interval is not None and interval <= 0:
                validity = "break"
            elif previous_valid is not None and timestamp <= previous_valid:
                validity = "break"
            else:
                validity = "valid"
            if validity == "valid":
                previous_valid = timestamp
            else:
                self._broken += 1

            event_number = _parse_int(cell.get("EventNumber"))
            self.frames.append(NormalizedFrame(
                event_index=index if event_number is None else event_number,
                timestamp=timestamp,
                time_basis=self.format.time_basis,
                process=self._process,
                swapchain_id=None,
                frame_stream_kind=self.format.frame_stream_kind,
                interval_ms=interval if validity == "valid" else None,
                frame_type=_text(cell.get("FrameType")),
                gpu_busy_ms=(
                    _parse_float(cell.get("MsGPUActive"))
                    if usable.get("MsGPUActive")
                    else None
                ),
                cpu_busy_ms=None,
                validity=validity,
            ))
            self.stream_values.append(_text(cell.get(STREAM_COLUMN)))
            self.frame_generation_values.append(_parse_bool(cell.get(FRAME_GENERATION_COLUMN)))

        if self._broken:
            self.limitations.append(
                f"{self._broken} row(s) broke the interval chain; intervals across a break"
                " are never reconnected"
            )

    def _cells(self, values: Sequence[str]) -> dict[str, str | None]:
        cell: dict[str, str | None] = {}
        for state in self.states:
            position = self.positions.get(state.source)
            if position is None or not state.present or position >= len(values):
                continue
            raw = values[position].strip()
            cell[state.source] = raw or None
        return cell


def _frame_generation(rows: _Rows) -> tuple[FieldValue[bool], list[str]]:
    state = _state(rows.states, FRAME_GENERATION_COLUMN)
    if state is None or not state.present:
        return _unknown_frame_generation(), [
            "the log does not record frame generation; the present stream is not claimed"
            " as application_present"
        ]
    values = rows.frame_generation_values
    known = [value for value in values if value is not None]
    if not known:
        return _unknown_frame_generation(), [
            "frame generation is not stated in a readable form; it stays unknown"
        ]
    if len(known) != len(values):
        return _unknown_frame_generation(), [
            "frame generation is not stated for every row; it stays unknown"
        ]
    if len(set(known)) > 1:
        return _unknown_frame_generation(), [
            "frame generation changes within the log; it is not reported as one state"
        ]
    value = known[0]
    return FieldValue[bool](
        value_state=ValueState.known,
        value=value,
        acquisition_status=AcquisitionStatus.succeeded,
        provenance=[Provenance(
            method=AcquisitionMethod.system_probe,
            observed_at=None,
            recorded_at=utc_now(),
            evidence_refs=[rows.evidence],
            note="frame generation state as recorded by the capture tool",
        )],
    ), []


def _unknown_frame_generation() -> FieldValue[bool]:
    return FieldValue[bool](
        value_state=ValueState.unknown,
        value=None,
        unknown_reason="not_provided",
        acquisition_status=AcquisitionStatus.not_attempted,
    )


def _observed_streams(rows: _Rows) -> list[StreamObservation]:
    state = _state(rows.states, STREAM_COLUMN)
    if state is None or not state.present:
        return []
    counts: dict[str, int] = {}
    for raw in rows.stream_values:
        key = raw or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return [
        StreamObservation(
            stream_key=key,
            frame_stream_kind=_STREAM_KINDS.get(key, FrameStreamKind.unknown),
            row_count=count,
        )
        for key, count in sorted(counts.items())
    ]


def _resolve_stream(
    format_: LogFormat,
    states: Sequence[_ColumnState],
    observed: Sequence[StreamObservation],
    frame_generation: FieldValue[bool],
) -> tuple[FrameStreamKind, list[str]]:
    state = _state(states, STREAM_COLUMN)
    if state is None or not state.present:
        return FrameStreamKind.unknown, [
            "the log does not state a frame stream, so the main stream is unknown"
        ]
    kinds = {item.frame_stream_kind for item in observed}
    if len(kinds) > 1:
        return FrameStreamKind.unknown, [
            "the log mixes frame streams without evidence selecting one; they are not merged"
        ]
    kind = kinds.pop() if kinds else FrameStreamKind.unknown
    if kind is FrameStreamKind.unknown:
        return FrameStreamKind.unknown, [
            "the log's frame stream value is unknown; the main stream stays unconfirmed"
        ]
    if frame_generation.value_state is not ValueState.known:
        return FrameStreamKind.unknown, [
            "frame generation is unknown, so the present stream is not claimed as"
            " application_present"
        ]
    if frame_generation.value is True:
        return FrameStreamKind.unknown, [
            "frame generation is active; the present stream mixes generated frames and is"
            " not claimed as application_present"
        ]
    if kind is not format_.frame_stream_kind:
        return kind, [
            f"the log declares stream {kind.value!r} while {format_.format_id} was verified"
            f" with {format_.frame_stream_kind.value!r}; it is reported as declared"
        ]
    return kind, []


def _continuous_intervals(frames: Sequence[NormalizedFrame]) -> list[float]:
    intervals: list[float] = []
    previous_valid = False
    for frame in frames:
        if frame.validity != "valid":
            previous_valid = False
            continue
        if previous_valid and frame.interval_ms is not None:
            intervals.append(frame.interval_ms)
        previous_valid = True
    return intervals


def _time_mapping(format_: LogFormat, states: Sequence[_ColumnState]) -> TimeMapping:
    state = _state(states, TIME_COLUMN)
    scale = 0.001 if state is not None and state.unit_ok and state.unit == "ms" else None
    return TimeMapping(
        source_basis=format_.time_basis,
        target_basis=TARGET_TIME_BASIS,
        source_unit=(state.unit if state is not None and state.unit else "unknown"),
        target_unit="s",
        # Without anchor evidence the offset is unknown: the log keeps its own
        # basis and is never attached to a historical segment on a guess.
        offset=None,
        scale=scale,
        checks=[_check(
            "time.anchor",
            CheckStatus.not_run,
            "no anchor evidence supplied; the mapping is not bindable",
        )],
    )


def _source_ranges(
    states: Sequence[_ColumnState], frames: Sequence[NormalizedFrame]
) -> list[TimeRange]:
    state = _state(states, TIME_COLUMN)
    if state is None or not state.unit_ok or state.unit != "ms":
        return []
    timestamps = [frame.timestamp for frame in frames]
    if len(timestamps) < 2 or timestamps[-1] <= timestamps[0]:
        return []
    return [TimeRange(
        start=timestamps[0],
        end=timestamps[-1],
        time_basis=TIME_BASIS,
        unit="ms",
    )]


def _quality_check_status(quality: LogQuality) -> CheckStatus:
    """A limited parse is not a proven pass and not a proven failure."""
    if quality is LogQuality.valid:
        return CheckStatus.pass_
    if quality is LogQuality.invalid:
        return CheckStatus.fail
    return CheckStatus.unknown


def _quality(
    frames: Sequence[NormalizedFrame],
    observed: Sequence[StreamObservation],
    limitations: Sequence[str],
) -> LogQuality:
    if not frames:
        return LogQuality.invalid
    if len({item.frame_stream_kind for item in observed}) > 1:
        return LogQuality.invalid
    if limitations:
        return LogQuality.limited
    return LogQuality.valid


def _budget_exhausted(
    format_: LogFormat, column_map: Sequence[ColumnMapping]
) -> LogParseResult:
    return LogParseResult(
        format_id=format_.format_id,
        source_schema=format_.format_id,
        tool_name=format_.tool_name,
        tool_version=format_.tool_version,
        column_map=list(column_map),
        time_mapping=TimeMapping(
            source_basis=format_.time_basis,
            target_basis=TARGET_TIME_BASIS,
            source_unit="unknown",
            target_unit="s",
            offset=None,
            scale=None,
            checks=[_check(
                "time.anchor",
                CheckStatus.not_run,
                "parsing stopped before any clock check could run",
            )],
        ),
        quality=LogQuality.invalid,
        frames=[],
        valid_intervals_ms=[],
        observed_streams=[],
        source_ranges=[],
        frame_stream_kind=FrameStreamKind.unknown,
        frame_generation=_unknown_frame_generation(),
        limitations=[
            "parsing exceeded the bounded time budget; the original is retained unparsed"
        ],
        checks=list(format_.checks),
    )


def _process_identity(pid: int, evidence: EvidenceRef) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        started_at=None,
        executable_path_ref=None,
        provenance=[Provenance(
            method=AcquisitionMethod.system_probe,
            observed_at=None,
            recorded_at=utc_now(),
            evidence_refs=[evidence],
            note=(
                "process id as recorded by the capture tool in the original log;"
                " start time and executable path are unknown"
            ),
        )],
    )


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    token = value.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return None


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


__all__ = [
    "DEFAULT_DEADLINE_SECONDS",
    "FormatRegistry",
    "LogColumn",
    "LogFormat",
    "LogParseResult",
    "SampleColumnFinding",
    "SampleInspection",
    "inspect_sample",
    "OFFLINE_FORMAT_ID",
    "StreamObservation",
    "default_registry",
    "parse_log",
]
