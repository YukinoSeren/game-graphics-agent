"""T15: reports describe seeded and real journeys without leaking paths."""

import json

from demo.tests.support import DemoCase


class ExportTests(DemoCase):
    def test_markdown_report_is_marked_and_has_no_local_paths(self):
        story = self.h.story()
        reply = self.h.command("export.create", {"format": "markdown"})
        file_id = self.h.ref(reply, "File")
        text = self.h.service.files.read(file_id).decode("utf-8")
        self.assertIn("演示报告，非真实实测", text)
        self.assertIn("仅用于临时演示", text)
        self.assertIn("使命召唤：现代战争 I", text)
        self.assertIn(story["a1_id"], text)
        self.assertNotIn("E:\\", text)
        self.assertNotIn("uploads/", text)
        self.assertNotIn("runtime", text)

    def test_json_report_reuses_summary_and_comparison_values(self):
        story = self.h.story()
        reply = self.h.command("export.create", {"format": "json"})
        file_id = self.h.ref(reply, "File")
        report = json.loads(self.h.service.files.read(file_id))
        self.assertIs(report["demo_only"], True)
        summary = next(item for item in report["summaries"] if item["id"] == story["s1_id"])
        stored = self.h.entity(story["s1_id"])
        self.assertEqual(summary["metrics"], stored["metrics"])
        self.assertEqual(summary["source_hash"], stored["source_hash"])
        comparison = self.h.entity(story["comparison_id"])
        self.assertEqual(report["comparison"]["threshold_ms"], comparison["threshold_ms"])
        self.assertEqual(report["comparison"]["metrics"], comparison["metrics"])
        self.assertIn("不构成单项优化因果结论", " ".join(report["limitations"]))

    def test_export_quotes_source_nature(self):
        self.h.story()
        reply = self.h.command("export.create", {"format": "markdown"})
        text = self.h.service.files.read(self.h.ref(reply, "File")).decode("utf-8")
        # The current complete pack is locally authored; it has no repost source.
        pack = self.h.service.catalog.get("mw2-rtx4060")
        self.assertEqual(len(pack["sources"]), 15)
        self.assertTrue(all(source["relationship"] == "original" for source in pack["sources"]))
        self.assertIn("original", text)
        for source in pack["sources"]:
            self.assertIn(source["title"], text)
        self.assertIn("模拟", text)
