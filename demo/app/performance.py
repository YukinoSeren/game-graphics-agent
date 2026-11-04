"""Demo performance flow: simulated capture, log association, and analysis."""

from __future__ import annotations

from .contracts import DemoError, DomainResult
from .metrics import adaptive_threshold, long_frames, summarize
from .service import require_fields
from .sessions import require_closed
from .traces import clean_trace, generate_trace, parse_trace

CAPTURE_PHASES = [
    {"key": "wait", "label": "模拟采集等待", "seconds": 12.0},
    {"key": "finalize", "label": "整理样例原件", "seconds": 0.5},
]
LABELS = ("baseline", "recommended", "adjusted", "no_gain", "short")
CHART_LIMIT = 240


def register(service) -> None:
    service.register("capture.start", validate_capture_start, handle_capture_start)
    service.register("capture.stop", validate_task_only, handle_capture_stop)
    service.register("log.inspect", validate_file_only, handle_inspect)
    service.register("log.associate", validate_associate, handle_associate)
    service.register("performance.analyze", validate_analyze, handle_analyze)
    service.tasks.register("capture", CAPTURE_PHASES, _capture_publisher(service))


# -- validation -------------------------------------------------------
def validate_capture_start(payload):
    require_fields(
        payload,
        required=(
            "session_id",
            "segment_id",
            "expected_segment_revision",
            "expected_binding_revision",
        ),
        allowed=(
            "session_id",
            "segment_id",
            "expected_segment_revision",
            "expected_binding_revision",
            "label",
        ),
    )
    label = payload.get("label", "recommended")
    if label not in LABELS:
        raise DemoError("invalid_label", "演示样例类别不受支持。")
    return payload


def validate_task_only(payload):
    return require_fields(payload, required=("task_id",), allowed=("task_id",))


def validate_file_only(payload):
    return require_fields(payload, required=("file_id",), allowed=("file_id",))


def validate_associate(payload):
    require_fields(
        payload,
        required=("file_id",),
        allowed=(
            "file_id",
            "segment_id",
            "segment_revision",
            "binding_revision",
            "session_id",
            "historical",
            "declared_start",
            "basis",
        ),
    )
    if payload.get("historical"):
        for key in ("session_id", "declared_start", "basis"):
            if not payload.get(key):
                raise DemoError(
                    "missing_field",
                    f"历史关联需要 {key}，不能用上传时刻代替。",
                )
    else:
        for key in ("segment_id", "segment_revision", "binding_revision"):
            if key not in payload:
                raise DemoError("missing_field", f"缺少 {key}。")
    return payload


def validate_analyze(payload):
    return require_fields(
        payload,
        required=("artifact_id", "segment_id", "segment_revision", "binding_revision"),
        allowed=("artifact_id", "segment_id", "segment_revision", "binding_revision"),
    )


# -- helpers ----------------------------------------------------------
def _open_capture(tx, segment_id):
    for task in tx.active_tasks():
        if task["kind"] == "capture" and task["input"].get("segment_id") == segment_id:
            return task
    return None


def _close_segment(tx, segment):
    elapsed = round(max(0.0, tx.clock.monotonic() - segment.get("start_monotonic", 0.0)), 3)
    return tx.update_entity(
        segment["id"],
        {"status": "closed", "end_s": elapsed, "ended_at": tx.clock.now_iso()},
        revision=segment["revision"] + 1,
    )


def _artifact_from_bytes(
    service, tx, *, data, label, session_id, segment_id, wait_s, origin, entity_origin=None
):
    parsed = parse_trace(data, scenario_id=tx.scope.scenario_id)
    if not parsed.trace_id:
        raise DemoError("invalid_trace", "演示 CSV 没有可识别的 trace_id。")
    staged = service.files.stage(data, "text/csv")
    try:
        record = service.files.register(tx, staged, f"{label}.csv")
    except Exception:
        service.files.discard(staged)
        raise
    duration = round(sum(sum(run) for run in parsed.runs) / 1000.0, 3)
    artifact = tx.insert(
        "LogArtifact",
        {
            "file_id": record["id"],
            "filename_label": record["filename_label"],
            "content_hash": record["content_hash"],
            "size": record["size"],
            "trace_id": parsed.trace_id,
            "label": label,
            "source": origin,
            "time_base": "simulated",
            "frame_count": len(parsed.frames),
            "declared_duration_s": duration,
            "capture_wait_s": wait_s,
            "session_id": session_id,
            "segment_id": segment_id,
            "frame_stream": "application_present",
            "created_seq": tx.latest_seq(),
        },
        origin=entity_origin
        or ("simulated" if origin == "capture" else "uploaded_demo_file"),
    )
    return artifact, parsed


