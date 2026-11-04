"""T14: feedback branches keep proposals separate from adoption."""

from demo.app.contracts import Command, DemoError, DomainResult, Reply
from demo.tests.support import DemoCase


class FeedbackTests(DemoCase):
    def next(self, payload):
        accepted = self.h.command("feedback.next", payload)
        task = self.h.run(accepted.body["task_id"])
        data = task["result"]
        refs = [{"kind": "ChangeSet", "id": data["change_set_id"]}] if data.get("change_set_id") else []
        return Reply(200, {"data": data, "refs": refs})

    def test_stutter_adjustment_preserves_texture_and_does_not_adopt_itself(self):
        a1 = self.h.adopt()
        summary = self.h.entity(self.h.summary("recommended", applied_id=a1))
        saved = self.h.command(
            "feedback.save",
            {
                "session_id": summary["session_id"],
                "summary_id": summary["id"],
                "satisfied": False,
                "symptoms": ["stutter"],
                "severity": "occasional",
                "raw_text": "交战时偶尔卡一下，希望保持画面清晰",
                "protected_setting_ids": ["mw2.texture_detail"],
            },
        )
        next_step = self.next(
            {
                "feedback_id": self.h.ref(saved, "Feedback"),
                "applied_id": a1,
                "summary_id": summary["id"],
            },
        )
        changes = self.h.entity(self.h.ref(next_step, "ChangeSet"))
        self.assertEqual(next_step.body["data"]["action"], "adjust")
        self.assertEqual(changes["base_applied_id"], a1)
        self.assertEqual(set(changes["changes"]), {"mw2.shadow_quality"})
        self.assertEqual(self.h.workspace()["current_applied_id"], a1)

    def test_satisfied_feedback_keeps_the_current_configuration(self):
        a1 = self.h.adopt()
        summary = self.h.entity(self.h.summary("recommended", applied_id=a1))
        saved = self.h.command(
            "feedback.save",
            {
                "session_id": summary["session_id"],
                "summary_id": summary["id"],
                "satisfied": True,
                "raw_text": "满意，保持这样",
            },
        )
        next_step = self.next(
            {
                "feedback_id": self.h.ref(saved, "Feedback"),
                "applied_id": a1,
                "summary_id": summary["id"],
            },
        )
        self.assertEqual(next_step.body["data"]["action"], "keep")
        self.assertIsNone(next_step.body["data"]["change_set_id"])

    def test_missing_performance_data_asks_for_a_measurement(self):
        a1 = self.h.adopt()
        session = self.h.session(applied_id=a1)
        saved = self.h.command(
            "feedback.save",
            {
                "session_id": session["session_id"],
                "satisfied": False,
                "symptoms": ["stutter"],
                "raw_text": "交战时卡顿",
            },
        )
        next_step = self.next(
            {"feedback_id": self.h.ref(saved, "Feedback"), "applied_id": a1},
        )
        self.assertEqual(next_step.body["data"]["action"], "measure")

    def test_protected_target_uses_the_alternative_setting(self):
        a1 = self.h.adopt()
        summary = self.h.entity(self.h.summary("recommended", applied_id=a1))
        saved = self.h.command(
            "feedback.save",
            {
                "session_id": summary["session_id"],
                "summary_id": summary["id"],
                "satisfied": False,
                "symptoms": ["stutter"],
                "raw_text": "卡顿",
                "protected_setting_ids": ["mw2.shadow_quality"],
            },
        )
        next_step = self.next(
            {
                "feedback_id": self.h.ref(saved, "Feedback"),
                "applied_id": a1,
                "summary_id": summary["id"],
            },
        )
        changes = self.h.entity(self.h.ref(next_step, "ChangeSet"))
        self.assertEqual(set(changes["changes"]), {"mw2.shadow_cascades"})
        self.assertIn("保护项", next_step.body["data"]["reason"])

    def test_unrecognized_feedback_is_not_given_a_generic_diagnosis(self):
        a1 = self.h.adopt()
        summary = self.h.entity(self.h.summary("recommended", applied_id=a1))
        saved = self.h.command(
            "feedback.save",
            {
                "session_id": summary["session_id"],
                "summary_id": summary["id"],
                "satisfied": False,
                "symptoms": [],
                "raw_text": "画面颜色有一点点奇怪，说不太清",
            },
        )
        next_step = self.next(
            {
                "feedback_id": self.h.ref(saved, "Feedback"),
                "applied_id": a1,
                "summary_id": summary["id"],
            },
        )
        self.assertEqual(next_step.body["data"]["action"], "insufficient_evidence")
        saved_entity = self.h.entity(self.h.ref(saved, "Feedback"))
        self.assertEqual(saved_entity["raw_text"], "画面颜色有一点点奇怪，说不太清")

    def test_two_failed_rounds_stop_the_suggestion_loop(self):
        a1 = self.h.adopt()
        summary = self.h.entity(self.h.summary("recommended", applied_id=a1))
        workspace = self.h.workspace()

        def seed(tx):
            for _ in range(2):
                tx.insert(
                    "Outcome",
                    {"accepted": False, "data_change": {"improved": False}},
                    origin="user_declared",
                )
            return DomainResult(200)

        self.h.service.store.mutate(
            Command(
                "seed-outcomes",
                "mw2-rtx4060",
                workspace["workspace_epoch"],
                workspace["revision"],
                "test.seed",
                {},
            ),
            seed,
        )
        saved = self.h.command(
            "feedback.save",
            {
                "session_id": summary["session_id"],
                "summary_id": summary["id"],
                "satisfied": False,
                "symptoms": ["stutter"],
                "raw_text": "还是卡顿",
            },
        )
        next_step = self.next(
            {
                "feedback_id": self.h.ref(saved, "Feedback"),
                "applied_id": a1,
                "summary_id": summary["id"],
            },
        )
        self.assertEqual(next_step.body["data"]["action"], "stop")

    def test_outcome_cannot_be_told_that_it_improved(self):
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "outcome.save",
                {
                    "change_set_id": "demo_change_x",
                    "applied_id": "demo_applied_x",
                    "comparison_id": "demo_comparison_x",
                    "accepted": True,
                    "improved": True,
                },
            )
        self.assertEqual(caught.exception.code, "unknown_field")

    def test_preference_and_target_responses_are_declarations(self):
        preference = self.h.command(
            "preference.save",
            {"preferences": {"preserve_clarity": True}, "explicit": True},
        )
        self.assertEqual(
            self.h.entity(self.h.ref(preference, "Preference"))["data_origin"],
            "user_declared",
        )
        target = self.h.command("target.respond", {"accepted": False, "note": "更看重清晰度"})
        response = self.h.entity(self.h.ref(target, "TargetResponse"))
        self.assertIs(response["accepted"], False)
        self.assertEqual(response["note_type"], "玩家声明")
