"""Deterministic grounding is a gate, not independent semantic verification."""

import re
from collections import Counter

from .schema import Extraction, Document, RECORD_GROUPS


def _norm(value):
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", str(value))
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return " ".join(text.split()).casefold()


def _contains(value, text):
    if isinstance(value, bool):
        return _norm(value) in _norm(text)
    if isinstance(value, (int, float)):
        return any(float(t.replace(",", "")) == value for t in re.findall(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w.])", text))
    return _norm(value) in _norm(text)


def proof_issues(proof, doc):
    blocks = {b.id: b for b in doc.blocks}
    if proof.block_id not in blocks:
        return ["missing_source_block"]
    block = blocks[proof.block_id]
    text = block.text
    if proof.scope == "cell":
        if proof.row is None or proof.column is None:
            return ["cell_coordinates_required"]
    if proof.row is not None or proof.column is not None:
        if block.kind != "table" or proof.row is None or proof.row >= len(block.rows):
            return ["invalid_table_coordinates"]
        if proof.column is not None and proof.scope != "row":
            if proof.column >= len(block.rows[proof.row]):
                return ["invalid_table_coordinates"]
            text = block.rows[proof.row][proof.column]
        else:
            text = " | ".join(block.rows[proof.row])
    return [] if _norm(proof.quote) in _norm(text) else ["quote_not_in_source_position"]


