"""Demo knowledge collection, import, follow-up, and revision updates."""

from __future__ import annotations

import hashlib
import json

from .contracts import DemoError, DomainResult
from .service import require_fields

PHASES = [
    {"key": "discover", "label": "发现来源", "seconds": 1.4},
    {"key": "read", "label": "读取内容", "seconds": 1.2},
    {"key": "extract", "label": "提取记录", "seconds": 1.4},
    {"key": "validate", "label": "校验与去重", "seconds": 1.2},
    {"key": "followup", "label": "补查缺口", "seconds": 1.2},
    {"key": "organize", "label": "整理知识", "seconds": 1.0},
]
MAX_STAGES = len(PHASES)
PACKAGE_SCHEMA = "demo-knowledge/1"


def register(service) -> None:
    service.register("knowledge.collect", validate_collect, handle_collect)
    service.register("knowledge.import", validate_import, handle_import)
    service.register("knowledge.followup", validate_followup, handle_followup)
    service.register("knowledge.update", validate_update, handle_update)
    service.tasks.register("knowledge.collect", PHASES, _publisher(service))


# -- validation -------------------------------------------------------
def validate_collect(payload):
    require_fields(
        payload,
        required=(),
        allowed=("source_ids", "mode", "budget", "intent_id"),
    )
    mode = payload.get("mode", "discover")
    if mode not in ("discover", "seeded"):
        raise DemoError("invalid_mode", "采集模式必须是 discover 或 seeded。")
    source_ids = payload.get("source_ids") or []
    if not isinstance(source_ids, list) or not all(
        isinstance(item, str) for item in source_ids
    ):
        raise DemoError("invalid_sources", "source_ids 必须是来源标识列表。")
    budget = payload.get("budget") or {}
    if not isinstance(budget, dict):
        raise DemoError("invalid_budget", "budget 必须是对象。")
    unknown = set(budget) - {"max_sources", "max_stages"}
    if unknown:
        raise DemoError("unknown_field", "budget 包含未知字段。")
    for key in ("max_sources", "max_stages"):
        if key in budget and (
            not isinstance(budget[key], int)
            or isinstance(budget[key], bool)
            or budget[key] < 1
        ):
            raise DemoError("invalid_budget", f"{key} 必须是正整数。")
    return payload


def validate_import(payload):
    return require_fields(payload, required=("file_id",), allowed=("file_id",))


def validate_followup(payload):
    return require_fields(
        payload,
        required=("knowledge_id",),
        allowed=("knowledge_id", "gap_id"),
    )


def validate_update(payload):
    return require_fields(payload, required=("knowledge_id",), allowed=("knowledge_id",))


# -- collection -------------------------------------------------------
def handle_collect(ctx, payload):
    active = ctx.tx.active_tasks()
    if active:
        raise DemoError(
            "task_busy",
            "当前已有资料整理任务，请等待完成或取消后再开始。",
            status=409,
            recovery_actions=["cancel_task"],
        )
    scenario = ctx.scenario
    known = {source["id"] for source in scenario["sources"]}
    selected = payload.get("source_ids") or sorted(known)
    missing = [item for item in selected if item not in known]
    if missing:
        raise DemoError(
            "unknown_source",
            "所选资料来源不存在。",
            field_errors=[{"field": "source_ids", "error": "unknown", "value": item} for item in missing],
        )
    intent_id = payload.get("intent_id")
    if intent_id:
        intent = ctx.tx.get(intent_id, kind="GameIntent")
        if intent.get("status") != "researching":
            raise DemoError("intent_not_ready", "这条游戏需求目前不能开始资料整理。", status=409)
    task = ctx.service.tasks.enqueue(
        ctx.tx,
        "knowledge.collect",
        {
            "source_ids": selected,
            "mode": payload.get("mode", "discover"),
            "budget": payload.get("budget") or {},
            "script": ctx.tx.workspace()["script"],
            "intent_id": intent_id,
            "card_not_before": intent.get("card_not_before") if intent_id else None,
        },
    )
    ctx.tx.emit("knowledge.collect.queued", {"task_id": task["id"]})
    return DomainResult(202, data={"task": task}, task_id=task["id"])


