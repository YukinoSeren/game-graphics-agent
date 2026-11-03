"""Deterministic frame cleaning and the project's own statistics (04 §6).

Pure functions only: frames arrive already parsed, nothing here touches the
repository, the clock, an adapter or a model.  The same helpers serve both the
analysis summary (segment-adaptive thresholds) and the comparison (one frozen
common threshold), so the two can never drift apart.

Cleaning never invents an interval.  A bad value, a duplicate or backwards
timestamp, a missing interval or a stated interval that disagrees with its own
timestamps all *break* the chain: the next interval starts a new continuous
run and nothing is reconnected across the break.  Real spikes are never
dropped as outliers; only an explicitly declared exclusion range with a reason
and evidence removes intervals, and an unexplained exclusion is ignored (and
reported) rather than applied.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from game_app.contracts.performance import (
    ColumnMapping,
    ExcludedRange,
    NormalizedFrame,
    TimeMapping,
)
from game_app.contracts.primitives import TimeRange

ANALYZER_VERSION = "perf-analyzer/1"
CLEANING_PROTOCOL_VERSION = "perf-cleaning/1"

ADAPTIVE_FLOOR_MS = 50.0
MIN_EFFECTIVE_SECONDS = 30.0
MIN_VALID_INTERVALS = 1000
TAIL_SAMPLE_FLOOR = 20

# The stated interval and the timestamp difference are the same measurement in
# two forms; a difference beyond float noise means the columns disagree.
_ENDPOINT_TOLERANCE_MS = 1e-6


@dataclass(frozen=True)
class ContinuousRun:
    """One run of adjacent valid intervals inside a single segment/binding."""

    start_s: float
    end_s: float
    intervals: tuple[float, ...]
    first_event_index: int
    last_event_index: int


@dataclass(frozen=True)
class Cleaning:
    runs: tuple[ContinuousRun, ...]
    breaks: tuple[dict[str, Any], ...]
    exclusions_applied: tuple[TimeRange, ...]
    exclusions_ignored: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...]

    @property
    def intervals(self) -> tuple[float, ...]:
        return tuple(
            interval for run in self.runs for interval in run.intervals
        )

    @property
    def valid_interval_count(self) -> int:
        return sum(len(run.intervals) for run in self.runs)

    @property
    def effective_duration_s(self) -> float:
        return sum(self.intervals) / 1000.0


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def map_time(timestamp: float, mapping: TimeMapping) -> float | None:
    """Source seconds/ms to session seconds; unknown mapping maps nothing."""
    if mapping.scale is None or mapping.offset is None:
        return None
    if not _finite(timestamp):
        return None
    mapped = timestamp * mapping.scale + mapping.offset
    return mapped if math.isfinite(mapped) else None


def _group_key(frame: NormalizedFrame) -> tuple[Any, ...]:
    process = frame.process
    return (
        process.pid,
        process.started_at,
        process.executable_path_ref,
        frame.swapchain_id,
        frame.frame_stream_kind.value,
    )


def _usable_exclusions(
    excluded_ranges: Sequence[ExcludedRange],
    limitations: list[str],
    ignored: list[dict[str, Any]],
) -> list[TimeRange]:
    """Only a declared, justified and evidenced range may remove intervals."""
    usable: list[TimeRange] = []
    for item in excluded_ranges:
        self_contained = (
            item.range.time_basis and item.range.unit in {"s", "ms"}
        )
        if (
            not item.reason.strip()
            or not item.evidence_refs
            or not self_contained
        ):
            ignored.append({
                "start": item.range.start,
                "end": item.range.end,
                "reason": item.reason,
            })
            limitations.append(
                "an exclusion range was not applied: it needs a stated reason,"
                " evidence and a comparable time basis"
            )
            continue
        usable.append(item.range)
    return usable


def _to_session_range(window: TimeRange, unit: str) -> tuple[float, float]:
    scale = 1.0 if unit == "s" else 0.001
    return window.start * scale, window.end * scale


def clean_frames(
    frames: Sequence[NormalizedFrame],
    *,
    time_mapping: TimeMapping,
    window: TimeRange,
    excluded_ranges: Sequence[ExcludedRange] = (),
) -> Cleaning:
    """Keep only adjacent valid intervals inside the window, split on breaks."""
    limitations: list[str] = []
    ignored: list[dict[str, Any]] = []
    exclusions = _usable_exclusions(excluded_ranges, limitations, ignored)
    lower, upper = _to_session_range(window, window.unit)
    exclusion_windows = [_to_session_range(item, item.unit) for item in exclusions]

    runs: list[ContinuousRun] = []
    breaks: list[dict[str, Any]] = []
    current: list[float] | None = None
    current_start: float | None = None
    current_first = 0
    current_key: tuple[Any, ...] | None = None
    previous: dict[tuple[Any, ...], tuple[float, NormalizedFrame] | None] = {}

    def close() -> None:
        nonlocal current, current_start, current_first, current_key
        if current:
            runs.append(ContinuousRun(
                start_s=float(current_start or 0.0),
                end_s=float(previous_end[0]),
                intervals=tuple(current),
                first_event_index=current_first,
                last_event_index=int(previous_end[1]),
            ))
        current = None
        current_start = None
        current_first = 0
        current_key = None

    # ``previous_end`` is the accepted interval's end time and event index.
    previous_end: list[Any] = [0.0, 0]

    for frame in frames:
        key = _group_key(frame)
        # Process, swapchain and stream are separate series: a run never spans
        # two of them.
        if current is not None and key != current_key:
            close()
        mapped = map_time(frame.timestamp, time_mapping)
        prior = previous.get(key)

        if frame.validity != "valid":
            close()
            # A break ends the chain: the next valid frame starts a new run
            # instead of reconnecting across the bad value.
            previous[key] = None
            breaks.append({
                "event_index": frame.event_index,
                "reason": "the frame is not a valid point in the interval chain",
            })
            continue
        if mapped is None:
            close()
            previous[key] = None
            breaks.append({
                "event_index": frame.event_index,
                "reason": "the frame has no interpretable session time",
            })
            continue
        if not (lower <= mapped <= upper):
            # Outside this binding: the chain cannot continue through it.
            close()
            previous[key] = None
            continue
        if prior is None:
            # The first valid frame of a run has no preceding interval; it is a
            # legitimate chain start even when its own interval column is blank.
            previous[key] = (mapped, frame)
            continue
        if not _finite(frame.interval_ms):
            close()
            previous[key] = None
            breaks.append({
                "event_index": frame.event_index,
                "reason": "the interval between adjacent valid frames is unknown",
            })
            continue
        prior_time, prior_frame = prior
        delta_ms = (mapped - prior_time) * 1000.0
        if delta_ms <= 0 or not math.isfinite(delta_ms):
            close()
            previous[key] = None
            breaks.append({
                "event_index": frame.event_index,
                "reason": "the timestamp does not advance",
            })
            continue
        if abs(frame.interval_ms - delta_ms) > _ENDPOINT_TOLERANCE_MS:
            close()
            previous[key] = None
            breaks.append({
                "event_index": frame.event_index,
                "reason": "the stated interval disagrees with its endpoints",
            })
            continue
        start_s = prior_time
        end_s = mapped
        excluded = any(
            start_s < ex_end and end_s > ex_start
            for ex_start, ex_end in exclusion_windows
        )
        if excluded:
            close()
            previous[key] = None
            breaks.append({
                "event_index": frame.event_index,
                "reason": "the interval lies inside a declared exclusion range",
            })
            continue
        if current is None:
            current = []
            current_start = start_s
            current_first = prior_frame.event_index
            current_key = key
        current.append(float(frame.interval_ms))
        previous_end[0] = end_s
        previous_end[1] = frame.event_index
        previous[key] = (mapped, frame)

    close()
    if not runs:
        limitations.append("no adjacent valid interval survives cleaning")
    return Cleaning(
        runs=tuple(runs),
        breaks=tuple(breaks),
        exclusions_applied=tuple(exclusions),
        exclusions_ignored=tuple(ignored),
        limitations=tuple(limitations),
    )


# --- statistics --------------------------------------------------------------


def mean_present_rate(intervals: Sequence[float]) -> float | None:
    """``F = 1000*m / sum(d)`` (04 §6 step 3); no intervals means unknown."""
    if not intervals:
        return None
    total = sum(intervals)
    if total <= 0:
        return None
    return 1000.0 * len(intervals) / total


def percentile_nearest_rank(intervals: Sequence[float], fraction: float) -> float | None:
    """Ascending item ``ceil(p*m)`` (04 §6 step 4); nearest rank, no interpolation."""
    if not intervals:
        return None
    ordered = sorted(intervals)
    rank = math.ceil(fraction * len(ordered))
    if rank < 1:
        rank = 1
    return ordered[rank - 1]


def slowest_tail_inverse_mean(
    intervals: Sequence[float], fraction: float = 0.01
) -> float | None:
    """Reciprocal of the mean of the largest ``ceil(fraction*m)`` intervals.

    This is the algorithm's raw value.  The project's display gate (the tail
    needs at least 20 samples) is applied separately by
    :func:`tail_sample_count`.
    """
    if not intervals:
        return None
    count = tail_sample_count(intervals, fraction)
    if count < 1:
        return None
    ordered = sorted(intervals, reverse=True)
    mean = sum(ordered[:count]) / count
    if mean <= 0:
        return None
    return 1000.0 / mean


def tail_sample_count(intervals: Sequence[float], fraction: float = 0.01) -> int:
    return math.ceil(fraction * len(intervals))


def adaptive_threshold_ms(intervals: Sequence[float]) -> float | None:
    """``max(50ms, 2 × the run's median interval)`` (original 04 §4)."""
    if not intervals:
        return None
    return max(ADAPTIVE_FLOOR_MS, 2.0 * statistics.median(intervals))


def long_frame_statistics(
    intervals: Sequence[float], threshold_ms: float
) -> tuple[int, int, float]:
    """Clusters, long-interval count and full duration for one threshold.

    Adjacent intervals above the threshold are one cluster; a normal interval
    (``d <= threshold``) or the end of the run closes it.  Equal-to-threshold is
    not a long frame.
    """
    clusters = 0
    count = 0
    duration = 0.0
    inside = False
    for interval in intervals:
        if interval > threshold_ms:
            count += 1
            duration += interval
            if not inside:
                clusters += 1
                inside = True
        else:
            inside = False
    return clusters, count, duration


def quality_for_sample(valid_interval_count: int, effective_duration_s: float) -> str:
    """The project's debug default: valid only past 30s and 1000 intervals."""
    if valid_interval_count <= 0 or effective_duration_s <= 0:
        return "invalid"
    if (
        effective_duration_s < MIN_EFFECTIVE_SECONDS
        or valid_interval_count < MIN_VALID_INTERVALS
    ):
        return "limited"
    return "valid"


def cleaning_manifest(
    cleaning: Cleaning,
    *,
    mapping: TimeMapping,
    column_map: Sequence[ColumnMapping],
) -> dict[str, Any]:
    """Deterministic bytes prepared outside the write transaction."""
    return {
        "analyzer_version": ANALYZER_VERSION,
        "cleaning_protocol_version": CLEANING_PROTOCOL_VERSION,
        "time_mapping": {
            "source_basis": mapping.source_basis,
            "target_basis": mapping.target_basis,
            "source_unit": mapping.source_unit,
            "target_unit": mapping.target_unit,
            "offset": mapping.offset,
            "scale": mapping.scale,
        },
        "column_map": [item.model_dump(mode="json") for item in column_map],
        "runs": [
            {
                "start_s": run.start_s,
                "end_s": run.end_s,
                "interval_count": len(run.intervals),
                "sum_ms": sum(run.intervals),
                "first_event_index": run.first_event_index,
                "last_event_index": run.last_event_index,
            }
            for run in cleaning.runs
        ],
        "breaks": [dict(item) for item in cleaning.breaks],
        "exclusions_applied": [
            item.model_dump(mode="json") for item in cleaning.exclusions_applied
        ],
        "exclusions_ignored": [dict(item) for item in cleaning.exclusions_ignored],
        "valid_interval_count": cleaning.valid_interval_count,
        "effective_duration_s": cleaning.effective_duration_s,
    }


__all__ = [
    "ANALYZER_VERSION",
    "ADAPTIVE_FLOOR_MS",
    "CLEANING_PROTOCOL_VERSION",
    "MIN_EFFECTIVE_SECONDS",
    "MIN_VALID_INTERVALS",
    "TAIL_SAMPLE_FLOOR",
    "Cleaning",
    "ContinuousRun",
    "adaptive_threshold_ms",
    "clean_frames",
    "cleaning_manifest",
    "long_frame_statistics",
    "map_time",
    "mean_present_rate",
    "percentile_nearest_rank",
    "quality_for_sample",
    "slowest_tail_inverse_mean",
    "tail_sample_count",
]
