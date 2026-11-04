"""Demo device context, simulated identification, and follow-up answers."""

from __future__ import annotations

from .contracts import DemoError, DomainResult
from .service import require_fields

CONTEXT_FIELDS = (
    "game_name",
    "gpu_name",
    "cpu",
    "memory_gb",
    "vram_gb",
    "driver_version",
    "game_version",
    "display_refresh_hz",
    "resolution",
    "install_path",
    "process_name",
    "scene_tag",
)
IDENTITY_FIELDS = ("game_name", "gpu_name", "resolution")
QUESTION_FIELDS = (
    ("cpu", "CPU 型号未知：请填写，或选择“不知道”。"),
    ("memory_gb", "内存容量未知：请填写，或选择“不知道”。"),
    ("vram_gb", "显存容量未知：请填写，或选择“不知道”。"),
    ("driver_version", "显卡驱动版本未知：请填写，或选择“不知道”。"),
    ("game_version", "游戏版本未知：请填写，或选择“不知道”。"),
    ("display_refresh_hz", "显示刷新率未知：请填写，或选择“不知道”。"),
)

# Presentation fixture requested by the user. This is intentionally static:
# clicking the web control never launches a process or queries the host OS.
FIXED_LOCAL_DEVICE = {
    "cpu": "Intel Core i7-14650HX",
    "gpu_name": "NVIDIA GeForce RTX 4060 Laptop GPU",
    "memory_gb": 16,
    "vram_gb": 8,
    "driver_version": "621.01",
    "display_refresh_hz": 165,
    "os_name": "Windows 11 专业版",
    "desktop_resolution": {"width": 2560, "height": 1600},
}
DEVICE_CONTEXT_FIELDS = (
    "cpu", "gpu_name", "memory_gb", "vram_gb", "driver_version", "display_refresh_hz",
)


def register(service) -> None:
    service.register("context.save", validate_context_save, handle_context_save)
    service.register("device.scan", validate_scan, handle_scan)
    service.register("device.check", validate_scan, handle_check)
    service.register("information.answer", validate_answer, handle_answer)
    service.register("information.amend", validate_answer, handle_amend)


# -- validation -------------------------------------------------------
def validate_context_save(payload):
    require_fields(
        payload,
        required=("expected_context_id",),
        allowed=("expected_context_id", "fields", "restore_preset"),
    )
    if "fields" not in payload and not payload.get("restore_preset"):
        raise DemoError("missing_field", "请提供背景字段或恢复预设标记。")
    fields = payload.get("fields") or {}
    if not isinstance(fields, dict):
        raise DemoError("invalid_context", "fields 必须是对象。")
    unknown = set(fields) - set(CONTEXT_FIELDS)
    if unknown:
        raise DemoError(
            "unknown_field",
            f"背景字段不受支持：{', '.join(sorted(unknown))}。",
        )
    _validate_context_values(fields)
    return payload


