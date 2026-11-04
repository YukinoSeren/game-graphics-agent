"""T03: explicit command dispatch, validators, and scoped queries."""

from demo.app.contracts import Command, DemoError, DomainResult
from demo.app.service import require_fields
from demo.tests.support import DemoCase


class ServiceTests(DemoCase):
    def test_missing_handler_is_not_reported_as_completed(self):
        with self.assertRaises(DemoError) as caught:
            self.h.command("not.registered")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "unknown_action")
        self.assertEqual(self.h.workspace()["revision"], 1)

    def test_registered_action_runs_inside_the_transaction(self):
        def validate(payload):
            return require_fields(payload, required=("text",))

        def handle(ctx, payload):
            item = ctx.tx.insert("Note", {"text": payload["text"]}, origin="user_declared")
            return DomainResult(201, [{"kind": "Note", "id": item["id"]}])

        self.h.service.register("test.note", validate, handle)
        reply = self.h.command("test.note", {"text": "演示"})
        self.assertEqual(reply.status, 201)
        self.assertEqual(reply.body["demo_only"], True)
        self.assertEqual(reply.body["workspace_revision"], 2)
        self.assertEqual(self.h.entity(self.h.ref(reply, "Note"))["text"], "演示")

    def test_unknown_fields_are_rejected_without_writing(self):
        def validate(payload):
            return require_fields(payload, required=("text",))

        self.h.service.register("test.note", validate, lambda ctx, payload: DomainResult(200))
        with self.assertRaises(DemoError) as caught:
            self.h.command("test.note", {"text": "演示", "extra": True})
        self.assertEqual(caught.exception.code, "unknown_field")
        self.assertEqual(self.h.workspace()["revision"], 1)

    def test_missing_required_field_is_rejected_without_writing(self):
        def validate(payload):
            return require_fields(payload, required=("text",))

        self.h.service.register("test.note", validate, lambda ctx, payload: DomainResult(200))
        with self.assertRaises(DemoError) as caught:
            self.h.command("test.note", {})
        self.assertEqual(caught.exception.code, "missing_field")
        self.assertEqual(self.h.workspace()["revision"], 1)

    def test_registration_rejects_duplicate_actions(self):
        self.h.service.register("test.note", lambda payload: payload, lambda ctx, payload: DomainResult())
        with self.assertRaises(RuntimeError):
            self.h.service.register(
                "test.note", lambda payload: payload, lambda ctx, payload: DomainResult()
            )

    def test_unknown_and_archived_scenarios_cannot_execute_commands(self):
        before = self.h.workspace()
        for scenario in ("not-a-case", "fh6-rtx5090"):
            with self.subTest(scenario=scenario), self.assertRaises(DemoError) as caught:
                self.h.service.command(Command("blocked-" + scenario, scenario, 1, 1, "plan.save", {"scene": "blocked"}))
            self.assertEqual(caught.exception.status, 404)
            self.assertEqual(caught.exception.code, "unsupported_game")
        self.assertEqual(self.h.workspace(), before)
