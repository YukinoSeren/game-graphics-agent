"""Demo feedback rules, single-setting changes, and outcome acceptance."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from .contracts import Command, DemoError, DomainResult
from .service import ActionContext, require_fields

SYMPTOMS = ("stutter", "blurry")
SEVERITIES = ("occasional", "frequent", "constant")
DEFAULT_PROTECTED = ("mw2.texture_detail", "mw2.texture_filtering", "mw2.render_resolution")
REVIEW_PHASES = [
    {"key": "understand", "label": "理解反馈", "seconds": 8},
    {"key": "compare", "label": "对照记录", "seconds": 10},
    {"key": "weigh", "label": "权衡调整", "seconds": 12},
    {"key": "publish", "label": "整理调整建议", "seconds": 0},
]


def register(service) -> None:
    service.register("feedback.save", validate_feedback, handle_feedback)
    service.register("feedback.next", validate_next, handle_next)
    service.register("feedback.review", validate_review, handle_review)
    service.register("preference.save", validate_preference, handle_preference)
    service.register("target.respond", validate_target, handle_target)
    service.register("outcome.save", validate_outcome, handle_outcome)
    service.tasks.register("feedback.review", REVIEW_PHASES, _review_publisher(service))


# -- validation -------------------------------------------------------
def validate_feedback(payload):
    require_fields(
        payload,
        required=("session_id",),
        allowed=(
            "session_id",
            "summary_id",
            "satisfied",
            "symptoms",
            "severity",
            "raw_text",
            "protected_setting_ids",
        ),
    )
    symptoms = payload.get("symptoms") or []
    if not isinstance(symptoms, list) or not set(symptoms) <= set(SYMPTOMS):
        raise DemoError("invalid_symptoms", "症状选项不受支持。")
    severity = payload.get("severity")
    if severity is not None and severity not in SEVERITIES:
        raise DemoError("invalid_severity", "严重程度选项不受支持。")
    satisfied = payload.get("satisfied")
    if satisfied is not None and not isinstance(satisfied, bool):
        raise DemoError("invalid_satisfaction", "satisfied 必须是布尔值或 null。")
    raw_text = payload.get("raw_text") or ""
    if not isinstance(raw_text, str) or len(raw_text) > 2000:
        raise DemoError("invalid_feedback", "反馈原话必须是 2000 字以内的文本。")
    protected = payload.get("protected_setting_ids") or []
    if not isinstance(protected, list) or not all(isinstance(item, str) for item in protected):
        raise DemoError("invalid_protection", "protected_setting_ids 必须是列表。")
    return payload


def validate_next(payload):
    return require_fields(
        payload,
        required=("feedback_id", "applied_id"),
        allowed=("feedback_id", "applied_id", "summary_id"),
    )


def validate_review(payload):
    require_fields(payload, required=("session_id", "summary_id", "applied_id"),
                   allowed=("session_id", "summary_id", "applied_id", "satisfied", "symptoms", "severity", "raw_text", "protected_setting_ids"))
    validate_feedback({key: value for key, value in payload.items() if key != "applied_id"})
    return payload


def validate_preference(payload):
    require_fields(
        payload,
        required=("preferences",),
        allowed=("preferences", "applied_id", "explicit"),
    )
    if not isinstance(payload["preferences"], dict):
        raise DemoError("invalid_preference", "preferences 必须是对象。")
    return payload


def validate_target(payload):
    require_fields(
        payload,
        required=("accepted",),
        allowed=("accepted", "recommendation_id", "note"),
    )
    if not isinstance(payload["accepted"], bool):
        raise DemoError("invalid_target", "目标接受必须是布尔值。")
    return payload


def validate_outcome(payload):
    require_fields(
        payload,
        required=("change_set_id", "applied_id", "comparison_id", "accepted"),
        allowed=(
            "change_set_id",
            "applied_id",
            "comparison_id",
            "accepted",
            "reason",
        ),
    )
    if not isinstance(payload["accepted"], bool):
        raise DemoError("invalid_outcome", "接受与否必须是玩家明确选择。")
    return payload


# -- helpers ----------------------------------------------------------
def _setting_ids(ctx):
    return {item["id"] for item in ctx.scenario["settings"]}


def _default_protected(ctx):
    return set(DEFAULT_PROTECTED) & _setting_ids(ctx)


def _infer_symptoms(ctx, feedback):
    symptoms = list(feedback["symptoms"])
    # A satisfied answer is an explicit "no new problem": free text must not
    # silently turn it into a stutter adjustment.
    if feedback.get("satisfied") is True:
        return symptoms
    text = feedback.get("raw_text") or ""
    keywords = ctx.scenario["rules"].get("keywords", {})
    for symptom, words in keywords.items():
        if symptom in SYMPTOMS and any(word in text for word in words):
            if symptom not in symptoms:
                symptoms.append(symptom)
    return symptoms


def _matching_rule(ctx, symptoms, severity, raw_text=""):
    for rule in sorted(ctx.scenario["rules"]["symptom_rules"], key=lambda item: item.get("priority", 0), reverse=True):
        if not set(rule["symptoms"]) & set(symptoms):
            continue
        if severity and rule.get("severities") and severity not in rule["severities"]:
            continue
        if rule.get("text_keywords") and not any(word in raw_text for word in rule["text_keywords"]):
            continue
        return rule
    return None


def _change_set(ctx, *, feedback, applied, rule, setting_id, from_value, to_value, alternative):
    evidence = []
    knowledge_id = getattr(ctx, "feedback_knowledge_id", None)
    source_ids = set(ctx.tx.get(knowledge_id, kind="KnowledgeSnapshot")["source_ids"]) if knowledge_id else set()
    for source_id in rule.get("evidence_source_ids", []):
        for entity_id in ctx.tx.find("Source", url=source_id):
            if entity_id["id"] in source_ids:
                evidence.append(entity_id["id"])
    change_set = ctx.tx.insert(
        "ChangeSet",
        {
            "feedback_id": feedback["id"],
            "base_applied_id": applied["id"],
            "analysis_id": getattr(ctx, "feedback_analysis_id", None),
            "summary_ids": [feedback["summary_id"]] if feedback.get("summary_id") else [],
            "knowledge_id": knowledge_id,
            "changes": {
                setting_id: {
                    "from": from_value,
                    "to": to_value,
                    "setting_id": setting_id,
                }
            },
            "hypothesis": rule["hypothesis"],
            "observation_note": "用户描述与所选日志的帧时间波动支持本次调整假设；日志没有爆炸事件标记，不能据此定位某次爆炸或保证帧率提升。",
            "evidence_ids": evidence,
            "protected_setting_ids": sorted(set(feedback.get("protected_setting_ids", [])) | _default_protected(ctx)),
            "retest": rule["retest"],
            "recovery": {
                "setting_id": setting_id,
                "restore_value": from_value,
                "note": "在游戏内手动恢复旧值，或使用配置回滚生成新版本。",
            },
            "alternative": alternative,
            "status": "proposed",
        },
        origin="simulated",
    )
    ctx.tx.emit("feedback.next", {"change_set_id": change_set["id"], "action": "adjust"})
    return change_set


def _no_improvement_rounds(ctx):
    rounds = 0
    for outcome in ctx.tx.find("Outcome"):
        data_change = outcome.get("data_change") or {}
        if data_change.get("improved") is False:
            rounds += 1
    return rounds


def _data_change(comparison):
    metrics = {row["key"]: row for row in comparison.get("metrics", [])}
    average = metrics.get("average_present_rate") or {}
    before_clusters = (comparison.get("before_long_frames") or {}).get("cluster_count")
    after_clusters = (comparison.get("after_long_frames") or {}).get("cluster_count")
    improved = False
    if before_clusters is not None and after_clusters is not None:
        improved = after_clusters < before_clusters * 0.75
    if not improved and average.get("delta") is not None:
        improved = average["delta"] > 0.5
    return {
        "improved": improved,
        "average_present_rate": {
            "before": average.get("before"),
            "after": average.get("after"),
            "delta": average.get("delta"),
            "unit": "fps",
        },
        "long_frame_clusters": {
            "before": before_clusters,
            "after": after_clusters,
        },
        "note": "数据变化仅描述本次记录，无法单独确认某项设置带来的影响。",
    }


# -- handlers ---------------------------------------------------------
def handle_feedback(ctx, payload):
    session = ctx.tx.get(payload["session_id"], kind="Session")
    summary_id = payload.get("summary_id")
    if summary_id is not None:
        ctx.tx.get(summary_id, kind="Summary")
    feedback = ctx.tx.insert(
        "Feedback",
        {
            "session_id": session["id"],
            "summary_id": summary_id,
            "satisfied": payload.get("satisfied"),
            "symptoms": payload.get("symptoms") or [],
            "severity": payload.get("severity"),
            "raw_text": payload.get("raw_text") or "",
            "protected_setting_ids": payload.get("protected_setting_ids") or [],
            "created_seq": ctx.tx.latest_seq(),
            "note": "原话按玩家输入保存；建议会结合已选择的症状与严重程度生成。",
        },
        origin="user_declared",
    )
    ctx.tx.emit("feedback.saved", {"feedback_id": feedback["id"]})
    return DomainResult(201, [{"kind": "Feedback", "id": feedback["id"]}])


def _retire_open_changes(ctx, reason):
    for change_set in ctx.tx.find("ChangeSet", status="proposed"):
        ctx.tx.update_entity(
            change_set["id"],
            {"status": "superseded", "superseded_reason": reason},
            revision=change_set["revision"] + 1,
        )


def _evaluate_next(ctx, payload, *, feedback_snapshot=None):
    feedback = feedback_snapshot or ctx.tx.get(payload["feedback_id"], kind="Feedback")
    applied = ctx.tx.get(payload["applied_id"], kind="AppliedConfiguration")
    summary_id = payload.get("summary_id")
    summary = ctx.tx.get(summary_id, kind="Summary") if summary_id else None
    symptoms = _infer_symptoms(ctx, feedback)

    def result(action, reason, *, refs=None, change_set=None):
        return DomainResult(
            200,
            refs or [],
            data={
                "action": action,
                "reason": reason,
                "feedback_id": feedback["id"],
                "change_set_id": change_set["id"] if change_set else None,
            },
        )

    if feedback.get("satisfied") is True and not symptoms:
        _retire_open_changes(ctx, "玩家表示满意并停止建议")
        return result("keep", "玩家明确表示满意，保留当前配置并停止建议。")

    if summary is None or summary.get("quality") == "unknown":
        return result("measure", "没有可用性能数据，请先完成一次测试。")

    if _no_improvement_rounds(ctx) >= int(
        ctx.scenario["rules"].get("no_improvement_limit", 2)
    ):
        _retire_open_changes(ctx, "连续无改善，停止建议")
        return result("stop", "连续两轮没有明显改善，建议保持、回滚或补充数据。")

    rule = _matching_rule(ctx, symptoms, feedback.get("severity"), feedback.get("raw_text", ""))
    if rule is None:
        return result(
            "insufficient_evidence",
            "反馈超出当前可处理范围，已保存原话并列出待确认问题。",
        )
    settings = applied.get("settings", {})
    target = rule["target_setting_id"]
    protected = set(feedback.get("protected_setting_ids", [])) | _default_protected(ctx)
    if target not in settings:
        return result("research", "当前配置缺少目标设置的声明，需要补充确认。")
    target_legal = (target not in protected and settings.get(target) != rule["to"]
                    and (not rule.get("from") or settings.get(target) == rule["from"]))
    if not target_legal:
        alternative = rule.get("alternative_setting_id")
        alternative_to = rule.get("alternative_to")
        alternative_from_matches = (not rule.get("alternative_from")
                                    or settings.get(alternative) == rule["alternative_from"])
        # For explosion feedback a lowered particle setting falls back to one
        # lower shadow tier; never raise an already lowered value or emit no-op.
        if target == "mw2.particle_quality" and alternative == "mw2.shadow_quality":
            tiers = ["high", "medium", "low", "off"]
            current = settings.get(alternative)
            alternative_to = tiers[tiers.index(current) + 1] if current in tiers[:-1] else None
            alternative_from_matches = True
        if (alternative and alternative not in protected and alternative in settings
                and alternative_to is not None and settings[alternative] != alternative_to
                and alternative_from_matches
                and alternative_to in ctx.setting(alternative).get("allowed_values", [])):
            change_set = _change_set(
                ctx,
                feedback=feedback,
                applied=applied,
                rule=rule,
                setting_id=alternative,
                from_value=settings.get(alternative),
                to_value=alternative_to,
                alternative=None,
            )
            return result(
                "adjust",
                "目标调整与保护项冲突，改用替代设置。",
                refs=[{"kind": "ChangeSet", "id": change_set["id"]}],
                change_set=change_set,
            )
        return result("measure", "当前设置已降低或受到保护，没有合适的单项调整。请在相似交战场景复看帧时间波动与画面观感。")
    change_set = _change_set(
        ctx,
        feedback=feedback,
        applied=applied,
        rule=rule,
        setting_id=target,
        from_value=settings.get(target),
        to_value=rule["to"],
        alternative={
            "setting_id": rule.get("alternative_setting_id"),
            "to": rule.get("alternative_to"),
        }
        if rule.get("alternative_setting_id")
        else None,
    )
    return result(
        "adjust",
        ("优先尝试降低粒子质量一档，保留纹理细节、纹理过滤和渲染分辨率。请在相似交战场景复看；调整后由你手动确认。"
         if target == "mw2.particle_quality" else
         "根据当前反馈整理出一项设置建议；采用前需要你在游戏中手动调整并确认。"),
        refs=[{"kind": "ChangeSet", "id": change_set["id"]}],
        change_set=change_set,
    )


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _lineage_knowledge(ctx, applied):
    """Resolve only the adopted configuration's own recommendation lineage."""
    pending = [applied["id"]]
    visited = set()
    while pending:
        applied_id = pending.pop()
        if applied_id in visited:
            continue
        visited.add(applied_id)
        candidate = ctx.tx.get(applied_id, kind="AppliedConfiguration")
        if candidate.get("context_id") != applied.get("context_id"):
            continue
        if candidate.get("recommendation_id"):
            recommendation = ctx.tx.get(candidate["recommendation_id"], kind="Recommendation")
            if recommendation.get("knowledge_id"):
                return recommendation["knowledge_id"]
        change_set_id = candidate.get("change_set_id")
        if change_set_id:
            change_set = ctx.tx.get(change_set_id, kind="ChangeSet")
            if change_set.get("knowledge_id"):
                return change_set["knowledge_id"]
            if change_set.get("base_applied_id"):
                pending.append(change_set["base_applied_id"])
        if candidate.get("parent_id"):
            pending.append(candidate["parent_id"])
        if candidate.get("rollback_target_id"):
            pending.append(candidate["rollback_target_id"])
    return None


