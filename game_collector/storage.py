"""Append-only source revisions and record snapshots, plus reviewable exports."""

import json
from pathlib import Path
import sqlite3


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Store:
    def __init__(self, output):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.output / "knowledge.sqlite3")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, url TEXT NOT NULL, revision_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, game_id TEXT NOT NULL, source_id TEXT NOT NULL, record_type TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
        """)

    def save(self, run, sources, records, gaps, extractions):
        encode = lambda obj: json.dumps(obj, ensure_ascii=False)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?)", (run["id"], encode(run)))
            for source in sources:
                self.db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?)", (source["id"], source["url"], source["revision_id"], source["status"], encode(source)))
            for record in records:
                self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?)", (record["id"], run["game_id"], record["source_id"], record["record_type"], record["record_status"], encode(record)))
        candidates = [r for r in records if r["record_type"] == "configurations" and r["record_status"] == "accepted" and r["recommendation_status"] != "excluded_branch" and not r.get("superseded_by")]
        for name, data in [("run", run), ("sources", sources), ("records", records), ("candidates", candidates), ("gaps", gaps), ("extractions", extractions)]:
            write_json(self.output / f"{name}.json", data)
        accepted = sum(r["record_status"] == "accepted" for r in records)
        open_gaps = [g for g in gaps if g["status"] != "resolved"]
        lines = [f"# {run['input']['game_name']} 采集报告", "", f"- 运行状态：{run['status']}", f"- 停止原因：{run['stop_reason']}", f"- 入口：{run['input']['mode']}", f"- 来源修订数：{len(sources)}；记录接受 {accepted}，隔离 {len(records)-accepted}", f"- 未解决问题：{len(open_gaps)}", f"- 实际调用：{json.dumps(run['calls'], ensure_ascii=False)}", "", "## 范围与限制", "", "本报告的完成状态仅指本次声明范围，不能证明已经覆盖全部游戏设置。", "词面、类型及引用校验不等于独立语义核验；同卡参考也不是完整兼容性保证。", "图片只登记线索；没有执行图片理解、长视频处理、个性化推荐或修改游戏设置。", "未计量金额，只报告调用与供应商返回的 token 用量。", "", "## 来源", ""]
        for source in sources:
            lines.append(f"- {source['url']} — {source['status']}；修订 {source['revision_id']}；{source.get('error') or source.get('role', '')}")
        lines.extend(["", "## 未解决问题", ""])
        for gap in open_gaps:
            lines.append(f"- {gap['priority']} {gap['question']}（{gap.get('stop_reason') or gap['status']}）")
        lines.extend(["", "## 可供后续审阅的配置资料", ""])
        for row in candidates:
            lines.append(f"- {row['payload']['name']} — {row['recommendation_status']}；出处 {row['source_id']}；适用性 {row['applicability']}")
        (self.output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def close(self):
        self.db.close()