def _capture_publisher(service):
    def publish(tx, task, phase):
        input_data = task["input"]
        if phase["key"] == "wait":
            return {"detail": {"waiting": True}}
        segment = tx.get(input_data["segment_id"], kind="Segment")
        if segment["status"] != "open":
            return {
                "status": "failed",
                "result": {"reason": "segment_closed"},
                "detail": {"reason": "segment_closed"},
            }
        recipe = service.catalog.recipe(tx.scope.scenario_id, input_data["label"])
        data = generate_trace(tx.scope.scenario_id, input_data["label"], recipe)
        artifact, _ = _artifact_from_bytes(
            service,
            tx,
            data=data,
            label=input_data["label"],
            session_id=input_data["session_id"],
            segment_id=segment["id"],
            wait_s=CAPTURE_PHASES[0]["seconds"],
            origin="capture",
        )
        closed = _close_segment(tx, segment)
        session = tx.get(input_data["session_id"], kind="Session")
        tx.update_entity(
            session["id"],
            {"active_segment_id": None},
            revision=session["revision"],
        )
        return {
            "status": "completed",
            "result": {
                "artifact_id": artifact["id"],
                "segment_id": closed["id"],
                "capture_wait_s": artifact["capture_wait_s"],
            },
            "detail": {"artifact_id": artifact["id"]},
        }

    return publish


def _chart(clean, threshold):
    points = []
    time_ms = 0.0
    for run_index, run in enumerate(clean.runs):
        for position, value in enumerate(run):
            time_ms += value
            point = {
                "t_s": round(time_ms / 1000.0, 3),
                "ms": round(value, 3),
            }
            if run_index > 0 and position == 0:
                # A timebase break starts a new run; keep that boundary visible.
                point["break"] = True
            points.append(point)
    if threshold is not None:
        for point in points:
            point["long"] = point["ms"] > threshold
    downsampled = len(points) > CHART_LIMIT
    if downsampled:
        # Downsample over the whole time range: every bucket keeps its peak
        # and its last point, so the curve still covers the full duration and
        # no spike is silently cut off by a prefix truncation.
        kept: list[dict] = []
        bucket_size = len(points) / CHART_LIMIT
        for bucket in range(CHART_LIMIT):
            start = int(bucket * bucket_size)
            end = int((bucket + 1) * bucket_size)
            if end <= start:
                continue
            window = points[start:end]
            peak = max(window, key=lambda point: point["ms"])
            last = window[-1]
            if peak not in kept:
                kept.append(peak)
            if last not in kept:
                kept.append(last)
        for point in points:
            if point.get("break") and point not in kept:
                kept.append(point)
        if points[0] not in kept:
            kept.insert(0, points[0])
        if points[-1] not in kept:
            kept.append(points[-1])
        kept.sort(key=lambda point: point["t_s"])
        points = kept
    return {
        "source_hash": clean.content_hash,
        "threshold_ms": threshold,
        "points": points,
        "point_count": len(points),
        "downsampled": downsampled,
        "bucket_count": CHART_LIMIT if downsampled else len(points),
        "covers_full_range": True,
        "preserves_peaks": True,
    }


# -- capture ----------------------------------------------------------
def handle_capture_start(ctx, payload):
    session = ctx.tx.get(payload["session_id"], kind="Session")
    segment = ctx.tx.get(payload["segment_id"], kind="Segment")
    if session["status"] != "active":
        raise DemoError("session_closed", "该会话已经结束。", status=409)
    if segment["session_id"] != session["id"]:
        raise DemoError("segment_conflict", "区段不属于该会话。", status=409)
    if segment["status"] != "open":
        raise DemoError("segment_closed", "该区段已经结束。", status=409)
    binding = ctx.tx.get(segment["binding_id"], kind="Binding")
    if segment["revision"] != payload["expected_segment_revision"]:
        raise DemoError("segment_conflict", "区段版本已变化，请刷新。", status=409)
    if binding["revision"] != payload["expected_binding_revision"]:
        raise DemoError("binding_conflict", "配置绑定版本已变化，请刷新。", status=409)
    if _open_capture(ctx.tx, segment["id"]) is not None:
        raise DemoError("capture_busy", "该区段已有性能采集在运行。", status=409)
    task = ctx.service.tasks.enqueue(
        ctx.tx,
        "capture",
        {
            "session_id": session["id"],
            "segment_id": segment["id"],
            "binding_id": binding["id"],
            "segment_revision": segment["revision"],
            "binding_revision": binding["revision"],
            "label": payload.get("label", "recommended"),
        },
    )
    ctx.tx.emit("capture.started", {"task_id": task["id"], "segment_id": segment["id"]})
    return DomainResult(202, task_id=task["id"], data={"task": task})