def _review_inputs(ctx, payload, feedback=None, *, require_summary=False):
    applied = ctx.tx.get(payload["applied_id"], kind="AppliedConfiguration")
    workspace = ctx.tx.workspace()
    session_id = feedback["session_id"] if feedback else payload["session_id"]
    session = ctx.tx.get(session_id, kind="Session")
    summary_id = payload.get("summary_id") or (feedback or {}).get("summary_id")
    summary = ctx.tx.get(summary_id, kind="Summary") if summary_id else None
    if (workspace["current_applied_id"] != applied["id"]
            or applied.get("context_id") != workspace["context_id"]):
        raise DemoError("feedback_configuration_changed", "配置已变化，请选择当前配置的游玩记录重新分析。", status=409,
                        recovery_actions=["refresh"])
    if (session.get("applied_id") != applied["id"]
            or session.get("context_id") != workspace["context_id"]
            or (summary and (summary.get("session_id") != session_id or summary.get("applied_id") != applied["id"]))):
        raise DemoError("feedback_relation_mismatch", "所选游玩记录与当前配置不一致，请读取当前配置的游玩日志。", status=409,
                        recovery_actions=["read_recent_log"])
    if require_summary and (not summary or summary.get("quality") == "unknown"):
        raise DemoError("feedback_summary_required", "请先读取当前配置的游玩日志，再提交体验反馈。", status=409,
                        recovery_actions=["read_recent_log"])
    context = ctx.tx.get(workspace["context_id"], kind="ContextSnapshot")
    knowledge_id = _lineage_knowledge(ctx, applied)
    knowledge = ctx.tx.get(knowledge_id, kind="KnowledgeSnapshot") if knowledge_id else {}
    source_pack_version = knowledge.get("source_pack_version") or knowledge.get("content_version") or ctx.scenario.get("source_pack_version")
    return applied, session, summary, context, knowledge_id, source_pack_version


