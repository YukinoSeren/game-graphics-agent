"""One-click complete demo stories. Seeded facts are labelled demo_seed."""

from __future__ import annotations

import hashlib

from . import comparison, configuration, feedback, knowledge, performance, recommendation
from .contracts import DemoError, DomainResult
from .service import require_fields
from .traces import generate_trace

POSITIONS = ("recommendation", "comparison")
SEED_ORIGIN = "demo_seed"


class SeedContext:
    def __init__(self, service, tx, scenario):
        self.service = service
        self.tx = tx
        self.catalog = service.catalog
        self.files = service.files
        self.clock = service.clock
        self.scenario = scenario
        self.scenario_id = tx.scope.scenario_id


def register(service) -> None:
    service.register("demo.seed", validate_seed, handle_seed)


def validate_seed(payload):
    require_fields(
        payload,
        required=("position", "expected_epoch"),
        allowed=("position", "expected_epoch"),
    )
    if payload["position"] not in POSITIONS:
        raise DemoError("invalid_position", "加载位置必须是 recommendation 或 comparison。")
    if not isinstance(payload["expected_epoch"], int) or isinstance(
        payload["expected_epoch"], bool
    ):
        raise DemoError("invalid_epoch", "expected_epoch 必须是整数。")
    return payload


def handle_seed(ctx, payload):
    workspace = ctx.tx.workspace()
    if payload["expected_epoch"] != workspace["workspace_epoch"]:
        raise DemoError(
            "stale_epoch", "当前工作区已重置，请刷新后再加载完整案例。", status=409
        )
    reset = ctx.tx.reset_workspace(ctx.scenario_id, ctx.catalog)
    tx = ctx.tx.rescope(reset["new_epoch"])
    seed = SeedContext(ctx.service, tx, ctx.scenario)
    context_id = tx.workspace()["context_id"]

    snapshot = _seed_knowledge(seed)
    rec = _seed_recommendation(seed, snapshot, context_id)
    if payload["position"] == "recommendation":
        tx.emit("demo.seed", {"position": "recommendation", "recommendation_id": rec["id"]})
        return DomainResult(
            200,
            [
                {"kind": "KnowledgeSnapshot", "id": snapshot["id"]},
                {"kind": "Recommendation", "id": rec["id"]},
            ],
            data={
                "position": "recommendation",
                "workspace_epoch": reset["new_epoch"],
                "recommendation_id": rec["id"],
                "knowledge_id": snapshot["id"],
                "_cleanup_file_ids": reset["file_ids"],
            },
        )

    plan = tx.insert(
        "TestPlan",
        {
            "scene": ctx.scenario["context"]["scene_tag"],
            "duration_s": 60,
            "repeats": 1,
            "metrics": [
                "average_present_rate",
                "interval_p95_ms",
                "interval_p99_ms",
                "slowest_1pct_fps",
            ],
            "conditions": {"background": "演示预设", "note": "完整案例预置计划"},
            "note": "演示计划；加载完整案例不表示真实执行。",
        },
        origin=SEED_ORIGIN,
    )
    a0, a1 = _seed_configurations(seed, rec, context_id)
    s1_session, s1_segment, s1_artifact, s1 = _seed_session(
        seed, applied=a1, plan=plan, label="recommended", end_s=60.0
    )
    protocol = tx.insert(
        "ComparisonProtocol",
        {
            "mode": "fixed",
            "threshold_source": "fixed",
            "threshold_ms": 50.0,
            "baseline_summary_ids": [s1["id"]],
            "exploratory": False,
            "created_seq": tx.latest_seq(),
            "status": "frozen",
            "note": "完整案例预置口径；加载后不原地修改。",
        },
        origin=SEED_ORIGIN,
    )
    feedback_entity, change_set = _seed_feedback(seed, s1, a1)
    a2 = _seed_adjustment(seed, a1, change_set, context_id)
    s2_session, s2_segment, s2_artifact, s2 = _seed_session(
        seed, applied=a2, plan=plan, label="adjusted", end_s=60.0
    )
    comparison_entity = comparison.build_comparison(
        seed, protocol, s1, s2, origin=SEED_ORIGIN
    )
    data_change = feedback._data_change(comparison_entity)
    outcome = tx.insert(
        "Outcome",
        {
            "change_set_id": change_set["id"],
            "applied_id": a2["id"],
            "comparison_id": comparison_entity["id"],
            "accepted": True,
            "reason": "完整案例预置结果",
            "data_change": data_change,
            "follow_up": "keep",
            "note": "数值变化与主观接受分开记录。",
        },
        origin=SEED_ORIGIN,
    )
    tx.update_workspace(current_applied_id=a2["id"])
    tx.emit(
        "demo.seed",
        {
            "position": "comparison",
            "comparison_id": comparison_entity["id"],
            "workspace_epoch": reset["new_epoch"],
        },
    )
    return DomainResult(
        200,
        [
            {"kind": "KnowledgeSnapshot", "id": snapshot["id"]},
            {"kind": "Recommendation", "id": rec["id"]},
            {"kind": "AppliedConfiguration", "id": a0["id"]},
            {"kind": "AppliedConfiguration", "id": a1["id"]},
            {"kind": "AppliedConfiguration", "id": a2["id"]},
            {"kind": "Summary", "id": s1["id"]},
            {"kind": "Summary", "id": s2["id"]},
            {"kind": "ComparisonProtocol", "id": protocol["id"]},
            {"kind": "Comparison", "id": comparison_entity["id"]},
            {"kind": "Outcome", "id": outcome["id"]},
        ],
        data={
            "position": "comparison",
            "workspace_epoch": reset["new_epoch"],
            "a0_id": a0["id"],
            "a1_id": a1["id"],
            "a2_id": a2["id"],
            "s1_id": s1["id"],
            "s2_id": s2["id"],
            "protocol_id": protocol["id"],
            "change_set_id": change_set["id"],
            "comparison_id": comparison_entity["id"],
            "outcome_id": outcome["id"],
            "_cleanup_file_ids": reset["file_ids"],
        },
    )


