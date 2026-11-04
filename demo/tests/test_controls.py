"""T05: demo modes, scripts, task control, and scoped epoch reset."""

from pathlib import Path

from demo.app.contracts import DemoError, DomainResult, Scope
from demo.tests.support import DemoCase

PHASES = [
    {"key": "one", "label": "一", "seconds": 1.0},
    {"key": "two", "label": "二", "seconds": 1.0},
]


def publish(tx, task, phase):
    tx.insert("StageFact", {"phase": phase["key"]}, origin="simulated")
    return {}


class ControlTests(DemoCase):
    def test_reset_changes_epoch_without_touching_other_case(self):
        before_a = self.h.workspace()
        before_b = self.h.service.store.find(Scope("fh6-rtx5090", 1), "ContextSnapshot")
        self.h.command("demo.reset", {"scope": "current"})
        after = self.h.workspace()
        self.assertEqual(after["workspace_epoch"], before_a["workspace_epoch"] + 1)
        self.assertIsNone(after["current_applied_id"])
        self.assertEqual(self.h.service.store.find(Scope("fh6-rtx5090", 1), "ContextSnapshot"), before_b)

    def test_reset_all_advances_only_active_epochs(self):
        before = self.h.workspace()["workspace_epoch"]
        archived = self.h.service.store.find(Scope("fh6-rtx5090", 1), "ContextSnapshot")
        reply = self.h.command("demo.reset", {"scope": "all"})
        self.assertEqual(self.h.workspace()["workspace_epoch"], before + 1)
        self.assertEqual([item["scenario_id"] for item in reply.body["data"]["workspaces"]], ["mw2-rtx4060"])
        self.assertEqual(self.h.service.store.find(Scope("fh6-rtx5090", 1), "ContextSnapshot"), archived)

    def test_reset_removes_only_managed_uploaded_files(self):
        file_id = self.h.upload(b"demo file", "text/plain", filename="note.txt")
        path = self.h.service.files.path_for(file_id)
        self.assertTrue(Path(path).exists())
        self.h.command("demo.reset", {"scope": "current"})
        self.assertFalse(Path(path).exists())
        self.assertFalse((self.h.runtime_root / "uploads" / Path(path).name).exists())

    def test_mode_switch_is_persisted_and_validated(self):
        self.h.command("demo.mode", {"mode": "fast"})
        self.assertEqual(self.h.workspace()["mode"], "fast")
        with self.assertRaises(DemoError) as caught:
            self.h.command("demo.mode", {"mode": "turbo"})
        self.assertEqual(caught.exception.code, "invalid_mode")
        self.assertEqual(self.h.workspace()["mode"], "fast")

    def test_failure_script_must_exist(self):
        self.h.command("demo.script", {"script": "budget_short"})
        self.assertEqual(self.h.workspace()["script"], "budget_short")
        with self.assertRaises(DemoError) as caught:
            self.h.command("demo.script", {"script": "no-such-script"})
        self.assertEqual(caught.exception.code, "unknown_script")
        self.assertEqual(self.h.workspace()["script"], "budget_short")

    def test_task_advance_is_rejected_outside_step_mode(self):
        self.h.service.tasks.register("test.work", PHASES, publish)

        def start(ctx, payload):
            task = ctx.service.tasks.enqueue(ctx.tx, "test.work", payload)
            return DomainResult(202, [{"kind": "Task", "id": task["id"]}], task_id=task["id"])

        self.h.service.register("test.start", lambda payload: payload, start)
        task_id = self.h.ref(self.h.command("test.start"), "Task")
        with self.assertRaises(DemoError) as caught:
            self.h.command("task.advance", {"task_id": task_id})
        self.assertEqual(caught.exception.code, "mode_not_step")
        self.assertEqual(self.h.task(task_id)["phase_index"], 0)

    def test_step_mode_advances_only_on_explicit_command(self):
        self.h.service.tasks.register("test.work", PHASES, publish)

        def start(ctx, payload):
            task = ctx.service.tasks.enqueue(ctx.tx, "test.work", payload)
            return DomainResult(202, [{"kind": "Task", "id": task["id"]}], task_id=task["id"])

        self.h.service.register("test.start", lambda payload: payload, start)
        self.h.command("demo.mode", {"mode": "step"})
        task_id = self.h.ref(self.h.command("test.start"), "Task")
        for _ in range(5):
            self.h.clock.advance(10)
            self.assertFalse(self.h.service.tasks.tick())
        self.assertEqual(self.h.task(task_id)["phase_index"], 0)
        self.h.command("task.advance", {"task_id": task_id})
        self.assertEqual(self.h.task(task_id)["phase_index"], 1)
        self.h.command("task.advance", {"task_id": task_id})
        self.assertEqual(self.h.task(task_id)["status"], "completed")

    def test_reset_twice_keeps_advancing_the_epoch(self):
        first = self.h.workspace()["workspace_epoch"]
        self.h.command("demo.reset", {"scope": "current"})
        second = self.h.workspace()["workspace_epoch"]
        self.h.command("demo.reset", {"scope": "current"})
        third = self.h.workspace()["workspace_epoch"]
        self.assertEqual((second, third), (first + 1, first + 2))