def _accepted(analysis, task):
    return DomainResult(202, [
        {"kind": "Feedback", "id": analysis["feedback_id"]},
        {"kind": "FeedbackAnalysis", "id": analysis["id"]},
        {"kind": "Task", "id": task["id"]},
    ], task_id=task["id"], data={
        "analysis_id": analysis["id"], "feedback_id": analysis["feedback_id"],
        "ready_not_before": analysis["ready_not_before"], "status": task["status"],
    })


def _supersede_analysis(ctx, analysis):
    try:
        ctx.tx.set_task_status(analysis["task_id"], "superseded")
    except DemoError as error:
        if error.code != "not_found":
            raise
        # Backups preserve business history but intentionally omit worker jobs.
        # Keep the restored input available and let this submission create a job.
        return ctx.tx.update_entity(analysis["id"], {
            "status": "superseded", "superseded_reason": "原分析任务已不可恢复，已重新提交分析。",
        }, revision=analysis["revision"] + 1)
    return ctx.tx.get(analysis["id"], kind="FeedbackAnalysis")


def _enqueue_review(ctx, payload, feedback=None, *, require_summary=False):
    applied, session, summary, context, knowledge_id, pack = _review_inputs(
        ctx, payload, feedback, require_summary=require_summary)
    values = feedback or payload
    user_input = {
        "session_id": session["id"], "summary_id": summary["id"] if summary else None,
        "satisfied": values.get("satisfied"), "symptoms": sorted(set(values.get("symptoms") or [])),
        "severity": values.get("severity"), "raw_text": values.get("raw_text") or "",
        "protected_setting_ids": sorted(set(values.get("protected_setting_ids") or []) | _default_protected(ctx)),
    }
    unknown = set(user_input["protected_setting_ids"]) - _setting_ids(ctx)
    if unknown:
        raise DemoError("invalid_protection", "保护项包含不支持的设置。")
    input_snapshot = {"applied": applied, "context": context, "summary": summary}
    fingerprint = _digest({"feedback": user_input, "snapshot": input_snapshot,
                           "knowledge_id": knowledge_id, "source_pack_version": pack})
    analyses = ctx.tx.find("FeedbackAnalysis")
    if analyses:
        current_analysis = analyses[-1]
        if current_analysis.get("request_fingerprint") == fingerprint and current_analysis["status"] != "superseded":
            try:
                current_task = ctx.tx.task_row(current_analysis["task_id"])
            except DemoError as error:
                if error.code != "not_found":
                    raise
                analyses[-1] = _supersede_analysis(ctx, current_analysis)
            else:
                return _accepted(current_analysis, current_task)
    for analysis in analyses:
        if analysis["status"] in ("queued", "running", "interrupted", "failed", "cancelled"):
            _supersede_analysis(ctx, analysis)
    if feedback is None:
        saved = handle_feedback(ctx, user_input)
        feedback = ctx.tx.get(saved.refs[0]["id"], kind="Feedback")
    now = ctx.clock.now_iso()
    deadline = (datetime.fromisoformat(now) + timedelta(seconds=30)).isoformat()
    cards = ctx.tx.find("GameCard")
    intent_id = cards[-1].get("intent_id") if cards else None
    analysis = ctx.tx.insert("FeedbackAnalysis", {
        "feedback_id": feedback["id"], "task_id": None, "game_id": ctx.scenario_id,
        "intent_id": intent_id, "applied_config_id": applied["id"], "context_id": context["id"],
        "summary_ids": [summary["id"]] if summary else [], "session_id": session["id"],
        "knowledge_id": knowledge_id, "source_pack_version": pack,
        "request_fingerprint": fingerprint, "input_snapshot": input_snapshot,
        "feedback_snapshot": user_input, "accepted_at": now, "ready_not_before": deadline,
        "status": "queued", "change_set_id": None, "result": None,
    }, origin="simulated")
    task = ctx.service.tasks.enqueue(ctx.tx, "feedback.review", {
        "analysis_id": analysis["id"], "ready_not_before": deadline,
    })
    analysis = ctx.tx.get(analysis["id"], kind="FeedbackAnalysis")
    ctx.tx.emit("feedback.review.accepted", {"analysis_id": analysis["id"], "task_id": task["id"]})
    return _accepted(analysis, task)


