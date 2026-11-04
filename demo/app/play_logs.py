"""Fixed virtual play logs for the isolated demo; no host filesystem scan."""

from datetime import datetime, timedelta

from .contracts import DemoError, DomainResult
from .service import require_fields
from .sessions import handle_session
from .performance import _artifact_from_bytes, _build_summary
from .traces import generate_trace, _interval_us

LOGS = (
    {"id": "last-play", "label": "最近一次游玩", "scene": "交战场景 · 固定路线", "recipe": "recommended", "minutes_ago": 35},
    {"id": "previous-play", "label": "上一次训练", "scene": "训练场 · 画面与操作", "recipe": "adjusted", "minutes_ago": 1480},
    {"id": "short-play", "label": "较早一次游玩", "scene": "交战场景 · 帧时间波动", "recipe": "no_gain", "minutes_ago": 2950},
)


def register(service):
    service.register("log.capture_recent", validate_capture, handle_capture)


def list_recent(service):
    now = datetime.fromisoformat(service.clock.now_iso())
    scenario_id = "mw2-rtx4060"
    return [
        {"id": log["id"], "label": log["label"], "scene": log["scene"],
         "ended_at": (now - timedelta(minutes=log["minutes_ago"])).isoformat(),
         "duration_s": round(sum(_interval_us(service.catalog.recipe(scenario_id, log["recipe"]), index)
                                 for index in range(1, service.catalog.recipe(scenario_id, log["recipe"])["frames"])) / 1000000, 3),
         "origin": "simulated", "demo_only": True}
        for log in LOGS
    ]


def validate_capture(payload):
    return require_fields(payload, required=("log_id", "expected_applied_id"))


def handle_capture(ctx, payload):
    log = next((item for item in LOGS if item["id"] == payload["log_id"]), None)
    if log is None:
        raise DemoError("unknown_recent_log", "这份游玩日志已不可用，请重新选择。", status=404)
    workspace = ctx.tx.workspace()
    applied_id = workspace["current_applied_id"]
    if not applied_id:
        raise DemoError("configuration_required", "请先确认日志对应的游戏设置。", status=409)
    if payload["expected_applied_id"] != applied_id:
        raise DemoError("pointer_conflict", "当前设置已变化，请刷新后重新读取。", status=409)
    cached = ctx.tx.find("RecentLogCapture", virtual_log_id=log["id"], applied_id=applied_id)
    if cached:
        saved = cached[-1]
        return DomainResult(200, refs=saved["refs"], data=saved["data"])
    if ctx.tx.active_tasks():
        raise DemoError("task_busy", "请先结束当前任务，再读取游玩日志。", status=409)

    descriptor = next(item for item in list_recent(ctx.service) if item["id"] == log["id"])
    session_result = handle_session(ctx, {
        "context_id": workspace["context_id"], "applied_id": applied_id,
        "run_conditions": {"scene": log["scene"], "virtual_log_id": log["id"],
                           "association": "按玩家当前已确认设置关联"},
    })
    run = session_result.data
    segment = ctx.tx.get(run["segment_id"], kind="Segment")
    recipe = ctx.catalog.recipe(ctx.scenario_id, log["recipe"])
    artifact, _ = _artifact_from_bytes(
        ctx.service, ctx.tx, data=generate_trace(ctx.scenario_id, log["recipe"], recipe),
        label=log["recipe"], session_id=run["session_id"], segment_id=segment["id"],
        wait_s=0, origin="recent_log", entity_origin="simulated",
    )
    segment = ctx.tx.update_entity(segment["id"], {
        "status": "closed", "end_s": artifact["declared_duration_s"], "ended_at": descriptor["ended_at"],
        "started_at": (datetime.fromisoformat(descriptor["ended_at"]) - timedelta(seconds=descriptor["duration_s"])).isoformat(),
    }, revision=segment["revision"] + 1)
    session = ctx.tx.get(run["session_id"], kind="Session")
    ctx.tx.update_entity(session["id"], {
        "status": "ended", "active_segment_id": None, "ended_at": descriptor["ended_at"],
        "started_at": (datetime.fromisoformat(descriptor["ended_at"]) - timedelta(seconds=descriptor["duration_s"])).isoformat(),
    }, revision=session["revision"] + 1)
    binding = ctx.tx.get(segment["binding_id"], kind="Binding")
    summary, _ = _build_summary(ctx, artifact, segment, binding, origin="simulated")
    refs = session_result.refs + [{"kind": "LogArtifact", "id": artifact["id"]}, {"kind": "Summary", "id": summary["id"]}]
    data = {**run, "segment_revision": segment["revision"], "artifact_id": artifact["id"], "log": descriptor}
    ctx.tx.insert("RecentLogCapture", {"virtual_log_id": log["id"], "applied_id": applied_id, "refs": refs, "data": data}, origin="simulated")
    ctx.tx.emit("log.recent.captured", {"summary_id": summary["id"], "virtual_log_id": log["id"]})
    return DomainResult(201, refs=refs, data=data)