def handle_capture_stop(ctx, payload):
    task = ctx.tx.task_row(payload["task_id"])
    if task["kind"] != "capture":
        raise DemoError("not_a_capture", "该任务不是性能采集任务。", status=409)
    if task["status"] not in ("queued", "running"):
        raise DemoError("task_already_terminal", "该采集任务已经结束。", status=409)
    input_data = task["input"]
    segment = ctx.tx.get(input_data["segment_id"], kind="Segment")
    if segment["status"] != "open":
        raise DemoError("segment_closed", "该区段已经结束。", status=409)
    ctx.tx.set_task_status(task["id"], "cancelled")
    recipe = ctx.service.catalog.recipe(ctx.scenario_id, "short")
    data = generate_trace(ctx.scenario_id, "short", recipe)
    artifact, _ = _artifact_from_bytes(
        ctx.service,
        ctx.tx,
        data=data,
        label="short",
        session_id=input_data["session_id"],
        segment_id=segment["id"],
        wait_s=0.0,
        origin="capture",
    )
    closed = _close_segment(ctx.tx, segment)
    session = ctx.tx.get(input_data["session_id"], kind="Session")
    ctx.tx.update_entity(
        session["id"],
        {"active_segment_id": None},
        revision=session["revision"],
    )
    ctx.tx.emit(
        "capture.stopped",
        {"task_id": task["id"], "artifact_id": artifact["id"], "segment_id": closed["id"]},
    )
    return DomainResult(
        200,
        [
            {"kind": "Task", "id": task["id"]},
            {"kind": "LogArtifact", "id": artifact["id"]},
        ],
        data={"artifact_id": artifact["id"]},
    )


# -- logs and analysis ------------------------------------------------
def _inspect(ctx, file_id):
    record = ctx.tx.get(file_id, kind="File")
    data = ctx.tx.file_bytes(file_id, expected_hash=record["content_hash"])
    parsed = parse_trace(data, scenario_id=ctx.scenario_id)
    duration = round(sum(parsed.runs[0]) / 1000.0, 3) if parsed.runs and parsed.runs[0] else 0.0
    return record, data, parsed, duration


def handle_inspect(ctx, payload):
    record, data, parsed, duration = _inspect(ctx, payload["file_id"])
    inspection = ctx.tx.insert(
        "LogInspection",
        {
            "file_id": record["id"],
            "content_hash": record["content_hash"],
            "filename_label": record["filename_label"],
            "trace_id": parsed.trace_id,
            "frame_count": len(parsed.frames),
            "duration_s": duration,
            "issues": parsed.issues,
            "preview": parsed.frames[:5],
            "note": "仅预览；关联与统计需要显式操作。",
        },
        origin="uploaded_demo_file",
    )
    ctx.tx.emit("log.inspected", {"inspection_id": inspection["id"]})
    return DomainResult(
        200,
        [{"kind": "LogInspection", "id": inspection["id"]}],
        data={"inspection": inspection},
    )


