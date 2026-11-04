"""T12: uploads are previewed and associated only by explicit commands."""

from pathlib import Path

from demo.app.contracts import DemoError, Scope
from demo.app.scenarios import ScenarioCatalog
from demo.app.traces import generate_trace
from demo.tests.support import DemoCase

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class LogImportTests(DemoCase):
    def _csv(self, label="recommended", scenario="mw2-rtx4060"):
        recipe = ScenarioCatalog.load(FIXTURES).recipe(scenario, label)
        return generate_trace(scenario, label, recipe)

    def _closed_segment(self):
        session = self.h.session()
        self.h.clock.advance(10)
        self.h.command(
            "session.end",
            {
                "session_id": session["session_id"],
                "expected_segment_id": session["segment_id"],
                "expected_segment_revision": session["segment_revision"],
                "expected_binding_revision": session["binding_revision"],
            },
        )
        segment = self.h.entity(session["segment_id"])
        binding = self.h.entity(segment["binding_id"])
        return segment, binding

    def test_inspect_previews_without_creating_a_session_or_summary(self):
        file_id = self.h.upload(self._csv(), filename="frames.csv")
        before = self.h.workspace()["revision"]
        reply = self.h.command("log.inspect", {"file_id": file_id})
        inspection = self.h.entity(self.h.ref(reply, "LogInspection"))
        self.assertEqual(inspection["frame_count"], 3600)
        self.assertTrue(inspection["preview"])
        scope = Scope("mw2-rtx4060", self.h.workspace()["workspace_epoch"])
        self.assertEqual(self.h.service.store.find(scope, "Session"), [])
        self.assertEqual(self.h.service.store.find(scope, "Summary"), [])
        self.assertEqual(self.h.workspace()["revision"], before + 1)

    def test_association_requires_a_closed_segment_and_matching_revisions(self):
        file_id = self.h.upload(self._csv(), filename="frames.csv")
        session = self.h.session()
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "log.associate",
                {
                    "file_id": file_id,
                    "segment_id": session["segment_id"],
                    "segment_revision": session["segment_revision"],
                    "binding_revision": session["binding_revision"],
                },
            )
        self.assertEqual(caught.exception.code, "segment_open")

    def test_uploaded_log_is_associated_and_analyzed_as_uploaded_data(self):
        segment, binding = self._closed_segment()
        file_id = self.h.upload(self._csv(), filename="frames.csv")
        reply = self.h.command(
            "log.associate",
            {
                "file_id": file_id,
                "segment_id": segment["id"],
                "segment_revision": segment["revision"],
                "binding_revision": binding["revision"],
            },
        )
        artifact = self.h.entity(self.h.ref(reply, "LogArtifact"))
        self.assertEqual(artifact["source"], "uploaded")
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
        self.assertEqual(summary["data_origin"], "uploaded_demo_file")
        self.assertEqual(summary["source_hash"], artifact["content_hash"])

    def test_historical_association_needs_an_explicit_time_and_basis(self):
        session = self.h.session()
        file_id = self.h.upload(self._csv(), filename="frames.csv")
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "log.associate",
                {"file_id": file_id, "historical": True, "session_id": session["session_id"]},
            )
        self.assertEqual(caught.exception.code, "missing_field")
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
        self.assertTrue(artifact["historical"])
        self.assertEqual(artifact["time_base"], "declared")
        self.assertIsNone(self.h.workspace()["current_applied_id"])

    def test_unsupported_frame_stream_is_rejected(self):
        data = self._csv().replace(b"application_present", b"other_stream")
        file_id = self.h.upload(data, filename="bad-stream.csv")
        with self.assertRaises(DemoError) as caught:
            self.h.command("log.inspect", {"file_id": file_id})
        self.assertEqual(caught.exception.code, "unsupported_stream")

    def test_cross_case_csv_is_rejected(self):
        file_id = self.h.upload(self._csv(scenario="fh6-rtx5090"), filename="fh6.csv")
        with self.assertRaises(DemoError) as caught:
            self.h.command("log.inspect", {"file_id": file_id})
        self.assertEqual(caught.exception.code, "cross_case_trace")
