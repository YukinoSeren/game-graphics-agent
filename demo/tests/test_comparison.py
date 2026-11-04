"""T13: one frozen threshold, scoped comparability, explicit downgrades."""

from demo.app.contracts import DemoError
from demo.tests.support import DemoCase


class ComparisonTests(DemoCase):
    def test_one_frozen_threshold_is_used_for_both_sides(self):
        a1 = self.h.adopt()
        s1 = self.h.summary("recommended", applied_id=a1)
        frozen = self.h.command(
            "comparison.freeze",
            {
                "mode": "fixed",
                "threshold_ms": 50.0,
                "baseline_summary_ids": [s1],
                "exploratory": False,
            },
        )
        protocol = self.h.ref(frozen, "ComparisonProtocol")
        s2 = self.h.summary("adjusted", applied_id=a1)
        result = self.h.command(
            "comparison.run",
            {
                "protocol_id": protocol,
                "before_summary_id": s1,
                "after_summary_id": s2,
            },
        )
        comparison = self.h.entity(self.h.ref(result, "Comparison"))
        self.assertEqual(comparison["threshold_ms"], 50.0)
        self.assertEqual(comparison["before_long_frames"]["threshold_ms"], 50.0)
        self.assertEqual(comparison["after_long_frames"]["threshold_ms"], 50.0)
        self.assertIs(comparison["demo_only"], True)

    def test_protocol_frozen_before_the_second_sample_can_be_comparable(self):
        a1 = self.h.adopt()
        s1 = self.h.summary("recommended", applied_id=a1)
        protocol = self.h.ref(
            self.h.command(
                "comparison.freeze",
                {
                    "mode": "fixed",
                    "threshold_ms": 50.0,
                    "baseline_summary_ids": [s1],
                    "exploratory": False,
                },
            ),
            "ComparisonProtocol",
        )
        s2 = self.h.summary("adjusted", applied_id=a1)
        comparison = self.h.entity(
            self.h.ref(
                self.h.command(
                    "comparison.run",
                    {
                        "protocol_id": protocol,
                        "before_summary_id": s1,
                        "after_summary_id": s2,
                    },
                ),
                "Comparison",
            )
        )
        self.assertTrue(comparison["comparable"])
        self.assertFalse(comparison["exploratory"])
        self.assertEqual(comparison["causality"], "not_established")

    def test_protocol_created_after_the_samples_is_exploratory(self):
        a1 = self.h.adopt()
        s1 = self.h.summary("recommended", applied_id=a1)
        s2 = self.h.summary("adjusted", applied_id=a1)
        protocol = self.h.ref(
            self.h.command(
                "comparison.freeze",
                {"mode": "fixed", "threshold_ms": 50.0, "baseline_summary_ids": [s1]},
            ),
            "ComparisonProtocol",
        )
        comparison = self.h.entity(
            self.h.ref(
                self.h.command(
                    "comparison.run",
                    {
                        "protocol_id": protocol,
                        "before_summary_id": s1,
                        "after_summary_id": s2,
                    },
                ),
                "Comparison",
            )
        )
        self.assertTrue(comparison["exploratory"])
        self.assertFalse(comparison["comparable"])
        self.assertTrue(any("之后" in note for note in comparison["comparability_notes"]))

    def test_previous_epoch_summary_is_rejected(self):
        other = self.h.summary("recommended")
        self.h.command("demo.reset", {"scope": "current"})
        a1 = self.h.adopt()
        s1 = self.h.summary("recommended", applied_id=a1)
        protocol = self.h.ref(
            self.h.command(
                "comparison.freeze",
                {"mode": "fixed", "threshold_ms": 50.0, "baseline_summary_ids": [s1]},
            ),
            "ComparisonProtocol",
        )
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "comparison.run",
                {
                    "protocol_id": protocol,
                    "before_summary_id": s1,
                    "after_summary_id": other,
                },
            )
        self.assertEqual(caught.exception.status, 404)

    def test_baseline_derived_threshold_is_recomputed_from_the_artifact(self):
        s1 = self.h.summary("recommended")
        protocol = self.h.entity(
            self.h.ref(
                self.h.command(
                    "comparison.freeze",
                    {
                        "mode": "baseline_derived",
                        "baseline_summary_ids": [s1],
                    },
                ),
                "ComparisonProtocol",
            )
        )
        self.assertEqual(protocol["threshold_source"], "baseline_derived")
        self.assertGreaterEqual(protocol["threshold_ms"], 50.0)

    def test_missing_artifact_degrades_only_the_recomputation(self):
        a1 = self.h.adopt()
        s1 = self.h.summary("recommended", applied_id=a1)
        protocol = self.h.ref(
            self.h.command(
                "comparison.freeze",
                {"mode": "fixed", "threshold_ms": 50.0, "baseline_summary_ids": [s1]},
            ),
            "ComparisonProtocol",
        )
        s2 = self.h.summary("adjusted", applied_id=a1)
        artifact = self.h.entity(self.h.entity(s1)["artifact_id"])
        path = self.h.service.files.path_for(artifact["file_id"])
        path.unlink()
        comparison = self.h.entity(
            self.h.ref(
                self.h.command(
                    "comparison.run",
                    {
                        "protocol_id": protocol,
                        "before_summary_id": s1,
                        "after_summary_id": s2,
                    },
                ),
                "Comparison",
            )
        )
        self.assertIsNone(comparison["before_long_frames"])
        self.assertTrue(comparison["metrics"])
        self.assertTrue(any("原件不可用" in note for note in comparison["comparability_notes"]))

    def test_unknown_configuration_downgrades_comparability(self):
        session = self.h.session(applied_id=None)
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
        first = self.h.ref(
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
                {"mode": "fixed", "threshold_ms": 50.0, "baseline_summary_ids": [first]},
            ),
            "ComparisonProtocol",
        )
        second = self.h.summary("adjusted")
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
        self.assertTrue(any("未确认" in note for note in comparison["comparability_notes"]))
