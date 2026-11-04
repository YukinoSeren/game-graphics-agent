"""Conversation-led first use for the isolated presentation workspace."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from .contracts import DemoError, DomainResult
from .service import require_fields

GAME_ID = "call-of-duty-modern-warfare-ii"
GAME_NAME = "使命召唤：现代战争 I"
REPLY_DELAY_SECONDS = 3
RESEARCH_SECONDS = 60
FLOW_VERSION = "demo-flow/2.0"
GAME_PATTERN = re.compile(
    r"(?i)(?:使命召唤\s*[:：]?\s*)?现代战争\s*(?:II|I|[12一])|"
    r"(?:call\s+of\s+duty\s*[:：]?\s*)?modern\s+warfare\s*(?:II|I|[12])|"
    r"\bmwii\b|\bmw[12]\b"
)
EXPERIENCE_HINTS = (
    "卡顿", "流畅", "帧率", "帧数", "清晰", "画质", "画面", "延迟", "稳定", "稳", "高帧",
    "低延迟", "少卡", "不卡", "性能", "温度", "安静", "功耗", "视觉", "效果",
)
PREFERENCE_GROUPS = {
    "画面丰富度": ("丰富", "层次", "氛围", "材质"),
    "流畅度优先": ("卡顿", "流畅", "帧率", "帧数", "少卡", "不卡", "稳定", "高帧"),
    "画面清晰": ("清晰", "画质", "画面", "视觉", "效果"),
    "低延迟": ("延迟", "响应"),
    "降低发热与功耗": ("温度", "安静", "功耗"),
}


def register(service) -> None:
    service.register("intent.submit", validate_submit, handle_submit)


def validate_submit(payload):
    require_fields(payload, required=("message",), allowed=("message", "reply_to"))
    message = payload["message"]
    if not isinstance(message, str) or not message.strip():
        raise DemoError("invalid_message", "请先告诉我你想玩什么游戏、希望得到怎样的体验。")
    if len(message.strip()) > 2000:
        raise DemoError("message_too_long", "这条需求有些长，请精简到 2000 字以内。")
    if "reply_to" in payload and payload["reply_to"] is not None and not isinstance(payload["reply_to"], str):
        raise DemoError("invalid_reply", "补充内容无法关联到当前需求。")
    return {"message": message.strip(), "reply_to": payload.get("reply_to")}


def _preferences(text: str) -> list[str]:
    return [label for label, words in PREFERENCE_GROUPS.items() if any(word in text for word in words)]


def handle_submit(ctx, payload):
    message = payload["message"]
    reply_to = payload.get("reply_to")
    parent = ctx.tx.get(reply_to, kind="GameIntent") if reply_to else None
    if parent and parent.get("status") not in ("needs_experience", "needs_game"):
        raise DemoError("invalid_reply", "这条补充需求已处理，请从新的游戏需求开始。", status=409)

    normalized = " ".join(message.casefold().split())
    fingerprint = hashlib.sha256(f"{parent['id'] if parent else ''}\n{normalized}".encode("utf-8")).hexdigest()
    previous = [item for item in ctx.tx.find("GameIntent", fingerprint=fingerprint)
                if item.get("flow_version") == FLOW_VERSION
                and item.get("context_id") == ctx.tx.workspace()["context_id"]]
    if previous:
        intent = previous[0]
        refs = [{"kind": "GameIntent", "id": intent["id"]}]
        if intent.get("task_id"):
            refs.append({"kind": "Task", "id": intent["task_id"]})
        if intent.get("card_id"):
            refs.append({"kind": "GameCard", "id": intent["card_id"]})
        return DomainResult(
            202 if intent.get("task_id") else 200,
            refs=refs,
            task_id=intent.get("task_id"),
            data={"status": intent["status"], "reply": intent["reply"]},
        )

    match = GAME_PATTERN.search(message)
    game_id = GAME_ID if match or (parent and parent.get("game_id") == GAME_ID) else None
    game_name = GAME_NAME if game_id else None
    experience = GAME_PATTERN.sub("", message).strip(" \t\r\n，,。.!！?？:：-—") if match else message
    preferences = _preferences(experience)
    unsupported_hint = any(word in message.casefold() for word in ("地平线", "forza", "horizon", "原神", "apex", "cs2"))
    if not game_id and not unsupported_hint:
        status = "needs_game"
        reply = "请告诉我游戏的完整名称。目前可以为使命召唤：现代战争 I 整理资料。"
    elif not game_id:
        status = "unsupported"
        reply = "目前还不能为这款游戏整理资料。你可以换一款游戏，或告诉我希望改善的具体体验。"
    elif len(experience) < 2 or not any(word in experience for word in EXPERIENCE_HINTS):
        status = "needs_experience"
        reply = "好的，你希望这款游戏带来什么样的体验？"
    else:
        status = "researching"
        reply = "我先整理这款游戏的资料，并根据你的设备和体验目标生成设置方案。完成后会一起加入“我的游戏”。"

    reply_at = datetime.fromisoformat(ctx.clock.now_iso()) + timedelta(seconds=REPLY_DELAY_SECONDS)
    card_not_before = (reply_at + timedelta(seconds=RESEARCH_SECONDS)).isoformat()
    intent = ctx.tx.insert(
        "GameIntent",
        {
            "fingerprint": fingerprint,
            "flow_version": FLOW_VERSION,
            "context_id": ctx.tx.workspace()["context_id"],
            "parent_intent_id": parent["id"] if parent else None,
            "raw_text": message,
            "game_id": game_id,
            "game_name": game_name,
            "experience_text": experience,
            "preferences": preferences,
            "status": status,
            "reply": reply,
            "reply_visible_at": reply_at.isoformat(),
            "card_not_before": card_not_before if status == "researching" else None,
            "task_id": None,
            "card_id": None,
        },
        origin="user_declared",
    )
    ctx.tx.insert(
        "ConversationMessage",
        {"role": "user", "content": message, "intent_id": intent["id"]},
        origin="user_declared",
    )

    refs = [{"kind": "GameIntent", "id": intent["id"]}]
    task_id = None
    if status == "researching":
        from .device import DEVICE_CONTEXT_FIELDS, FIXED_LOCAL_DEVICE, handle_check

        scans = ctx.tx.find("DeviceScan")
        last_profile = scans[-1].get("recognized", {}) if scans else {}
        context = ctx.tx.get(ctx.tx.workspace()["context_id"], kind="ContextSnapshot")
        declared_hardware = context.get("data_origin") == "user_declared" and any(
            context.get(key) is not None and context.get(key) != FIXED_LOCAL_DEVICE[key]
            for key in DEVICE_CONTEXT_FIELDS
            if key != "gpu_name" or key in context.get("declared_fields", [])
            or context.get(key) != ctx.scenario["context"].get(key)
        )
        if not declared_hardware and any(last_profile.get(key) != FIXED_LOCAL_DEVICE[key] for key in DEVICE_CONTEXT_FIELDS):
            device_reply = handle_check(ctx, {})
            refs.extend(device_reply.refs)
    if status == "researching":
        from .knowledge import handle_collect

        result = handle_collect(ctx, {"intent_id": intent["id"]})
        task_id = result.task_id
        ctx.tx.update_entity(intent["id"], {"task_id": task_id, "context_id": ctx.tx.workspace()["context_id"]}, revision=intent["revision"] + 1)
        refs.append({"kind": "Task", "id": task_id})
        ctx.tx.insert(
            "ConversationMessage",
            {"role": "assistant", "content": reply, "intent_id": intent["id"], "task_id": task_id,
             "visible_at": reply_at.isoformat()},
            origin="simulated",
        )
    else:
        ctx.tx.update_entity(intent["id"], {"status": status}, revision=intent["revision"] + 1)
        ctx.tx.insert(
            "ConversationMessage",
            {"role": "assistant", "content": reply, "intent_id": intent["id"], "visible_at": reply_at.isoformat()},
            origin="simulated",
        )

    return DomainResult(
        202 if task_id else 200,
        refs=refs,
        task_id=task_id,
        data={"status": status, "reply": reply, "experience_text": experience, "preferences": preferences},
    )
