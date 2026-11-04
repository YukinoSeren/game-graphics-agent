"""Demo comparison protocols and scoped before/after differences."""

from __future__ import annotations

import math

from .contracts import DemoError, DomainResult
from .metrics import adaptive_threshold, long_frames, median, valid_intervals
from .service import require_fields
from .traces import clean_trace, parse_trace

METRIC_UNITS = {
    "average_present_rate": "fps",
    "interval_p95_ms": "ms",
    "interval_p99_ms": "ms",
    "slowest_1pct_fps": "fps",
}


def register(service) -> None:
    service.register("comparison.freeze", validate_freeze, handle_freeze)
    service.register("comparison.run", validate_run, handle_run)


def validate_freeze(payload):
    require_fields(
        payload,
        required=("mode", "baseline_summary_ids"),
        allowed=("mode", "threshold_ms", "baseline_summary_ids", "exploratory"),
    )
    if payload["mode"] not in ("fixed", "baseline_derived"):
        raise DemoError("invalid_mode", "比较口径必须是 fixed 或 baseline_derived。")
    ids = payload["baseline_summary_ids"]
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise DemoError("invalid_baseline", "baseline_summary_ids 必须是列表。")
    if len(set(ids)) != len(ids):
        raise DemoError("invalid_baseline", "基线摘要不能重复。")
    if payload["mode"] == "baseline_derived" and not ids:
        raise DemoError("invalid_baseline", "派生阈值需要至少一个基线摘要。")
    threshold = payload.get("threshold_ms")
    if threshold is not None:
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not math.isfinite(threshold)
            or threshold <= 0
        ):
            raise DemoError("invalid_threshold", "长帧阈值必须是正数。")
    if not isinstance(payload.get("exploratory", False), bool):
        raise DemoError("invalid_exploratory", "exploratory 必须是布尔值。")
    return payload


def validate_run(payload):
    return require_fields(
        payload,
        required=("protocol_id", "before_summary_id", "after_summary_id"),
        allowed=("protocol_id", "before_summary_id", "after_summary_id"),
    )


def _load_clean(ctx, summary):
    try:
        artifact = ctx.tx.get(summary["artifact_id"], kind="LogArtifact")
        data = ctx.tx.file_bytes(
            artifact["file_id"], expected_hash=artifact["content_hash"]
        )
        return clean_trace(parse_trace(data, scenario_id=ctx.scenario_id))
    except DemoError:
        return None


def handle_freeze(ctx, payload):
    summaries = [
        ctx.tx.get(summary_id, kind="Summary")
        for summary_id in payload["baseline_summary_ids"]
    ]
    if payload["mode"] == "fixed":
        threshold = payload.get("threshold_ms")
        threshold = 50.0 if threshold is None else float(threshold)
        source = "fixed"
    else:
        intervals: list[float] = []
        for summary in summaries:
            clean = _load_clean(ctx, summary)
            if clean is None:
                raise DemoError(
                    "artifact_unavailable",
                    "基线原件不可用，无法派生共同阈值。",
                    status=409,
                    recovery_actions=["reload_sample"],
                )
            intervals.extend(clean.intervals)
        threshold = adaptive_threshold(intervals)
        if threshold is None:
            raise DemoError("invalid_baseline", "基线没有有效帧间隔。")
        source = "baseline_derived"
    protocol = ctx.tx.insert(
        "ComparisonProtocol",
        {
            "mode": payload["mode"],
            "threshold_source": source,
            "threshold_ms": threshold,
            "baseline_summary_ids": payload["baseline_summary_ids"],
            "exploratory": bool(payload.get("exploratory", False)),
            "created_seq": ctx.tx.latest_seq(),
            "status": "frozen",
            "note": "比较口径创建后保持不变；需要新的比较条件时请另行创建。",
        },
        origin="user_declared",
    )
    ctx.tx.emit(
        "comparison.frozen",
        {"protocol_id": protocol["id"], "threshold_ms": threshold},
    )
    return DomainResult(201, [{"kind": "ComparisonProtocol", "id": protocol["id"]}])


def _metric_rows(before, after):
    rows = []
    for key, unit in METRIC_UNITS.items():
        before_value = (before.get("metrics") or {}).get(key)
        after_value = (after.get("metrics") or {}).get(key)
        delta = None
        ratio = None
        if before_value is not None and after_value is not None:
            delta = round(after_value - before_value, 3)
            if before_value != 0:
                ratio = round((after_value - before_value) / before_value, 4)
        rows.append(
            {
                "key": key,
                "unit": unit,
                "before": before_value,
                "after": after_value,
                "delta": delta,
                "delta_ratio": ratio,
            }
        )
    return rows


