"""T02: scoped persistence, atomic transactions, and replayable commands."""

import tempfile
import threading
import unittest
from pathlib import Path

from demo.app.contracts import Command, DemoError, DomainResult, Scope
from demo.app.scenarios import ScenarioCatalog
from demo.app.store import DemoStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def make_store(directory: str) -> DemoStore:
    store = DemoStore(Path(directory), clock=None)
    store.initialize(ScenarioCatalog.load(FIXTURES))
    return store


class StoreTests(unittest.TestCase):
    def test_retry_replays_before_stale_workspace_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            cmd = Command("retry-1", "mw2-rtx4060", 1, 1, "test.save", {})

            def save(tx):
                item = tx.insert("Note", {"text": "演示"}, origin="user_declared")
                return DomainResult(201, [{"kind": "Note", "id": item["id"]}])

            first = store.mutate(cmd, save)
            second = store.mutate(cmd, save)
            self.assertEqual(first, second)
            self.assertEqual(store.workspace("mw2-rtx4060")["revision"], 2)

    def test_failed_handler_leaves_no_entity_pointer_or_operation_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            cmd = Command("fail-1", "mw2-rtx4060", 1, 1, "test.save", {})

            def fail(tx):
                tx.insert("Note", {"text": "不应保存"}, origin="user_declared")
                raise DemoError("boom", "演示失败")

            with self.assertRaises(DemoError):
                store.mutate(cmd, fail)
            self.assertEqual(store.workspace("mw2-rtx4060")["revision"], 1)
            self.assertEqual(store.find(Scope("mw2-rtx4060", 1), "Note"), [])
            with self.assertRaises(DemoError):
                store.mutate(cmd, fail)
            self.assertEqual(store.find(Scope("mw2-rtx4060", 1), "Note"), [])

    def test_same_key_with_different_content_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            first = Command("dup-1", "mw2-rtx4060", 1, 1, "test.save", {"text": "一"})
            store.mutate(first, lambda tx: DomainResult(200))
            second = Command("dup-1", "mw2-rtx4060", 1, 2, "test.save", {"text": "二"})
            with self.assertRaises(DemoError) as caught:
                store.mutate(second, lambda tx: DomainResult(200))
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(caught.exception.code, "request_id_conflict")

    def test_cross_case_reference_is_rejected_without_partial_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)

            def save_a(tx):
                item = tx.insert("Note", {"text": "案例 A"}, origin="user_declared")
                return DomainResult(201, [{"kind": "Note", "id": item["id"]}])

            reply = store.mutate(
                Command("a-1", "mw2-rtx4060", 1, 1, "test.save", {}), save_a
            )
            note_id = reply.body["refs"][0]["id"]

            def steal(tx):
                tx.get(note_id)
                return DomainResult(200)

            with self.assertRaises(DemoError) as caught:
                store.mutate(Command("b-1", "fh6-rtx5090", 1, 1, "test.save", {}), steal)
            self.assertEqual(caught.exception.status, 404)
            self.assertEqual(store.workspace("mw2-rtx4060")["revision"], 2)
            self.assertEqual(store.entity(Scope("mw2-rtx4060", 1), note_id)["text"], "案例 A")
            self.assertEqual(store.find(Scope("fh6-rtx5090", 1), "Note"), [])

    def test_stale_epoch_cannot_write_into_the_new_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            store.bump_epoch("mw2-rtx4060")
            with self.assertRaises(DemoError) as caught:
                store.mutate(
                    Command("old-1", "mw2-rtx4060", 1, 1, "test.save", {}),
                    lambda tx: DomainResult(200),
                )
            self.assertEqual(caught.exception.code, "stale_epoch")
            reply = store.mutate(
                Command("new-1", "mw2-rtx4060", 2, 1, "test.save", {}),
                lambda tx: DomainResult(200),
            )
            self.assertEqual(reply.body["workspace_epoch"], 2)

    def test_concurrent_same_key_creates_one_entity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            barrier = threading.Barrier(2)
            replies = []
            errors = []

            def save(tx):
                tx.insert("Note", {"text": "并发"}, origin="user_declared")
                return DomainResult(201)

            def worker():
                try:
                    barrier.wait(timeout=5)
                    replies.append(
                        store.mutate(
                            Command("race-1", "mw2-rtx4060", 1, 1, "test.save", {}), save
                        )
                    )
                except Exception as exc:  # pragma: no cover - failure detail
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(len(replies), 2)
            self.assertEqual(len(store.find(Scope("mw2-rtx4060", 1), "Note")), 1)
            self.assertEqual(store.workspace("mw2-rtx4060")["revision"], 2)

    def test_boolean_revision_is_not_an_integer_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            with self.assertRaises(DemoError) as caught:
                store.mutate(
                    Command("bool-1", "mw2-rtx4060", 1, True, "test.save", {}),
                    lambda tx: DomainResult(200),
                )
            self.assertEqual(caught.exception.code, "invalid_revision")

    def test_non_finite_payload_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            with self.assertRaises(DemoError) as caught:
                store.mutate(
                    Command("nan-1", "mw2-rtx4060", 1, 1, "test.save", {"v": float("inf")}),
                    lambda tx: DomainResult(200),
                )
            self.assertEqual(caught.exception.code, "invalid_payload")
            self.assertEqual(store.workspace("mw2-rtx4060")["revision"], 1)

    def test_initialize_is_idempotent_and_keeps_the_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            store.mutate(
                Command("init-1", "mw2-rtx4060", 1, 1, "test.save", {}),
                lambda tx: DomainResult(200),
            )
            store.initialize(ScenarioCatalog.load(FIXTURES))
            self.assertEqual(store.workspace("mw2-rtx4060")["revision"], 2)
            context = store.entity(
                Scope("mw2-rtx4060", 1), store.workspace("mw2-rtx4060")["context_id"]
            )
            self.assertEqual(context["gpu_name"], "NVIDIA GeForce RTX 4060")
            self.assertIsNone(context["cpu"])

    def test_only_active_workspace_is_initialized_and_entities_remain_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(directory)
            a = store.workspace("mw2-rtx4060")
            context_a = store.entity(Scope("mw2-rtx4060", 1), a["context_id"])
            self.assertIn("4060", context_a["gpu_name"])
            with self.assertRaises(DemoError) as missing:
                store.workspace("fh6-rtx5090")
            self.assertEqual(missing.exception.code, "unknown_scenario")
            with self.assertRaises(DemoError):
                store.entity(Scope("fh6-rtx5090", 1), a["context_id"])


if __name__ == "__main__":
    unittest.main()
