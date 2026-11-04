"""T07: demo knowledge package import is validated and reusable."""

import json

from demo.app.contracts import DemoError
from demo.tests.support import DemoCase


def package(*, scenario="mw2-rtx4060", source_id=None, record_source=None):
    source_id = source_id or f"demo://{scenario}/src/imported"
    return {
        "schema": "demo-knowledge/1",
        "demo_only": True,
        "scenario_id": scenario,
        "content_version": "demo-kb-import-1",
        "sources": [
            {
                "id": source_id,
                "title": "导入的演示来源",
                "author_label": "演示导入来源（模拟）",
                "published": "2025-09-01",
                "revision": "r1",
                "relationship": "original",
                "setting_ids": [f"{'mw2' if scenario.startswith('mw2') else 'fh6'}.texture_detail"],
                "content_blocks": [{"type": "paragraph", "text": "导入演示内容。"}],
                "limitations": "演示导入样例",
            }
        ],
        "records": [
            {
                "source_id": record_source or source_id,
                "setting_id": f"{'mw2' if scenario.startswith('mw2') else 'fh6'}.texture_detail",
                "value": "high",
                "status": "accepted",
            }
        ],
        "gaps": [{"code": "import-gap", "label": "导入示例缺口"}],
    }


class KnowledgeImportTests(DemoCase):
    def _upload(self, document, *, media_type="application/json", filename="package.json"):
        data = document if isinstance(document, bytes) else json.dumps(document).encode("utf-8")
        return self.h.upload(data, media_type, filename=filename)

    def test_package_import_publishes_one_snapshot(self):
        file_id = self._upload(package())
        reply = self.h.command("knowledge.import", {"file_id": file_id})
        snapshot = self.h.entity(self.h.ref(reply, "KnowledgeSnapshot"))
        self.assertEqual(snapshot["data_origin"], "uploaded_demo_file")
        self.assertEqual(len(snapshot["source_ids"]), 1)
        self.assertEqual(snapshot["accepted_count"], 1)
        self.assertEqual(snapshot["status"], "completed")

    def test_importing_the_same_package_twice_reuses_the_snapshot(self):
        first_file = self._upload(package())
        first = self.h.command("knowledge.import", {"file_id": first_file})
        first_id = self.h.ref(first, "KnowledgeSnapshot")
        second_file = self._upload(package())
        second = self.h.command("knowledge.import", {"file_id": second_file})
        self.assertEqual(self.h.ref(second, "KnowledgeSnapshot"), first_id)
        workspace = self.h.workspace()
        from demo.app.contracts import Scope

        scope = Scope("mw2-rtx4060", workspace["workspace_epoch"])
        self.assertEqual(len(self.h.service.store.find(scope, "KnowledgeSnapshot")), 1)

    def test_cross_case_package_is_rejected(self):
        file_id = self._upload(package(scenario="fh6-rtx5090"))
        with self.assertRaises(DemoError) as caught:
            self.h.command("knowledge.import", {"file_id": file_id})
        self.assertEqual(caught.exception.code, "cross_case_package")
        workspace = self.h.workspace()
        from demo.app.contracts import Scope

        scope = Scope("mw2-rtx4060", workspace["workspace_epoch"])
        self.assertEqual(self.h.service.store.find(scope, "KnowledgeSnapshot"), [])

    def test_malformed_package_is_rejected(self):
        file_id = self._upload(b"{not json")
        with self.assertRaises(DemoError) as caught:
            self.h.command("knowledge.import", {"file_id": file_id})
        self.assertEqual(caught.exception.code, "invalid_package")

    def test_record_must_reference_a_package_source(self):
        document = package(record_source="demo://mw2-rtx4060/src/other")
        file_id = self._upload(document)
        with self.assertRaises(DemoError) as caught:
            self.h.command("knowledge.import", {"file_id": file_id})
        self.assertEqual(caught.exception.code, "invalid_package")

    def test_unknown_setting_reference_is_rejected(self):
        document = package()
        document["sources"][0]["setting_ids"] = ["mw2.not_a_setting"]
        file_id = self._upload(document)
        with self.assertRaises(DemoError) as caught:
            self.h.command("knowledge.import", {"file_id": file_id})
        self.assertEqual(caught.exception.code, "invalid_package")
