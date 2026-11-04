"""Demo test plans, sessions, segments, and immutable configuration bindings."""

from __future__ import annotations

from .contracts import DemoError, DomainResult
from .service import require_fields

METRIC_KEYS = (
    "average_present_rate",
    "interval_p95_ms",
    "interval_p99_ms",
    "slowest_1pct_fps",
)


def register(service) -> None:
    service.register("plan.save", validate_plan, handle_plan)
    service.register("session.create", validate_session, handle_session)
    service.register("session.split", validate_segment_action, handle_split)
    service.register("session.end", validate_segment_action, handle_end)
    service.register("binding.revise", validate_binding, handle_binding)


# -- validation -------------------------------------------------------
def validate_plan(payload):
    require_fields(
        payload,
        required=("scene",),
        allowed=("scene", "duration_s", "repeats", "metrics", "conditions"),
    )
    if not isinstance(payload["scene"], str) or not payload["scene"].strip():
        raise DemoError("invalid_scene", "测试场景不能为空。")
    for key in ("duration_s", "repeats"):
        value = payload.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise DemoError("invalid_plan", f"{key} 必须是正整数或 null。")
    metrics = payload.get("metrics", list(METRIC_KEYS))
    if not isinstance(metrics, list) or not metrics:
        raise DemoError("invalid_metrics", "测试计划需要至少一个指标。")
    if not set(metrics) <= set(METRIC_KEYS):
        raise DemoError("invalid_metrics", "存在不支持的指标。")
    if payload.get("conditions") is not None and not isinstance(payload["conditions"], dict):
        raise DemoError("invalid_conditions", "conditions 必须是对象。")
    return payload


def validate_session(payload):
    require_fields(
        payload,
        required=("context_id",),
        allowed=("context_id", "applied_id", "plan_id", "run_conditions"),
    )
    if payload.get("run_conditions") is not None and not isinstance(
        payload["run_conditions"], dict
    ):
        raise DemoError("invalid_conditions", "run_conditions 必须是对象。")
    return payload


def validate_segment_action(payload):
    require_fields(
        payload,
        required=(
            "session_id",
            "expected_segment_id",
            "expected_segment_revision",
            "expected_binding_revision",
        ),
        allowed=(
            "session_id",
            "expected_segment_id",
            "expected_segment_revision",
            "expected_binding_revision",
        ),
    )
    return payload


def validate_binding(payload):
    require_fields(
        payload,
        required=("segment_id", "expected_binding_revision", "applied_id"),
        allowed=("segment_id", "expected_binding_revision", "applied_id", "reason"),
    )
    return payload


# -- checks -----------------------------------------------------------
def _open_segment(tx, session):
    if session["status"] != "active":
        raise DemoError("session_closed", "该会话已经结束。", status=409)
    segment = tx.get(session["active_segment_id"], kind="Segment")
    binding = tx.get(segment["binding_id"], kind="Binding")
    return segment, binding


def _check_revisions(payload, segment, binding):
    if segment["id"] != payload["expected_segment_id"]:
        raise DemoError("segment_conflict", "活动区段已变化，请刷新。", status=409)
    if segment["revision"] != payload["expected_segment_revision"]:
        raise DemoError("segment_conflict", "区段已在其他位置变化，请刷新。", status=409)
    if binding["revision"] != payload["expected_binding_revision"]:
        raise DemoError("binding_conflict", "配置绑定已在其他位置变化，请刷新。", status=409)


def _close_segment(tx, segment):
    elapsed = round(max(0.0, tx.clock.monotonic() - segment.get("start_monotonic", 0.0)), 3)
    closed = tx.update_entity(
        segment["id"],
        {"status": "closed", "end_s": elapsed, "ended_at": tx.clock.now_iso()},
        revision=segment["revision"] + 1,
    )
    return closed


