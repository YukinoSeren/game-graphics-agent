"""Demo recommendation snapshots with traceable settings and variants."""

from __future__ import annotations

from copy import deepcopy

from .contracts import DemoError, DomainResult
from .service import require_fields

TEMPLATES = ("main", "quality", "performance")


def register(service) -> None:
    service.register("recommendation.generate", validate_generate, handle_generate)
    service.register("recommendation.variant", validate_variant, handle_variant)
    service.register("recommendation.review", validate_review, handle_review)


def validate_generate(payload):
    require_fields(
        payload,
        required=("context_id", "knowledge_id"),
        allowed=("context_id", "knowledge_id", "template"),
    )
    template = payload.get("template", "main")
    if template not in TEMPLATES:
        raise DemoError("invalid_template", "推荐模板必须是 main、quality 或 performance。")
    return payload


def validate_variant(payload):
    require_fields(payload, required=("recommendation_id", "variant"), allowed=("recommendation_id", "variant"))
    if payload["variant"] not in ("quality", "performance"):
        raise DemoError("invalid_variant", "替代方案必须是 quality 或 performance。")
    return payload


def validate_review(payload):
    return require_fields(payload, required=("recommendation_id",), allowed=("recommendation_id",))


def _source_map(tx, knowledge):
    mapping = {}
    for source_id in knowledge.get("source_ids", []):
        source = tx.get(source_id, kind="Source")
        mapping[source["url"]] = source_id
    return mapping


def _build_settings(tx, scenario, template_name, sources, accepted_records=None):
    template_values = scenario["templates"][template_name]["values"]
    evidence_by_setting = {}
    for record in accepted_records or []:
        if record.get("status") != "accepted":
            continue
        evidence_by_setting.setdefault(record["setting_id"], []).append(record["id"])
    built = []
    missing = []
    for setting in scenario["settings"]:
        setting_id = setting["id"]
        evidence = [
            sources[url]
            for url in setting["evidence_source_ids"]
            if url in sources
        ]
        if not evidence:
            missing.append(setting_id)
        built.append(
            {
                "setting_id": setting_id,
                "display_name": setting["display_name"],
                "demo_original_name": setting["demo_original_name"],
                "group": setting["group"],
                "type": setting["type"],
                "allowed_values": setting["allowed_values"],
                "value": template_values.get(setting_id),
                "reason": setting["reason"],
                "evidence_ids": evidence,
                "evidence_record_ids": evidence_by_setting.get(setting_id, []),
                "dependencies": setting["dependencies"],
                "needs_verification": setting["needs_verification"],
                "unknown": setting["unknown"],
            }
        )
        decision = deepcopy(setting.get("evidence_decision") or {})
        if decision:
            views = []
            for view in decision.get("source_views", []):
                matches = [tx.get(entity_id, kind="Source") for entity_id in evidence
                           if tx.get(entity_id, kind="Source").get("source_key") == view.get("source_key")]
                if matches:
                    source = matches[0]
                    views.append({**view, "source_id": source["id"], "title": source["title"], "channel": source.get("channel")})
            decision["source_views"] = views
            decision["selected_value"] = template_values.get(setting_id)
            if template_name != "main" and template_values.get(setting_id) != setting.get("recommended_value"):
                decision["decision"] = ("本版本偏重画面细节，保留更高的效果等级，并接受复杂场景的额外负担。"
                                        if template_name == "quality" else
                                        "本版本偏重交战稳定性，降低这项设置的负担，接受对应画面细节减少。")
                decision["tradeoff"] = ("与主方案相比，此处更重视画面表现；请结合相似场景的帧时间和观感选择。"
                                        if template_name == "quality" else
                                        "与主方案相比，此处更重视稳定性；请留意相关材质、阴影或特效细节的变化。")
            built[-1]["evidence_decision"] = decision
    return built, missing


def _readiness(context, knowledge, missing):
    if int(knowledge.get("accepted_count", 0)) <= 0:
        return "needs_review", "知识快照没有已接受的证据记录，不能作为推荐依据。"
    if missing:
        return "needs_review", "有设置缺少可追溯证据，需要补充资料。"
    if knowledge.get("status") == "partial":
        return "provisional", "知识采集只完成了一部分，方案适用性待确认。"
    if context.get("applicability") != "preset":
        return "provisional", "当前背景为自定义内容，方案适用性待确认。"
    return "ready", "已根据当前设备背景与整理资料生成。"