def handle_review(ctx, payload):
    return _enqueue_review(ctx, payload, require_summary=True)


def handle_next(ctx, payload):
    feedback = ctx.tx.get(payload["feedback_id"], kind="Feedback")
    return _enqueue_review(ctx, payload, feedback)


def sync_analysis_status(tx, task, status):
    """Keep the persisted analysis in sync for cancel, restart and resume."""
    if task["kind"] != "feedback.review":
        return
    analysis_id = (task.get("input") or {}).get("analysis_id")
    if not analysis_id:
        return
    analysis = tx.get(analysis_id, kind="FeedbackAnalysis")
    if analysis.get("status") == "superseded" and status != "superseded":
        return
    if (analysis.get("task_id") not in (None, task["id"])
            and not (status == "queued" and task.get("parent_id") == analysis["task_id"])):
        return
    tx.update_entity(analysis_id, {"task_id": task["id"], "status": status},
                     revision=analysis["revision"] + 1)


def _review_publisher(service):
    def publish(tx, task, phase):
        analysis = tx.get(task["input"]["analysis_id"], kind="FeedbackAnalysis")
        if analysis["status"] == "superseded" or analysis["task_id"] != task["id"]:
            return {"status": "superseded"}
        snapshot = analysis["input_snapshot"]
        workspace = tx.workspace()
        unchanged = (workspace["current_applied_id"] == analysis["applied_config_id"]
                     and workspace["context_id"] == analysis["context_id"])
        for key, kind in (("applied", "AppliedConfiguration"), ("context", "ContextSnapshot"), ("summary", "Summary")):
            entity = snapshot.get(key)
            if entity and tx.get(entity["id"], kind=kind) != entity:
                unchanged = False
        if not unchanged:
            result = {"analysis_id": analysis["id"], "feedback_id": analysis["feedback_id"],
                      "action": "invalidated", "reason": "配置已变化，请重新分析。", "change_set_id": None}
            tx.update_entity(analysis["id"], {"status": "superseded", "result": result}, revision=analysis["revision"] + 1)
            return {"status": "superseded", "result": result}
        if phase["key"] != "publish":
            sync_analysis_status(tx, task, "running")
            return {"detail": {"analysis_id": analysis["id"]}}
        ctx = ActionContext(tx, Command("feedback-review-task", tx.scope.scenario_id,
                            tx.scope.workspace_epoch, workspace["revision"], "feedback.review", {}),
                            service.catalog, service.files, service.clock, service)
        ctx.feedback_knowledge_id = analysis["knowledge_id"]
        ctx.feedback_analysis_id = analysis["id"]
        evaluated = _evaluate_next(ctx, {
            "feedback_id": analysis["feedback_id"], "applied_id": analysis["applied_config_id"],
            "summary_id": next(iter(analysis["summary_ids"]), None),
        }, feedback_snapshot={**analysis["feedback_snapshot"], "id": analysis["feedback_id"]})
        result = {**evaluated.data, "analysis_id": analysis["id"]}
        tx.update_entity(analysis["id"], {"status": "completed", "result": result,
                         "change_set_id": result["change_set_id"]}, revision=analysis["revision"] + 1)
        tx.emit("feedback.review.completed", result)
        return {"status": "completed", "result": result}
    return publish


