"""T06: device context revisions and simulated identification."""

from demo.app.contracts import DemoError, Scope
from demo.tests.support import DemoCase


class DeviceTests(DemoCase):
    def test_check_fills_the_fixed_local_profile_without_changing_the_game(self):
        self.assertIn("device.check", self.h.service.registered_actions())
        original = self.h.entity(self.h.workspace()["context_id"])
        reply = self.h.command("device.check", {})
        context = self.h.entity(self.h.workspace()["context_id"])
        scan = self.h.entity(self.h.ref(reply, "DeviceScan"))

        self.assertEqual(context["cpu"], "Intel Core i7-14650HX")
        self.assertEqual(context["gpu_name"], "NVIDIA GeForce RTX 4060 Laptop GPU")
        self.assertEqual(context["memory_gb"], 16)
        self.assertEqual(context["vram_gb"], 8)
        self.assertEqual(context["driver_version"], "621.01")
        self.assertEqual(context["display_refresh_hz"], 165)
        self.assertEqual(context["game_name"], original["game_name"])
        self.assertEqual(context["resolution"], original["resolution"])
        self.assertEqual(scan["data_origin"], "simulated")
        self.assertEqual(scan["recognized"]["os_name"], "Windows 11 专业版")
        self.assertEqual(scan["recognized"]["desktop_resolution"],
                         {"width": 2560, "height": 1600})
        self.assertIsNone(self.h.entity(original["id"])["cpu"])
        self.assertFalse(any(ref["kind"] == "InformationRequest" for ref in reply.body["refs"]))

    def test_checked_device_profile_survives_service_restart(self):
        self.assertIn("device.check", self.h.service.registered_actions())
        self.h.command("device.check", {})
        saved_id = self.h.workspace()["context_id"]
        self.h.reopen()
        self.assertEqual(self.h.workspace()["context_id"], saved_id)
        self.assertEqual(self.h.entity(saved_id)["vram_gb"], 8)

    def test_rechecking_unchanged_device_keeps_the_context_reference(self):
        self.h.command("device.check", {})
        context_id = self.h.workspace()["context_id"]
        self.h.command("device.check", {})
        self.assertEqual(self.h.workspace()["context_id"], context_id)

    def test_custom_gpu_keeps_sample_unverified(self):
        original = self.h.workspace()["context_id"]
        reply = self.h.command(
            "context.save",
            {
                "expected_context_id": original,
                "fields": {"gpu_name": "演示者填写的其他显卡"},
            },
        )
        context = self.h.entity(self.h.ref(reply, "ContextSnapshot"))
        self.assertEqual(context["applicability"], "needs_confirmation")
        self.assertEqual(context["data_origin"], "user_declared")
        self.assertIsNone(context["cpu"])
        self.assertEqual(context["supersedes_id"], original)
        self.assertEqual(context["revision"], 2)

    def test_stale_context_pointer_is_rejected(self):
        original = self.h.workspace()["context_id"]
        self.h.command(
            "context.save",
            {"expected_context_id": original, "fields": {"gpu_name": "显卡甲"}},
        )
        with self.assertRaises(DemoError) as caught:
            self.h.command(
                "context.save",
                {"expected_context_id": original, "fields": {"gpu_name": "显卡乙"}},
            )
        self.assertEqual(caught.exception.code, "context_conflict")
        current = self.h.entity(self.h.workspace()["context_id"])
        self.assertEqual(current["gpu_name"], "显卡甲")

    def test_restore_preset_returns_to_the_fixture_background(self):
        original = self.h.workspace()["context_id"]
        changed = self.h.command(
            "context.save",
            {
                "expected_context_id": original,
                "fields": {"gpu_name": "其他显卡", "resolution": {"width": 1280, "height": 720}},
            },
        )
        custom_id = self.h.ref(changed, "ContextSnapshot")
        restored = self.h.command(
            "context.save",
            {
                "expected_context_id": custom_id,
                "restore_preset": True,
            },
        )
        context = self.h.entity(self.h.ref(restored, "ContextSnapshot"))
        self.assertEqual(context["gpu_name"], "NVIDIA GeForce RTX 4060")
        self.assertEqual(context["resolution"]["width"], 1920)
        self.assertEqual(context["applicability"], "preset")
        self.assertEqual(context["data_origin"], "fixture")

    def test_invalid_resolution_is_rejected(self):
        original = self.h.workspace()["context_id"]
        with self.assertRaises(DemoError):
            self.h.command(
                "context.save",
                {
                    "expected_context_id": original,
                    "fields": {"resolution": {"width": 1920}},
                },
            )
        self.assertEqual(self.h.workspace()["context_id"], original)

    def test_scan_is_simulated_and_asks_only_missing_fields(self):
        reply = self.h.command("device.scan", {})
        scan = self.h.entity(self.h.ref(reply, "DeviceScan"))
        request = self.h.entity(self.h.ref(reply, "InformationRequest"))
        self.assertEqual(scan["data_origin"], "simulated")
        self.assertIn("cpu", scan["unknown_fields"])
        self.assertIn("模拟", scan["note"])
        asked = [item["field"] for item in request["fields"]]
        self.assertIn("cpu", asked)
        self.assertNotIn("gpu_name", asked)
        self.assertEqual(request["status"], "open")

    def test_repeated_scan_does_not_duplicate_questions(self):
        first = self.h.command("device.scan", {})
        request_id = self.h.ref(first, "InformationRequest")
        second = self.h.command("device.scan", {})
        self.assertEqual(self.h.ref(second, "InformationRequest"), request_id)
        workspace = self.h.workspace()
        scope = Scope("mw2-rtx4060", workspace["workspace_epoch"])
        self.assertEqual(len(self.h.service.store.find(scope, "InformationRequest")), 1)
