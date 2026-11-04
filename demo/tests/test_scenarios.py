"""T01: the two demo packs load, validate, and stay isolated."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from demo.app.scenarios import ScenarioCatalog, ScenarioError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MEMBERS = (
    "context.json",
    "sources.json",
    "settings.json",
    "templates.json",
    "rules.json",
    "trace-recipes.json",
)


def copy_fixtures() -> tuple[tempfile.TemporaryDirectory, Path]:
    directory = tempfile.TemporaryDirectory()
    target = Path(directory.name) / "fixtures"
    shutil.copytree(FIXTURES, target)
    return directory, target


def rewrite_manifest_hash(root: Path, scenario_id: str, member: str) -> None:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["scenarios"]:
        if entry["scenario_id"] == scenario_id:
            import hashlib

            path = root / entry["files"][member]
            entry["hashes"][member] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class ScenarioTests(unittest.TestCase):
    def test_two_cases_have_distinct_settings_and_unknown_cpu(self):
        catalog = ScenarioCatalog.load(FIXTURES)
        a = catalog.get("mw2-rtx4060")
        b = catalog.get("fh6-rtx5090")
        self.assertIn("4060", a["context"]["gpu_name"])
        self.assertIn("5090", b["context"]["gpu_name"])
        self.assertIsNone(a["context"]["cpu"])
        self.assertIsNone(b["context"]["cpu"])
        self.assertGreaterEqual(len(a["settings"]), 12)
        self.assertTrue(
            set(x["id"] for x in a["settings"]).isdisjoint(x["id"] for x in b["settings"])
        )

    def test_each_case_has_its_own_simulated_sources_and_evidence(self):
        catalog = ScenarioCatalog.load(FIXTURES)
        for scenario_id in ("mw2-rtx4060", "fh6-rtx5090"):
            scenario = catalog.get(scenario_id)
            source_ids = {source["id"] for source in scenario["sources"]}
            self.assertTrue(all(sid.startswith(f"demo://{scenario_id}/") for sid in source_ids))
            for setting in scenario["settings"]:
                for evidence in setting["evidence_source_ids"]:
                    self.assertIn(evidence, source_ids)
            reposts = [s for s in scenario["sources"] if s["relationship"] == "repost"]
            self.assertEqual(len(reposts), 0 if scenario_id == 'mw2-rtx4060' else 1)

    def test_manifest_lists_exactly_two_cases(self):
        catalog = ScenarioCatalog.load(FIXTURES)
        self.assertEqual(
            [item["scenario_id"] for item in catalog.list()],
            ["mw2-rtx4060"],
        )

    def test_missing_member_is_rejected(self):
        directory, root = copy_fixtures()
        self.addCleanup(directory.cleanup)
        (root / "mw2-rtx4060" / "settings.json").unlink()
        with self.assertRaises(ScenarioError):
            ScenarioCatalog.load(root)

    def test_hash_mismatch_is_rejected(self):
        directory, root = copy_fixtures()
        self.addCleanup(directory.cleanup)
        path = root / "fh6-rtx5090" / "sources.json"
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaises(ScenarioError):
            ScenarioCatalog.load(root)

    def test_cross_case_setting_reference_is_rejected(self):
        directory, root = copy_fixtures()
        self.addCleanup(directory.cleanup)
        path = root / "mw2-rtx4060" / "settings.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["settings"][0]["evidence_source_ids"] = [
            "demo://fh6-rtx5090/src/settings-guide"
        ]
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        rewrite_manifest_hash(root, "mw2-rtx4060", "settings.json")
        with self.assertRaises(ScenarioError):
            ScenarioCatalog.load(root)

    def test_fixture_member_escaping_the_case_directory_is_rejected(self):
        directory, root = copy_fixtures()
        self.addCleanup(directory.cleanup)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["scenarios"][0]["files"]["context.json"] = "../failure-scripts.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with self.assertRaises(ScenarioError):
            ScenarioCatalog.load(root)


if __name__ == "__main__":
    unittest.main()