def handle_associate(ctx, payload):
    record, data, parsed, duration = _inspect(ctx, payload["file_id"])
    if payload.get("historical"):
        session = ctx.tx.get(payload["session_id"], kind="Session")
        if session["scenario_id"] != ctx.scenario_id:
            raise DemoError("cross_case", "会话不属于当前案例。", status=409)
        # Historical logs still need a closed, bound segment so the normal
        # analysis path can verify the same revision guards as live samples.
        segment = ctx.tx.insert(
            "Segment",
            {
                "session_id": session["id"],
                "binding_id": None,
                "status": "closed",
                "end_s": None,
                "start_monotonic": 0.0,
                "started_at": payload["declared_start"],
                "ended_at": payload["declared_start"],
                "time_base": "declared",
                "historical": True,
                "declared_start": payload["declared_start"],
                "basis": payload["basis"],
            },
            origin="uploaded_demo_file",
        )
        binding = ctx.tx.insert(
            "Binding",
            {
                "segment_id": segment["id"],
                "context_id": session["context_id"],
                "applied_id": session.get("applied_id"),
                "plan_id": session.get("plan_id"),
                "configuration_state": (
                    "confirmed" if session.get("applied_id") else "unverified"
                ),
                "supersedes_id": None,
                "historical": True,
            },
            origin="uploaded_demo_file",
        )
        segment = ctx.tx.update_entity(
            segment["id"], {"binding_id": binding["id"]}, revision=1
        )
        artifact = ctx.tx.insert(
            "LogArtifact",
            {
                "file_id": record["id"],
                "filename_label": record["filename_label"],
                "content_hash": record["content_hash"],
                "size": record["size"],
                "trace_id": parsed.trace_id,
                "label": "uploaded",
                "source": "uploaded",
                "time_base": "declared",
                "frame_count": len(parsed.frames),
                "declared_duration_s": duration,
                "capture_wait_s": None,
                "session_id": session["id"],
                "segment_id": segment["id"],
                "frame_stream": "application_present",
                "historical": True,
                "declared_start": payload["declared_start"],
                "basis": payload["basis"],
                "created_seq": ctx.tx.latest_seq(),
            },
            origin="uploaded_demo_file",
        )
        ctx.tx.emit(
            "log.associated",
            {
                "artifact_id": artifact["id"],
                "historical": True,
                "segment_id": segment["id"],
            },
        )
        return DomainResult(
            201,
            [
                {"kind": "LogArtifact", "id": artifact["id"]},
                {"kind": "Segment", "id": segment["id"]},
                {"kind": "Binding", "id": binding["id"]},
            ],
            data={
                "artifact_id": artifact["id"],
                "segment_id": segment["id"],
                "segment_revision": segment["revision"],
                "binding_revision": binding["revision"],
            },
        )
    segment, binding = require_closed(
        ctx.tx,
        payload["segment_id"],
        payload["segment_revision"],
        payload["binding_revision"],
    )
    artifact = ctx.tx.insert(
        "LogArtifact",
        {
            "file_id": record["id"],
            "filename_label": record["filename_label"],
            "content_hash": record["content_hash"],
            "size": record["size"],
            "trace_id": parsed.trace_id,
            "label": "uploaded",
            "source": "uploaded",
            "time_base": "simulated",
            "frame_count": len(parsed.frames),
            "declared_duration_s": duration,
            "capture_wait_s": None,
            "session_id": segment["session_id"],
            "segment_id": segment["id"],
            "frame_stream": "application_present",
            "created_seq": ctx.tx.latest_seq(),
        },
        origin="uploaded_demo_file",
    )
    ctx.tx.emit("log.associated", {"artifact_id": artifact["id"]})
    return DomainResult(201, [{"kind": "LogArtifact", "id": artifact["id"]}])


def _build_summary(ctx, artifact, segment, binding, *, origin=None):
    data = ctx.tx.file_bytes(
        artifact["file_id"], expected_hash=artifact["content_hash"]
    )
    parsed = parse_trace(data, scenario_id=ctx.scenario_id)
    clean = clean_trace(parsed)
    metrics = summarize(clean)
    threshold = adaptive_threshold(clean.intervals)
    longs = long_frames(clean, threshold) if threshold is not None else None
    chart = _chart(clean, threshold)
    summary = ctx.tx.insert(
        "Summary",
        {
            "artifact_id": artifact["id"],
            "file_id": artifact["file_id"],
            "source_hash": artifact["content_hash"],
            "segment_id": segment["id"],
            "session_id": artifact["session_id"],
            "applied_id": binding["applied_id"],
            "binding_id": binding["id"],
            "segment_revision": segment["revision"],
            "binding_revision": binding["revision"],
            "metrics": metrics,
            "long_frames": longs,
            "chart": chart,
            "frame_stream": "application_present",
            "time_base": artifact.get("time_base", "simulated"),
            "label": artifact["label"],
            "quality": metrics["quality"],
            "issues": clean.issues,
            "exclusions": clean.exclusions,
            "created_seq": ctx.tx.latest_seq(),
        },
        origin=origin
        or ("simulated" if artifact["source"] == "capture" else "uploaded_demo_file"),
    )
    return summary, clean


def handle_analyze(ctx, payload):
    segment, binding = require_closed(
        ctx.tx,
        payload["segment_id"],
        payload["segment_revision"],
        payload["binding_revision"],
    )
    artifact = ctx.tx.get(payload["artifact_id"], kind="LogArtifact")
    if artifact.get("segment_id") != segment["id"]:
        raise DemoError("artifact_segment_mismatch", "原件不属于该区段。", status=409)
    summary, clean = _build_summary(ctx, artifact, segment, binding)
    ctx.tx.emit(
        "performance.analyzed",
        {
            "summary_id": summary["id"],
            "source_hash": summary["source_hash"],
            "quality": summary["quality"],
        },
    )
    return DomainResult(201, [{"kind": "Summary", "id": summary["id"]}])
