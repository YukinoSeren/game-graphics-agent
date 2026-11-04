"""T10: plans, sessions, segments, and binding revisions."""

from demo.app.contracts import Command, DemoError, Scope
from demo.app.sessions import require_closed, METRIC_KEYS
from demo.tests.support import DemoCase


class SessionTests(DemoCase):
    def test_unknown_configuration_is_allowed_but_not_confirmed(self):
        session = self.h.session(applied_id=None)
        segment = self.h.entity(session["segment_id"])
        binding = self.h.entity(segment["binding_id"])
        self.assertIsNone(binding["applied_id"])
        self.assertEqual(binding["configuration_state"], "unverified")
        self.assertIsNone(segment["end_s"])

    def test_plan_does_not_invent_duration_or_repeats(self):
        reply = self.h.command("plan.save", {"scene": "交战场景甲"})
        plan = self.h.entity(self.h.ref(reply, "TestPlan"))
        self.assertIsNone(plan["duration_s"])
        self.assertIsNone(plan["repeats"])
        self.assertEqual(set(plan["metrics"]), set(METRIC_KEYS))

    def test_split_closes_the_old_segment_at_one_boundary(self):
        session = self.h.session()
        self.h.clock.advance(5)
        reply = self.h.command(
            "session.split",
            {
                "session_id": session["session_id"],
                "expected_segment_id": session["segment_id"],
                "expected_segment_revision": session["segment_revision"],
                "expected_binding_revision": session["binding_revision"],
            },
        )
        closed = self.h.entity(session["segment_id"])
        new_segment = self.h.entity(reply.body["data"]["segment_id"])
        new_binding = self.h.entity(new_segment["binding_id"])
        old_binding = self.h.entity(session["binding_id"])
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["end_s"], 5.0)
        self.assertEqual(new_segment["status"], "open")
        self.assertEqual(new_binding["supersedes_id"], old_binding["id"])
        self.assertEqual(new_binding["applied_id"], old_binding["applied_id"])
        self.assertEqual(self.h.entity(session["session_id"])["active_segment_id"], new_segment["id"])

    def test_end_closes_the_session_and_segment(self):
        session = self.h.session()
        self.h.clock.advance(12)
        reply = self.h.command(
            "session.end",
            {
                "session_id": session["session_id"],
                "expected_segment_id": session["segment_id"],
                "expected_segment_revision": session["segment_revision"],
                "expected_binding_revision": session["binding_revision"],
            },
        )
        self.assertEqual(self.h.entity(session["session_id"])["status"], "closed")
        self.assertEqual(self.h.entity(session["segment_id"])["end_s"], 12.0)
        with self.assertRaises(DemoError):
            self.h.command(
                "session.end",
                {
                    "session_id": session["session_id"],
                    "expected_segment_id": session["segment_id"],
                    "expected_segment_revision": session["segment_revision"],
                    "expected_binding_revision": session["binding_revision"],
                },
            )

    def test_binding_revision_appends_without_rewriting_the_old_binding(self):
        a1 = self.h.adopt()
        session = self.h.session(applied_id=a1)
        original = self.h.entity(session["binding_id"])
        reply = self.h.command(
            "binding.revise",
            {
                "segment_id": session["segment_id"],
                "expected_binding_revision": session["binding_revision"],
                "applied_id": None,
                "reason": "历史补绑：该区段没有确认配置",
            },
        )
        revised = self.h.entity(self.h.ref(reply, "Binding"))
        segment = self.h.entity(session["segment_id"])
        self.assertEqual(revised["supersedes_id"], original["id"])
        self.assertIsNone(revised["applied_id"])
        self.assertEqual(revised["configuration_state"], "unverified")
        self.assertEqual(segment["binding_id"], revised["id"])
        self.assertEqual(self.h.entity(original["id"]), original)

    def test_stale_binding_revision_is_rejected(self):
        session = self.h.session()
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "binding.revise",
                {
                    "segment_id": session["segment_id"],
                    "expected_binding_revision": session["binding_revision"] + 5,
                    "applied_id": None,
                },
            )
        self.assertEqual(caught.exception.code, "binding_conflict")

    def test_open_segment_cannot_be_analyzed(self):
        session = self.h.session()
        workspace = self.h.workspace()
        scope = Scope("mw2-rtx4060", workspace["workspace_epoch"])
        with self.assertRaises(DemoError) as caught:
            self.h.service.store.mutate(
                Command(
                    "analyze-open",
                    "mw2-rtx4060",
                    workspace["workspace_epoch"],
                    workspace["revision"],
                    "test.analyze",
                    {},
                ),
                lambda tx: require_closed(
                    tx,
                    session["segment_id"],
                    session["segment_revision"],
                    session["binding_revision"],
                ),
            )
        self.assertEqual(caught.exception.code, "segment_open")
