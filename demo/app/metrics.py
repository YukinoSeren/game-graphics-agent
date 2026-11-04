"""Deterministic demo metrics. Pure functions with no hidden state."""

from __future__ import annotations

import math
from typing import Iterable

UNKNOWN = None


def valid_intervals(intervals: Iterable[float]) -> list[float]:
    values = []
    for value in intervals:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            values.append(float(value))
    return values


def nearest_rank(sorted_values: list[float], percentile: float) -> float | None:
    if not sorted_values:
        return None
    rank = max(1, math.ceil(percentile * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def summarize_intervals(intervals: list[float]) -> dict:
    """Summary of one segment's valid frame intervals.

    Average present rate is total frames over total duration, not the mean of
    per-frame rates. Unknown values stay null; they never become zero.
    """
    values = valid_intervals(intervals)
    count = len(values)
    result = {
        "interval_count": count,
        "valid_duration_s": 0.0,
        "average_present_rate": None,
        "interval_p95_ms": None,
        "interval_p99_ms": None,
        "interval_median_ms": None,
        "interval_max_ms": None,
        "slowest_1pct_fps": None,
        "quality": "unknown",
        "quality_reason": "没有有效帧间隔。",
    }
    if count == 0:
        return result
    total = sum(values)
    ordered = sorted(values)
    result["valid_duration_s"] = round(total / 1000.0, 3)
    result["average_present_rate"] = round(1000.0 * count / total, 3)
    result["interval_p95_ms"] = round(nearest_rank(ordered, 0.95), 3)
    result["interval_p99_ms"] = round(nearest_rank(ordered, 0.99), 3)
    result["interval_median_ms"] = round(median(values), 3)
    result["interval_max_ms"] = round(ordered[-1], 3)
    tail = max(1, math.ceil(0.01 * count))
    if tail < 20:
        result["slowest_1pct_fps"] = None
        result["slowest_1pct_reason"] = "最慢 1% 的尾部样本少于 20 个，不展示该值。"
    else:
        mean_tail = sum(ordered[-tail:]) / tail
        result["slowest_1pct_fps"] = round(1000.0 / mean_tail, 3)
    if count < 1000 or result["valid_duration_s"] < 30:
        result["quality"] = "limited"
        result["quality_reason"] = "有效样本少于 1000 个间隔或不足 30 秒。"
    else:
        result["quality"] = "usable"
        result["quality_reason"] = "演示样例满足本地统计的最小样本条件。"
    return result


def long_frame_runs(runs: list[list[float]], threshold_ms: float) -> dict:
    """Long frames are strictly greater than the shared threshold."""
    if not isinstance(threshold_ms, (int, float)) or isinstance(threshold_ms, bool):
        raise ValueError("threshold must be a number")
    if not math.isfinite(threshold_ms) or threshold_ms <= 0:
        raise ValueError("threshold must be a positive finite number")
    clusters = 0
    long_count = 0
    long_total = 0.0
    all_intervals: list[float] = []
    cluster_details: list[dict] = []
    for run_index, run in enumerate(runs):
        current = 0
        offset = 0
        for position, value in enumerate(run):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if not math.isfinite(value) or value <= 0:
                continue
            all_intervals.append(float(value))
            if value > threshold_ms:
                if current == 0:
                    offset = position
                current += 1
                long_count += 1
                long_total += float(value)
                clusters += 1 if current == 1 else 0
            else:
                if current:
                    cluster_details.append(
                        {"run_index": run_index, "start_offset": offset, "length": current}
                    )
                current = 0
        if current:
            cluster_details.append(
                {"run_index": run_index, "start_offset": offset, "length": current}
            )
    duration_s = sum(all_intervals) / 1000.0
    return {
        "threshold_ms": float(threshold_ms),
        "cluster_count": clusters,
        "interval_count": long_count,
        "total_ms": round(long_total, 3),
        "valid_interval_count": len(all_intervals),
        "valid_total_ms": round(sum(all_intervals), 3),
        "valid_duration_s": round(duration_s, 3),
        "clusters_per_minute": round(clusters / (duration_s / 60.0), 3) if duration_s > 0 else None,
        "long_frame_time_ratio": (
            round(long_total / sum(all_intervals), 4) if all_intervals else None
        ),
        "clusters": cluster_details,
    }


def adaptive_threshold(intervals: list[float]) -> float | None:
    values = valid_intervals(intervals)
    middle = median(values)
    if middle is None:
        return None
    return round(max(50.0, 2.0 * middle), 3)


def summarize(clean) -> dict:
    return summarize_intervals(clean.intervals)


def long_frames(clean, threshold_ms: float) -> dict:
    return long_frame_runs(clean.runs, threshold_ms)
