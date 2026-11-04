"""T15: demo backups restore business history into a new epoch only."""

import base64
import json

from demo.app.contracts import DemoError, Scope
from demo.tests.support import DemoCase


class BackupTests(DemoCase):
    def test_restored_pending_feedback_can_be_resubmitted_without_its_old_task(self):
        applied_id = self.h.adopt()
        summary = self.h.entity(self.h.summary(applied_id=applied_id))
        payload = {"session_id": summary["session_id"], "summary_id": summary["id"],
                   "applied_id": applied_id, "raw_text": "爆炸时画面卡顿"}
        pending = self.h.command("feedback.review", payload)
        old_analysis_id = self.h.ref(pending, "FeedbackAnalysis")
        backup = self.h.command("backup.create", {})
        self.h.command("backup.restore", {"file_id": self.h.ref(backup, "File")})
        self.assertEqual(self.h.entity(old_analysis_id)["feedback_snapshot"]["raw_text"], payload["raw_text"])
        current = self.h.command("feedback.review", payload)
        self.assertNotEqual(current.body["task_id"], pending.body["task_id"])
        self.assertEqual(self.h.entity(old_analysis_id)["status"], "superseded")
        task = self.h.run(current.body["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"]["action"], "adjust")

    def test_backup_round_trip_restores_history_and_files(self):
        story = self.h.story()
        backup = self.h.command("backup.create", {})
        backup_file = self.h.ref(backup, "File")
        old_epoch = self.h.workspace()["workspace_epoch"]
        restore = self.h.command("backup.restore", {"file_id": backup_file})
        self.assertEqual(restore.body["workspace_epoch"], old_epoch + 1)
        scope = Scope("mw2-rtx4060", restore.body["workspace_epoch"])
        store = self.h.service.store
        self.assertEqual(len(store.find(scope, "Summary")), 2)
        self.assertEqual(len(store.find(scope, "AppliedConfiguration")), 3)
        self.assertEqual(store.find(scope, "Comparison")[0]["id"], story["comparison_id"])
        self.assertEqual(store.tasks(scope), [])
        summary = store.find(scope, "Summary")[0]
        artifact = self.h.service.entity(scope, summary["artifact_id"])
        data = self.h.service.files.read(artifact["file_id"])
        self.assertTrue(data.startswith(b"demo_only"))
        self.assertEqual(self.h.workspace()["current_applied_id"], story["a2_id"])

    def test_archived_case_backup_is_rejected_without_restoring(self):
        self.h.story()
        backup = self.h.command("backup.create", {})
        document = json.loads(self.h.service.files.read(self.h.ref(backup, "File")))
        document["scenario_id"] = "fh6-rtx5090"
        foreign = self.h.upload(json.dumps(document).encode("utf-8"), "application/json", filename="foreign-backup.json")
        before = self.h.workspace()
        with self.assertRaises(DemoError) as caught:
            self.h.command("backup.restore", {"file_id": foreign})
        self.assertEqual(caught.exception.code, "cross_case_backup")
        self.assertEqual(self.h.workspace(), before)

    def test_tampered_backup_content_is_rejected_before_restore(self):
        self.h.story()
        backup = self.h.command("backup.create", {})
        document = json.loads(self.h.service.files.read(self.h.ref(backup, "File")))
        document["files"][0]["content_b64"] = base64.b64encode(b"tampered").decode("ascii")
        bad = self.h.upload(
            json.dumps(document).encode("utf-8"), "application/json", filename="bad.json"
        )
        with self.assertRaises(DemoError) as caught:
            self.h.command("backup.restore", {"file_id": bad})
        self.assertEqual(caught.exception.code, "invalid_backup")

    def test_unknown_backup_schema_is_rejected(self):
        bad = self.h.upload(
            json.dumps({"schema": "other/1", "demo_only": True}).encode("utf-8"),
            "application/json",
            filename="bad-schema.json",
        )
        with self.assertRaises(DemoError) as caught:
            self.h.command("backup.restore", {"file_id": bad})
        self.assertEqual(caught.exception.code, "invalid_backup")
