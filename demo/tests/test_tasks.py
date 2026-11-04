"""T05: staged simulated tasks, cancellation, resume, and recovery."""

from demo.app.contracts import DomainResult, Scope
from demo.tests.support import DemoCase

PHASES = [
    {"key": "discover", "label": "发现", "seconds": 1.0},
    {"key": "read", "label": "读取", "seconds": 1.0},
    {"key": "extract", "label": "提取", "seconds": 1.0},
]


def publish(tx, task, phase):
    tx.insert("StageFact", {"phase": phase["key"]}, origin="simulated")
    return {"detail": {"phase": phase["key"]}}


class TaskTests(DemoCase):
    def setUp(self):
        super().setUp()
        self.h.service.tasks.register("test.work", PHASES, publish)

        def start(ctx, payload):
            task = ctx.service.tasks.enqueue(ctx.tx, "test.work", payload)
            return DomainResult(
                202, [{"kind": "Task", "id": task["id"]}], task_id=task["id"]
            )

        self.h.service.register("test.start", lambda payload: payload, start)

    def _start(self, **payload):
        reply = self.h.command("test.start", payload)
        return self.h.ref(reply, "Task")

    def _facts(self):
        workspace = self.h.workspace()
        scope = Scope("mw2-rtx4060", workspace["workspace_epoch"])
        return self.h.service.store.find(scope, "StageFact")

    def test_queries_do_not_drive_task_phases(self):
        task_id = self._start()
        for _ in range(5):
            self.h.workspace()
            self.h.task(task_id)
        self.assertEqual(self.h.task(task_id)["phase_index"], 0)
        self.assertEqual(self._facts(), [])
        self.assertTrue(self.h.service.tasks.tick())
        self.assertEqual(self.h.task(task_id)["phase_index"], 1)
        self.assertEqual(len(self._facts()), 1)

    def test_each_tick_publishes_one_phase_and_completes(self):
        task_id = self._start()
        task = self.h.run(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual([fact["phase"] for fact in self._facts()], ["discover", "read", "extract"])
        self.assertTrue(all(phase["status"] == "done" for phase in task["phases"]))

    def test_cancel_stops_further_publication(self):
        task_id = self._start()
        self.h.service.tasks.tick()
        self.h.command("task.cancel", {"task_id": task_id})
        for _ in range(10):
            self.h.clock.advance(5)
            self.h.service.tasks.tick()
        task = self.h.task(task_id)
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(len(self._facts()), 1)

    def test_resume_creates_a_linked_task_without_republishing(self):
        task_id = self._start()
        self.h.service.tasks.tick()
        self.h.command("task.cancel", {"task_id": task_id})
        reply = self.h.command("task.resume", {"task_id": task_id})
        resumed_id = self.h.ref(reply, "Task")
        resumed = self.h.task(resumed_id)
        self.assertEqual(resumed["parent_id"], task_id)
        self.assertEqual(
            [phase["key"] for phase in resumed["phases"]], ["read", "extract"]
        )
        self.h.run(resumed_id)
        self.assertEqual(
            [fact["phase"] for fact in self._facts()],
            ["discover", "read", "extract"],
        )

    def test_waiting_input_releases_the_execution_slot(self):
        def publish_waiting(tx, task, phase):
            if phase["key"] == "discover":
                return {"status": "waiting_input", "detail": {"needs": "cpu"}}
            tx.insert("StageFact", {"phase": phase["key"]}, origin="simulated")
            return {}

        self.h.service.tasks._definitions["test.work"] = (list(PHASES), publish_waiting)
        first = self._start()
        self.h.service.tasks.tick()
        self.assertEqual(self.h.task(first)["status"], "waiting_input")
        second = self._start()
        self.h.service.tasks.tick()
        self.assertEqual(self.h.task(second)["phase_index"], 1)

    def test_restart_marks_incomplete_tasks_interrupted_and_resume_continues(self):
        task_id = self._start()
        self.h.service.tasks.tick()
        self.h.reopen()
        self.h.service.tasks.register("test.work", PHASES, publish)
        task = self.h.task(task_id)
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["phase_index"], 1)
        self.assertEqual(len(self._facts()), 1)
        reply = self.h.command("task.resume", {"task_id": task_id})
        resumed_id = self.h.ref(reply, "Task")
        self.h.run(resumed_id)
        self.assertEqual(
            [fact["phase"] for fact in self._facts()],
            ["discover", "read", "extract"],
        )

    def test_old_epoch_task_cannot_publish_after_reset(self):
        task_id = self._start()
        self.h.service.tasks.tick()
        old_epoch = self.h.workspace()["workspace_epoch"]
        self.h.command("demo.reset", {"scope": "current"})
        self.assertEqual(self.h.workspace()["workspace_epoch"], old_epoch + 1)
        with self.assertRaises(Exception):
            self.h.task(task_id)
        advanced = self.h.service.store.advance_task(
            Scope("mw2-rtx4060", old_epoch), task_id, 1, publish
        )
        self.assertFalse(advanced)
        new_scope = Scope("mw2-rtx4060", old_epoch + 1)
        self.assertEqual(self.h.service.store.find(new_scope, "StageFact"), [])
