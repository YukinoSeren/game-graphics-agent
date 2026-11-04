"""Deterministic demo frame traces: generation, parsing, and cleaning."""

from __future__ import annotations

import csv
import hashlib
import io
import math
from dataclasses import dataclass, field

from .contracts import DemoError, JsonObject

HEADER = [
    "demo_only",
    "scenario_id",
    "trace_id",
    "frame_index",
    "timestamp_ms",
    "interval_ms",
    "frame_stream",
]
FRAME_STREAM = "application_present"
TIME_TOLERANCE_MS = 0.001


@dataclass
class ParsedTrace:
    scenario_id: str
    trace_id: str
    frames: list[JsonObject] = field(default_factory=list)
    runs: list[list[float]] = field(default_factory=list)
    issues: list[JsonObject] = field(default_factory=list)
    content_hash: str = ""
    declared_demo_only: bool = True


@dataclass
class CleanTrace:
    scenario_id: str
    trace_id: str
    runs: list[list[float]]
    intervals: list[float]
    issues: list[JsonObject]
    exclusions: list[JsonObject]
    content_hash: str

    @property
    def total_ms(self) -> float:
        return round(sum(self.intervals), 3)


def _interval_us(recipe: JsonObject, index: int) -> int:
    if recipe.get("spike_every") and index % int(recipe["spike_every"]) == 0:
        return int(recipe["spike_us"])
    jitter = int(recipe["jitter_us"])
    span = 2 * jitter + 1
    value = int(recipe["base_interval_us"]) + ((index * 137) % span) - jitter
    return max(1000, value)