def handle_preference(ctx, payload):
    preference = ctx.tx.insert(
        "Preference",
        {
            "applied_id": payload.get("applied_id"),
            "preferences": payload["preferences"],
            "explicit": bool(payload.get("explicit", True)),
            "note": "玩家明确偏好；推断偏好不会自动升级为声明。",
        },
        origin="user_declared",
    )
    ctx.tx.emit("preference.saved", {"preference_id": preference["id"]})
    return DomainResult(201, [{"kind": "Preference", "id": preference["id"]}])


def handle_target(ctx, payload):
    recommendation_id = payload.get("recommendation_id")
    if recommendation_id:
        ctx.tx.get(recommendation_id, kind="Recommendation")
    response = ctx.tx.insert(
        "TargetResponse",
        {
            "recommendation_id": recommendation_id,
            "accepted": payload["accepted"],
            "note": payload.get("note", ""),
            "note_type": "玩家声明",
        },
        origin="user_declared",
    )
    ctx.tx.emit("target.responded", {"target_response_id": response["id"]})
    return DomainResult(201, [{"kind": "TargetResponse", "id": response["id"]}])


def handle_outcome(ctx, payload):
    change_set = ctx.tx.get(payload["change_set_id"], kind="ChangeSet")
    applied = ctx.tx.get(payload["applied_id"], kind="AppliedConfiguration")
    comparison = ctx.tx.get(payload["comparison_id"], kind="Comparison")
    before = ctx.tx.get(comparison["before_summary_id"], kind="Summary")
    after = ctx.tx.get(comparison["after_summary_id"], kind="Summary")

    def mismatch(detail):
        raise DemoError(
            "outcome_relation_mismatch",
            f"结果记录与采用链不一致：{detail}",
            status=409,
            recovery_actions=["refresh"],
        )

    if applied["id"] != after.get("applied_id"):
        mismatch("所记录的采用版本不是后测所用版本。")
    if change_set.get("base_applied_id") != before.get("applied_id"):
        mismatch("变更建议的基准不是前测所用版本。")
    base = ctx.tx.get(change_set["base_applied_id"], kind="AppliedConfiguration")
    for setting_id, change in change_set.get("changes", {}).items():
        if base["settings"].get(setting_id) != change.get("from"):
            mismatch("基准配置的原值与变更建议不一致。")
        if applied["settings"].get(setting_id) != change.get("to"):
            mismatch("后测采用的版本没有实施变更建议的新值。")
    data_change = _data_change(comparison)
    outcome = ctx.tx.insert(
        "Outcome",
        {
            "change_set_id": change_set["id"],
            "applied_id": applied["id"],
            "comparison_id": comparison["id"],
            "accepted": payload["accepted"],
            "reason": payload.get("reason", ""),
            "data_change": data_change,
            "follow_up": "keep" if payload["accepted"] else "rollback",
            "note": "数值变化与主观接受分开记录。",
        },
        origin="user_declared",
    )
    ctx.tx.emit(
        "outcome.saved",
        {"outcome_id": outcome["id"], "accepted": outcome["accepted"]},
    )
    return DomainResult(
        201,
        [{"kind": "Outcome", "id": outcome["id"]}],
        data={"outcome": outcome},
    )