def _validate_context_values(fields):
    for key in ("game_name", "gpu_name", "cpu", "driver_version", "game_version"):
        if key in fields:
            value = fields[key]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise DemoError(
                    "invalid_context",
                    f"{key} 必须是非空文本或 null。",
                    field_errors=[{"field": key, "error": "invalid"}],
                )
    for key in ("memory_gb", "vram_gb"):
        if key in fields and fields[key] is not None:
            value = fields[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise DemoError(
                    "invalid_context",
                    f"{key} 必须是正数或 null。",
                    field_errors=[{"field": key, "error": "invalid"}],
                )
    if "display_refresh_hz" in fields and fields["display_refresh_hz"] is not None:
        value = fields["display_refresh_hz"]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise DemoError("invalid_context", "display_refresh_hz 必须是正整数或 null。")
    if "resolution" in fields:
        resolution = fields["resolution"]
        if not isinstance(resolution, dict):
            raise DemoError("invalid_context", "resolution 必须是包含宽高的对象。")
        width = resolution.get("width")
        height = resolution.get("height")
        for name, value in (("width", width), ("height", height)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise DemoError(
                    "invalid_context",
                    "分辨率宽高必须是正整数。",
                    field_errors=[{"field": f"resolution.{name}", "error": "invalid"}],
                )


def validate_scan(payload):
    return require_fields(payload, required=(), allowed=())


def validate_answer(payload):
    require_fields(
        payload,
        required=("request_id", "question_revision", "context_revision", "answers"),
        allowed=(
            "request_id",
            "question_revision",
            "context_revision",
            "answers",
            "skipped_fields",
            "correction_of",
        ),
    )
    if not isinstance(payload["answers"], dict):
        raise DemoError("invalid_answers", "answers 必须是对象。")
    _validate_context_values(payload["answers"])
    skipped = payload.get("skipped_fields") or []
    if not isinstance(skipped, list) or not all(isinstance(item, str) for item in skipped):
        raise DemoError("invalid_skipped", "skipped_fields 必须是字段名列表。")
    return payload


# -- helpers ----------------------------------------------------------
def _fixture_context(ctx):
    source = ctx.scenario["context"]
    return {
        key: value
        for key, value in source.items()
        if key not in ("schema", "demo_only", "scenario_id")
    }


def _current_context(ctx):
    return ctx.tx.get(ctx.tx.workspace()["context_id"], kind="ContextSnapshot")


def _applicability(current, fixture):
    fixed = (
        current.get("game_name") == fixture.get("game_name")
        and current.get("resolution") == fixture.get("resolution")
        and all(current.get(key) == FIXED_LOCAL_DEVICE[key] for key in DEVICE_CONTEXT_FIELDS)
    )
    if fixed:
        return "preset"
    for key in IDENTITY_FIELDS:
        if current.get(key) != fixture.get(key):
            return "needs_confirmation"
    for key in DEVICE_CONTEXT_FIELDS:
        if current.get(key) != fixture.get(key):
            return "needs_confirmation"
    return "preset"


def _new_context(ctx, base, changes, *, origin, restore=False):
    fixture = _fixture_context(ctx)
    payload = {
        key: base.get(key)
        for key in (
            "game_name",
            "gpu_name",
            "cpu",
            "memory_gb",
            "vram_gb",
            "driver_version",
            "game_version",
            "display_refresh_hz",
            "resolution",
            "install_path",
            "process_name",
            "scene_tag",
            "narrative",
        )
    }
    if restore:
        payload.update(fixture)
        declared = []
    else:
        payload.update(changes)
        declared = sorted(changes)
    payload["applicability"] = _applicability(payload, fixture)
    payload["unknown_fields"] = sorted(
        key for key in fixture.get("unknown_fields", []) if payload.get(key) is None
    )
    payload["declared_fields"] = declared
    payload["supersedes_id"] = None if restore else base["id"]
    return ctx.tx.insert(
        "ContextSnapshot",
        payload,
        origin=origin,
        revision=1 if restore else base["revision"] + 1,
    )


def _open_request(ctx):
    for request in ctx.tx.find("InformationRequest", status="open"):
        return request
    return None


def _remaining_fields(request):
    answered = request.get("answers", {})
    skipped = set(request.get("skipped_fields", []))
    return [
        item for item in request["fields"]
        if item["field"] not in answered and item["field"] not in skipped
    ]


# -- handlers ---------------------------------------------------------
def handle_context_save(ctx, payload):
    context = _current_context(ctx)
    if payload["expected_context_id"] != context["id"]:
        raise DemoError(
            "context_conflict",
            "背景已在其他位置更新，请刷新后再保存。",
            status=409,
            recovery_actions=["refresh"],
        )
    restore = bool(payload.get("restore_preset"))
    new_context = _new_context(
        ctx,
        context,
        payload.get("fields") or {},
        origin="fixture" if restore else "user_declared",
        restore=restore,
    )
    ctx.tx.update_workspace(context_id=new_context["id"])
    ctx.tx.emit(
        "context.saved",
        {
            "context_id": new_context["id"],
            "applicability": new_context["applicability"],
            "restore_preset": restore,
        },
    )
    return DomainResult(200, [{"kind": "ContextSnapshot", "id": new_context["id"]}])


def handle_check(ctx, payload):
    context = _current_context(ctx)
    changes = {key: FIXED_LOCAL_DEVICE[key] for key in DEVICE_CONTEXT_FIELDS}
    saved = context
    if any(context.get(key) != value for key, value in changes.items()):
        saved = _new_context(ctx, context, changes, origin="simulated")
        ctx.tx.update_workspace(context_id=saved["id"])
    recognized = {
        **changes,
        "os_name": FIXED_LOCAL_DEVICE["os_name"],
        "desktop_resolution": dict(FIXED_LOCAL_DEVICE["desktop_resolution"]),
    }
    scan = ctx.tx.insert(
        "DeviceScan",
        {
            "recognized": recognized,
            "unknown_fields": [],
            "context_id": saved["id"],
            "context_revision": saved["revision"],
            "note": "固定设备配置；未执行系统查询或真实硬件探测。",
        },
        origin="simulated",
    )
    ctx.tx.emit("device.checked", {"scan_id": scan["id"], "context_id": saved["id"]})
    return DomainResult(
        200,
        [
            {"kind": "DeviceScan", "id": scan["id"]},
            {"kind": "ContextSnapshot", "id": saved["id"]},
        ],
        data={"recognized": recognized},
    )


def handle_scan(ctx, payload):
    context = _current_context(ctx)
    fixture = _fixture_context(ctx)
    recognized = {
        "gpu_name": context.get("gpu_name"),
        "game_name": context.get("game_name"),
        "resolution": context.get("resolution"),
    }
    unknown = [key for key, _ in QUESTION_FIELDS if context.get(key) is None]
    scan = ctx.tx.insert(
        "DeviceScan",
        {
            "recognized": recognized,
            "unknown_fields": unknown,
            "note": "模拟设备识别：未读取系统、驱动或游戏进程。",
            "context_id": context["id"],
            "context_revision": context["revision"],
        },
        origin="simulated",
    )
    request = _open_request(ctx)
    if request is None and not unknown:
        latest = ctx.tx.find("InformationRequest")
        request = latest[-1] if latest else None
    created = False
    if request is None:
        question_items = [
            {"field": field, "prompt": prompt}
            for field, prompt in QUESTION_FIELDS
            if field in unknown
        ]
        request = ctx.tx.insert(
            "InformationRequest",
            {
                "status": "resolved" if not question_items else "open",
                "context_id": context["id"],
                "context_revision": context["revision"],
                "fields": question_items,
                "answers": {},
                "skipped_fields": [],
            },
            origin="simulated",
        )
        created = True
    ctx.tx.emit(
        "device.scan",
        {"scan_id": scan["id"], "request_id": request["id"], "created": created},
    )
    refs = [
        {"kind": "DeviceScan", "id": scan["id"]},
        {"kind": "InformationRequest", "id": request["id"]},
    ]
    return DomainResult(200, refs, data={"recognized": recognized, "unknown_fields": unknown})


def _check_revisions(ctx, payload, request, *, allow_closed=False):
    if request["revision"] != payload["question_revision"]:
        raise DemoError(
            "question_conflict",
            "问题已在其他位置更新，请刷新后重新回答。",
            status=409,
        )
    context = _current_context(ctx)
    if context["revision"] != payload["context_revision"]:
        raise DemoError(
            "context_conflict",
            "背景已在其他位置更新，请刷新后重新回答。",
            status=409,
        )
    if request["status"] != "open" and not allow_closed:
        raise DemoError("request_closed", "该补充问题已经结束。", status=409)


def _apply_answers(ctx, payload):
    request = ctx.tx.get(payload["request_id"], kind="InformationRequest")
    _check_revisions(ctx, payload, request)
    answers = payload["answers"]
    skipped = list(payload.get("skipped_fields") or [])
    known_fields = {item["field"] for item in request["fields"]}
    if not set(answers) <= known_fields:
        raise DemoError(
            "unknown_question",
            "回答包含未提问的字段。",
            field_errors=[
                {"field": key, "error": "not_asked"}
                for key in sorted(set(answers) - known_fields)
            ],
        )
    if not set(skipped) <= known_fields:
        raise DemoError("unknown_question", "跳过项包含未提问的字段。")
    answer = ctx.tx.insert(
        "InformationAnswer",
        {
            "request_id": request["id"],
            "question_revision": request["revision"],
            "answers": answers,
            "skipped_fields": skipped,
            "declared_by": "player",
            "note": "玩家声明，不是自动探测结果。",
        },
        origin="user_declared",
    )
    merged_answers = dict(request.get("answers", {}))
    merged_answers.update(answers)
    merged_skipped = sorted(set(request.get("skipped_fields", [])) | set(skipped))
    updated = {
        "status": "open",
        "answers": merged_answers,
        "skipped_fields": merged_skipped,
    }
    remaining = _remaining_fields({**request, **updated})
    updated["status"] = "resolved" if not remaining else "open"
    ctx.tx.update_entity(
        request["id"],
        updated,
        revision=request["revision"] + 1,
    )
    refs = [
        {"kind": "InformationAnswer", "id": answer["id"]},
        {"kind": "InformationRequest", "id": request["id"]},
    ]
    context = _current_context(ctx)
    if answers:
        new_context = _new_context(
            ctx, context, dict(answers), origin="user_declared"
        )
        ctx.tx.update_workspace(context_id=new_context["id"])
        refs.append({"kind": "ContextSnapshot", "id": new_context["id"]})
    ctx.tx.emit(
        "information.answered",
        {
            "request_id": request["id"],
            "answer_id": answer["id"],
            "remaining": [item["field"] for item in remaining],
        },
    )
    return DomainResult(200, refs)


def handle_answer(ctx, payload):
    return _apply_answers(ctx, payload)


def handle_amend(ctx, payload):
    request = ctx.tx.get(payload["request_id"], kind="InformationRequest")
    if request["status"] == "open":
        _check_revisions(ctx, payload, request)
        return _apply_answers(ctx, payload)
    # A correction must be based on the question and background the caller
    # actually saw. A newer background revision makes this draft stale even
    # though the original request is already closed.
    _check_revisions(ctx, payload, request, allow_closed=True)
    # A resolved or skipped request is not resurrected. A correction creates a
    # fresh request and a derived context snapshot; old answers and results stay.
    context = _current_context(ctx)
    fields = [
        {"field": field, "prompt": f"更正：{prompt}"}
        for field, prompt in QUESTION_FIELDS
        if field in payload["answers"] or field in (payload.get("skipped_fields") or [])
    ]
    if not fields:
        raise DemoError("nothing_to_amend", "没有需要更正的字段。")
    new_request = ctx.tx.insert(
        "InformationRequest",
        {
            "status": "resolved" if not fields else "open",
            "context_id": context["id"],
            "context_revision": context["revision"],
            "fields": fields,
            "answers": {},
            "skipped_fields": [],
            "correction_of": request["id"],
        },
        origin="user_declared",
    )
    correction = dict(payload)
    correction["request_id"] = new_request["id"]
    correction["question_revision"] = new_request["revision"]
    correction["context_revision"] = context["revision"]
    return _apply_answers(ctx, correction)
