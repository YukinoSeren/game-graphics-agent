"""Compare a real collector run to the frozen selected Dot Esports sample.

The mapping is explicit bilingual bookkeeping, not a model answer or an unseen test.
"""

import argparse
import hashlib
import json
from pathlib import Path


MAPPING = {
    "预设": "Preset", "车辆细节": "Car Level of Detail",
    "环境纹理": "Environment Texture Quality", "环境几何": "Environment Geometry Quality",
    "车身反射": "Car Reflection Quality", "屏幕空间反射": "Screen Space Reflections Quality",
    "光追反射": "Raytraced Reflections Quality", "阴影": "Shadow Quality", "夜间阴影": "Night Shadows",
    "屏幕空间全局光照": "Screen Space GI Quality", "光追全局光照": "Raytraced GI Quality",
    "着色器": "Shader Quality", "音频": "Audio Quality", "可变形地形": "Deformable Terrain Quality",
    "粒子效果": "Particle Effects Quality", "体积雾": "Volumetric Fog Quality", "镜头效果": "Lens Effects",
    "运动模糊质量": "Motion Blur Quality", "垂直同步": "Vertical Synchronization", "全屏": "Full Screen",
    "运动模糊": "Motion Blur", "DLAA": "NVIDIA DLAA", "DLSS": "NVIDIA DLSS", "Reflex": "NVIDIA Reflex Low Latency",
}


def compare(run_path, gold_path):
    records = json.loads((run_path / "records.json").read_text("utf-8"))
    gold = json.loads(gold_path.read_text("utf-8"))["dot"]
    cfg = next(r for r in records if r["record_type"] == "configurations" and r["record_status"] == "accepted" and not r.get("superseded_by"))
    settings = {r["id"]: r["payload"]["raw_name"] for r in records if r["record_type"] == "settings"}
    prefix = cfg["reference_scope"] + ":"
    actual = {settings[prefix + a["setting_id"]]: a["value"] for a in cfg["payload"]["assignments"]}
    comparisons = []
    for item in gold["graphics_settings"] + gold["video_selected_fixed"]:
        name = MAPPING[item["setting"]]
        value = actual.get(name)
        comparisons.append({"sample_setting": item["setting"], "raw_name": name, "expected": item["value"], "actual": value, "match": bool(value and value["value_kind"] == "fixed" and value["value"] == item["value"])})
    hdr = [r for r in records if r["record_type"] == "evidence" and r["payload"].get("detected_state") == "not_detected"]
    fps = [r for r in records if r["record_type"] == "evidence" and r["payload"].get("numeric") and r["payload"]["numeric"].get("lower") == 70 and r["payload"]["numeric"].get("upper") == 85]
    semantics = {
        "hdr_observation_retained": bool(hdr) and "HDR" not in actual,
        "resolution_not_universal_fixed": actual.get("Resolution", {}).get("value_kind") == "conditional",
        "fps_interval_not_promoted_to_average": bool(fps) and all(r["payload"]["metric"] == "fps_unspecified" and r["payload"]["numeric"]["value"] is None for r in fps),
    }
    return {"run_path": str(run_path), "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(), "independent_blind_evaluation": False, "scope": "24 selected fixed values and 3 semantic preservation checks from a previously seen article; not full source coverage", "selected_values": comparisons, "matched": sum(c["match"] for c in comparisons), "total": len(comparisons), "semantic_checks": semantics, "fps_record_statuses": [r["record_status"] for r in fps], "recommendation_status": cfg["recommendation_status"], "note": "Semantic preservation can be correct while a measurement is quarantined due to its invalid run-configuration reference. This does not prove source claims or applicability."}


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.run, root / "fh6-new-source-extraction.json")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    lines = ["# Dot Esports 原型提取与冻结样本对照", "", f"选定固定项：{result['matched']}/{result['total']} 一致。", "", "| 样本项 | 原文名称 | 期望值 | 一致 |", "|---|---|---|---|"]
    lines.extend(f"| {c['sample_setting']} | {c['raw_name']} | {c['expected']} | {c['match']} |" for c in result["selected_values"])
    lines.extend(["", "语义检查：" + json.dumps(result["semantic_checks"], ensure_ascii=False), "", "FPS 区间记录状态：" + ", ".join(result["fps_record_statuses"]), "", "该区间保留正确，但本次运行引用了结构不合法的测试配置，所以测量仍隔离。", "配置模板保持 needs_followup；不能据固定项一致宣称整份推荐适用。", "", "这是已见样本的回归对照，不是独立盲测、四篇全量验收或作者主张真实性证明。"])
    (args.output / "comparison.md").write_text("\n".join(lines) + "\n", "utf-8")
    print(json.dumps({"matched": result["matched"], "total": result["total"], "semantic_checks": result["semantic_checks"]}))
    return 0 if result["matched"] == result["total"] and all(result["semantic_checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
