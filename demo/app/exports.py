"""Demo reports, backup, and restore. All artifacts stay inside demo/runtime."""

from __future__ import annotations

import base64
import hashlib
import json

from .contracts import DemoError, DomainResult
from .service import require_fields

BACKUP_SCHEMA = "demo-backup/1"
MAX_BACKUP_BYTES = 5 * 1024 * 1024
REPORT_TITLE = "演示报告，非真实实测"

KINDS_FOR_REPORT = (
    "ContextSnapshot",
    "KnowledgeSnapshot",
    "Recommendation",
    "AppliedConfiguration",
    "TestPlan",
    "Session",
    "Summary",
    "ComparisonProtocol",
    "Comparison",
    "Feedback",
    "ChangeSet",
    "Outcome",
)


def register(service) -> None:
    service.register("export.create", validate_export, handle_export)
    service.register("backup.create", validate_backup_create, handle_backup_create)
    service.register("backup.restore", validate_backup_restore, handle_backup_restore)


def validate_export(payload):
    require_fields(payload, required=("format",), allowed=("format", "comparison_id"))
    if payload["format"] not in ("markdown", "json"):
        raise DemoError("invalid_format", "导出格式必须是 markdown 或 json。")
    return payload


def validate_backup_create(payload):
    return require_fields(payload, required=(), allowed=())


def validate_backup_restore(payload):
    return require_fields(payload, required=("file_id",), allowed=("file_id",))


def _context_for_summary(tx, summary):
    binding = tx.get(summary["binding_id"], kind="Binding")
    return tx.get(binding["context_id"], kind="ContextSnapshot")


def _background_entry(context, summaries):
    return {
        "context_id": context["id"],
        "game_name": context.get("game_name"),
        "gpu_name": context.get("gpu_name"),
        "resolution": context.get("resolution"),
        "applicability": context.get("applicability"),
        "unknown_fields": context.get("unknown_fields", []),
        "summary_ids": [summary["id"] for summary in summaries],
    }


def _report(ctx, comparison_id=None):
    tx = ctx.tx
    workspace = tx.workspace()
    summaries = tx.find("Summary")
    comparisons = tx.find("Comparison")
    if comparison_id is not None:
        chosen = tx.get(comparison_id, kind="Comparison")
    else:
        chosen = comparisons[-1] if comparisons else None

    summary_contexts = {}
    for summary in summaries:
        summary_contexts[summary["id"]] = _context_for_summary(tx, summary)

    if chosen is not None:
        before = tx.get(chosen["before_summary_id"], kind="Summary")
        after = tx.get(chosen["after_summary_id"], kind="Summary")
        focus = summary_contexts.get(before["id"]) or _context_for_summary(tx, before)
        report_summaries = [before, after]
    elif summaries:
        latest = summaries[-1]
        focus = summary_contexts[latest["id"]]
        report_summaries = summaries
    else:
        focus = tx.get(workspace["context_id"], kind="ContextSnapshot")
        report_summaries = []

    grouped = {}
    for summary in report_summaries:
        context = summary_contexts[summary["id"]]
        grouped.setdefault(context["id"], {"context": context, "summaries": []})[
            "summaries"
        ].append(summary)
    backgrounds = [
        _background_entry(item["context"], item["summaries"])
        for item in grouped.values()
    ]
    configs = tx.find("AppliedConfiguration")
    by_id = {config["id"]: config for config in configs}
    return {
        "demo_only": True,
        "title": REPORT_TITLE,
        "scenario_id": ctx.scenario_id,
        "game_name": focus.get("game_name"),
        "gpu_name": focus.get("gpu_name"),
        "background": {
            "context_id": focus["id"],
            "resolution": focus.get("resolution"),
            "applicability": focus.get("applicability"),
            "unknown_fields": focus.get("unknown_fields", []),
        },
        "backgrounds": backgrounds,
        "mixed_backgrounds": len(grouped) > 1,
        "sources": [
            {
                "url": source.get("url"),
                "title": source.get("title"),
                "relationship": source.get("relationship"),
                "author_label": source.get("author_label"),
            }
            for source in tx.find("Source")
        ],
        "configuration_chain": [
            {
                "id": config["id"],
                "origin": config.get("origin"),
                "parent_id": config.get("parent_id"),
                "rollback_target_id": config.get("rollback_target_id"),
                "settings": config.get("settings"),
            }
            for config in configs
        ],
        "current_applied_id": workspace.get("current_applied_id"),
        "plans": [
            {
                "id": plan["id"],
                "scene": plan.get("scene"),
                "duration_s": plan.get("duration_s"),
                "repeats": plan.get("repeats"),
                "metrics": plan.get("metrics"),
            }
            for plan in tx.find("TestPlan")
        ],
        "summaries": [
            {
                "id": summary["id"],
                "label": summary.get("label"),
                "source_hash": summary.get("source_hash"),
                "quality": summary.get("quality"),
                "metrics": summary.get("metrics"),
                "applied_id": summary.get("applied_id"),
                "context_id": summary_contexts[summary["id"]]["id"],
                "game_name": summary_contexts[summary["id"]].get("game_name"),
                "gpu_name": summary_contexts[summary["id"]].get("gpu_name"),
            }
            for summary in summaries
        ],
        "comparison": None
        if chosen is None
        else {
            "id": chosen["id"],
            "threshold_ms": chosen.get("threshold_ms"),
            "comparable": chosen.get("comparable"),
            "exploratory": chosen.get("exploratory"),
            "metrics": chosen.get("metrics"),
            "notes": chosen.get("comparability_notes", []),
            "before_source_hash": chosen.get("before_source_hash"),
            "after_source_hash": chosen.get("after_source_hash"),
        },
        "feedback": [
            {
                "id": item["id"],
                "raw_text": item.get("raw_text"),
                "symptoms": item.get("symptoms"),
                "protected_setting_ids": item.get("protected_setting_ids"),
            }
            for item in tx.find("Feedback")
        ],
        "change_sets": [
            {
                "id": item["id"],
                "base_applied_id": item.get("base_applied_id"),
                "changes": item.get("changes"),
                "hypothesis": item.get("hypothesis"),
            }
            for item in tx.find("ChangeSet")
        ],
        "outcomes": [
            {
                "id": item["id"],
                "accepted": item.get("accepted"),
                "data_change": item.get("data_change"),
            }
            for item in tx.find("Outcome")
        ],
        "limitations": [
            "本报告只描述演示样例，不是真实游戏测量。",
            "设置名称与效果未在真实游戏中核验。",
            "样例数值变化不构成单项优化因果结论。",
        ],
        "config_parents": {
            config["id"]: config.get("parent_id") for config in configs
        },
    }