def _publisher(service):
    def publish(tx, task, phase):
        scenario = service.catalog.get(tx.scope.scenario_id)
        input_data = task["input"]
        result = dict(task["result"])
        index = task["phase_index"]
        budget = input_data.get("budget") or {}
        max_stages = min(int(budget.get("max_stages", MAX_STAGES)), MAX_STAGES)
        stop_after = (index + 1) >= max_stages
        key = phase["key"]
        detail = {}

        if key == "discover":
            sources = _selected_sources(tx, scenario, input_data)
            source_ids = []
            for source in sources:
                entity_id = _ensure_source(tx, source)
                source_ids.append(entity_id)
            result["source_ids"] = source_ids
            detail = {"sources": len(source_ids)}
        elif key == "read":
            excerpt_ids = []
            for source_id in result.get("source_ids", []):
                source = tx.get(source_id, kind="Source")
                paragraph = next(
                    (
                        block.get("text")
                        for block in source.get("content_blocks", [])
                        if block.get("type") == "paragraph"
                    ),
                    "",
                )
                excerpt_ids.append(
                    tx.insert(
                        "SourceExcerpt",
                        {"source_id": source_id, "text": paragraph},
                        origin="simulated",
                    )["id"]
                )
            result["excerpt_ids"] = excerpt_ids
            detail = {"excerpts": len(excerpt_ids)}
        elif key == "extract":
            record_ids = []
            template = scenario["templates"]["main"]["values"]
            for source_id in result.get("source_ids", []):
                source = tx.get(source_id, kind="Source")
                for setting_id in source.get("setting_ids", []):
                    record = tx.insert(
                        "EvidenceRecord",
                        {
                            "source_id": source_id,
                            "source_url": source["url"],
                            "setting_id": setting_id,
                            "value": template.get(setting_id),
                            "status": "pending",
                            "reason": None,
                            "independent_support": source["relationship"] == "original",
                        },
                        origin="simulated",
                    )
                    record_ids.append(record["id"])
            result["record_ids"] = record_ids
            detail = {"records": len(record_ids)}
        elif key == "validate":
            accepted = 0
            quarantined = []
            for record_id in result.get("record_ids", []):
                record = tx.get(record_id, kind="EvidenceRecord")
                if record["value"] is None:
                    tx.update_entity(
                        record_id,
                        {"status": "quarantined", "reason": "unknown_value"},
                        revision=2,
                    )
                    quarantined.append(record_id)
                else:
                    reason = None
                    if not record["independent_support"]:
                        reason = "转载来源，不构成独立支持"
                    tx.update_entity(
                        record_id,
                        {"status": "accepted", "reason": reason},
                        revision=2,
                    )
                    accepted += 1
            result["quarantined_ids"] = quarantined
            detail = {"accepted": accepted, "quarantined": len(quarantined)}
        elif key == "followup" and scenario.get("source_pack_version") == "mw1-richness/2":
            result["gap_ids"] = []
            result["followup_ids"] = []
            detail = {"gaps": 0}
        elif key == "followup":
            gaps = [
                {
                    "code": "P1",
                    "label": "缺少本机性能数据",
                    "status": "open",
                    "detail": "需要先获得可比较的性能数据。",
                },
                {
                    "code": "version",
                    "label": "游戏版本未确认",
                    "status": "open",
                    "detail": "资料没有提供游戏版本号。",
                },
            ]
            gap_ids = []
            for gap in gaps:
                gap_ids.append(tx.insert("KnowledgeGap", gap, origin="simulated")["id"])
            note = tx.insert(
                "FollowUpNote",
                {
                    "gap_code": "version",
                    "summary": "资料未提供版本号，保留待玩家核对。",
                },
                origin="simulated",
            )
            gaps[1]["status"] = "resolved"
            tx.update_entity(gap_ids[1], {"status": "resolved"}, revision=2)
            result["gap_ids"] = gap_ids
            result["followup_ids"] = [note["id"]]
            detail = {"gaps": len(gap_ids)}
        elif key == "organize":
            snapshot = _create_snapshot(
                tx,
                result,
                status="completed",
                origin="simulated",
                mode=input_data.get("mode", "discover"),
                budget=budget,
                fingerprint=_collect_fingerprint(tx.scope.scenario_id, result),
            )
            result["knowledge_id"] = snapshot["id"]
            detail = {"knowledge_id": snapshot["id"]}
            intent_id = input_data.get("intent_id")
            if intent_id:
                intent = tx.get(intent_id, kind="GameIntent")
                if intent.get("status") == "researching":
                    from .recommendation import build_snapshot

                    context = tx.get(tx.workspace()["context_id"], kind="ContextSnapshot")
                    if scenario.get("source_pack_version") == "mw1-richness/2" and len(snapshot["source_ids"]) != 15:
                        raise DemoError("incomplete_pack", "资料整理未完成，请重新整理完整资料。")
                    recommendation = build_snapshot(tx, scenario, context, snapshot)
                    if recommendation["missing_evidence"]:
                        raise DemoError("incomplete_plan", "设置依据不完整，方案未发布。")
                    variants = [build_snapshot(tx, scenario, context, snapshot, name, variant_of=recommendation["id"])["id"] for name in ("quality", "performance")]
                    if intent.get("task_id") != task["id"]:
                        intent = tx.update_entity(
                            intent_id,
                            {"task_id": task["id"]},
                            revision=intent["revision"] + 1,
                        )
                    card_fields = {
                        "game_id": "call-of-duty-modern-warfare-ii",
                        "game_name": intent["game_name"],
                        "intent_id": intent_id,
                        "intent_created_at": intent["created_at"],
                        "knowledge_id": snapshot["id"],
                        "context_id": tx.workspace()["context_id"],
                        "recommendation_id": recommendation["id"],
                        "variant_ids": variants,
                        "source_pack_version": scenario.get("source_pack_version"),
                        "experience_text": intent["experience_text"],
                        "preferences": intent["preferences"],
                        "status": "plan_ready",
                        "current_step": "background",
                        }
                    existing_cards = tx.find(
                        "GameCard", game_id=card_fields["game_id"]
                    )
                    if existing_cards:
                        card = tx.update_entity(
                            existing_cards[-1]["id"],
                            card_fields,
                            revision=existing_cards[-1]["revision"] + 1,
                        )
                    else:
                        card = tx.insert("GameCard", card_fields, origin="simulated")
                    tx.update_entity(
                        intent_id,
                        {"status": "ready", "card_id": card["id"]},
                        revision=intent["revision"] + 1,
                    )
                    tx.emit("game.card.published", {"card_id": card["id"], "intent_id": intent_id})
                    tx.insert(
                        "ConversationMessage",
                        {
                            "role": "assistant",
                            "content": "已整理 15 份资料并生成设置方案，这款游戏已加入“我的游戏”。",
                            "intent_id": intent_id,
                            "card_id": card["id"],
                        },
                        origin="simulated",
                    )
                    detail["card_id"] = card["id"]
                    result["card_id"] = card["id"]
                    result["recommendation_id"] = recommendation["id"]

        update = {"result": result, "detail": detail}
        if stop_after and key != "organize":
            snapshot = _create_snapshot(
                tx,
                result,
                status="partial",
                origin="simulated",
                mode=input_data.get("mode", "discover"),
                budget=budget,
                fingerprint=_collect_fingerprint(tx.scope.scenario_id, result),
            )
            result["knowledge_id"] = snapshot["id"]
            update["status"] = "partial"
            update["detail"] = {**detail, "knowledge_id": snapshot["id"], "partial": True}
        return update

    return publish