def handle_run(ctx, payload):
    protocol = ctx.tx.get(payload["protocol_id"], kind="ComparisonProtocol")
    before = ctx.tx.get(payload["before_summary_id"], kind="Summary")
    after = ctx.tx.get(payload["after_summary_id"], kind="Summary")
    comparison = build_comparison(ctx, protocol, before, after)
    ctx.tx.emit(
        "comparison.run",
        {
            "comparison_id": comparison["id"],
            "threshold_ms": comparison["threshold_ms"],
            "comparable": comparison["comparable"],
        },
    )
    return DomainResult(201, [{"kind": "Comparison", "id": comparison["id"]}])


def _session_conditions(ctx, summary):
    session = ctx.tx.get(summary["session_id"], kind="Session")
    run_conditions = session.get("run_conditions") or {}
    plan_conditions = {}
    plan_id = session.get("plan_id")
    if plan_id:
        plan_conditions = ctx.tx.get(plan_id, kind="TestPlan").get("conditions") or {}
    return {"run": run_conditions, "plan": plan_conditions}


def _check_run_conditions(ctx, before, after, notes):
    before_conditions = _session_conditions(ctx, before)
    after_conditions = _session_conditions(ctx, after)
    declared = (
        bool(before_conditions["run"])
        or bool(after_conditions["run"])
        or bool(before_conditions["plan"])
        or bool(after_conditions["plan"])
    )
    if not declared:
        notes.append("两次会话都没有声明运行条件，无法确认受控比较。")
        return False
    if (
        before_conditions["run"] != after_conditions["run"]
        or before_conditions["plan"] != after_conditions["plan"]
    ):
        notes.append("两次会话声明的运行条件不同，不能作为受控比较。")
        return False
    return True


def build_comparison(ctx, protocol, before, after, *, origin="simulated"):
    if before["id"] == after["id"]:
        raise DemoError("invalid_comparison", "前后摘要不能是同一条。", status=409)
    before_binding = ctx.tx.get(before["binding_id"], kind="Binding")
    after_binding = ctx.tx.get(after["binding_id"], kind="Binding")

    notes = []
    comparable = True
    exploratory = bool(protocol.get("exploratory"))
    if before_binding["context_id"] != after_binding["context_id"]:
        comparable = False
        notes.append("两次摘要背景修订不同，不能作为受控比较。")
    if before_binding["applied_id"] is None or after_binding["applied_id"] is None:
        comparable = False
        notes.append("存在配置未确认的区段，只能描述差异。")
    if not _check_run_conditions(ctx, before, after, notes):
        comparable = False
    if before["data_origin"] != "simulated" or after["data_origin"] != "simulated":
        exploratory = True
        notes.append("上传文件缺少可核验的采样先后依据，仅作探索性差异。")
    after_artifact = ctx.tx.get(after["artifact_id"], kind="LogArtifact")
    sampled_seq = after_artifact.get("created_seq")
    planned = (
        isinstance(sampled_seq, int)
        and not isinstance(sampled_seq, bool)
        and protocol.get("created_seq", 0) < sampled_seq
        and protocol.get("created_seq", 0) < after.get("created_seq", 0)
    )
    if not planned:
        exploratory = True
        notes.append("比较口径在样本之后建立，不能视为预先计划。")
    if exploratory:
        comparable = False

    before_clean = _load_clean(ctx, before)
    after_clean = _load_clean(ctx, after)
    threshold = protocol["threshold_ms"]
    before_long = after_long = None
    if before_clean is None or after_clean is None:
        notes.append("原件不可用，共同阈值重算不可用；仅保留已保存摘要的描述性数值。")
    else:
        before_long = long_frames(before_clean, threshold)
        after_long = long_frames(after_clean, threshold)
    before_config = (
        ctx.tx.get(before_binding["applied_id"], kind="AppliedConfiguration")
        if before_binding["applied_id"]
        else None
    )
    after_config = (
        ctx.tx.get(after_binding["applied_id"], kind="AppliedConfiguration")
        if after_binding["applied_id"]
        else None
    )
    settings_changed = []
    if before_config and after_config:
        settings_changed = sorted(
            key
            for key in set(before_config["settings"]) | set(after_config["settings"])
            if before_config["settings"].get(key) != after_config["settings"].get(key)
        )
    comparison = ctx.tx.insert(
        "Comparison",
        {
            "protocol_id": protocol["id"],
            "before_summary_id": before["id"],
            "after_summary_id": after["id"],
            "threshold_ms": threshold,
            "threshold_source": protocol.get("threshold_source"),
            "before_long_frames": before_long,
            "after_long_frames": after_long,
            "metrics": _metric_rows(before, after),
            "settings_changed": settings_changed,
            "comparable": comparable,
            "exploratory": exploratory,
            "comparability_notes": notes,
            "causality": "not_established",
            "time_base": "simulated",
            "before_source_hash": before["source_hash"],
            "after_source_hash": after["source_hash"],
        },
        origin=origin,
    )
    return comparison
