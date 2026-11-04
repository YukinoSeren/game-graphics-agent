"""Offline feedback publication deadlines and immutable input relationships."""

from demo.app.contracts import DemoError, DomainResult, Scope
from demo.tests.support import DemoCase
from unittest.mock import patch


class FlowFeedbackTests(DemoCase):
    def test_atomic_review_action_is_registered(self):
        self.assertIn("feedback.review", self.h.service.registered_actions())

    def test_fractional_acceptance_time_keeps_the_full_thirty_seconds(self):
        with patch.object(self.h.clock, "now_iso", lambda: self.h.clock._now.isoformat(timespec="milliseconds")):
            self.h.clock.advance(0.75)
            payload = self.prepare()
            self.h.command("demo.mode", {"mode": "step"})
            reply = self.h.command("feedback.review", payload)
            self.advance(reply.body["task_id"])
            self.h.clock.advance(29.5)
            self.advance(reply.body["task_id"])
            self.assertEqual(self.entities("ChangeSet"), [])
            self.h.clock.advance(0.5)
            self.advance(reply.body["task_id"])
            self.assertEqual(self.h.task(reply.body["task_id"])["status"], "completed")

    def prepare(self):
        applied = self.h.adopt()
        summary = self.h.entity(self.h.summary(applied_id=applied))
        return {
            "session_id": summary["session_id"], "summary_id": summary["id"],
            "applied_id": applied, "raw_text": "爆炸时画面卡顿",
        }

    def entities(self, kind):
        ws = self.h.workspace()
        return self.h.service.find(Scope("mw2-rtx4060", ws["workspace_epoch"]), kind)

    def advance(self, task_id, times=4):
        for _ in range(times):
            if self.h.task(task_id)["status"] in ("queued", "running"):
                self.h.command("task.advance", {"task_id": task_id})

    def test_29_30_boundary_and_duplicate_submission(self):
        payload = self.prepare()
        self.h.command("demo.mode", {"mode": "step"})
        reply = self.h.command("feedback.review", payload)
        task_id = reply.body["task_id"]
        analysis_id = self.h.ref(reply, "FeedbackAnalysis")
        duplicate = self.h.command("feedback.review", payload)
        self.assertEqual(duplicate.body["task_id"], task_id)
        self.assertEqual(len(self.entities("Feedback")), 1)
        self.assertEqual(len(self.entities("FeedbackAnalysis")), 1)
        self.advance(task_id)
        self.h.clock.advance(29)
        self.advance(task_id)
        self.assertEqual(self.entities("ChangeSet"), [])
        self.assertNotIn("change_set_id", self.h.task(task_id)["result"])
        self.h.clock.advance(1)
        self.advance(task_id)
        task = self.h.task(task_id)
        self.assertEqual(task["status"], "completed")
        change = self.h.entity(task["result"]["change_set_id"])
        self.assertEqual(change["changes"], {
            "mw2.particle_quality": {"from": "high", "to": "medium", "setting_id": "mw2.particle_quality"},
        })
        self.assertEqual(task["result"]["analysis_id"], analysis_id)
        self.assertEqual(self.h.entity(analysis_id)["status"], "completed")
        self.assertTrue({"mw2.texture_detail", "mw2.texture_filtering", "mw2.render_resolution"}
                        <= set(change["protected_setting_ids"]))
        self.assertEqual({self.h.entity(source_id)["url"].rsplit("/", 1)[-1] for source_id in change["evidence_ids"]},
                         {"s07", "s11", "s14"})
        self.h.command("feedback.review", payload)
        self.assertEqual(len(self.entities("ChangeSet")), 1)

    def test_fast_and_legacy_next_respect_deadline(self):
        payload = self.prepare()
        self.h.command("demo.mode", {"mode": "fast"})
        saved = self.h.command("feedback.save", {k: v for k, v in payload.items() if k != "applied_id"})
        reply = self.h.command("feedback.next", {
            "feedback_id": self.h.ref(saved, "Feedback"),
            "applied_id": payload["applied_id"], "summary_id": payload["summary_id"],
        })
        for _ in range(29):
            self.h.clock.advance(1)
            self.h.service.tasks.tick()
        self.assertEqual(self.entities("ChangeSet"), [])
        self.h.clock.advance(1)
        self.h.service.tasks.tick()
        self.assertEqual(self.h.task(reply.body["task_id"])["status"], "completed")

    def test_changed_configuration_invalidates_old_analysis(self):
        payload = self.prepare()
        reply = self.h.command("feedback.review", payload)
        self.h.command("configuration.edit", {
            "expected_current_id": payload["applied_id"],
            "changes": {"mw2.particle_quality": "medium"}, "confirmation": "已调整",
        })
        task = self.h.run(reply.body["task_id"])
        self.assertEqual(task["status"], "superseded")
        self.assertEqual(self.entities("ChangeSet"), [])
        self.assertIn("配置已变化", task["result"]["reason"])

    def test_different_feedback_supersedes_and_cancellation_does_not_publish(self):
        payload = self.prepare()
        old = self.h.command("feedback.review", payload)
        new = self.h.command("feedback.review", {**payload, "raw_text": "爆炸时画面频繁卡顿"})
        self.assertEqual(self.h.task(old.body["task_id"])["status"], "superseded")
        self.h.command("task.cancel", {"task_id": new.body["task_id"]})
        self.h.clock.advance(40)
        self.h.service.tasks.tick()
        self.assertEqual(self.entities("ChangeSet"), [])
        self.assertEqual(self.h.entity(self.h.ref(new, "FeedbackAnalysis"))["status"], "cancelled")

    def test_restart_resume_preserves_deadline(self):
        payload = self.prepare()
        reply = self.h.command("feedback.review", payload)
        analysis_id = self.h.ref(reply, "FeedbackAnalysis")
        deadline = self.h.entity(analysis_id)["ready_not_before"]
        self.h.clock.advance(29)
        self.h.reopen()
        self.assertEqual(self.h.entity(analysis_id)["status"], "interrupted")
        resumed = self.h.command("task.resume", {"task_id": reply.body["task_id"]})
        self.assertEqual(self.h.entity(analysis_id)["task_id"], resumed.body["task_id"])
        self.assertEqual(self.h.entity(analysis_id)["ready_not_before"], deadline)
        task = self.h.run(resumed.body["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(self.entities("ChangeSet")), 1)

    def test_protected_particle_falls_back_and_noop_is_not_created(self):
        payload = self.prepare()
        reply = self.h.command("feedback.review", {
            **payload, "protected_setting_ids": ["mw2.particle_quality"],
        })
        task = self.h.run(reply.body["task_id"])
        change = self.h.entity(task["result"]["change_set_id"])
        self.assertEqual(set(change["changes"]), {"mw2.shadow_quality"})
        self.assertNotEqual(change["changes"]["mw2.shadow_quality"]["from"], change["changes"]["mw2.shadow_quality"]["to"])
        reply = self.h.command("feedback.review", {
            **payload, "protected_setting_ids": ["mw2.particle_quality", "mw2.shadow_quality"],
        })
        task = self.h.run(reply.body["task_id"])
        self.assertIsNone(task["result"]["change_set_id"])
        self.assertEqual(len(self.entities("ChangeSet")), 1)

    def test_review_rejects_unrelated_summary_atomically(self):
        payload = self.prepare()
        other_session = self.h.session(applied_id=payload["applied_id"])
        with self.assertRaises(DemoError):
            self.h.command("feedback.review", {**payload, "session_id": other_session["session_id"]})
        self.assertEqual(self.entities("Feedback"), [])
        self.assertEqual(self.entities("FeedbackAnalysis"), [])

    def test_lowered_particle_uses_one_shadow_step(self):
        payload = self.prepare()
        edited = self.h.command("configuration.edit", {
            "expected_current_id": payload["applied_id"],
            "changes": {"mw2.particle_quality": "medium", "mw2.shadow_quality": "medium"},
            "confirmation": "已调整",
        })
        applied_id = self.h.ref(edited, "AppliedConfiguration")
        summary = self.h.entity(self.h.summary(applied_id=applied_id))
        reply = self.h.command("feedback.review", {
            "session_id": summary["session_id"], "summary_id": summary["id"],
            "applied_id": applied_id, "raw_text": "爆炸时画面卡顿",
        })
        result = self.h.run(reply.body["task_id"])["result"]
        changes = self.h.entity(result["change_set_id"])["changes"]
        self.assertEqual(changes, {"mw2.shadow_quality": {
            "setting_id": "mw2.shadow_quality", "from": "medium", "to": "low",
        }})

    def test_failure_rolls_back_partial_result_and_can_resume(self):
        payload = self.prepare()
        reply = self.h.command("feedback.review", payload)
        analysis_id = self.h.ref(reply, "FeedbackAnalysis")
        deadline = self.h.entity(analysis_id)["ready_not_before"]

        def fail(ctx, *args, **kwargs):
            ctx.tx.insert("ChangeSet", {"status": "proposed"}, origin="simulated")
            raise RuntimeError("unexpected evaluation failure")

        with patch("demo.app.feedback._evaluate_next", side_effect=fail):
            task = self.h.run(reply.body["task_id"])
        self.assertEqual(task["status"], "failed")
        self.assertEqual(self.h.entity(analysis_id)["status"], "failed")
        self.assertEqual(self.entities("ChangeSet"), [])
        resumed = self.h.command("task.resume", {"task_id": reply.body["task_id"]})
        self.assertEqual(self.h.entity(analysis_id)["ready_not_before"], deadline)
        task = self.h.run(resumed.body["task_id"])
        self.assertEqual(task["status"], "completed")
        self.h.reopen()
        self.assertEqual(self.h.entity(analysis_id)["status"], "completed")

    def test_changed_background_invalidates_analysis(self):
        payload = self.prepare()
        reply = self.h.command("feedback.review", payload)
        context = self.h.entity(self.h.workspace()["context_id"])
        self.h.command("context.save", {"expected_context_id": context["id"], "fields": {"scene_tag": "另一场景"}})
        task = self.h.run(reply.body["task_id"])
        self.assertEqual(task["status"], "superseded")
        self.assertEqual(self.entities("ChangeSet"), [])

    def test_revisiting_old_feedback_supersedes_the_current_analysis(self):
        payload = self.prepare()
        first = self.h.command("feedback.review", payload)
        self.h.run(first.body["task_id"])
        pending = self.h.command("feedback.review", {**payload, "raw_text": "交战时偶尔卡顿"})
        current = self.h.command("feedback.review", payload)
        self.assertEqual(self.h.task(pending.body["task_id"])["status"], "superseded")
        self.assertNotEqual(current.body["task_id"], first.body["task_id"])
        self.h.run(current.body["task_id"])
        self.assertEqual(self.h.task(pending.body["task_id"])["status"], "superseded")

    def test_manual_edit_retains_the_original_knowledge_scope(self):
        payload = self.prepare()
        original = self.h.entity(payload["applied_id"])
        recommendation = self.h.entity(original["recommendation_id"])
        knowledge = self.h.entity(recommendation["knowledge_id"])
        source = next(self.h.entity(source_id) for source_id in knowledge["source_ids"]
                      if self.h.entity(source_id)["url"].endswith("/s07"))

        def add_unrelated(ctx, _payload):
            item = ctx.tx.insert("Source", {"url": source["url"], "title": "另一版本的来源"}, origin="simulated")
            return DomainResult(201, [{"kind": "Source", "id": item["id"]}])

        self.h.service.register("test.unrelated_source", lambda data: data, add_unrelated)
        unrelated_id = self.h.ref(self.h.command("test.unrelated_source"), "Source")
        edited = self.h.command("configuration.edit", {
            "expected_current_id": payload["applied_id"],
            "changes": {"mw2.particle_quality": "medium"}, "confirmation": "已调整",
        })
        applied_id = self.h.ref(edited, "AppliedConfiguration")
        summary = self.h.entity(self.h.summary(applied_id=applied_id))
        reply = self.h.command("feedback.review", {
            "session_id": summary["session_id"], "summary_id": summary["id"],
            "applied_id": applied_id, "raw_text": "爆炸时画面卡顿",
        })
        analysis = self.h.entity(self.h.ref(reply, "FeedbackAnalysis"))
        self.assertEqual(analysis["knowledge_id"], knowledge["id"])
        self.assertEqual(analysis["source_pack_version"], knowledge["content_version"])
        result = self.h.run(reply.body["task_id"])["result"]
        change = self.h.entity(result["change_set_id"])
        self.assertTrue(set(change["evidence_ids"]) <= set(knowledge["source_ids"]))
        self.assertNotIn(unrelated_id, change["evidence_ids"])
