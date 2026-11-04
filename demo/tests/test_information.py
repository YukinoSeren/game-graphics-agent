"""T06: answers and corrections never become automatic probes."""

from demo.app.contracts import DemoError
from demo.tests.support import DemoCase


class InformationTests(DemoCase):
    def _scan(self):
        reply = self.h.command("device.scan", {})
        return self.h.ref(reply, "InformationRequest")

    def _answer(self, request_id, answers, skipped=None):
        request = self.h.entity(request_id)
        context = self.h.entity(self.h.workspace()["context_id"])
        return self.h.command(
            "information.answer",
            {
                "request_id": request_id,
                "question_revision": request["revision"],
                "context_revision": context["revision"],
                "answers": answers,
                "skipped_fields": skipped or [],
            },
        )

    def test_answer_is_recorded_as_a_player_declaration(self):
        request_id = self._scan()
        reply = self._answer(request_id, {"cpu": "演示者填写的 CPU"})
        context = self.h.entity(self.h.ref(reply, "ContextSnapshot"))
        self.assertEqual(context["cpu"], "演示者填写的 CPU")
        self.assertEqual(context["data_origin"], "user_declared")
        self.assertIn("cpu", context["declared_fields"])
        self.assertIsNone(context["driver_version"])
        request = self.h.entity(request_id)
        self.assertEqual(request["revision"], 2)
        self.assertEqual(request["answers"]["cpu"], "演示者填写的 CPU")
        remaining = [
            item["field"]
            for item in request["fields"]
            if item["field"] not in request["answers"]
            and item["field"] not in request["skipped_fields"]
        ]
        self.assertNotIn("cpu", remaining)

    def test_skipped_fields_stay_unknown_and_are_not_asked_again(self):
        request_id = self._scan()
        self._answer(request_id, {}, skipped=["memory_gb"])
        context = self.h.entity(self.h.workspace()["context_id"])
        self.assertIsNone(context["memory_gb"])
        request = self.h.entity(request_id)
        self.assertIn("memory_gb", request["skipped_fields"])
        again = self.h.command("device.scan", {})
        self.assertEqual(self.h.ref(again, "InformationRequest"), request_id)

    def test_question_revision_conflict_is_rejected(self):
        request_id = self._scan()
        request = self.h.entity(request_id)
        context = self.h.entity(self.h.workspace()["context_id"])
        self._answer(request_id, {"cpu": "甲"})
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "information.answer",
                {
                    "request_id": request_id,
                    "question_revision": request["revision"],
                    "context_revision": context["revision"],
                    "answers": {"cpu": "乙"},
                },
            )
        self.assertEqual(caught.exception.code, "question_conflict")

    def test_context_revision_conflict_is_rejected(self):
        request_id = self._scan()
        request = self.h.entity(request_id)
        context = self.h.entity(self.h.workspace()["context_id"])
        self.h.command(
            "context.save",
            {
                "expected_context_id": context["id"],
                "fields": {"gpu_name": "另一块显卡"},
            },
        )
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "information.answer",
                {
                    "request_id": request_id,
                    "question_revision": request["revision"],
                    "context_revision": context["revision"],
                    "answers": {"cpu": "甲"},
                },
            )
        self.assertEqual(caught.exception.code, "context_conflict")

    def test_unasked_field_is_rejected(self):
        request_id = self._scan()
        with self.assertRaises(DemoError) as caught:
            self._answer(request_id, {"gpu_name": "未提问的字段"})
        self.assertEqual(caught.exception.code, "unknown_question")

    def test_amend_after_resolution_creates_a_new_context_and_keeps_history(self):
        request_id = self._scan()
        first = self._answer(
            request_id,
            {"cpu": "旧 CPU", "memory_gb": 16, "vram_gb": 8,
             "driver_version": "旧驱动", "game_version": "旧版本",
             "display_refresh_hz": 60},
        )
        first_context_id = self.h.ref(first, "ContextSnapshot")
        first_context = self.h.entity(first_context_id)
        self.assertEqual(self.h.entity(request_id)["status"], "resolved")
        amended = self.h.command(
            "information.amend",
            {
                "request_id": request_id,
                "question_revision": self.h.entity(request_id)["revision"],
                "context_revision": first_context["revision"],
                "answers": {"cpu": "新 CPU"},
            },
        )
        new_context = self.h.entity(self.h.ref(amended, "ContextSnapshot"))
        self.assertEqual(new_context["cpu"], "新 CPU")
        self.assertNotEqual(new_context["id"], first_context_id)
        self.assertEqual(self.h.entity(first_context_id)["cpu"], "旧 CPU")
        self.assertEqual(self.h.entity(first_context_id)["revision"], first_context["revision"])

    def test_answer_field_is_validated(self):
        request_id = self._scan()
        with self.assertRaises(DemoError):
            self._answer(request_id, {"memory_gb": -4})
