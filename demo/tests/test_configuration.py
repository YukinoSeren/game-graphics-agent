"""T09: adopted configuration versions, edits, rollback, and history."""

from demo.app.contracts import Command, DemoError, Scope
from demo.tests.support import DemoCase


class ConfigurationTests(DemoCase):
    def test_rollback_adds_a_version_instead_of_erasing_the_edit(self):
        a1 = self.h.adopt()
        edited = self.h.command(
            "configuration.edit",
            {
                "expected_current_id": a1,
                "changes": {"mw2.shadow_quality": "low"},
                "confirmation": "我已手动调整",
            },
        )
        a2 = self.h.ref(edited, "AppliedConfiguration")
        reply = self.h.command(
            "configuration.rollback",
            {
                "expected_current_id": a2,
                "target_id": a1,
                "confirmation": "我已手动恢复",
            },
        )
        a3 = self.h.entity(self.h.ref(reply, "AppliedConfiguration"))
        self.assertEqual(a3["parent_id"], a2)
        self.assertEqual(a3["rollback_target_id"], a1)
        self.assertEqual(a3["settings"], self.h.entity(a1)["settings"])
        self.assertEqual(self.h.entity(a2)["settings"]["mw2.shadow_quality"], "low")

    def test_baseline_then_adoption_form_a_parent_chain(self):
        a1 = self.h.entity(self.h.adopt())
        a0 = self.h.entity(a1["parent_id"])
        self.assertEqual(a0["origin"], "baseline")
        self.assertEqual(a1["origin"], "recommendation")
        self.assertEqual(a1["activation"], "user_declared")
        self.assertNotIn("active_confirmed", a1)
        self.assertEqual(a1["settings"]["mw2.upscaling"], "off")

    def test_stale_current_pointer_is_rejected_without_a_new_version(self):
        a1 = self.h.adopt()
        a0 = self.h.entity(a1)["parent_id"]
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "configuration.edit",
                {
                    "expected_current_id": a0,
                    "changes": {"mw2.shadow_quality": "low"},
                    "confirmation": "我已手动调整",
                },
            )
        self.assertEqual(caught.exception.code, "configuration_conflict")
        self.assertEqual(self.h.workspace()["current_applied_id"], a1)

    def test_history_record_does_not_move_the_current_pointer(self):
        a1 = self.h.adopt()
        reply = self.h.command(
            "configuration.record_history",
            {
                "settings": {"mw2.shadow_quality": "medium", "mw2.texture_detail": "medium"},
                "occurred_at": "2026-10-01T20:00:00+08:00",
                "basis": "玩家回忆并手工补录",
            },
        )
        recorded = self.h.entity(self.h.ref(reply, "AppliedConfiguration"))
        self.assertEqual(recorded["origin"], "history_record")
        self.assertEqual(self.h.workspace()["current_applied_id"], a1)
        self.assertNotEqual(recorded["id"], a1)

    def test_out_of_catalog_value_is_rejected(self):
        a1 = self.h.adopt()
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "configuration.edit",
                {
                    "expected_current_id": a1,
                    "changes": {"mw2.shadow_quality": "ultra"},
                    "confirmation": "我已手动调整",
                },
            )
        self.assertEqual(caught.exception.code, "invalid_setting_value")
        self.assertEqual(self.h.workspace()["current_applied_id"], a1)

    def test_confirmation_is_required_for_adoption(self):
        self.h.recommendation()
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "configuration.adopt",
                {
                    "origin": "baseline",
                    "expected_current_id": None,
                    "settings": {"mw2.shadow_quality": "medium"},
                    "confirmation": "",
                },
            )
        self.assertEqual(caught.exception.code, "confirmation_required")
        self.assertIsNone(self.h.workspace()["current_applied_id"])

    def test_same_local_setting_name_creates_a_separate_declaration(self):
        payload = {
            "display_name": "演示自定义设置",
            "group": "自定义",
            "value_type": "quality",
            "allowed_values": ["on", "off"],
            "value": "on",
        }
        first = self.h.command("setting.register", payload)
        second = self.h.command("setting.register", payload)
        self.assertNotEqual(
            self.h.ref(first, "SettingDeclaration"),
            self.h.ref(second, "SettingDeclaration"),
        )
        catalog = self.h.service.catalog.get("mw2-rtx4060")
        self.assertNotIn(
            "演示自定义设置", [item["display_name"] for item in catalog["settings"]]
        )

    def test_archived_case_cannot_adopt_or_change_the_active_configuration(self):
        applied = self.h.adopt()
        before = self.h.workspace()
        command = Command("archive-adoption", "fh6-rtx5090", 1, 1, "configuration.adopt", {
            "origin": "baseline", "expected_current_id": None,
            "settings": {"fh6.reflection_quality": "medium"}, "confirmation": "manual",
        })
        with self.assertRaises(DemoError) as caught:
            self.h.service.command(command)
        self.assertEqual(caught.exception.code, "unsupported_game")
        self.assertEqual(self.h.workspace(), before)
        self.assertEqual(self.h.workspace()["current_applied_id"], applied)
        self.assertTrue(all(key.startswith("mw2.") for key in self.h.entity(applied)["settings"]))
        self.assertEqual(self.h.service.store.find(Scope("fh6-rtx5090", 1), "AppliedConfiguration"), [])

    def test_adopted_configuration_history_is_queryable(self):
        a1 = self.h.adopt()
        workspace = self.h.workspace()
        history = self.h.service.history(Scope("mw2-rtx4060", workspace["workspace_epoch"]))
        events = [event["event"] for event in history["events"]]
        self.assertIn("configuration.adopted", events)
        self.assertEqual(history["next_cursor"], history["events"][-1]["seq"])