def validate_extraction(extraction: Extraction, document: Document, context: dict) -> list[dict]:
    objects = [(group, item) for group in RECORD_GROUPS for item in getattr(extraction, group)]
    counts = Counter(item.id for _, item in objects)
    items = {item.id: item for _, item in objects}
    groups = {item.id: group for group, item in objects}
    rows, refs = {}, {}
    game_issues = proof_issues(extraction.game_proof, document)
    if extraction.game_match != "matched":
        game_issues.append("game_identity_unconfirmed")
    # Literal alias check is additional protection; no guessing localized game aliases.
    aliases = [context.get("game_name", ""), *context.get("game_aliases", [])]
    if not any(alias and _norm(alias) in _norm(extraction.game_proof.quote) for alias in aliases):
        game_issues.append("game_name_not_grounded_in_declared_aliases")
    if extraction.platform and not any(s in extraction.platform.casefold() for s in ["pc", "windows"]):
        game_issues.append("platform_mismatch")

    for group, item in objects:
        issues = list(game_issues)
        dependencies = []
        if counts[item.id] > 1:
            issues.append("duplicate_record_id")
        proof = getattr(item, "proof", None)
        if proof:
            issues.extend(proof_issues(proof, document))
        if group == "settings" and not _contains(item.raw_name, item.proof.quote):
            issues.append("setting_name_not_grounded")
        if group == "environments":
            for field in item.fields.values():
                if field.proof:
                    issues.extend(proof_issues(field.proof, document))
                    if field.value is not None and not _contains(field.value, field.proof.quote):
                        issues.append("environment_value_not_grounded")
        if group == "evidence":
            dependencies.extend((i, "evidence") for i in item.input_ids)
            dependencies.extend((i, "settings") for i in item.setting_ids)
            if item.configuration_id:
                dependencies.append((item.configuration_id, "configurations"))
            if item.environment_id:
                dependencies.append((item.environment_id, "environments"))
            if item.numeric and item.origin != "agent_derived":
                numbers = [item.numeric.value, item.numeric.lower, item.numeric.upper]
                if any(n is not None and not _contains(n, item.proof.quote) for n in numbers):
                    issues.append("numeric_not_grounded")
                if item.metric == "average_fps" and not re.search(r"average|\bavg\b|平均", item.proof.quote, re.I):
                    issues.append("average_statistic_not_grounded")
                quote = item.proof.quote
                if "fps" in item.metric:
                    if not re.search(r"fps|frames?\s+per\s+second|帧", quote, re.I) or item.unit.casefold() not in {"fps", "frames/s", "帧/秒"}:
                        issues.append("metric_unit_not_grounded")
                elif item.metric in {"latency", "frame_time"}:
                    if not re.search(r"\bms\b|milliseconds?|毫秒", quote, re.I) or item.unit.casefold() not in {"ms", "milliseconds", "毫秒"}:
                        issues.append("metric_unit_not_grounded")
                elif item.metric == "memory_usage" and not re.search(r"\b[gm]i?b\b|[兆吉]字节", quote, re.I):
                    issues.append("metric_unit_not_grounded")
            if item.origin == "agent_derived" and item.numeric and not item.conversion:
                issues.append("derived_numeric_method_missing")
        if group == "configurations":
            dependencies.extend((i, "environments") for i in item.environment_ids)
            for assignment in item.assignments:
                dependencies.append((assignment.setting_id, "settings"))
                dependencies.extend((i, "evidence") for i in assignment.evidence_ids)
                evs = [items.get(i) for i in assignment.evidence_ids if groups.get(i) == "evidence"]
                source_text = " ".join(e.proof.quote for e in evs if e and e.proof)
                val = assignment.value
                values = [val.value] if val.value_kind == "fixed" else val.alternatives if val.value_kind == "alternatives" else [b.value for b in val.branches]
                if any(v is not None and not _contains(v, source_text) for v in values):
                    issues.append("assignment_value_not_grounded")
                setting = items.get(assignment.setting_id)
                for ev in evs:
                    if ev.setting_ids and assignment.setting_id not in ev.setting_ids:
                        issues.append("assignment_evidence_setting_mismatch")
                    if ev.configuration_id and ev.configuration_id != item.id:
                        issues.append("assignment_evidence_configuration_mismatch")
                    if setting and ev.proof and setting.proof.block_id == ev.proof.block_id and setting.proof.row is not None and ev.proof.row is not None and setting.proof.row != ev.proof.row:
                        issues.append("assignment_table_row_mismatch")
                    if ev.proof and item.proof.scope == "cell" and ev.proof.scope == "cell" and item.proof.block_id == ev.proof.block_id and item.proof.column != ev.proof.column:
                        issues.append("assignment_configuration_column_mismatch")
                    for environment_id in item.environment_ids:
                        environment = items.get(environment_id)
                        if groups.get(environment_id) == "environments":
                            for field in environment.fields.values():
                                p = field.proof
                                if p and p.scope == "cell" and ev.proof and ev.proof.scope == "cell" and p.block_id == ev.proof.block_id and p.column != ev.proof.column:
                                    issues.append("assignment_environment_column_mismatch")
                if val.value_kind == "conditional" and any(not _contains(b.condition, source_text) for b in val.branches):
                    issues.append("conditional_text_not_grounded")
            if len({a.setting_id for a in item.assignments}) != len(item.assignments):
                issues.append("duplicate_setting_assignment")
        if group == "comparisons":
            dependencies.extend((i, "evidence") for i in item.measurement_ids)
            measurements = [items.get(i) for i in item.measurement_ids]
            if any(m is None or groups.get(m.id) != "evidence" or m.kind != "measurement" for m in measurements):
                issues.append("comparison_requires_measurements")
            elif item.strictly_comparable:
                envs = [items.get(m.environment_id) for m in measurements]
                required = {"gpu", "cpu", "resolution", "scene"}
                fields = [{k: v.value for k, v in e.fields.items()} if e and groups.get(e.id) == "environments" else {} for e in envs]
                if any(not required.issubset(f) or any(f[k] is None for k in required) for f in fields) or fields[0] != fields[1] or measurements[0].metric != measurements[1].metric:
                    issues.append("comparison_conditions_differ_or_unknown")
            if not item.proof and not item.formula:
                issues.append("comparison_provenance_missing")
        if group in {"constraints", "version_impacts"}:
            dependencies.extend([(item.evidence_id, "evidence"), (item.setting_id, "settings")])
            ev = items.get(item.evidence_id)
            expected_kind = "constraint" if group == "constraints" else "version_change"
            if ev and groups.get(ev.id) == "evidence" and ev.kind != expected_kind:
                issues.append("relation_evidence_kind_mismatch")
            if group == "constraints" and ev and groups.get(ev.id) == "evidence":
                text = ev.proof.quote if ev.proof else ""
                setting = items.get(item.setting_id)
                if not setting or groups.get(item.setting_id) != "settings" or not _contains(setting.raw_name, text):
                    issues.append("constraint_setting_not_grounded")
                if any(not _contains(v, text) for v in [*item.when.values(), *item.affected_values]):
                    issues.append("constraint_predicate_not_grounded")
                if item.kind == "exclusion" and not re.search(r"not support|doesn.t support|unsupported|cannot|not compatible|不支持|不可|不兼容", text, re.I):
                    issues.append("exclusion_relation_not_grounded")
                for required_id, required_value in item.requires.items():
                    required_setting = items.get(required_id)
                    if groups.get(required_id) != "settings" or not _contains(required_setting.raw_name, text) or not _contains(required_value, text):
                        issues.append("constraint_dependency_not_grounded")
            if group == "version_impacts":
                dependencies.extend((i, None) for i in item.review_record_ids)
        for ref, expected in dependencies:
            if ref not in items or (expected and groups[ref] != expected):
                issues.append("invalid_reference:" + ref)
        refs[item.id] = [i for i, _ in dependencies]
        rows[item.id] = {
            "local_id": item.id, "record_type": group, "payload": item.model_dump(),
            "record_status": "quarantined" if issues else "accepted", "issues": sorted(set(issues)),
            "extraction_status": "parsed", "check_method": "deterministic_shape_reference_and_lexical_grounding",
            "applicability": "unknown", "recommendation_status": None, "branch_checks": [],
            "checks_not_performed": ["independent_semantic_review", "current_build_coverage", "complete_hardware_compatibility"],
        }

    # Fixed point: an accepted object may not silently depend on quarantined evidence.
    for _ in range(len(rows)):
        changed = False
        for id, row in rows.items():
            if row["record_status"] == "accepted" and any(rows.get(r, {}).get("record_status") == "quarantined" for r in refs[id]):
                row["record_status"] = "quarantined"
                row["issues"].append("quarantined_dependency")
                changed = True
        if not changed:
            break
    for cfg in extraction.configurations:
        row = rows[cfg.id]
        if row["record_status"] != "accepted":
            row["recommendation_status"] = "needs_followup"
            continue
        unresolved = any(a.value.value_kind != "fixed" for a in cfg.assignments) or not cfg.assignments
        blocked, suspect = False, False
        for rule in extraction.constraints:
            if rows[rule.id]["record_status"] != "accepted":
                continue
            relevant = [a for a in cfg.assignments if a.setting_id == rule.setting_id]
            if not relevant:
                continue
            if not rule.verified_scope or rule.kind == "suspected_conflict" or not rule.when or any(context.get(k) != v for k, v in rule.when.items()):
                suspect = True
                continue
            for a in relevant:
                candidates = [a.value.value] if a.value.value_kind == "fixed" else a.value.alternatives
                affected = [v for v in candidates if not rule.affected_values or v in rule.affected_values]
                if rule.kind == "exclusion" and affected:
                    row["branch_checks"].append({"setting_id": a.setting_id, "values": affected, "status": "excluded_branch", "evidence_id": rule.evidence_id})
                    blocked |= a.value.value_kind == "fixed" or len(affected) == len(candidates)
                if rule.kind == "dependency":
                    actual = {x.setting_id: x.value.value for x in cfg.assignments if x.value.value_kind == "fixed"}
                    if any(actual.get(k) != v for k, v in rule.requires.items()):
                        suspect = True
        if any(cfg.id in v.review_record_ids for v in extraction.version_impacts):
            suspect = True  # Keep history. Never auto-change a setting after a patch.
        if any(cfg.id in g.record_ids and g.priority in {"P0", "P1"} for g in extraction.gaps):
            suspect = True
        matching_env = any(context.get("gpu") and e.fields.get("gpu") and e.fields["gpu"].value == context["gpu"] for e in extraction.environments if e.id in cfg.environment_ids and rows[e.id]["record_status"] == "accepted")
        if blocked:
            row.update(applicability="fail", recommendation_status="excluded_branch")
        elif matching_env and not unresolved and not suspect:
            row.update(recommendation_status="candidate")  # Conditional reference; still not a full compatibility pass.
        else:
            row.update(recommendation_status="needs_followup")
    return list(rows.values())
