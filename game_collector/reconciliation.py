"""Recheck previous claims against newly grounded rules; keep source payloads immutable."""

from copy import deepcopy
import re

from .schema import now
from .validation import _contains, _norm


def recheck_configurations(records, context):
    index = {r["id"]: r for r in records}
    rules = [r for r in records if r["record_type"] == "constraints" and r["record_status"] == "accepted"]
    for cfg in records:
        if cfg["record_type"] != "configurations" or cfg["record_status"] != "accepted" or cfg.get("superseded_by"):
            continue
        prefix = cfg["reference_scope"] + ":"
        assignments = cfg["payload"]["assignments"]
        before = {k: deepcopy(cfg.get(k)) for k in ("applicability", "recommendation_status", "branch_checks")}
        used = set(cfg.get("review_evidence_ids", []))
        for rule in rules:
            r = rule["payload"]
            rule_prefix = rule["reference_scope"] + ":"
            setting = index.get(rule_prefix + r["setting_id"])
            if not setting:
                continue
            # Exact original names only; aliases are not automatic equivalence rules.
            name = _norm(setting["payload"]["raw_name"])
            affected = [a for a in assignments if index.get(prefix + a["setting_id"], {}).get("record_type") == "settings" and _norm(index[prefix + a["setting_id"]]["payload"]["raw_name"]) == name]
            if not affected or not r["verified_scope"] or not r["when"] or any(context.get(k) != v for k, v in r["when"].items()):
                continue
            evidence_id = rule_prefix + r["evidence_id"]
            if index.get(evidence_id, {}).get("record_status") != "accepted":
                continue
            if r["kind"] == "exclusion":
                for assignment in affected:
                    v = assignment["value"]
                    values = [v["value"]] if v["value_kind"] == "fixed" else v["alternatives"]
                    bad = [value for value in values if not r["affected_values"] or value in r["affected_values"]]
                    if bad:
                        check = {"setting_id": assignment["setting_id"], "values": bad, "status": "excluded_branch", "evidence_id": evidence_id}
                        if check not in cfg["branch_checks"]:
                            cfg["branch_checks"].append(check)
                        used.add(evidence_id)
                        if v["value_kind"] == "fixed" or len(bad) == len(values):
                            cfg.update(applicability="fail", recommendation_status="excluded_branch")
                        else:
                            cfg["recommendation_status"] = "needs_followup"
            elif r["kind"] in {"dependency", "suspected_conflict"}:
                cfg["recommendation_status"] = "needs_followup"
                cfg.setdefault("pending_rule_evidence_ids", [])
                if evidence_id not in cfg["pending_rule_evidence_ids"]:
                    cfg["pending_rule_evidence_ids"].append(evidence_id)
        cfg["review_evidence_ids"] = sorted(used)
        if any(before[k] != cfg.get(k) for k in before):
            cfg.setdefault("state_history", []).append({"at": now(), "before": before, "basis_evidence_ids": sorted(used), "method": "cross_source_grounded_rule_recheck"})


def evidence_signature(row):
    p = row["payload"]
    proof = p.get("proof") or {}
    return p.get("kind"), _norm(proof.get("quote", "")), str(p.get("numeric")), p.get("metric")


def resolution_is_grounded(resolution, gap, accepted_evidence, prior_signatures, records):
    if resolution.field != gap["field"]:
        return False
    answers = [accepted_evidence.get(id) for id in resolution.evidence_ids]
    if not all(answers) or not any(evidence_signature(row) not in prior_signatures for row in answers):
        return False
    quotes = " ".join((r["payload"].get("proof") or {}).get("quote", "") for r in answers)
    if not _contains(resolution.answer, quotes):
        return False
    if not any((r["payload"].get("proof") or {}).get("scope") == resolution.scope for r in answers):
        return False
    # Conservative lexical checks. Unsupported field classes remain open for review.
    field = gap["field"].casefold()
    patterns = {
        "gpu": r"\b(?:RTX|GTX|RX|Arc|Radeon|GeForce|NVIDIA|Intel)\b",
        "resolution": r"\d{3,5}\s*[x×]\s*\d{3,5}|\b[248]k\b|\d{3,4}p\b",
        "compatibility": r"support|compatible|不支持|支持|兼容",
        "game_build": r"build|version|版本|构建",
    }
    pattern = patterns.get(field)
    if pattern is None or not re.search(pattern, quotes, re.I):
        return False
    if gap["record_ids"]:
        targets = {r["id"]: r for r in records}
        answer_ids = {r["id"] for r in answers}
        if any(not answer_ids.intersection(targets.get(id, {}).get("review_evidence_ids", [])) for id in gap["record_ids"]):
            return False
    return True
