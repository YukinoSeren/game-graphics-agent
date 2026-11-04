"""Demo configuration catalog, adoption, edits, rollback, and history record."""

from __future__ import annotations

from .contracts import DemoError, DomainResult
from .service import require_fields

ADOPT_ORIGINS = ("baseline", "recommendation")


def register(service) -> None:
    service.register("setting.register", validate_register, handle_register)
    service.register("configuration.adopt", validate_adopt, handle_adopt)
    service.register("configuration.edit", validate_edit, handle_edit)
    service.register("configuration.rollback", validate_rollback, handle_rollback)
    service.register(
        "configuration.record_history", validate_record_history, handle_record_history
    )


# -- validation -------------------------------------------------------
def _require_confirmation(payload):
    value = payload.get("confirmation")
    if not isinstance(value, str) or not value.strip():
        raise DemoError(
            "confirmation_required",
            "确认采用或恢复需要玩家明确声明。",
            field_errors=[{"field": "confirmation", "error": "required"}],
        )


def validate_adopt(payload):
    require_fields(
        payload,
        required=("origin", "confirmation"),
        allowed=(
            "origin",
            "recommendation_id",
            "expected_current_id",
            "settings",
            "preset",
            "confirmation",
        ),
    )
    if payload["origin"] not in ADOPT_ORIGINS:
        raise DemoError("invalid_origin", "采用来源必须是 baseline 或 recommendation。")
    _require_confirmation(payload)
    if payload.get("settings") is not None and not isinstance(payload["settings"], dict):
        raise DemoError("invalid_settings", "settings 必须是对象。")
    return payload


def validate_edit(payload):
    require_fields(
        payload,
        required=("expected_current_id", "changes", "confirmation"),
        allowed=("expected_current_id", "changes", "confirmation", "change_set_id"),
    )
    _require_confirmation(payload)
    if not isinstance(payload["changes"], dict) or not payload["changes"]:
        raise DemoError("invalid_changes", "至少需要一项设置变化。")
    return payload


def validate_rollback(payload):
    require_fields(
        payload,
        required=("expected_current_id", "target_id", "confirmation"),
        allowed=("expected_current_id", "target_id", "confirmation"),
    )
    _require_confirmation(payload)
    return payload


def validate_record_history(payload):
    require_fields(
        payload,
        required=("settings", "occurred_at", "basis"),
        allowed=("settings", "occurred_at", "basis", "confirmation"),
    )
    if not isinstance(payload["settings"], dict) or not payload["settings"]:
        raise DemoError("invalid_settings", "补录配置至少需要一项设置。")
    if not isinstance(payload["occurred_at"], str) or not payload["occurred_at"].strip():
        raise DemoError("invalid_time", "补录配置需要过去的时间依据。")
    if not isinstance(payload["basis"], str) or not payload["basis"].strip():
        raise DemoError("invalid_basis", "补录配置需要声明依据。")
    return payload


def validate_register(payload):
    require_fields(
        payload,
        required=("display_name", "group", "value_type"),
        allowed=(
            "display_name",
            "group",
            "value_type",
            "allowed_values",
            "value",
            "reason",
        ),
    )
    if payload["value_type"] not in ("quality", "fixed", "unknown"):
        raise DemoError("invalid_value_type", "设置类型必须是 quality、fixed 或 unknown。")
    allowed = payload.get("allowed_values") or []
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise DemoError("invalid_allowed_values", "allowed_values 必须是文本列表。")
    if payload["value_type"] != "unknown" and not allowed:
        raise DemoError("invalid_allowed_values", "已知设置需要至少一个可选值。")
    if "value" in payload and payload["value"] is not None and payload["value"] not in allowed:
        raise DemoError("invalid_value", "设置的当前值不在可选值范围内。")
    return payload


# -- helpers ----------------------------------------------------------
def _declarations(tx):
    return {declaration["id"]: declaration for declaration in tx.find("SettingDeclaration")}


def _validate_settings(tx, scenario, settings):
    catalog = {item["id"]: item for item in scenario["settings"]}
    declarations = _declarations(tx)
    for setting_id, value in settings.items():
        if setting_id in catalog:
            item = catalog[setting_id]
            if value is None:
                if not item["unknown"] and item["type"] != "unknown":
                    raise DemoError(
                        "invalid_setting_value",
                        f"{setting_id} 不能为未知。",
                        field_errors=[{"field": setting_id, "error": "null_not_allowed"}],
                    )
                continue
            if value not in item["allowed_values"]:
                raise DemoError(
                    "invalid_setting_value",
                    f"{setting_id} 的取值不在演示目录中。",
                    field_errors=[{"field": setting_id, "error": "not_allowed"}],
                )
        elif setting_id in declarations:
            declaration = declarations[setting_id]
            if value is None and declaration["value_type"] == "unknown":
                continue
            if value not in declaration["allowed_values"]:
                raise DemoError(
                    "invalid_setting_value",
                    f"{setting_id} 的取值不在声明范围内。",
                    field_errors=[{"field": setting_id, "error": "not_allowed"}],
                )
        else:
            raise DemoError(
                "unknown_setting",
                f"设置 {setting_id} 不在演示目录中。",
                field_errors=[{"field": setting_id, "error": "unknown"}],
            )


