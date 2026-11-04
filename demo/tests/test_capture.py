"""T12: simulated capture closes segments and records demo artifacts."""

from demo.app.contracts import DemoError
from demo.tests.support import DemoCase


class CaptureTests(DemoCase):
    def _start(self, session, *, label="recommended"):
        return self.h.command(
            "capture.start",
            {
                "session_id": session["session_id"],
                "segment_id": session["segment_id"],
                "expected_segment_revision": session["segment_revision"],
                "expected_binding_revision": session["binding_revision"],
                "label": label,
            },
        )

    def test_capture_closes_the_segment_and_records_wait_and_data_ranges(self):
        a1 = self.h.adopt()
        session = self.h.session(applied_id=a1)
        reply = self._start(session)
        task = self.h.run(reply.body["task_id"])
        artifact = self.h.entity(task["result"]["artifact_id"])
        segment = self.h.entity(session["segment_id"])
        self.assertEqual(artifact["label"], "recommended")
        self.assertEqual(artifact["capture_wait_s"], 12.0)
        self.assertEqual(artifact["source"], "capture")
        self.assertEqual(artifact["frame_stream"], "application_present")
        self.assertEqual(segment["status"], "closed")
        self.assertIsNotNone(segment["end_s"])
        self.assertNotIn("pid", artifact)
        self.assertNotIn("process", artifact)
        self.assertEqual(self.h.entity(session["session_id"])["active_segment_id"], None)

    def test_second_capture_for_the_same_segment_is_rejected(self):
        session = self.h.session()
        self._start(session)
        with self.assertRaises(DemoError) as caught:
            self._start(session)
        self.assertEqual(caught.exception.code, "capture_busy")

    def test_stopping_early_uses_a_short_sample(self):
        session = self.h.session()
        reply = self._start(session)
        task_id = reply.body["task_id"]
        stopped = self.h.command("capture.stop", {"task_id": task_id})
        artifact = self.h.entity(self.h.ref(stopped, "LogArtifact"))
        self.assertEqual(artifact["label"], "short")
        self.assertEqual(artifact["capture_wait_s"], 0.0)
        self.assertEqual(self.h.task(task_id)["status"], "cancelled")
        with self.assertRaises(DemoError) as caught:
            self.h.command("capture.stop", {"task_id": task_id})
        self.assertEqual(caught.exception.code, "task_already_terminal")

    def test_capture_task_has_no_real_process_leases(self):
        session = self.h.session()
        reply = self._start(session)
        task = self.h.run(reply.body["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertNotIn("real_pid", task)
        self.assertIsNone(task["result"].get("real_pid"))
