"""T20: active presentation story, archived isolation, restarts and replay."""

from demo.app.contracts import Command, DemoError, Scope
from demo.tests.support import DemoCase


def _events(harness, scenario):
    workspace = harness.workspace(scenario)
    return harness.service.history(
        Scope(scenario, workspace["workspace_epoch"]), limit=200
    )["events"]


class EndToEndTests(DemoCase):
    def test_active_story_completes_with_a_distinct_adoption_chain(self):
        story = self.h.story(accepted=True)
        self.assertEqual(len({story["a0_id"], story["a1_id"], story["a2_id"]}), 3)
        outcome = self.h.entity(story["outcome_id"])
        comparison = self.h.entity(story["comparison_id"])
        self.assertEqual(outcome["scenario_id"], "mw2-rtx4060")
        self.assertIs(outcome["accepted"], True)
        self.assertTrue(comparison["comparable"])
        self.assertEqual(comparison["scenario_id"], "mw2-rtx4060")
        self.assertEqual(self.h.service.store.find(Scope("fh6-rtx5090", 1), "Outcome"), [])

    def test_rejected_active_story_keeps_its_settings_and_rollback_history(self):
        story = self.h.story(accepted=False)
        settings = self.h.entity(story["a2_id"])["settings"]
        self.assertIn("mw2.shadow_quality", settings)
        self.assertTrue(all(key.startswith("mw2.") for key in settings))
        rollback = self.h.entity(story["rollback_id"])
        self.assertEqual(rollback["rollback_target_id"], story["a1_id"])
        self.assertEqual(rollback["parent_id"], story["a2_id"])
        self.assertFalse(self.h.entity(story["outcome_id"])["accepted"])

    def test_archived_commands_and_cross_scope_entity_reads_are_rejected(self):
        story = self.h.story(accepted=True)
        before = self.h.workspace()
        for action, payload in (
            ("session.create", {"context_id": before["context_id"], "applied_id": story["a1_id"]}),
            ("comparison.freeze", {"mode": "fixed", "threshold_ms": 50.0, "baseline_summary_ids": [story["s1_id"]]}),
        ):
            with self.subTest(action=action), self.assertRaises(DemoError) as caught:
                self.h.service.command(Command("archive-" + action, "fh6-rtx5090", 1, 1, action, payload))
            self.assertEqual(caught.exception.code, "unsupported_game")
        with self.assertRaises(DemoError) as scoped:
            self.h.service.store.entity(Scope("fh6-rtx5090", 1), story["a1_id"])
        self.assertEqual(scoped.exception.code, "not_found")
        with self.assertRaises(DemoError) as public:
            self.h.service.entity(Scope("fh6-rtx5090", 1), story["a1_id"])
        self.assertEqual(public.exception.code, "unsupported_game")
        self.assertEqual(self.h.workspace(), before)

    def test_restart_preserves_the_story_and_replays_the_last_command(self):
        story = self.h.story(accepted=True)
        upload_id = self.h.upload(
            b"timestamp_ms,interval_ms\n0,16.6\n", filename="restart.csv"
        )
        payload = b"timestamp_ms,interval_ms\n0,16.6\n"
        saved = self.h.command(
            "plan.save", {"scene": "重启重放", "duration_s": 60, "repeats": 1}
        )
        plan_id = saved.body["refs"][0]["id"]
        revision = self.h.workspace()["revision"]
        events = _events(self.h, "mw2-rtx4060")
        replay_command = self.h.last_command

        self.h.reopen()

        self.assertEqual(self.h.workspace()["revision"], revision)
        self.assertEqual(self.h.workspace()["current_applied_id"], story["a2_id"])
        self.assertEqual(self.h.entity(plan_id)["scene"], "重启重放")
        self.assertEqual(len(_events(self.h, "mw2-rtx4060")), len(events))
        self.assertEqual(self.h.service.files.read(upload_id), payload)
        replay = self.h.service.command(replay_command)
        self.assertEqual(replay.body, saved.body)
        self.assertEqual(self.h.workspace()["revision"], revision)
        scope = Scope("mw2-rtx4060", self.h.workspace()["workspace_epoch"])
        statuses = {task["status"] for task in self.h.service.store.tasks(scope)}
        self.assertTrue(statuses <= {"completed", "partial", "failed", "cancelled", "interrupted"})

    def test_reset_with_an_open_capture_invalidates_the_old_epoch(self):
        applied = self.h.adopt()
        session = self.h.session(applied_id=applied)
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
        task_id = reply.body["task_id"]
        old_workspace = self.h.workspace()
        reset = self.h.command("demo.reset", {"scope": "current"})
        new_epoch = reset.body["data"]["workspaces"][0]["workspace_epoch"]
        self.assertEqual(new_epoch, old_workspace["workspace_epoch"] + 1)
        self.h.service.tasks.tick()
        scope = Scope("mw2-rtx4060", new_epoch)
        self.assertEqual(self.h.service.store.tasks(scope), [])
        with self.assertRaises(DemoError):
            self.h.task(task_id)
        with self.assertRaises(DemoError) as stale:
            self.h.command(
                "plan.save",
                {"scene": "过期会话"},
                epoch=old_workspace["workspace_epoch"],
            )
        self.assertEqual(stale.exception.code, "stale_epoch")
        self.assertEqual(
            [p for p in (self.h.runtime_root / "uploads").rglob("*") if p.is_file()],
            [],
        )