def _validate_values_only(tx, scenario, settings):
    "Validate value shape without requiring every key to be known."
    catalog = {item["id"]: item for item in scenario["settings"]}
    declarations = _declarations(tx)
    for setting_id, value in settings.items():
        if setting_id in catalog:
            item = catalog[setting_id]
            if value is None:
                if not item["unknown"] and item["type"] != "unknown":
                    raise DemoError("invalid_setting_value", f"{setting_id} 不能为未知。")
            elif value not in item["allowed_values"]:
                raise DemoError("invalid_setting_value", f"{setting_id} 的取值不在演示目录中。")
        elif setting_id in declarations:
            if value is not None and value not in declarations[setting_id]["allowed_values"]:
                raise DemoError("invalid_setting_value", f"{setting_id} 的取值不在声明范围内。")
        else:
            raise DemoError("unknown_setting", f"设置 {setting_id} 不在演示目录中。")


def _diff(parent, child):
    changes = {}
    for key in sorted(set(parent) | set(child)):
        before = parent.get(key)
        after = child.get(key)
        if before != after:
            changes[key] = {"from": before, "to": after}
    return changes


def _insert_configuration(
    ctx,
    *,
    settings,
    origin,
    parent,
    recommendation_id,
    preset,
    confirmation,
    entity_origin="user_declared",
    **extra,
):
    payload = {
        "context_id": ctx.tx.workspace()["context_id"],
        "parent_id": parent["id"] if parent else None,
        "origin": origin,
        "recommendation_id": recommendation_id,
        "settings": settings,
        "preset": preset,
        "confirmation": confirmation,
        "activation": "user_declared",
        "changes_from_parent": _diff(parent["settings"], settings) if parent else {},
        "rollback_target_id": None,
        "change_set_id": None,
    }
    payload.update(extra)
    return ctx.tx.insert("AppliedConfiguration", payload, origin=entity_origin)


def _current_or_error(ctx, expected, *, allow_none=False):
    workspace = ctx.tx.workspace()
    current_id = workspace["current_applied_id"]
    if expected != current_id:
        raise DemoError(
            "configuration_conflict",
            "当前采用版本已在其他位置变化，请刷新后重新确认。",
            status=409,
            recovery_actions=["refresh"],
        )
    if current_id is None:
        if allow_none:
            return None
        raise DemoError("no_configuration", "当前还没有可编辑的采用版本。", status=409)
    return ctx.tx.get(current_id, kind="AppliedConfiguration")


# -- handlers ---------------------------------------------------------
def handle_register(ctx, payload):
    declaration = ctx.tx.insert(
        "SettingDeclaration",
        {
            "display_name": payload["display_name"],
            "group": payload["group"],
            "value_type": payload["value_type"],
            "allowed_values": payload.get("allowed_values") or [],
            "value": payload.get("value"),
            "reason": payload.get("reason", "玩家补充的本地设置声明"),
        },
        origin="user_declared",
    )
    ctx.tx.emit("setting.registered", {"setting_id": declaration["id"]})
    return DomainResult(
        201, [{"kind": "SettingDeclaration", "id": declaration["id"]}]
    )


def handle_adopt(ctx, payload):
    parent = _current_or_error(
        ctx, payload.get("expected_current_id"), allow_none=True
    )
    scenario = ctx.scenario
    recommendation_id = payload.get("recommendation_id")
    if payload["origin"] == "baseline":
        if recommendation_id is not None:
            raise DemoError("invalid_origin", "原配置基线不能引用推荐。")
        preset = payload.get("preset") or "baseline"
        values = payload.get("settings")
        if not values:
            values = dict(scenario["templates"].get(preset, {}).get("values") or {})
        recommendation_id = None
    else:
        if not recommendation_id:
            raise DemoError("missing_field", "采用推荐需要 recommendation_id。")
        recommendation = ctx.tx.get(recommendation_id, kind="Recommendation")
        workspace_context_id = ctx.tx.workspace()["context_id"]
        if recommendation["context_id"] != workspace_context_id:
            raise DemoError(
                "context_mismatch",
                "推荐是基于另一份背景生成的，请先重新生成或明确复核后再采用。",
                status=409,
                recovery_actions=["refresh", "regenerate_recommendation"],
            )
        values = {
            item["setting_id"]: item["value"] for item in recommendation["settings"]
        }
        values.update(payload.get("settings") or {})
        preset = payload.get("preset") or recommendation["template"]
    if not values:
        raise DemoError("invalid_settings", "采用方案至少需要一项设置。")
    _validate_settings(ctx.tx, scenario, values)
    configuration = _insert_configuration(
        ctx,
        settings=values,
        origin=payload["origin"],
        parent=parent,
        recommendation_id=recommendation_id,
        preset=preset,
        confirmation=payload["confirmation"],
    )
    ctx.tx.update_workspace(current_applied_id=configuration["id"])
    ctx.tx.emit(
        "configuration.adopted",
        {
            "configuration_id": configuration["id"],
            "parent_id": configuration["parent_id"],
            "origin": configuration["origin"],
        },
    )
    return DomainResult(201, [{"kind": "AppliedConfiguration", "id": configuration["id"]}])


