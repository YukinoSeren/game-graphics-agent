"""Regression tests for the findings recorded in demo/CODE_REVIEW.md.

Each test targets one reported defect. These are demo-only checks; they do
not change the formal project's requirements or acceptance criteria.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from demo.app.contracts import Command, DemoError, Scope
from demo.app.metrics import summarize_intervals
from demo.app.performance import _chart
from demo.app.traces import clean_trace, parse_trace
from demo.tests.support import DemoCase, FIXTURES
from demo.app.scenarios import ScenarioCatalog

CSV_HEADER = "demo_only,scenario_id,trace_id,frame_index,timestamp_ms,interval_ms,frame_stream\n"


def _frame_trace(rows):
    lines = [CSV_HEADER.rstrip("\n")]
    lines.extend(
        f"true,mw2-rtx4060,demo_trace_upload,{index},{timestamp},{interval},application_present"
        for index, timestamp, interval in rows
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


class ContextCorrectionTests(DemoCase):
    def test_amend_after_a_newer_background_is_rejected(self):
        scan = self.h.command("device.scan", {})
        request_id = self.h.ref(scan, "InformationRequest")
        request = self.h.entity(request_id)
        old_context = self.h.entity(self.h.workspace()["context_id"])
        self.h.command(
            "information.answer",
            {
                "request_id": request_id,
                "question_revision": request["revision"],
                "context_revision": old_context["revision"],
                "answers": {
                    "cpu": "旧 CPU",
                    "memory_gb": 16,
                    "vram_gb": 8,
                    "driver_version": "旧驱动",
                    "game_version": "旧版本",
                    "display_refresh_hz": 60,
                },
            },
        )
        current = self.h.entity(self.h.workspace()["context_id"])
        self.h.command(
            "context.save",
            {"expected_context_id": current["id"], "fields": {"cpu": "新 CPU"}},
        )
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "information.amend",
                {
                    "request_id": request_id,
                    "question_revision": self.h.entity(request_id)["revision"],
                    "context_revision": current["revision"],
                    "answers": {"cpu": "STALE"},
                },
            )
        self.assertEqual(caught.exception.code, "context_conflict")
        self.assertEqual(
            self.h.entity(self.h.workspace()["context_id"])["cpu"], "新 CPU"
        )


class BindingRevisionTests(DemoCase):
    def test_two_revisions_cannot_both_use_the_same_expected_revision(self):
        applied = self.h.adopt()
        session = self.h.session(applied_id=applied)
        payload = {
            "segment_id": session["segment_id"],
            "expected_binding_revision": session["binding_revision"],
            "applied_id": None,
        }
        first = self.h.command("binding.revise", payload)
        revised = self.h.entity(self.h.ref(first, "Binding"))
        self.assertEqual(revised["revision"], session["binding_revision"] + 1)
        segment = self.h.entity(session["segment_id"])
        self.assertEqual(segment["revision"], session["segment_revision"] + 1)
        with self.assertRaises(DemoError) as caught:
            self.h.command("binding.revise", dict(payload))
        self.assertEqual(caught.exception.code, "binding_conflict")


class ConfigurationRelationTests(DemoCase):
    def _change_set(self):
        applied = self.h.adopt()
        summary = self.h.entity(self.h.summary("recommended", applied_id=applied))
        saved = self.h.command(
            "feedback.save",
            {
                "session_id": summary["session_id"],
                "summary_id": summary["id"],
                "satisfied": False,
                "symptoms": ["stutter"],
                "raw_text": "卡顿",
            },
        )
        reply = self.h.command(
            "feedback.next",
            {
                "feedback_id": self.h.ref(saved, "Feedback"),
                "applied_id": applied,
                "summary_id": summary["id"],
            },
        )
        result = self.h.run(reply.body["task_id"])["result"]
        change_set = self.h.entity(result["change_set_id"])
        to_value = next(iter(change_set["changes"].values()))["to"]
        return applied, change_set, to_value

    def test_edit_rejects_unknown_change_set(self):
        applied, change_set, _ = self._change_set()
        changes = {
            setting_id: change["to"]
            for setting_id, change in change_set["changes"].items()
        }
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "configuration.edit",
                {
                    "expected_current_id": applied,
                    "changes": changes,
                    "change_set_id": "demo_changeset_missing",
                    "confirmation": "我已手动调整",
                },
            )
        self.assertEqual(caught.exception.code, "not_found")

    def test_edit_rejects_a_change_set_from_an_old_workspace_epoch(self):
        _, old_change_set, _ = self._change_set()
        self.h.command("demo.reset", {"scope": "current"})
        applied, change_set, _ = self._change_set()
        changes = {setting_id: change["to"] for setting_id, change in change_set["changes"].items()}
        with self.assertRaises(DemoError) as caught:
            self.h.command("configuration.edit", {
                "expected_current_id": applied, "changes": changes,
                "change_set_id": old_change_set["id"], "confirmation": "manual",
            })
        self.assertEqual(caught.exception.code, "not_found")
        self.assertEqual(self.h.workspace()["current_applied_id"], applied)

    def test_edit_rejects_values_that_do_not_match_the_change_set(self):
        applied, change_set, _ = self._change_set()
        setting_id = next(iter(change_set["changes"]))
        target = change_set["changes"][setting_id]["to"]
        allowed = [
            setting["allowed_values"]
            for setting in self.h.service.catalog.get("mw2-rtx4060")["settings"]
            if setting["id"] == setting_id
        ][0]
        different = next(value for value in allowed if value != target)
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "configuration.edit",
                {
                    "expected_current_id": applied,
                    "changes": {setting_id: different},
                    "change_set_id": change_set["id"],
                    "confirmation": "我已手动调整",
                },
            )
        self.assertEqual(caught.exception.code, "change_set_mismatch")

    def test_adopting_a_recommendation_for_another_background_is_rejected(self):
        recommendation = self.h.recommendation()
        context = self.h.entity(self.h.workspace()["context_id"])
        self.h.command(
            "context.save",
            {"expected_context_id": context["id"], "fields": {"gpu_name": "别的显卡"}},
        )
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "configuration.adopt",
                {
                    "origin": "recommendation",
                    "recommendation_id": recommendation,
                    "expected_current_id": None,
                    "confirmation": "我已手动设置",
                },
            )
        self.assertEqual(caught.exception.code, "context_mismatch")


class FeedbackRetirementTests(DemoCase):
    def test_keep_retires_the_old_proposal_and_blocks_adoption(self):
        applied = self.h.adopt()
        summary = self.h.entity(self.h.summary("recommended", applied_id=applied))
        saved = self.h.command(
            "feedback.save",
            {
                "session_id": summary["session_id"],
                "summary_id": summary["id"],
                "satisfied": False,
                "symptoms": ["stutter"],
                "raw_text": "卡顿",
            },
        )
        first = self.h.command(
            "feedback.next",
            {
                "feedback_id": self.h.ref(saved, "Feedback"),
                "applied_id": applied,
                "summary_id": summary["id"],
            },
        )
        result = self.h.run(first.body["task_id"])["result"]
        change_set = self.h.entity(result["change_set_id"])
        changes = {
            setting_id: change["to"]
            for setting_id, change in change_set["changes"].items()
        }
        satisfied = self.h.command(
            "feedback.save",
            {
                "session_id": summary["session_id"],
                "summary_id": summary["id"],
                "satisfied": True,
                "raw_text": "交战时偶尔卡一下，希望保持画面清晰",
            },
        )
        keep = self.h.command(
            "feedback.next",
            {
                "feedback_id": self.h.ref(satisfied, "Feedback"),
                "applied_id": applied,
                "summary_id": summary["id"],
            },
        )
        result = self.h.run(keep.body["task_id"])["result"]
        self.assertEqual(result["action"], "keep")
        self.assertEqual(self.h.entity(change_set["id"])["status"], "superseded")
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "configuration.edit",
                {
                    "expected_current_id": applied,
                    "changes": changes,
                    "change_set_id": change_set["id"],
                    "confirmation": "我已手动调整",
                },
            )
        self.assertEqual(caught.exception.code, "change_set_not_current")


class ComparisonPlanningTests(DemoCase):
    def _capture(self, *, run_conditions=None, applied_id=None):
        session = self.h.session(
            applied_id=applied_id, run_conditions=run_conditions or {}
        )
        reply = self.h.command(
            "capture.start",
            {
                "session_id": session["session_id"],
                "segment_id": session["segment_id"],
                "expected_segment_revision": session["segment_revision"],
                "expected_binding_revision": session["binding_revision"],
                "label": "recommended",
            },
        )
        task = self.h.run(reply.body["task_id"])
        segment = self.h.entity(session["segment_id"])
        binding = self.h.entity(segment["binding_id"])
        return task["result"]["artifact_id"], segment, binding

    def _analyze(self, artifact_id, segment, binding):
        reply = self.h.command(
            "performance.analyze",
            {
                "artifact_id": artifact_id,
                "segment_id": segment["id"],
                "segment_revision": segment["revision"],
                "binding_revision": binding["revision"],
            },
        )
        return self.h.ref(reply, "Summary")

    def test_protocol_frozen_after_the_second_artifact_is_exploratory(self):
        applied = self.h.adopt()
        first = self.h.summary("recommended", applied_id=applied)
        artifact, segment, binding = self._capture(applied_id=applied)
        protocol = self.h.ref(
            self.h.command(
                "comparison.freeze",
                {"mode": "fixed", "threshold_ms": 50.0, "baseline_summary_ids": [first]},
            ),
            "ComparisonProtocol",
        )
        second = self._analyze(artifact, segment, binding)
        comparison = self.h.entity(
            self.h.ref(
                self.h.command(
                    "comparison.run",
                    {
                        "protocol_id": protocol,
                        "before_summary_id": first,
                        "after_summary_id": second,
                    },
                ),
                "Comparison",
            )
        )
        self.assertTrue(comparison["exploratory"])
        self.assertFalse(comparison["comparable"])

    def test_undeclared_run_conditions_are_not_comparable(self):
        first_artifact, first_segment, first_binding = self._capture()
        first = self._analyze(first_artifact, first_segment, first_binding)
        protocol = self.h.ref(
            self.h.command(
                "comparison.freeze",
                {"mode": "fixed", "threshold_ms": 50.0, "baseline_summary_ids": [first]},
            ),
            "ComparisonProtocol",
        )
        second_artifact, second_segment, second_binding = self._capture()
        second = self._analyze(second_artifact, second_segment, second_binding)
        comparison = self.h.entity(
            self.h.ref(
                self.h.command(
                    "comparison.run",
                    {
                        "protocol_id": protocol,
                        "before_summary_id": first,
                        "after_summary_id": second,
                    },
                ),
                "Comparison",
            )
        )
        self.assertFalse(comparison["comparable"])
        self.assertTrue(
            any("没有声明运行条件" in note for note in comparison["comparability_notes"])
        )

    def test_different_run_conditions_are_not_comparable(self):
        applied = self.h.adopt()
        first = self.h.summary("recommended", applied_id=applied)
        protocol = self.h.ref(
            self.h.command(
                "comparison.freeze",
                {"mode": "fixed", "threshold_ms": 50.0, "baseline_summary_ids": [first]},
            ),
            "ComparisonProtocol",
        )
        session = self.h.session(
            applied_id=applied,
            run_conditions={"resolution": "3840x2160", "scene": "菜单"},
        )
        reply = self.h.command(
            "capture.start",
            {
                "session_id": session["session_id"],
                "segment_id": session["segment_id"],
                "expected_segment_revision": session["segment_revision"],
                "expected_binding_revision": session["binding_revision"],
                "label": "adjusted",
            },
        )
        task = self.h.run(reply.body["task_id"])
        segment = self.h.entity(session["segment_id"])
        binding = self.h.entity(segment["binding_id"])
        second = self._analyze(task["result"]["artifact_id"], segment, binding)
        first_entity = self.h.entity(first)
        self.h.service.store.mutate(
            Command(
                "set-conditions",
                "mw2-rtx4060",
                workspace_epoch=self.h.workspace()["workspace_epoch"],
                expected_revision=self.h.workspace()["revision"],
                action="test.set_conditions",
                payload={},
            ),
            lambda tx: _set_conditions(tx, first_entity["session_id"]),
        )
        comparison = self.h.entity(
            self.h.ref(
                self.h.command(
                    "comparison.run",
                    {
                        "protocol_id": protocol,
                        "before_summary_id": first,
                        "after_summary_id": second,
                    },
                ),
                "Comparison",
            )
        )
        self.assertFalse(comparison["comparable"])
        self.assertTrue(
            any("运行条件" in note for note in comparison["comparability_notes"])
        )


def _set_conditions(tx, session_id):
    from demo.app.contracts import DomainResult

    session = tx.get(session_id, kind="Session")
    tx.update_entity(
        session["id"],
        {"run_conditions": {"resolution": "1920x1080", "scene": "交战"}},
        revision=session["revision"] + 1,
    )
    return DomainResult(200)


class OutcomeRelationTests(DemoCase):
    def test_outcome_must_reference_the_applied_after_version(self):
        story = self.h.story()
        a0 = self.h.entity(story["a1_id"])["parent_id"]
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "outcome.save",
                {
                    "change_set_id": story["change_set_id"],
                    "applied_id": a0,
                    "comparison_id": story["comparison_id"],
                    "accepted": True,
                },
            )
        self.assertEqual(caught.exception.code, "outcome_relation_mismatch")

    def test_outcome_must_reference_the_change_set_base(self):
        story = self.h.story()
        a1 = self.h.entity(story["a1_id"])
        session = self.h.session(applied_id=a1["parent_id"])
        reply = self.h.command(
            "capture.start",
            {
                "session_id": session["session_id"],
                "segment_id": session["segment_id"],
                "expected_segment_revision": session["segment_revision"],
                "expected_binding_revision": session["binding_revision"],
                "label": "no_gain",
            },
        )
        task = self.h.run(reply.body["task_id"])
        segment = self.h.entity(session["segment_id"])
        binding = self.h.entity(segment["binding_id"])
        other = self.h.ref(
            self.h.command(
                "performance.analyze",
                {
                    "artifact_id": task["result"]["artifact_id"],
                    "segment_id": segment["id"],
                    "segment_revision": segment["revision"],
                    "binding_revision": binding["revision"],
                },
            ),
            "Summary",
        )
        protocol = self.h.ref(
            self.h.command(
                "comparison.freeze",
                {
                    "mode": "fixed",
                    "threshold_ms": 50.0,
                    "baseline_summary_ids": [story["s1_id"]],
                },
            ),
            "ComparisonProtocol",
        )
        comparison = self.h.ref(
            self.h.command(
                "comparison.run",
                {
                    "protocol_id": protocol,
                    "before_summary_id": story["s2_id"],
                    "after_summary_id": other,
                },
            ),
            "Comparison",
        )
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "outcome.save",
                {
                    "change_set_id": story["change_set_id"],
                    "applied_id": story["a2_id"],
                    "comparison_id": comparison,
                    "accepted": True,
                },
            )
        self.assertEqual(caught.exception.code, "outcome_relation_mismatch")


class HistoricalLogTests(DemoCase):
    def test_historical_log_can_be_analyzed_through_its_own_segment(self):
        session = self.h.session()
        data = ScenarioCatalog.load(FIXTURES).recipe("mw2-rtx4060", "short")
        from demo.app.traces import generate_trace

        file_id = self.h.upload(
            generate_trace("mw2-rtx4060", "short", data), filename="old.csv"
        )
        reply = self.h.command(
            "log.associate",
            {
                "file_id": file_id,
                "historical": True,
                "session_id": session["session_id"],
                "declared_start": "2026-10-30T20:00:00+08:00",
                "basis": "玩家说明这是过去的记录",
            },
        )
        artifact = self.h.entity(self.h.ref(reply, "LogArtifact"))
        self.assertIsNotNone(artifact["segment_id"])
        segment = self.h.entity(artifact["segment_id"])
        binding = self.h.entity(segment["binding_id"])
        self.assertEqual(segment["status"], "closed")
        analyzed = self.h.command(
            "performance.analyze",
            {
                "artifact_id": artifact["id"],
                "segment_id": segment["id"],
                "segment_revision": segment["revision"],
                "binding_revision": binding["revision"],
            },
        )
        summary = self.h.entity(self.h.ref(analyzed, "Summary"))
        self.assertEqual(summary["source_hash"], artifact["content_hash"])


class StatisticsTests(DemoCase):
    def test_timebase_break_drops_the_unverifiable_interval(self):
        parsed = parse_trace(
            _frame_trace(
                [
                    (0, "0.000", ""),
                    (1, "16.000", "16.000"),
                    (2, "1000.000", "16.000"),
                    (3, "1016.000", "16.000"),
                ]
            ),
            scenario_id="mw2-rtx4060",
        )
        clean = clean_trace(parsed)
        self.assertIn(
            "timebase_break", [item["problem"] for item in clean.issues]
        )
        self.assertEqual(clean.runs, [[16.0], [16.0]])

    def test_tail_requires_twenty_samples(self):
        short = summarize_intervals([16.0] * 100)
        self.assertIsNone(short["slowest_1pct_fps"])
        long = summarize_intervals([16.0] * 2000)
        self.assertIsNotNone(long["slowest_1pct_fps"])

    def test_chart_buckets_keep_the_full_time_range_and_peaks(self):
        from types import SimpleNamespace

        first = [80.0 if index % 3 == 0 else 16.0 for index in range(2400)]
        second = [16.0 for _ in range(600)]
        intervals = first + second
        clean = SimpleNamespace(
            runs=[first, second], content_hash="demo", intervals=intervals
        )
        chart = _chart(clean, 50.0)
        total_s = sum(intervals) / 1000.0
        self.assertTrue(chart["downsampled"])
        self.assertGreaterEqual(chart["points"][-1]["t_s"], total_s - 0.08)
        self.assertEqual(
            max(point["ms"] for point in chart["points"]),
            max(intervals),
        )
        self.assertLessEqual(chart["point_count"], 2 * 240 + 2)
        breaks = [point["t_s"] for point in chart["points"] if point.get("break")]
        self.assertEqual(breaks, [round((sum(first) + second[0]) / 1000.0, 3)])


class KnowledgeCoverageTests(DemoCase):
    def test_update_cannot_upgrade_an_empty_collection(self):
        reply = self.h.command(
            "knowledge.collect", {"budget": {"max_stages": 1}}
        )
        task = self.h.run(reply.body["task_id"])
        partial_id = task["result"]["knowledge_id"]
        self.assertEqual(self.h.entity(partial_id)["accepted_count"], 0)
        updated = self.h.command("knowledge.update", {"knowledge_id": partial_id})
        new_id = self.h.ref(updated, "KnowledgeSnapshot")
        self.assertEqual(self.h.entity(new_id)["status"], "partial")
        recommendation = self.h.command(
            "recommendation.generate",
            {
                "context_id": self.h.workspace()["context_id"],
                "knowledge_id": new_id,
            },
        )
        entity = self.h.entity(self.h.ref(recommendation, "Recommendation"))
        self.assertEqual(entity["readiness"], "needs_review")

    def test_review_uses_the_newest_knowledge_revision(self):
        knowledge_id = self.h.knowledge()
        recommendation_id = self.h.recommendation(knowledge_id=knowledge_id)
        updated = self.h.command("knowledge.update", {"knowledge_id": knowledge_id})
        newest = self.h.ref(updated, "KnowledgeSnapshot")
        reviewed = self.h.entity(
            self.h.ref(
                self.h.command(
                    "recommendation.review", {"recommendation_id": recommendation_id}
                ),
                "Recommendation",
            )
        )
        self.assertEqual(reviewed["knowledge_id"], newest)
        self.assertEqual(self.h.entity(recommendation_id)["review_state"], "needs_review")

    def test_followup_does_not_rewrite_the_old_snapshot_gap(self):
        # The complete 15-source pack has no gaps. An imported package can
        # explicitly declare one and still exercises immutable follow-up history.
        from demo.tests.test_knowledge_import import package
        file_id = self.h.upload(json.dumps(package()).encode("utf-8"), "application/json", filename="with-gap.json")
        knowledge_id = self.h.ref(self.h.command("knowledge.import", {"file_id": file_id}), "KnowledgeSnapshot")
        original = self.h.entity(knowledge_id)
        gap_id = original["gap_ids"][-1]
        before = self.h.entity(gap_id)
        reply = self.h.command(
            "knowledge.followup", {"knowledge_id": knowledge_id, "gap_id": gap_id}
        )
        new_id = self.h.ref(reply, "KnowledgeSnapshot")
        new_snapshot = self.h.entity(new_id)
        new_gap_id = new_snapshot["gap_ids"][-1]
        self.assertNotEqual(new_gap_id, gap_id)
        self.assertEqual(self.h.entity(new_gap_id)["status"], "resolved")
        self.assertEqual(self.h.entity(gap_id), before)
        self.assertEqual(self.h.entity(knowledge_id)["gap_ids"], original["gap_ids"])


class BackupRobustnessTests(DemoCase):
    def test_corrupt_graph_is_rejected_and_the_workspace_is_kept(self):
        story = self.h.story()
        backup = self.h.ref(self.h.command("backup.create", {}), "File")
        document = json.loads(self.h.service.files.read(backup))
        document["entities"] = [
            entity for entity in document["entities"] if entity["id"] != story["a1_id"]
        ]
        bad = self.h.upload(
            json.dumps(document).encode("utf-8"),
            "application/json",
            filename="corrupt.json",
        )
        epoch = self.h.workspace()["workspace_epoch"]
        with self.assertRaises(DemoError) as caught:
            self.h.command("backup.restore", {"file_id": bad})
        self.assertEqual(caught.exception.code, "invalid_backup")
        self.assertEqual(self.h.workspace()["workspace_epoch"], epoch)
        self.assertEqual(self.h.workspace()["current_applied_id"], story["a2_id"])
        self.assertIsNotNone(self.h.entity(story["a1_id"]))

    def test_repeated_backups_do_not_embed_previous_backups(self):
        self.h.story()
        first = self.h.ref(self.h.command("backup.create", {}), "File")
        first_size = len(self.h.service.files.read(first))
        second = self.h.ref(self.h.command("backup.create", {}), "File")
        second_size = len(self.h.service.files.read(second))
        self.assertLess(second_size, first_size * 1.5)
        document = json.loads(self.h.service.files.read(second))
        self.assertFalse(
            any(
                item.get("filename_label") == "demo-backup.json"
                for item in document["entities"]
                if item["kind"] == "File"
            )
        )

    def test_restore_failure_removes_the_files_it_moved(self):
        self.h.story()
        backup = self.h.ref(self.h.command("backup.create", {}), "File")
        document = json.loads(self.h.service.files.read(backup))
        document["entities"][0]["data_origin"] = "not-a-demo-origin"
        bad = self.h.upload(
            json.dumps(document).encode("utf-8"),
            "application/json",
            filename="bad-origin.json",
        )
        uploads = Path(self.h.runtime_root) / "uploads"
        before = {path.name for path in uploads.iterdir() if path.is_file()}
        with self.assertRaises(DemoError):
            self.h.command("backup.restore", {"file_id": bad})
        after = {path.name for path in uploads.iterdir() if path.is_file()}
        self.assertEqual(after, before)

    def test_idempotent_upload_releases_the_staged_copy(self):
        data = b"demo_only,scenario_id\n"
        import hashlib
        import uuid

        workspace = self.h.workspace()
        command = Command(
            request_id=uuid.uuid4().hex,
            scenario_id="mw2-rtx4060",
            workspace_epoch=workspace["workspace_epoch"],
            expected_revision=workspace["revision"],
            action="file.upload",
            payload={
                "media_type": "text/csv",
                "filename": "same.csv",
                "size": len(data),
                "content_hash": hashlib.sha256(data).hexdigest(),
            },
        )
        self.h.service.upload(command, data=data, media_type="text/csv", filename="same.csv")
        self.h.service.upload(command, data=data, media_type="text/csv", filename="same.csv")
        staging = Path(self.h.runtime_root) / "uploads" / ".staging"
        self.assertEqual([path for path in staging.iterdir() if path.is_file()], [])


class ExportBackgroundTests(DemoCase):
    def test_report_keeps_the_background_of_the_chosen_result(self):
        story = self.h.story()
        context = self.h.entity(self.h.workspace()["context_id"])
        self.h.command(
            "context.save",
            {
                "expected_context_id": context["id"],
                "fields": {"gpu_name": "自定义显卡", "game_name": "自定义游戏"},
            },
        )
        reply = self.h.command(
            "export.create",
            {"format": "json", "comparison_id": story["comparison_id"]},
        )
        report = json.loads(
            self.h.service.files.read(self.h.ref(reply, "File"))
        )
        self.assertNotEqual(report["gpu_name"], "自定义显卡")
        self.assertIn("4060", report["gpu_name"])
        self.assertIn("使命召唤", report["game_name"])
        backgrounds = report["backgrounds"]
        self.assertTrue(backgrounds)
        self.assertTrue(all(item["gpu_name"] != "自定义显卡" for item in backgrounds))


class DataDirTests(DemoCase):
    def test_cli_refuses_a_runtime_directory_outside_the_demo_root(self):
        import contextlib
        import io

        from demo import server

        with tempfile.TemporaryDirectory() as directory:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = server.main(["--data-dir", directory])
            self.assertEqual(code, 2)
            self.assertEqual(list(Path(directory).iterdir()), [])
