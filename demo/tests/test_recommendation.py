"""T08: recommendations are traceable, non-adopting snapshots."""

from demo.app.contracts import Command, DemoError, Scope
from demo.tests.support import DemoCase


class RecommendationTests(DemoCase):
    def test_recommendation_does_not_invent_a_local_result_or_adoption(self):
        recommendation = self.h.entity(self.h.recommendation())
        self.assertGreaterEqual(len(recommendation["settings"]), 12)
        self.assertIsNone(recommendation["local_summary_id"])
        self.assertIsNone(self.h.workspace()["current_applied_id"])
        self.assertTrue(all(s["evidence_ids"] for s in recommendation["settings"]))

    def test_every_setting_carries_reason_conditions_and_original_name(self):
        recommendation = self.h.entity(self.h.recommendation())
        for setting in recommendation["settings"]:
            self.assertTrue(setting["reason"])
            self.assertTrue(setting["demo_original_name"])
            self.assertIn("dependencies", setting)
            self.assertIn(setting["value"], setting["allowed_values"] or [None])

    def test_target_reference_and_local_result_are_separate_fields(self):
        recommendation = self.h.entity(self.h.recommendation())
        self.assertIn("target_proposal", recommendation)
        self.assertIsInstance(recommendation["reference_results"], list)
        self.assertIsNone(recommendation["local_summary_id"])
        # Fixed source notes do not invent measured third-party performance.
        self.assertEqual(recommendation["reference_results"], [])

    def test_variant_does_not_modify_the_base_recommendation(self):
        base_id = self.h.recommendation()
        before = self.h.entity(base_id)
        reply = self.h.command(
            "recommendation.variant",
            {"recommendation_id": base_id, "variant": "performance"},
        )
        variant = self.h.entity(self.h.ref(reply, "Recommendation"))
        self.assertEqual(variant["variant_of"], base_id)
        self.assertEqual(variant["template"], "performance")
        self.assertEqual(self.h.entity(base_id), before)
        shadow = next(
            item for item in variant["settings"] if item["setting_id"] == "mw2.shadow_quality"
        )
        self.assertEqual(shadow["value"], "medium")

    def test_custom_background_lowers_readiness(self):
        context_id = self.h.workspace()["context_id"]
        self.h.command(
            "context.save",
            {"expected_context_id": context_id, "fields": {"gpu_name": "其他显卡"}},
        )
        knowledge_id = self.h.knowledge()
        reply = self.h.command(
            "recommendation.generate",
            {"context_id": self.h.workspace()["context_id"], "knowledge_id": knowledge_id},
        )
        recommendation = self.h.entity(self.h.ref(reply, "Recommendation"))
        self.assertEqual(recommendation["readiness"], "provisional")
        self.assertIn("自定义", recommendation["readiness_reason"])

    def test_review_creates_a_new_snapshot_and_keeps_the_old_one(self):
        base_id = self.h.recommendation()
        base = self.h.entity(base_id)
        reply = self.h.command("recommendation.review", {"recommendation_id": base_id})
        reviewed = self.h.entity(self.h.ref(reply, "Recommendation"))
        self.assertEqual(reviewed["supersedes_id"], base_id)
        self.assertEqual(reviewed["revision"], base["revision"] + 1)
        self.assertEqual(self.h.entity(base_id)["readiness"], base["readiness"])

    def test_active_knowledge_cannot_be_read_through_archived_case_scope(self):
        knowledge_id = self.h.knowledge()
        before = self.h.workspace()
        with self.assertRaises(DemoError) as caught:
            self.h.service.entity(Scope("fh6-rtx5090", before["workspace_epoch"]), knowledge_id)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "unsupported_game")
        self.assertEqual(self.h.workspace(), before)
        self.assertEqual(self.h.entity(knowledge_id)["scenario_id"], "mw2-rtx4060")

    def test_archived_case_recommendation_command_is_rejected_without_changing_active_case(self):
        a_id = self.h.recommendation()
        a = self.h.entity(a_id)
        before = self.h.workspace()
        command = Command(
            request_id="archived-case-recommendation",
            scenario_id="fh6-rtx5090", workspace_epoch=1, expected_revision=1,
            action="recommendation.generate",
            payload={"context_id": before["context_id"], "knowledge_id": a["knowledge_id"]},
        )
        with self.assertRaises(DemoError) as caught:
            self.h.service.command(command)
        self.assertEqual(caught.exception.code, "unsupported_game")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.h.workspace(), before)
        self.assertEqual(self.h.entity(a_id), a)
        self.assertTrue(all(item["setting_id"].startswith("mw2.") for item in a["settings"]))