def _validate_change_set(ctx, current, changes, change_set_id):
    if change_set_id is None:
        return
    change_set = ctx.tx.get(change_set_id, kind="ChangeSet")
    if change_set.get("status", "proposed") != "proposed":
        raise DemoError(
            "change_set_not_current",
            "该变更建议已被后续结论取代，请重新获取建议。",
            status=409,
            recovery_actions=["refresh"],
        )
    if change_set.get("base_applied_id") != current["id"]:
        raise DemoError(
            "change_set_mismatch",
            "该变更建议不是基于当前采用版本，请刷新后重新确认。",
            status=409,
            recovery_actions=["refresh"],
        )
    expected = {
        setting_id: change.get("to")
        for setting_id, change in change_set.get("changes", {}).items()
    }
    if set(changes) != set(expected):
        raise DemoError(
            "change_set_mismatch",
            "手动调整的项与变更建议不一致；纯手动调整请不要附带变更集。",
            status=409,
        )
    for setting_id, target in expected.items():
        change = change_set["changes"][setting_id]
        if changes[setting_id] != target:
            raise DemoError(
                "change_set_mismatch",
                "提交的新值与变更建议不一致。",
                status=409,
                field_errors=[
                    {"field": setting_id, "expected": target, "received": changes[setting_id]}
                ],
            )
        if current["settings"].get(setting_id) != change.get("from"):
            raise DemoError(
                "change_set_mismatch",
                "当前配置的原值与变更建议记录的旧值不一致。",
                status=409,
                field_errors=[
                    {
                        "field": setting_id,
                        "expected": change.get("from"),
                        "received": current["settings"].get(setting_id),
                    }
                ],
            )


def handle_edit(ctx, payload):
    current = _current_or_error(ctx, payload["expected_current_id"])
    _validate_settings(ctx.tx, ctx.scenario, payload["changes"])
    _validate_change_set(
        ctx, current, payload["changes"], payload.get("change_set_id")
    )
    settings = {**current["settings"], **payload["changes"]}
    configuration = _insert_configuration(
        ctx,
        settings=settings,
        origin="manual",
        parent=current,
        recommendation_id=None,
        preset=current.get("preset"),
        confirmation=payload["confirmation"],
        change_set_id=payload.get("change_set_id"),
    )
    ctx.tx.update_workspace(current_applied_id=configuration["id"])
    ctx.tx.emit(
        "configuration.edited",
        {"configuration_id": configuration["id"], "changed": sorted(payload["changes"])},
    )
    return DomainResult(201, [{"kind": "AppliedConfiguration", "id": configuration["id"]}])


def handle_rollback(ctx, payload):
    current = _current_or_error(ctx, payload["expected_current_id"])
    target = ctx.tx.get(payload["target_id"], kind="AppliedConfiguration")
    settings = dict(current["settings"])
    for setting_id, value in target["settings"].items():
        settings[setting_id] = value
    _validate_settings(ctx.tx, ctx.scenario, settings)
    configuration = _insert_configuration(
        ctx,
        settings=settings,
        origin="rollback",
        parent=current,
        recommendation_id=None,
        preset=target.get("preset"),
        confirmation=payload["confirmation"],
        rollback_target_id=target["id"],
    )
    ctx.tx.update_workspace(current_applied_id=configuration["id"])
    ctx.tx.emit(
        "configuration.rolled_back",
        {"configuration_id": configuration["id"], "target_id": target["id"]},
    )
    return DomainResult(
        201,
        [{"kind": "AppliedConfiguration", "id": configuration["id"]}],
    )


def handle_record_history(ctx, payload):
    _validate_settings(ctx.tx, ctx.scenario, payload["settings"])
    configuration = _insert_configuration(
        ctx,
        settings=dict(payload["settings"]),
        origin="history_record",
        parent=None,
        recommendation_id=None,
        preset=payload.get("preset"),
        confirmation=payload.get("confirmation") or "补录的过去配置（玩家声明）",
        occurred_at=payload["occurred_at"],
        basis=payload["basis"],
    )
    ctx.tx.emit(
        "configuration.recorded",
        {"configuration_id": configuration["id"], "occurred_at": payload["occurred_at"]},
    )
    return DomainResult(201, [{"kind": "AppliedConfiguration", "id": configuration["id"]}])