def _markdown(report):
    lines = [
        f"# {report['title']}",
        "",
        "> 仅用于临时演示。正式开发不得以本报告作为依据。",
        "",
        f"- 案例：`{report['scenario_id']}`",
        f"- 游戏：{report['game_name']}",
        f"- 显卡：{report['gpu_name']}",
        f"- 演示背景：{report['background']['resolution']}（{report['background']['applicability']}）",
        "",
        "## 来源性质",
        "",
    ]
    for source in report["sources"]:
        lines.append(
            f"- {source['title']}（{source['relationship']}，{source['author_label']}）"
        )
    lines.extend(["", "## 配置链", ""])
    for config in report["configuration_chain"]:
        lines.append(
            f"- `{config['id']}` origin={config['origin']} parent={config['parent_id']} "
            f"rollback={config['rollback_target_id']}"
        )
    lines.extend(["", "## 样例摘要", ""])
    for summary in report["summaries"]:
        metrics = summary["metrics"] or {}
        lines.append(
            f"- `{summary['id']}` {summary['label']}：平均呈现帧率 "
            f"{metrics.get('average_present_rate')} fps，P95 {metrics.get('interval_p95_ms')} ms，"
            f"原件 {summary['source_hash']}"
        )
        lines.append(
            f"  - 该摘要背景：{summary.get('game_name')} / {summary.get('gpu_name')}"
        )
    if report["mixed_backgrounds"]:
        lines.extend(["", "## 各摘要背景", ""])
        lines.append("- 历史摘要来自不同背景，按摘要分组列出，不使用当前背景覆盖。")
        for item in report["backgrounds"]:
            lines.append(
                f"- {item['game_name']} / {item['gpu_name']}，分辨率 {item['resolution']}"
                f"（{item['applicability']}）：{', '.join(item['summary_ids'])}"
            )
    comparison = report["comparison"]
    if comparison:
        lines.extend(["", "## 前后比较（冻结口径）", ""])
        lines.append(f"- 共同阈值：{comparison['threshold_ms']} ms")
        lines.append(f"- 可比较：{comparison['comparable']}；探索性：{comparison['exploratory']}")
        for row in comparison["metrics"]:
            lines.append(
                f"- {row['key']}：{row['before']} → {row['after']} {row['unit']}"
            )
        for note in comparison["notes"]:
            lines.append(f"- 局限：{note}")
    lines.extend(["", "## 反馈与结果", ""])
    for item in report["feedback"]:
        lines.append(f"- 反馈：{item['raw_text']}")
    for item in report["outcomes"]:
        lines.append(f"- 结果接受：{item['accepted']}")
    lines.extend(["", "## 限制", ""])
    for item in report["limitations"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def handle_export(ctx, payload):
    report = _report(ctx, payload.get("comparison_id"))
    if payload["format"] == "json":
        content = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        media_type = "application/json"
        filename = "demo-report.json"
    else:
        content = _markdown(report)
        media_type = "text/plain"
        filename = "demo-report.md"
    staged = ctx.files.stage(content.encode("utf-8"), media_type)
    try:
        record = ctx.files.register(ctx.tx, staged, filename)
    except Exception:
        ctx.files.discard(staged)
        raise
    ctx.tx.emit("export.created", {"file_id": record["id"], "format": payload["format"]})
    return DomainResult(
        201,
        [{"kind": "File", "id": record["id"]}],
        data={"file_id": record["id"], "format": payload["format"]},
    )


def _referenced_file_ids(entities):
    """File IDs that a business entity points at.

    Backup and export artifacts are themselves File entities but are not
    referenced by any business object, so they stay out of later backups.
    """
    referenced = set()
    for entity in entities:
        if entity["kind"] == "File":
            continue
        file_id = entity["payload"].get("file_id")
        if isinstance(file_id, str):
            referenced.add(file_id)
    return referenced


def handle_backup_create(ctx, payload):
    tx = ctx.tx
    entities = tx.all_entities()
    referenced = _referenced_file_ids(entities)
    files = []
    for entity in entities:
        if entity["kind"] != "File" or entity["id"] not in referenced:
            continue
        payload_data = entity["payload"]
        data = ctx.files.read(entity["id"], expected_hash=payload_data["content_hash"])
        files.append(
            {
                "id": entity["id"],
                "media_type": payload_data["media_type"],
                "filename_label": payload_data["filename_label"],
                "content_hash": payload_data["content_hash"],
                "size": payload_data["size"],
                "content_b64": base64.b64encode(data).decode("ascii"),
            }
        )
    entities = [
        entity
        for entity in entities
        if entity["kind"] != "File" or entity["id"] in referenced
    ]
    workspace = tx.workspace()
    document = {
        "schema": BACKUP_SCHEMA,
        "demo_only": True,
        "scenario_id": ctx.scenario_id,
        "created_at": ctx.clock.now_iso(),
        "workspace": {
            "context_id": workspace["context_id"],
            "current_applied_id": workspace["current_applied_id"],
            "mode": workspace["mode"],
            "script": workspace["script"],
        },
        "entities": entities,
        "files": files,
    }
    content = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(content) > MAX_BACKUP_BYTES:
        raise DemoError("backup_too_large", "演示备份不能超过 5 MiB。", status=413)
    staged = ctx.files.stage(content, "application/json")
    try:
        record = ctx.files.register(ctx.tx, staged, "demo-backup.json")
    except Exception:
        ctx.files.discard(staged)
        raise
    ctx.tx.emit("backup.created", {"file_id": record["id"]})
    return DomainResult(
        201,
        [{"kind": "File", "id": record["id"]}],
        data={"file_id": record["id"]},
    )


def _decode_backup(data, scenario_id):
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemoError("invalid_backup", "备份不是有效 JSON。") from exc
    if not isinstance(document, dict):
        raise DemoError("invalid_backup", "备份必须是 JSON 对象。")
    if document.get("schema") != BACKUP_SCHEMA or document.get("demo_only") is not True:
        raise DemoError("invalid_backup", "只支持 demo-backup/1 演示备份。")
    if document.get("scenario_id") != scenario_id:
        raise DemoError("cross_case_backup", "备份属于另一个演示案例。", status=409)
    decoded = []
    total = 0
    for item in document.get("files", []):
        try:
            raw = base64.b64decode(item["content_b64"], validate=True)
        except Exception as exc:
            raise DemoError("invalid_backup", "备份文件内容无法解码。") from exc
        total += len(raw)
        if total > MAX_BACKUP_BYTES:
            raise DemoError("backup_too_large", "演示备份不能超过 5 MiB。", status=413)
        if hashlib.sha256(raw).hexdigest() != item.get("content_hash"):
            raise DemoError("invalid_backup", "备份文件哈希与内容不一致。")
        if len(raw) != item.get("size"):
            raise DemoError("invalid_backup", "备份文件大小与内容不一致。")
        decoded.append((item, raw))
    return document, decoded


ENTITY_REF_KINDS = {
    "context_id": {"ContextSnapshot"},
    "applied_id": {"AppliedConfiguration"},
    "plan_id": {"TestPlan"},
    "active_segment_id": {"Segment"},
    "binding_id": {"Binding"},
    "session_id": {"Session"},
    "segment_id": {"Segment"},
    "artifact_id": {"LogArtifact"},
    "protocol_id": {"ComparisonProtocol"},
    "before_summary_id": {"Summary"},
    "after_summary_id": {"Summary"},
    "summary_id": {"Summary"},
    "feedback_id": {"Feedback"},
    "change_set_id": {"ChangeSet"},
    "comparison_id": {"Comparison"},
    "recommendation_id": {"Recommendation"},
    "knowledge_id": {"KnowledgeSnapshot"},
    "request_id": {"InformationRequest"},
    "correction_of": {"InformationRequest"},
    "parent_id": {"AppliedConfiguration"},
    "rollback_target_id": {"AppliedConfiguration"},
    "local_summary_id": {"Summary"},
    "variant_of": {"Recommendation"},
    "source_id": {"Source"},
    "file_id": {"__file__"},
}
LIST_REF_KINDS = {
    "source_ids": {"Source"},
    "record_ids": {"EvidenceRecord"},
    "gap_ids": {"KnowledgeGap"},
    "followup_ids": {"FollowUpNote"},
    "evidence_ids": {"Source"},
    "baseline_summary_ids": {"Summary"},
}
SUPERSEDES_KINDS = {
    "ContextSnapshot": {"ContextSnapshot"},
    "AppliedConfiguration": {"AppliedConfiguration"},
    "Binding": {"Binding"},
    "Recommendation": {"Recommendation"},
    "KnowledgeSnapshot": {"KnowledgeSnapshot"},
    "KnowledgeGap": {"KnowledgeGap"},
}


def _validate_entity_graph(document, file_ids):
    """Reject a backup whose object graph does not close on itself."""
    from .contracts import is_positive_int
    from .store import ORIGINS

    records = []
    ids: dict[str, str] = {}
    for entity in document.get("entities", []):
        if not isinstance(entity, dict):
            raise DemoError("invalid_backup", "备份实体必须是对象。")
        entity_id = entity.get("id")
        kind = entity.get("kind")
        payload = entity.get("payload")
        if not isinstance(entity_id, str) or not entity_id:
            raise DemoError("invalid_backup", "备份实体缺少标识。")
        if not isinstance(kind, str) or not kind:
            raise DemoError("invalid_backup", "备份实体缺少类型。")
        if not isinstance(payload, dict):
            raise DemoError("invalid_backup", "备份实体内容必须是对象。")
        if payload.get("id") != entity_id or payload.get("kind") != kind:
            raise DemoError("invalid_backup", "备份实体的标识或类型不一致。")
        if entity_id in ids:
            raise DemoError("invalid_backup", "备份实体标识重复。")
        if payload.get("data_origin") not in ORIGINS:
            raise DemoError("invalid_backup", "备份实体包含不支持的数据来源。")
        if entity.get("data_origin") not in ORIGINS:
            raise DemoError("invalid_backup", "备份实体包含不支持的数据来源。")
        if not is_positive_int(payload.get("revision")):
            raise DemoError("invalid_backup", "备份实体修订号无效。")
        ids[entity_id] = kind
        records.append(payload)

    def require(ref, allowed, label):
        if ref is None:
            return
        if not isinstance(ref, str) or not ref:
            raise DemoError("invalid_backup", f"{label} 引用无效。")
        if "__file__" in allowed and ref in file_ids:
            return
        if ref in ids:
            if ids[ref] not in allowed:
                raise DemoError("invalid_backup", f"{label} 引用了错误类型的对象。")
            return
        raise DemoError("invalid_backup", f"{label} 引用了缺失的演示对象。")

    for payload in records:
        for field, allowed in ENTITY_REF_KINDS.items():
            if field in payload:
                require(payload[field], allowed, field)
        for field, allowed in LIST_REF_KINDS.items():
            value = payload.get(field)
            if value is None:
                continue
            if not isinstance(value, list):
                raise DemoError("invalid_backup", f"{field} 必须是引用列表。")
            for ref in value:
                require(ref, allowed, field)
        supersedes = payload.get("supersedes_id")
        if supersedes is not None:
            allowed = SUPERSEDES_KINDS.get(payload["kind"])
            if allowed is None:
                raise DemoError("invalid_backup", "该对象不应包含 supersedes_id。")
            require(supersedes, allowed, "supersedes_id")
        if payload["kind"] == "Recommendation":
            for setting in payload.get("settings", []):
                for ref in setting.get("evidence_ids", []):
                    require(ref, {"Source"}, "settings.evidence_ids")
                for ref in setting.get("evidence_record_ids", []):
                    require(ref, {"EvidenceRecord"}, "settings.evidence_record_ids")
        if payload["kind"] == "KnowledgeSnapshot":
            for record in payload.get("records", []):
                require(record.get("id"), {"EvidenceRecord"}, "records.id")
                require(record.get("source_id"), {"Source"}, "records.source_id")

    workspace_state = document.get("workspace") or {}
    context_id = workspace_state.get("context_id")
    current_id = workspace_state.get("current_applied_id")
    require(context_id, {"ContextSnapshot"}, "workspace.context_id")
    require(current_id, {"AppliedConfiguration"}, "workspace.current_applied_id")


def handle_backup_restore(ctx, payload):
    record = ctx.tx.get(payload["file_id"], kind="File")
    data = ctx.files.read(payload["file_id"], expected_hash=record["content_hash"])
    document, decoded_files = _decode_backup(data, ctx.scenario_id)
    file_ids = {item["id"] for item, _ in decoded_files}
    if len(file_ids) != len(decoded_files):
        raise DemoError("invalid_backup", "备份文件标识重复。")
    # Validate the whole graph before touching the current workspace or moving
    # any managed file. A corrupt backup is rejected as a unit.
    _validate_entity_graph(document, file_ids)
    reset = ctx.tx.reset_workspace(ctx.scenario_id, ctx.catalog)
    tx = ctx.tx.rescope(reset["new_epoch"])
    file_map = {}
    moved_files: list[str] = []
    try:
        for item, raw in decoded_files:
            staged = ctx.files.stage(raw, item["media_type"])
            try:
                registered = ctx.files.register(tx, staged, item["filename_label"])
            except Exception:
                ctx.files.discard(staged)
                raise
            file_map[item["id"]] = registered["id"]
            moved_files.append(registered["id"])
        reserved = {
            "id",
            "kind",
            "scenario_id",
            "workspace_epoch",
            "created_at",
            "demo_only",
            "data_origin",
            "revision",
        }
        restored = 0
        restored_ids = set()
        for entity in document.get("entities", []):
            if entity["kind"] == "File":
                continue
            body = {
                key: value
                for key, value in entity["payload"].items()
                if key not in reserved
            }
            if "file_id" in body:
                if body["file_id"] not in file_map:
                    raise DemoError("invalid_backup", "备份引用了缺失的演示文件。")
                body["file_id"] = file_map[body["file_id"]]
            tx.insert(
                entity["kind"],
                body,
                origin=entity["data_origin"],
                entity_id=entity["id"],
                revision=entity["revision"],
            )
            restored_ids.add(entity["id"])
            restored += 1
        workspace_state = document.get("workspace") or {}
        context_id = workspace_state.get("context_id")
        current_id = workspace_state.get("current_applied_id")
        tx.update_workspace(
            context_id=context_id if context_id in restored_ids else tx.workspace()["context_id"],
            current_applied_id=current_id if current_id in restored_ids else None,
        )
        tx.emit("backup.restored", {"entities": restored})
    except Exception:
        # The transaction will roll back the entity rows; remove the physical
        # copies moved into the new epoch so no unregistered files remain.
        for file_id in moved_files:
            ctx.files.unlink_managed(file_id)
        raise
    return DomainResult(
        200,
        data={
            "workspace_epoch": reset["new_epoch"],
            "entities": restored,
            "files": len(file_map),
            "_cleanup_file_ids": reset["file_ids"],
        },
    )
