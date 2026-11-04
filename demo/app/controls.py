"""Demo controls: modes, failure scripts, task control, and epoch reset."""

from __future__ import annotations

from .contracts import DemoError, DomainResult
from .service import require_fields

MODE_IDS = ("narrated", "fast", "step")
RESET_SCOPES = ("current", "all")
RESUMABLE_STATUSES = ("cancelled", "partial", "failed", "interrupted")


def register(service) -> None:
    service.register("demo.mode", validate_mode, handle_mode)
    service.register("demo.script", validate_script, handle_script)
    service.register("task.cancel", validate_task, handle_cancel)
    service.register("task.resume", validate_task, handle_resume)
    service.register("task.advance", validate_task, handle_advance)
    service.register("demo.reset", validate_reset, handle_reset)


def validate_mode(payload):
    require_fields(payload, required=("mode",))
    if payload["mode"] not in MODE_IDS:
        raise DemoError(
            "invalid_mode",
            "演示模式必须是讲解、快速或分步模式。",
            field_errors=[{"field": "mode", "error": "unsupported"}],
        )
    return payload


def handle_mode(ctx, payload):
    ctx.tx.update_workspace(mode=payload["mode"])
    ctx.tx.emit("demo.mode", {"mode": payload["mode"]})
    return DomainResult(200, data={"mode": payload["mode"]})


def validate_script(payload):
    require_fields(payload, required=("script",))
    if not isinstance(payload["script"], str):
        raise DemoError("invalid_script", "演示剧本标识必须是文本。")
    return payload


def handle_script(ctx, payload):
    script = payload["script"]
    known = {item["id"] for item in ctx.catalog.failure_scripts()}
    if script != "none" and script not in known:
        raise DemoError(
            "unknown_script",
            "演示剧本不存在。",
            field_errors=[{"field": "script", "error": "unknown"}],
        )
    ctx.tx.update_workspace(script=script)
    ctx.tx.emit("demo.script", {"script": script})
    return DomainResult(200, data={"script": script})


def validate_task(payload):
    return require_fields(payload, required=("task_id",))


def handle_cancel(ctx, payload):
    task = ctx.tx.task_row(payload["task_id"])
    if task["status"] in ("completed", "partial", "failed", "cancelled", "interrupted", "superseded"):
        raise DemoError(
            "task_already_terminal",
            "该任务已经结束，不能再取消。",
            status=409,
            recovery_actions=["resume_task"],
        )
    ctx.tx.set_task_status(task["id"], "cancelled")
    return DomainResult(
        200, [{"kind": "Task", "id": task["id"]}], data={"status": "cancelled"}
    )


def handle_advance(ctx, payload):
    workspace = ctx.tx.workspace()
    if workspace["mode"] != "step":
        raise DemoError(
            "mode_not_step",
            "只有分步模式可以手动推进任务。",
            status=409,
            recovery_actions=["switch_mode"],
        )
    task = ctx.tx.task_row(payload["task_id"])
    if task["status"] not in ("queued", "running"):
        raise DemoError("task_not_advanceable", "该任务当前不能推进。", status=409)
    advanced = ctx.service.tasks.advance_in_transaction(ctx.tx, task["id"])
    if not advanced:
        raise DemoError("task_not_advanceable", "任务阶段已变化，请刷新。", status=409)
    updated = ctx.tx.task_row(task["id"])
    return DomainResult(
        200,
        [{"kind": "Task", "id": task["id"]}],
        data={"task": updated},
    )


def handle_resume(ctx, payload):
    task = ctx.tx.task_row(payload["task_id"])
    if task["kind"] == "feedback.review":
        analysis = ctx.tx.get(task["input"]["analysis_id"], kind="FeedbackAnalysis")
        if analysis["task_id"] != task["id"] or analysis["status"] == "superseded":
            raise DemoError("task_not_resumable", "这条分析已被替代，请使用当前分析任务。", status=409)
    if task["status"] not in RESUMABLE_STATUSES:
        raise DemoError(
            "task_not_resumable",
            "只有已取消、中断或部分完成的任务可以继续。",
            status=409,
        )
    remaining = [dict(phase) for phase in task["phases"][task["phase_index"]:]]
    if not remaining:
        raise DemoError("nothing_to_resume", "该任务没有剩余阶段。", status=409)
    for phase in remaining:
        phase["status"] = "pending"
    new_task = ctx.tx.insert_task(
        task["kind"],
        phases=remaining,
        input_data=task["input"],
        parent_id=task["id"],
        result=task["result"],
    )
    intent_id = (task.get("input") or {}).get("intent_id")
    if intent_id:
        intent = ctx.tx.get(intent_id, kind="GameIntent")
        ctx.tx.update_entity(
            intent_id,
            {"task_id": new_task["id"], "status": "researching"},
            revision=intent["revision"] + 1,
        )
    ctx.tx.emit(
        "task.resumed",
        {
            "parent_id": task["id"],
            "task_id": new_task["id"],
            "remaining_phases": [phase["key"] for phase in remaining],
        },
    )
    return DomainResult(
        202, [{"kind": "Task", "id": new_task["id"]}], task_id=new_task["id"]
    )


def validate_reset(payload):
    require_fields(payload, required=("scope",))
    if payload["scope"] not in RESET_SCOPES:
        raise DemoError(
            "invalid_reset_scope",
            "重置范围只能是当前案例或全部案例。",
            field_errors=[{"field": "scope", "error": "unsupported"}],
        )
    return payload


def handle_reset(ctx, payload):
    targets = (
        [ctx.scenario_id]
        if payload["scope"] == "current"
        else [scenario["scenario_id"] for scenario in ctx.catalog.list()]
    )
    resets = []
    file_ids = []
    for scenario_id in targets:
        info = ctx.tx.reset_workspace(scenario_id, ctx.catalog)
        resets.append(info)
        file_ids.extend(info["file_ids"])
    workspaces = [ctx.tx.workspace_for(scenario_id) for scenario_id in targets]
    for workspace in workspaces:
        ctx.tx.emit(
            "demo.reset",
            {
                "scenario_id": workspace["scenario_id"],
                "workspace_epoch": workspace["workspace_epoch"],
            },
        )
    return DomainResult(
        200,
        data={
            "resets": resets,
            "workspaces": workspaces,
            "_cleanup_file_ids": file_ids,
        },
    )
