"""T15: one-click complete cases are valid, scoped, and non-stacking."""

from demo.app.contracts import DemoError, Scope
from demo.tests.support import DemoCase


class SeedTests(DemoCase):
    def test_complete_case_is_scoped_and_contains_real_artifact_references(self):
        untouched = self.h.service.store.find(Scope("fh6-rtx5090", 1), "ContextSnapshot")
        reply = self.h.command(
            "demo.seed",
            {
                "position": "comparison",
                "expected_epoch": self.h.workspace()["workspace_epoch"],
            },
        )
        comparison = self.h.entity(self.h.ref(reply, "Comparison"))
        before = self.h.entity(comparison["before_summary_id"])
        self.assertTrue(self.h.entity(before["artifact_id"])["content_hash"])
        self.assertEqual(self.h.service.store.find(Scope("fh6-rtx5090", 1), "ContextSnapshot"), untouched)

    def test_seed_moves_to_a_fresh_epoch_and_marks_the_story(self):
        before = self.h.workspace()["workspace_epoch"]
        reply = self.h.command(
            "demo.seed",
            {"position": "comparison", "expected_epoch": before},
        )
        self.assertEqual(self.h.workspace()["workspace_epoch"], before + 1)
        self.assertEqual(reply.body["workspace_epoch"], before + 1)
        summary = self.h.entity(reply.body["data"]["s1_id"])
        self.assertEqual(summary["data_origin"], "demo_seed")
        recommendation = self.h.entity(reply.body["data"]["a1_id"])
        self.assertEqual(recommendation["origin"], "recommendation")
        current = self.h.entity(self.h.workspace()["current_applied_id"])
        self.assertEqual(current["id"], reply.body["data"]["a2_id"])

    def test_recommendation_position_does_not_pretend_to_adopt(self):
        reply = self.h.command(
            "demo.seed",
            {
                "position": "recommendation",
                "expected_epoch": self.h.workspace()["workspace_epoch"],
            },
        )
        self.assertIsNone(self.h.workspace()["current_applied_id"])
        self.assertEqual(reply.body["data"]["position"], "recommendation")
        scope = Scope("mw2-rtx4060", self.h.workspace()["workspace_epoch"])
        self.assertEqual(self.h.service.store.find(scope, "AppliedConfiguration"), [])

    def test_loading_twice_does_not_stack_the_same_story(self):
        for _ in range(2):
            self.h.command(
                "demo.seed",
                {
                    "position": "comparison",
                    "expected_epoch": self.h.workspace()["workspace_epoch"],
                },
            )
        scope = Scope("mw2-rtx4060", self.h.workspace()["workspace_epoch"])
        self.assertEqual(len(self.h.service.store.find(scope, "Summary")), 2)
        self.assertEqual(len(self.h.service.store.find(scope, "Comparison")), 1)
        self.assertEqual(len(self.h.service.store.find(scope, "AppliedConfiguration")), 3)

    def test_stale_epoch_seed_is_rejected(self):
        epoch = self.h.workspace()["workspace_epoch"]
        self.h.command("demo.reset", {"scope": "current"})
        with self.assertRaises(DemoError) as caught:
            self.h.command("demo.seed", {"position": "comparison", "expected_epoch": epoch})
        self.assertEqual(caught.exception.code, "stale_epoch")