def _seed_knowledge(seed):
    tx = seed.tx
    scenario = seed.scenario
    source_ids = [knowledge._ensure_source(tx, source) for source in scenario["sources"]]
    template_values = scenario["templates"]["main"]["values"]
    record_ids = []
    for source_id in source_ids:
        source = tx.get(source_id, kind="Source")
        for setting_id in source["setting_ids"]:
            value = template_values.get(setting_id)
            record_ids.append(
                tx.insert(
                    "EvidenceRecord",
                    {
                        "source_id": source_id,
                        "source_url": source["url"],
                        "setting_id": setting_id,
                        "value": value,
                        "status": "quarantined" if value is None else "accepted",
                        "reason": "unknown_value"
                        if value is None
                        else (None if source["relationship"] == "original" else "转载来源，不构成独立支持"),
                        "independent_support": source["relationship"] == "original",
                    },
                    origin=SEED_ORIGIN,
                )["id"]
            )
    gaps = [
        tx.insert(
            "KnowledgeGap",
            {
                "code": "P1",
                "label": "缺少本机性能数据",
                "status": "open",
                "detail": "需要完成模拟采集后才能与冻结口径比较。",
            },
            origin=SEED_ORIGIN,
        ),
        tx.insert(
            "KnowledgeGap",
            {
                "code": "version",
                "label": "游戏版本未确认",
                "status": "resolved",
                "detail": "完整案例预置：保留待玩家核对。",
            },
            origin=SEED_ORIGIN,
        ),
    ]
    fingerprint = hashlib.sha256(
        f"{scenario['scenario_id']}:seed-knowledge".encode("utf-8")
    ).hexdigest()
    return knowledge._create_snapshot(
        tx,
        {
            "source_ids": source_ids,
            "record_ids": record_ids,
            "gap_ids": [gap["id"] for gap in gaps],
        },
        status="completed",
        origin=SEED_ORIGIN,
        mode="seeded",
        budget={"seeded": True},
        fingerprint=fingerprint,
    )


def _seed_recommendation(seed, snapshot, context_id):
    tx = seed.tx
    scenario = seed.scenario
    context = tx.get(context_id, kind="ContextSnapshot")
    sources = recommendation._source_map(tx, snapshot)
    settings, missing = recommendation._build_settings(
        tx, scenario, "main", sources, snapshot.get("records", [])
    )
    readiness, reason = recommendation._readiness(context, snapshot, missing)
    template = scenario["templates"]["main"]
    return tx.insert(
        "Recommendation",
        {
            "context_id": context["id"],
            "knowledge_id": snapshot["id"],
            "template": "main",
            "label": template["label"],
            "description": template["description"],
            "settings": settings,
            "target_proposal": template["target_proposal"],
            "reference_results": template["reference_results"],
            "local_summary_id": None,
            "readiness": readiness,
            "readiness_reason": reason,
            "missing_evidence": missing,
            "variant_of": None,
            "review_state": "current",
            "supersedes_id": None,
        },
        origin=SEED_ORIGIN,
    )