def require_closed(tx, segment_id: str, segment_revision: int, binding_revision: int):
    segment = tx.get(segment_id, kind="Segment")
    if segment["status"] != "closed":
        raise DemoError(
            "segment_open",
            "开放区段不能分析，请先结束该区段。",
            status=409,
            recovery_actions=["end_segment"],
        )
    if segment["revision"] != segment_revision:
        raise DemoError("segment_conflict", "区段版本已变化，请刷新。", status=409)
    binding = tx.get(segment["binding_id"], kind="Binding")
    if binding["revision"] != binding_revision:
        raise DemoError("binding_conflict", "配置绑定版本已变化，请刷新。", status=409)
    return segment, binding


def _new_binding(
    tx,
    *,
    segment_id,
    context_id,
    applied_id,
    plan_id,
    supersedes_id=None,
    revision=1,
):
    return tx.insert(
        "Binding",
        {
            "segment_id": segment_id,
            "context_id": context_id,
            "applied_id": applied_id,
            "plan_id": plan_id,
            "configuration_state": "confirmed" if applied_id else "unverified",
            "supersedes_id": supersedes_id,
        },
        origin="user_declared",
        revision=revision,
    )


# -- handlers ---------------------------------------------------------
def handle_plan(ctx, payload):
    plan = ctx.tx.insert(
        "TestPlan",
        {
            "scene": payload["scene"],
            "duration_s": payload.get("duration_s"),
            "repeats": payload.get("repeats"),
            "metrics": payload.get("metrics", list(METRIC_KEYS)),
            "conditions": payload.get("conditions") or {},
            "note": "保存计划不代表已经执行。",
        },
        origin="user_declared",
    )
    ctx.tx.emit("plan.saved", {"plan_id": plan["id"]})
    return DomainResult(201, [{"kind": "TestPlan", "id": plan["id"]}])


def handle_session(ctx, payload):
    workspace = ctx.tx.workspace()
    context = ctx.tx.get(payload["context_id"], kind="ContextSnapshot")
    applied_id = payload.get("applied_id")
    if applied_id is not None:
        ctx.tx.get(applied_id, kind="AppliedConfiguration")
    plan_id = payload.get("plan_id")
    if plan_id is not None:
        ctx.tx.get(plan_id, kind="TestPlan")
    session = ctx.tx.insert(
        "Session",
        {
            "context_id": context["id"],
            "applied_id": applied_id,
            "plan_id": plan_id,
            "run_conditions": payload.get("run_conditions") or {},
            "status": "active",
            "active_segment_id": None,
            "started_at": ctx.clock.now_iso(),
            "time_base": "simulated",
        },
        origin="simulated",
    )
    segment = ctx.tx.insert(
        "Segment",
        {
            "session_id": session["id"],
            "binding_id": None,
            "status": "open",
            "end_s": None,
            "start_monotonic": ctx.clock.monotonic(),
            "started_at": ctx.clock.now_iso(),
            "time_base": "simulated",
        },
        origin="simulated",
    )
    binding = _new_binding(
        ctx.tx,
        segment_id=segment["id"],
        context_id=context["id"],
        applied_id=applied_id,
        plan_id=plan_id,
    )
    segment = ctx.tx.update_entity(segment["id"], {"binding_id": binding["id"]}, revision=1)
    ctx.tx.update_entity(session["id"], {"active_segment_id": segment["id"]}, revision=1)
    ctx.tx.emit(
        "session.created",
        {"session_id": session["id"], "segment_id": segment["id"], "applied_id": applied_id},
    )
    return DomainResult(
        201,
        [
            {"kind": "Session", "id": session["id"]},
            {"kind": "Segment", "id": segment["id"]},
            {"kind": "Binding", "id": binding["id"]},
        ],
        data={
            "session_id": session["id"],
            "segment_id": segment["id"],
            "binding_id": binding["id"],
            "segment_revision": segment["revision"],
            "binding_revision": binding["revision"],
        },
    )