def generate_trace(scenario_id: str, label: str, recipe: JsonObject) -> bytes:
    """Build a fixed demo CSV. Same recipe always produces the same bytes."""
    frames = int(recipe["frames"])
    trace_id = f"demo_trace_{label}"
    lines = [",".join(HEADER)]
    total_us = 0
    for index in range(frames):
        if index == 0:
            lines.append(
                f"true,{scenario_id},{trace_id},0,0.000,,{FRAME_STREAM}"
            )
            continue
        total_us += _interval_us(recipe, index)
        lines.append(
            "true,{scenario},{trace},{index},{timestamp:.3f},{interval:.3f},{stream}".format(
                scenario=scenario_id,
                trace=trace_id,
                index=index,
                timestamp=total_us / 1000.0,
                interval=_interval_us(recipe, index) / 1000.0,
                stream=FRAME_STREAM,
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_trace(data: bytes, *, scenario_id: str) -> ParsedTrace:
    if not isinstance(data, (bytes, bytearray)):
        raise DemoError("unsupported_format", "请选择 CSV 格式的性能记录。")
    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DemoError("unsupported_format", "CSV 文件不是有效的 UTF-8 文本。") from exc
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise DemoError("unsupported_format", "CSV 文件为空。")
    header = [cell.strip() for cell in rows[0]]
    if header != HEADER:
        raise DemoError(
            "unsupported_format",
            "CSV 列格式不受支持，请使用性能记录导出格式。",
        )
    parsed = ParsedTrace(
        scenario_id=scenario_id,
        trace_id="",
        content_hash=hashlib.sha256(bytes(data)).hexdigest(),
    )
    parsed.runs = [[]]
    previous_timestamp = None
    previous_interval = None
    seen_index = -1

    def break_run(line: int, problem: str) -> None:
        nonlocal previous_timestamp, previous_interval
        parsed.issues.append({"line": line, "problem": problem})
        if parsed.runs and parsed.runs[-1]:
            parsed.runs.append([])
        previous_timestamp = None
        previous_interval = None

    for line_number, row in enumerate(rows[1:], start=2):
        if not row or all(not cell.strip() for cell in row):
            break_run(line_number, "blank_line")
            continue
        if len(row) != 7:
            break_run(line_number, "wrong_column_count")
            continue
        demo, scen, trace_id, index_text, timestamp_text, interval_text, stream = (
            cell.strip() for cell in row
        )
        if demo.lower() != "true":
            raise DemoError("invalid_trace", "性能记录缺少必要的来源标记。")
        if scen != scenario_id:
            raise DemoError(
                "cross_case_trace",
                "该 CSV 属于其他游戏，不能与当前记录混用。",
                status=409,
            )
        if stream != FRAME_STREAM:
            raise DemoError(
                "unsupported_stream",
                "仅支持应用呈现帧时间数据。",
            )
        if not trace_id:
            break_run(line_number, "missing_trace_id")
            continue
        if not parsed.trace_id:
            parsed.trace_id = trace_id
        elif trace_id != parsed.trace_id:
            break_run(line_number, "trace_id_changed")
            continue
        try:
            frame_index = int(index_text)
        except ValueError:
            break_run(line_number, "bad_frame_index")
            continue
        if frame_index != seen_index + 1:
            break_run(line_number, "frame_index_gap")
        seen_index = frame_index
        try:
            timestamp = float(timestamp_text)
        except ValueError:
            break_run(line_number, "bad_timestamp")
            continue
        if not math.isfinite(timestamp) or timestamp < 0:
            break_run(line_number, "bad_timestamp")
            continue
        interval = None
        if interval_text:
            try:
                interval = float(interval_text)
            except ValueError:
                break_run(line_number, "bad_interval")
                continue
            if not math.isfinite(interval) or interval <= 0:
                break_run(line_number, "bad_interval")
                continue
        if previous_timestamp is not None:
            if interval is None:
                break_run(line_number, "missing_interval")
                continue
            expected = previous_timestamp + interval
            if abs(timestamp - expected) > TIME_TOLERANCE_MS:
                # The interval cannot be verified against the timestamps, so it
                # must not be counted in the new run.
                break_run(line_number, "timebase_break")
                continue
        elif frame_index == 0 and interval is not None:
            break_run(line_number, "first_interval_not_empty")
            continue
        if frame_index > 0 and interval is None:
            break_run(line_number, "missing_interval")
            continue
        run_index = len(parsed.runs) - 1
        parsed.frames.append(
            {
                "frame_index": frame_index,
                "timestamp_ms": timestamp,
                "interval_ms": interval,
                "run_index": run_index,
            }
        )
        if interval is not None:
            parsed.runs[-1].append(interval)
        previous_timestamp = timestamp
    return parsed


def clean_trace(
    parsed: ParsedTrace, *, exclusions: list[JsonObject] | None = None
) -> CleanTrace:
    clean_exclusions = []
    excluded_frames: set[int] = set()
    for item in exclusions or []:
        if not isinstance(item, dict):
            raise DemoError("invalid_exclusion", "排除区间必须是对象。")
        unknown = set(item) - {"start_frame", "end_frame", "reason"}
        if unknown:
            raise DemoError("invalid_exclusion", "排除区间包含未知字段。")
        start = item.get("start_frame")
        end = item.get("end_frame")
        for value in (start, end):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise DemoError("invalid_exclusion", "排除区间需要非负整数帧范围。")
        if end < start:
            raise DemoError("invalid_exclusion", "排除区间结束帧不能小于开始帧。")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise DemoError("invalid_exclusion", "排除区间需要说明原因。")
        clean_exclusions.append(
            {"start_frame": start, "end_frame": end, "reason": item["reason"]}
        )
        excluded_frames.update(range(start, end + 1))

    runs: list[list[float]] = []
    current: list[float] = []
    current_run = None
    previous_index = None
    for frame in parsed.frames:
        if frame["frame_index"] in excluded_frames:
            continue
        if (
            frame["run_index"] != current_run
            or (previous_index is not None and frame["frame_index"] != previous_index + 1)
        ):
            if current:
                runs.append(current)
            current = []
            current_run = frame["run_index"]
        previous_index = frame["frame_index"]
        if frame["interval_ms"] is not None:
            current.append(frame["interval_ms"])
    if current:
        runs.append(current)
    intervals = [value for run in runs for value in run]
    return CleanTrace(
        scenario_id=parsed.scenario_id,
        trace_id=parsed.trace_id,
        runs=runs,
        intervals=intervals,
        issues=list(parsed.issues),
        exclusions=clean_exclusions,
        content_hash=parsed.content_hash,
    )