def _seed_configurations(seed, rec, context_id):
    tx = seed.tx
    scenario = seed.scenario
    baseline = scenario["templates"]["baseline"]["values"]
    a0 = configuration._insert_configuration(
        seed,
        settings=dict(baseline),
        origin="baseline",
        parent=None,
        recommendation_id=None,
        preset="baseline",
        confirmation="完整案例预置的原配置（演示声明）",
        entity_origin=SEED_ORIGIN,
    )
    values = {item["setting_id"]: item["value"] for item in rec["settings"]}
    a1 = configuration._insert_configuration(
        seed,
        settings=values,
        origin="recommendation",
        parent=a0,
        recommendation_id=rec["id"],
        preset=rec["template"],
        confirmation="完整案例预置的采用声明",
        entity_origin=SEED_ORIGIN,
    )
    return a0, a1


def _seed_session(seed, *, applied, plan, label, end_s):
    tx = seed.tx
    context_id = tx.workspace()["context_id"]
    session = tx.insert(
        "Session",
        {
            "context_id": context_id,
            "applied_id": applied["id"],
            "plan_id": plan["id"],
            "run_conditions": {"scene": plan["scene"], "note": "完整案例预置会话"},
            "status": "closed",
            "active_segment_id": None,
            "started_at": seed.clock.now_iso(),
            "ended_at": seed.clock.now_iso(),
            "time_base": "simulated",
        },
        origin=SEED_ORIGIN,
    )
    segment = tx.insert(
        "Segment",
        {
            "session_id": session["id"],
            "binding_id": None,
            "status": "open",
            "end_s": None,
            "start_monotonic": 0.0,
            "started_at": seed.clock.now_iso(),
            "time_base": "simulated",
        },
        origin=SEED_ORIGIN,
    )
    binding = tx.insert(
        "Binding",
        {
            "segment_id": segment["id"],
            "context_id": context_id,
            "applied_id": applied["id"],
            "plan_id": plan["id"],
            "configuration_state": "confirmed",
            "supersedes_id": None,
        },
        origin=SEED_ORIGIN,
    )
    segment = tx.update_entity(segment["id"], {"binding_id": binding["id"]}, revision=1)
    recipe = seed.catalog.recipe(seed.scenario_id, label)
    data = generate_trace(seed.scenario_id, label, recipe)
    artifact, _ = performance._artifact_from_bytes(
        seed.service,
        tx,
        data=data,
        label=label,
        session_id=session["id"],
        segment_id=segment["id"],
        wait_s=12.0,
        origin="capture",
        entity_origin=SEED_ORIGIN,
    )
    closed = tx.update_entity(
        segment["id"],
        {"status": "closed", "end_s": end_s, "ended_at": seed.clock.now_iso()},
        revision=segment["revision"] + 1,
    )
    summary, _ = performance._build_summary(
        seed, artifact, closed, binding, origin=SEED_ORIGIN
    )
    return session, closed, artifact, summary


def _seed_feedback(seed, summary, applied):
    tx = seed.tx
    rule = seed.scenario["rules"]["symptom_rules"][0]
    feedback_entity = tx.insert(
        "Feedback",
        {
            "session_id": summary["session_id"],
            "summary_id": summary["id"],
            "satisfied": False,
            "symptoms": list(rule["symptoms"]),
            "severity": "occasional",
            "raw_text": "完整案例预置：交战时偶尔卡顿，希望保持画面清晰",
            "protected_setting_ids": [seed.scenario["rules"]["protected_options"][0]],
            "created_seq": tx.latest_seq(),
            "note": "完整案例预置反馈",
        },
        origin=SEED_ORIGIN,
    )
    setting_id = rule["target_setting_id"]
    change_set = tx.insert(
        "ChangeSet",
        {
            "feedback_id": feedback_entity["id"],
            "base_applied_id": applied["id"],
            "changes": {
                setting_id: {
                    "from": rule["from"],
                    "to": rule["to"],
                    "setting_id": setting_id,
                }
            },
            "hypothesis": rule["hypothesis"],
            "evidence_ids": [],
            "protected_setting_ids": feedback_entity["protected_setting_ids"],
            "retest": rule["retest"],
            "recovery": {
                "setting_id": setting_id,
                "restore_value": rule["from"],
                "note": "在游戏内手动恢复旧值。",
            },
            "alternative": None,
            "status": "proposed",
        },
        origin=SEED_ORIGIN,
    )
    return feedback_entity, change_set


def _seed_adjustment(seed, a1, change_set, context_id):
    tx = seed.tx
    settings = dict(a1["settings"])
    for setting_id, change in change_set["changes"].items():
        settings[setting_id] = change["to"]
    return configuration._insert_configuration(
        seed,
        settings=settings,
        origin="manual",
        parent=a1,
        recommendation_id=None,
        preset=a1.get("preset"),
        confirmation="完整案例预置的手动调整声明",
        entity_origin=SEED_ORIGIN,
        change_set_id=change_set["id"],
    )
