"""T07: evidence collection stages, gaps, and revision behavior."""

from demo.app.contracts import DemoError, Scope
from demo.tests.support import DemoCase


class KnowledgeTests(DemoCase):
    def test_collect_exports_inspectable_demo_sources(self):
        snapshot_id = self.h.knowledge()
        snapshot = self.h.entity(snapshot_id)
        sources = [self.h.entity(i) for i in snapshot["source_ids"]]
        self.assertTrue(sources)
        self.assertTrue(all(x["url"].startswith("demo://") for x in sources))
        self.assertEqual(
            snapshot["accepted_count"],
            sum(x["status"] == "accepted" for x in snapshot["records"]),
        )

    def test_each_stage_publishes_incremental_facts(self):
        reply = self.h.command("knowledge.collect", {})
        task_id = reply.body["task_id"]
        task = self.h.task(task_id)
        self.assertEqual(task["phase"], "discover")
        seen = []
        for _ in range(6):
            self.h.clock.advance(2)
            self.h.service.tasks.tick()
            task = self.h.task(task_id)
            seen.append((task["phase"], len(task["result"].get("source_ids", []))))
        self.assertEqual(task["status"], "completed")
        self.assertEqual(seen[0][1], 15)
        self.assertEqual(len(task["result"]["record_ids"]), len(set(task["result"]["record_ids"])))
        self.assertEqual(task["result"]["gap_ids"], [])

    def test_archived_catalog_preserves_repost_relation_without_exposing_a_workspace(self):
        archived = self.h.service.catalog.get("fh6-rtx5090")
        sources = archived["sources"]
        by_id = {source["id"]: source for source in sources}
        reposts = [source for source in sources if source["relationship"] == "repost"]
        self.assertEqual(len(sources), 4)
        self.assertEqual(len(reposts), 1)
        self.assertEqual(sum(source["relationship"] == "original" for source in sources), 3)
        self.assertTrue(reposts[0]["related_ids"])
        for source_id in reposts[0]["related_ids"]:
            self.assertEqual(by_id[source_id]["relationship"], "original")
        with self.assertRaises(DemoError) as caught:
            self.h.workspace("fh6-rtx5090")
        self.assertEqual(caught.exception.code, "unsupported_game")

    def test_short_budget_leaves_a_partial_snapshot(self):
        snapshot = self.h.entity(self.h.knowledge(budget={"max_stages": 4}))
        self.assertEqual(snapshot["status"], "partial")
        self.assertGreater(snapshot["accepted_count"], 0)
        self.assertEqual(snapshot["gap_ids"], [])
        self.assertTrue(snapshot["source_ids"])

    def test_complete_pack_rejects_followup_without_inventing_a_gap_or_revision(self):
        knowledge_id = self.h.knowledge()
        original = self.h.entity(knowledge_id)
        before_workspace = self.h.workspace()
        self.assertEqual(original["gap_ids"], [])
        self.assertEqual(len(original["source_ids"]), 15)
        with self.assertRaises(DemoError) as caught:
            self.h.command("knowledge.followup", {"knowledge_id": knowledge_id})
        self.assertEqual(caught.exception.code, "no_gap")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.h.entity(knowledge_id), original)
        self.assertEqual(self.h.workspace(), before_workspace)

    def test_update_creates_a_new_revision_without_rewriting_history(self):
        knowledge_id = self.h.knowledge()
        original = self.h.entity(knowledge_id)
        reply = self.h.command("knowledge.update", {"knowledge_id": knowledge_id})
        new_id = self.h.ref(reply, "KnowledgeSnapshot")
        updated = self.h.entity(new_id)
        self.assertEqual(updated["supersedes_id"], knowledge_id)
        self.assertEqual(updated["revision"], original["revision"] + 1)
        self.assertEqual(self.h.entity(knowledge_id)["fingerprint"], original["fingerprint"])

    def test_second_heavy_task_is_rejected_while_one_is_queued(self):
        self.h.command("knowledge.collect", {})
        with self.assertRaises(DemoError) as caught:
            self.h.command("knowledge.collect", {})
        self.assertEqual(caught.exception.code, "task_busy")

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(DemoError) as caught:
            self.h.command("knowledge.collect", {"source_ids": ["demo://nope"]})
        self.assertEqual(caught.exception.code, "unknown_source")

    def test_collect_does_not_create_archived_case_sources_or_activate_the_case(self):
        scope = Scope("fh6-rtx5090", 1)
        before = self.h.service.store.find(scope, "Source")
        self.h.knowledge()
        self.assertEqual(self.h.service.store.find(scope, "Source"), before)
        self.assertEqual(before, [])
        with self.assertRaises(DemoError) as caught:
            self.h.workspace("fh6-rtx5090")
        self.assertEqual(caught.exception.code, "unsupported_game")