def handle_split(ctx, payload):
    session = ctx.tx.get(payload["session_id"], kind="Session")
    segment, binding = _open_segment(ctx.tx, session)
    _check_revisions(payload, segment, binding)
    closed = _close_segment(ctx.tx, segment)
    new_segment = ctx.tx.insert(
        "Segment",
        {
            "session_id": session["id"],
            "binding_id": None,
            "status": "open",
            "end_s": None,
            "start_monotonic": ctx.clock.monotonic(),
            "started_at": ctx.clock.now_iso(),
            "time_base": "simulated",
        },
        origin="simulated",
    )
    new_binding = _new_binding(
        ctx.tx,
        segment_id=new_segment["id"],
        context_id=binding["context_id"],
        applied_id=binding["applied_id"],
        plan_id=binding["plan_id"],
        supersedes_id=binding["id"],
    )
    new_segment = ctx.tx.update_entity(
        new_segment["id"], {"binding_id": new_binding["id"]}, revision=1
    )
    ctx.tx.update_entity(
        session["id"], {"active_segment_id": new_segment["id"]}, revision=session["revision"] + 1
    )
    ctx.tx.emit(
        "session.split",
        {
            "session_id": session["id"],
            "closed_segment_id": closed["id"],
            "new_segment_id": new_segment["id"],
        },
    )
    return DomainResult(
        200,
        [
            {"kind": "Segment", "id": closed["id"]},
            {"kind": "Segment", "id": new_segment["id"]},
            {"kind": "Binding", "id": new_binding["id"]},
        ],
        data={
            "closed_segment_id": closed["id"],
            "segment_id": new_segment["id"],
            "binding_id": new_binding["id"],
            "segment_revision": new_segment["revision"],
            "binding_revision": new_binding["revision"],
        },
    )


def handle_end(ctx, payload):
    session = ctx.tx.get(payload["session_id"], kind="Session")
    segment, binding = _open_segment(ctx.tx, session)
    _check_revisions(payload, segment, binding)
    closed = _close_segment(ctx.tx, segment)
    updated_session = ctx.tx.update_entity(
        session["id"],
        {"status": "closed", "active_segment_id": None, "ended_at": ctx.clock.now_iso()},
        revision=session["revision"] + 1,
    )
    ctx.tx.emit("session.ended", {"session_id": session["id"], "segment_id": closed["id"]})
    return DomainResult(
        200,
        [
            {"kind": "Session", "id": updated_session["id"]},
            {"kind": "Segment", "id": closed["id"]},
        ],
        data={"segment_id": closed["id"], "segment_revision": closed["revision"]},
    )


def handle_binding(ctx, payload):
    segment = ctx.tx.get(payload["segment_id"], kind="Segment")
    current = ctx.tx.get(segment["binding_id"], kind="Binding")
    if current["revision"] != payload["expected_binding_revision"]:
        raise DemoError(
            "binding_conflict",
            "配置绑定已在其他位置变化，请刷新。",
            status=409,
        )
    applied_id = payload.get("applied_id")
    if applied_id is not None:
        ctx.tx.get(applied_id, kind="AppliedConfiguration")
    revised = _new_binding(
        ctx.tx,
        segment_id=segment["id"],
        context_id=current["context_id"],
        applied_id=applied_id,
        plan_id=current["plan_id"],
        supersedes_id=current["id"],
        revision=current["revision"] + 1,
    )
    segment = ctx.tx.update_entity(
        segment["id"],
        {"binding_id": revised["id"]},
        revision=segment["revision"] + 1,
    )
    ctx.tx.emit(
        "binding.revised",
        {"segment_id": segment["id"], "binding_id": revised["id"]},
    )
    return DomainResult(
        201,
        [{"kind": "Binding", "id": revised["id"]}],
        data={
            "binding_revision": revised["revision"],
            "segment_revision": segment["revision"],
        },
    )