def _selected_sources(tx, scenario, input_data):
    selected = set(input_data.get("source_ids") or [])
    sources = [s for s in scenario["sources"] if not selected or s["id"] in selected]
    limit = (input_data.get("budget") or {}).get("max_sources")
    if limit:
        sources = sources[: int(limit)]
    return sources


def _ensure_source(tx, source):
    for existing in tx.find("Source", url=source["id"]):
        if existing.get("source_revision") == source["revision"]:
            return existing["id"]
    created = tx.insert(
        "Source",
        {
            "url": source["id"],
            "title": source["title"],
            "author_label": source["author_label"],
            "published": source["published"],
            "source_revision": source["revision"],
            "relationship": source["relationship"],
            "related_ids": source.get("related_ids", []),
            "setting_ids": source.get("setting_ids", []),
            "content_blocks": source.get("content_blocks", []),
            "limitations": source["limitations"],
            **{key: source.get(key) for key in ("source_key", "channel", "summary", "display_blocks", "pack_version", "external_url")},
        },
        origin="simulated" if source.get("data_origin") == "simulated" else "fixture",
    )
    return created["id"]


def _collect_fingerprint(scenario_id, result):
    material = json.dumps(
        {
            "scenario_id": scenario_id,
            "sources": sorted(result.get("source_ids", [])),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _create_snapshot(
    tx,
    result,
    *,
    status,
    origin,
    mode,
    budget,
    fingerprint,
    revision=1,
    supersedes_id=None,
):
    record_ids = result.get("record_ids", [])
    records = []
    accepted = 0
    quarantined = 0
    for record_id in record_ids:
        record = tx.get(record_id, kind="EvidenceRecord")
        if record["status"] == "accepted":
            accepted += 1
        if record["status"] == "quarantined":
            quarantined += 1
        records.append(
            {
                "id": record_id,
                "status": record["status"],
                "setting_id": record["setting_id"],
                "source_id": record["source_id"],
                "independent_support": record["independent_support"],
            }
        )
    source_ids = result.get("source_ids", [])
    independent = sum(
        1
        for source_id in source_ids
        if tx.get(source_id, kind="Source")["relationship"] == "original"
    )
    return tx.insert(
        "KnowledgeSnapshot",
        {
            "source_ids": source_ids,
            "record_ids": record_ids,
            "gap_ids": result.get("gap_ids", []),
            "followup_ids": result.get("followup_ids", []),
            "records": records,
            "accepted_count": accepted,
            "quarantined_count": quarantined,
            "independent_source_count": independent,
            "status": status,
            "mode": mode,
            "budget": budget,
            "fingerprint": fingerprint,
            "supersedes_id": supersedes_id,
            "content_version": (tx.get(source_ids[0], kind="Source").get("pack_version") if source_ids else None) or "demo-kb-1",
            "source_pack_version": (tx.get(source_ids[0], kind="Source").get("pack_version") if source_ids else None),
        },
        origin=origin,
        revision=revision,
    )


# -- import -----------------------------------------------------------
def handle_import(ctx, payload):
    record = ctx.tx.get(payload["file_id"], kind="File")
    data = ctx.files.read(payload["file_id"], expected_hash=record["content_hash"])
    try:
        package = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemoError("invalid_package", "知识包不是有效 JSON。") from exc
    _validate_package(ctx, package)
    fingerprint = hashlib.sha256(data).hexdigest()
    existing = ctx.tx.find("KnowledgeSnapshot", fingerprint=fingerprint)
    if existing:
        latest = existing[-1]
        ctx.tx.emit("knowledge.reused", {"knowledge_id": latest["id"]})
        return DomainResult(200, [{"kind": "KnowledgeSnapshot", "id": latest["id"]}])

    source_ids = []
    for source in package["sources"]:
        source_ids.append(
            ctx.tx.insert(
                "Source",
                {
                    "url": source["id"],
                    "title": source["title"],
                    "author_label": source.get("author_label", "演示导入来源（模拟）"),
                    "published": source.get("published"),
                    "source_revision": source.get("revision", "r1"),
                    "relationship": source.get("relationship", "original"),
                    "related_ids": source.get("related_ids", []),
                    "setting_ids": source.get("setting_ids", []),
                    "content_blocks": source.get("content_blocks", []),
                    "limitations": source.get("limitations", "演示导入样例"),
                },
                origin="uploaded_demo_file",
            )["id"]
        )
    by_url = {
        ctx.tx.get(entity_id, kind="Source")["url"]: entity_id for entity_id in source_ids
    }
    record_ids = []
    for item in package.get("records", []):
        record_ids.append(
            ctx.tx.insert(
                "EvidenceRecord",
                {
                    "source_id": by_url[item["source_id"]],
                    "source_url": item["source_id"],
                    "setting_id": item["setting_id"],
                    "value": item.get("value"),
                    "status": item.get("status", "accepted"),
                    "reason": item.get("reason"),
                    "independent_support": item.get("independent_support", True),
                },
                origin="uploaded_demo_file",
            )["id"]
        )
    gap_ids = [
        ctx.tx.insert(
            "KnowledgeGap",
            {
                "code": item["code"],
                "label": item["label"],
                "status": item.get("status", "open"),
                "detail": item.get("detail", "导入演示知识包"),
            },
            origin="uploaded_demo_file",
        )["id"]
        for item in package.get("gaps", [])
    ]
    snapshot = _create_snapshot(
        ctx.tx,
        {"source_ids": source_ids, "record_ids": record_ids, "gap_ids": gap_ids},
        status="completed",
        origin="uploaded_demo_file",
        mode="import",
        budget={},
        fingerprint=fingerprint,
    )
    ctx.tx.emit("knowledge.imported", {"knowledge_id": snapshot["id"], "fingerprint": fingerprint})
    return DomainResult(201, [{"kind": "KnowledgeSnapshot", "id": snapshot["id"]}])


def _validate_package(ctx, package):
    if not isinstance(package, dict):
        raise DemoError("invalid_package", "知识包必须是 JSON 对象。")
    if package.get("schema") != PACKAGE_SCHEMA or package.get("demo_only") is not True:
        raise DemoError("invalid_package", "只支持 demo-knowledge/1 演示知识包。")
    if package.get("scenario_id") != ctx.scenario_id:
        raise DemoError(
            "cross_case_package",
            "知识包属于另一个演示案例，不能导入当前案例。",
            status=409,
        )
    sources = package.get("sources")
    if not isinstance(sources, list) or not sources:
        raise DemoError("invalid_package", "知识包缺少来源。")
    source_ids = {source.get("id") for source in sources if isinstance(source, dict)}
    if len(source_ids) != len(sources):
        raise DemoError("invalid_package", "知识包来源标识重复或缺失。")
    settings = {item["id"] for item in ctx.scenario["settings"]}
    for source in sources:
        if not isinstance(source.get("id"), str) or not source["id"].startswith("demo://"):
            raise DemoError("invalid_package", "来源标识必须使用 demo://。")
        for setting_id in source.get("setting_ids", []):
            if setting_id not in settings:
                raise DemoError("invalid_package", "知识包引用了不存在的设置。")
    for record in package.get("records", []):
        if record.get("source_id") not in source_ids:
            raise DemoError("invalid_package", "知识记录引用了不存在的来源。")
        if record.get("setting_id") not in settings:
            raise DemoError("invalid_package", "知识记录引用了不存在的设置。")


# -- follow-up and update ---------------------------------------------
def handle_followup(ctx, payload):
    knowledge = ctx.tx.get(payload["knowledge_id"], kind="KnowledgeSnapshot")
    gap_id = payload.get("gap_id")
    gaps = knowledge.get("gap_ids", [])
    if not gaps:
        raise DemoError("no_gap", "该知识快照没有可补查的缺口。", status=409)
    target_gap = gap_id or gaps[-1]
    gap = ctx.tx.get(target_gap, kind="KnowledgeGap")
    note = ctx.tx.insert(
        "FollowUpNote",
        {
            "gap_code": gap["code"],
            "summary": "模拟补查修订：保留缺口原因与下一步核对建议。",
            "knowledge_id": knowledge["id"],
        },
        origin="simulated",
    )
    # Gap facts are versioned, not edited in place: the old snapshot and the
    # old gap keep the meaning they had when they were published.
    resolved_gap = ctx.tx.insert(
        "KnowledgeGap",
        {
            "code": gap["code"],
            "label": gap["label"],
            "status": "resolved",
            "detail": gap["detail"],
            "previous_status": gap["status"],
            "supersedes_id": gap["id"],
            "resolved_by": note["id"],
        },
        origin="simulated",
        revision=gap["revision"] + 1,
    )
    new_gaps = [resolved_gap["id"] if item == target_gap else item for item in gaps]
    new_result = {
        "source_ids": knowledge["source_ids"],
        "record_ids": knowledge["record_ids"],
        "gap_ids": new_gaps,
        "followup_ids": knowledge.get("followup_ids", []) + [note["id"]],
    }
    snapshot = _create_snapshot(
        ctx.tx,
        new_result,
        status=(
            "completed"
            if knowledge.get("status") == "completed"
            and int(knowledge.get("accepted_count", 0)) > 0
            else "partial"
        ),
        origin="simulated",
        mode=knowledge.get("mode", "discover"),
        budget=knowledge.get("budget", {}),
        fingerprint=knowledge["fingerprint"],
        revision=knowledge["revision"] + 1,
        supersedes_id=knowledge["id"],
    )
    ctx.tx.emit("knowledge.followup", {"knowledge_id": snapshot["id"], "gap_id": resolved_gap["id"]})
    return DomainResult(
        200,
        [
            {"kind": "KnowledgeSnapshot", "id": snapshot["id"]},
            {"kind": "FollowUpNote", "id": note["id"]},
        ],
    )


def handle_update(ctx, payload):
    knowledge = ctx.tx.get(payload["knowledge_id"], kind="KnowledgeSnapshot")
    update = ctx.tx.insert(
        "KnowledgeUpdate",
        {
            "knowledge_id": knowledge["id"],
            "summary": "模拟资料版本更新：新增条件说明，旧配置与摘要保持不变。",
        },
        origin="simulated",
    )
    # Updating the package cannot invent coverage. Without accepted evidence
    # the new revision stays partial, so a recommendation built on it cannot
    # become "ready" just because an update happened.
    has_evidence = int(knowledge.get("accepted_count", 0)) > 0
    status = "completed" if knowledge.get("status") == "completed" and has_evidence else "partial"
    snapshot = _create_snapshot(
        ctx.tx,
        {
            "source_ids": knowledge["source_ids"],
            "record_ids": knowledge["record_ids"],
            "gap_ids": knowledge.get("gap_ids", []),
            "followup_ids": knowledge.get("followup_ids", []),
        },
        status=status,
        origin="simulated",
        mode=knowledge.get("mode", "discover"),
        budget=knowledge.get("budget", {}),
        fingerprint=f"{knowledge['fingerprint']}-update-{update['id']}",
        revision=knowledge["revision"] + 1,
        supersedes_id=knowledge["id"],
    )
    for recommendation in ctx.tx.find("Recommendation"):
        if recommendation.get("knowledge_id") == knowledge["id"]:
            ctx.tx.update_entity(
                recommendation["id"],
                {"review_state": "needs_review"},
                revision=recommendation["revision"] + 1,
            )
    ctx.tx.emit("knowledge.updated", {"knowledge_id": snapshot["id"], "update_id": update["id"]})
    return DomainResult(
        200,
        [
            {"kind": "KnowledgeSnapshot", "id": snapshot["id"]},
            {"kind": "KnowledgeUpdate", "id": update["id"]},
        ],
    )
