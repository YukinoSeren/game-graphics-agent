"""T12: summaries keep one artifact, one hash, and one computation."""

from demo.app.contracts import DemoError
from demo.tests.support import DemoCase


class AnalysisTests(DemoCase):
    def test_summary_keeps_one_source_for_metrics_and_curve(self):
        summary = self.h.entity(self.h.summary("recommended"))
        artifact = self.h.entity(summary["artifact_id"])
        self.assertEqual(summary["source_hash"], artifact["content_hash"])
        self.assertEqual(summary["chart"]["source_hash"], summary["source_hash"])
        self.assertEqual(summary["data_origin"], "simulated")
        self.assertEqual(summary["frame_stream"], "application_present")

    def test_metrics_match_the_chart_source_and_stay_inside_the_sample(self):
        summary = self.h.entity(self.h.summary("recommended"))
        metrics = summary["metrics"]
        chart = summary["chart"]
        self.assertEqual(metrics["interval_count"], 3599)
        self.assertIsNotNone(metrics["average_present_rate"])
        self.assertEqual(metrics["quality"], "usable")
        self.assertLessEqual(chart["point_count"], metrics["interval_count"] + 1)
        self.assertEqual(
            chart["threshold_ms"],
            summary["long_frames"]["threshold_ms"],
        )

    def test_short_sample_reports_limited_quality_and_unknown_tail(self):
        summary = self.h.entity(self.h.summary("short"))
        self.assertEqual(summary["quality"], "limited")
        self.assertIsNone(summary["metrics"]["slowest_1pct_fps"])
        self.assertEqual(summary["metrics"]["interval_count"], 17)

    def test_tampered_artifact_is_rejected_instead_of_recomputed(self):
        summary = self.h.entity(self.h.summary("recommended"))
        artifact = self.h.entity(summary["artifact_id"])
        path = self.h.service.files.path_for(artifact["file_id"])
        path.write_bytes(path.read_bytes().replace(b"true,", b"true,", 1) + b"\n")
        segment = self.h.entity(summary["segment_id"])
        binding = self.h.entity(summary["binding_id"])
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "performance.analyze",
                {
                    "artifact_id": artifact["id"],
                    "segment_id": segment["id"],
                    "segment_revision": segment["revision"],
                    "binding_revision": binding["revision"],
                },
            )
        self.assertEqual(caught.exception.code, "artifact_hash_mismatch")

    def test_analyzing_twice_adds_a_new_summary_without_rewriting_the_old(self):
        summary_id = self.h.summary("recommended")
        original = self.h.entity(summary_id)
        segment = self.h.entity(original["segment_id"])
        binding = self.h.entity(original["binding_id"])
        reply = self.h.command(
            "performance.analyze",
            {
                "artifact_id": original["artifact_id"],
                "segment_id": segment["id"],
                "segment_revision": segment["revision"],
                "binding_revision": binding["revision"],
            },
        )
        second_id = self.h.ref(reply, "Summary")
        self.assertNotEqual(second_id, summary_id)
        self.assertEqual(self.h.entity(summary_id), original)

    def test_summary_records_the_bound_applied_configuration(self):
        a1 = self.h.adopt()
        summary = self.h.entity(self.h.summary("recommended", applied_id=a1))
        self.assertEqual(summary["applied_id"], a1)
        self.assertEqual(summary["binding_revision"], 1)