def build_snapshot(tx, scenario, context, knowledge, template_name="main", *, variant_of=None):
    """Build in the caller's transaction; collection and commands share this path."""
    sources = _source_map(tx, knowledge)
    settings, missing = _build_settings(
        tx, scenario, template_name, sources, knowledge.get("records", [])
    )
    readiness, reason = _readiness(context, knowledge, missing)
    template = scenario["templates"][template_name]
    recommendation = tx.insert(
        "Recommendation",
        {
            "context_id": context["id"],
            "knowledge_id": knowledge["id"],
            "template": template_name,
            "label": template["label"],
            "description": template["description"],
            "settings": settings,
            "target_proposal": template["target_proposal"],
            "reference_results": template["reference_results"],
            "local_summary_id": None,
            "readiness": readiness,
            "readiness_reason": reason,
            "missing_evidence": missing,
            "variant_of": variant_of,
            "review_state": "current",
            "supersedes_id": None,
            "source_pack_version": scenario.get("source_pack_version"),
        },
        origin="simulated",
    )
    tx.emit(
        "recommendation.generated",
        {
            "recommendation_id": recommendation["id"],
            "readiness": readiness,
            "settings": len(settings),
        },
    )
    return recommendation


def handle_generate(ctx, payload):
    context = ctx.tx.get(payload["context_id"], kind="ContextSnapshot")
    knowledge = ctx.tx.get(payload["knowledge_id"], kind="KnowledgeSnapshot")
    recommendation = build_snapshot(ctx.tx, ctx.scenario, context, knowledge, payload.get("template", "main"))
    return DomainResult(201, [{"kind": "Recommendation", "id": recommendation["id"]}])


def handle_variant(ctx, payload):
    base = ctx.tx.get(payload["recommendation_id"], kind="Recommendation")
    context = ctx.tx.get(base["context_id"], kind="ContextSnapshot")
    knowledge = ctx.tx.get(base["knowledge_id"], kind="KnowledgeSnapshot")
    recommendation = build_snapshot(ctx.tx, ctx.scenario, context, knowledge,
                                    payload["variant"], variant_of=base["id"])
    ctx.tx.emit("recommendation.variant", {
        "recommendation_id": recommendation["id"], "variant_of": base["id"],
    })
    return DomainResult(201, [{"kind": "Recommendation", "id": recommendation["id"]}])


def _latest_knowledge(tx, knowledge_id):
    """Follow the supersedes chain to the newest revision of a snapshot."""
    current = knowledge_id
    seen = set()
    while current not in seen:
        seen.add(current)
        candidates = tx.find("KnowledgeSnapshot", supersedes_id=current)
        if not candidates:
            break
        current = max(
            candidates, key=lambda item: (int(item.get("revision", 1)), item["created_at"])
        )["id"]
    return tx.get(current, kind="KnowledgeSnapshot")


def handle_review(ctx, payload):
    base = ctx.tx.get(payload["recommendation_id"], kind="Recommendation")
    current = ctx.tx.get(ctx.tx.workspace()["context_id"], kind="ContextSnapshot")
    changed_background = current["id"] != base["context_id"]
    knowledge = _latest_knowledge(ctx.tx, base["knowledge_id"])
    used_new_knowledge = knowledge["id"] != base["knowledge_id"]
    sources = _source_map(ctx.tx, knowledge)
    settings, missing = _build_settings(
        ctx.tx,
        ctx.scenario,
        base["template"],
        sources,
        knowledge.get("records", []),
    )
    if changed_background:
        readiness = "provisional"
        reason = "背景已修订，需要确认预设方案对新背景的适用性。"
    else:
        readiness, reason = _readiness(current, knowledge, missing)
    if base.get("review_state") == "needs_review" and not used_new_knowledge:
        readiness = "needs_review"
        reason = "没有可用于复核的新知识修订，保留待复核状态。"
    reviewed = ctx.tx.insert(
        "Recommendation",
        {
            "context_id": base["context_id"],
            "knowledge_id": knowledge["id"],
            "template": base["template"],
            "label": base["label"],
            "description": base["description"],
            "settings": settings,
            "target_proposal": base["target_proposal"],
            "reference_results": base["reference_results"],
            "local_summary_id": base.get("local_summary_id"),
            "readiness": readiness,
            "readiness_reason": reason,
            "missing_evidence": missing,
            "variant_of": base.get("variant_of"),
            "review_state": "needs_review" if readiness == "needs_review" else "current",
            "supersedes_id": base["id"],
        },
        origin="simulated",
        revision=base["revision"] + 1,
    )
    ctx.tx.emit("recommendation.reviewed", {"recommendation_id": reviewed["id"]})
    return DomainResult(200, [{"kind": "Recommendation", "id": reviewed["id"]}])
