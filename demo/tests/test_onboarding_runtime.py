"""Regression coverage for the conversation-led, fixed-data presentation."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from urllib.request import urlopen

from demo.app.contracts import Scope
from demo.tests.support import DemoCase, running_http


class OnboardingRuntimeTests(DemoCase):
    def entities(self, kind):
        workspace = self.h.workspace()
        return self.h.service.find(Scope("mw2-rtx4060", workspace["workspace_epoch"]), kind)

    def test_bootstrap_and_health_identify_the_required_runtime(self):
        with running_http(self.h.service) as base:
            for endpoint in ("health", "bootstrap"):
                with urlopen(f"{base}/api/demo/v1/{endpoint}") as response:
                    payload = json.load(response)
                self.assertEqual(payload.get("ui_contract"), "conversation/2")
                self.assertTrue({"device.check", "intent.submit", "log.capture_recent", "feedback.review"}.issubset(payload.get("capabilities", [])))

    def test_device_check_returns_full_fixed_profile_without_adding_a_game(self):
        self.h.command("device.check", {})
        scan = self.entities("DeviceScan")[-1]
        self.assertEqual(scan["recognized"]["memory_gb"], 16)
        self.assertEqual(scan["recognized"]["desktop_resolution"], {"width": 2560, "height": 1600})
        self.assertEqual(self.entities("GameCard"), [])

    def test_fixed_data_journey_from_intent_through_test_and_feedback(self):
        reply = self.h.command("intent.submit", {"message": "我想玩使命召唤现代战争II，希望交战少卡顿，保持画面清晰"})
        self.assertEqual(self.entities("GameCard"), [])
        task = self.h.run(reply.body["task_id"])
        self.assertEqual(task["status"], "completed")
        cards = self.entities("GameCard")
        self.assertEqual(len(cards), 1)
        self.assertEqual(self.h.entity(cards[0]["knowledge_id"])["status"], "completed")
        recommendation = self.h.recommendation(knowledge_id=cards[0]["knowledge_id"])
        applied = self.h.adopt(recommendation_id=recommendation)
        summary_id = self.h.summary(applied_id=applied)
        summary = self.h.entity(summary_id)
        self.assertGreater(summary["metrics"]["average_present_rate"], 0)
        saved = self.h.command("feedback.save", {
            "session_id": summary["session_id"], "summary_id": summary_id,
            "satisfied": False, "symptoms": ["stutter"], "severity": "occasional",
            "raw_text": "交战偶尔卡顿，希望保留画面清晰度", "protected_setting_ids": [],
        })
        action = self.h.command("feedback.next", {
            "feedback_id": self.h.ref(saved, "Feedback"), "applied_id": applied, "summary_id": summary_id,
        })
        task = self.h.run(action.body['task_id'])
        self.assertEqual(task['result']['action'], 'adjust')

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the browser compatibility guard")
    def test_frontend_rejects_old_runtime_before_enabling_operations(self):
        api_url = (Path(__file__).resolve().parents[1] / "static/js/api.js").as_uri()
        script = f"""
import assert from 'node:assert/strict';
import {{ requireCompatibleService }} from {json.dumps(api_url)};
assert.throws(() => requireCompatibleService({{scenarios: []}}), /重新启动/);
assert.throws(() => requireCompatibleService({{ui_contract: 'conversation/1', capabilities: ['device.check']}}), /重新启动/);
assert.doesNotThrow(() => requireCompatibleService({{ui_contract: 'conversation/2', capabilities: ['device.check', 'intent.submit', 'log.capture_recent', 'feedback.review']}}));
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the conversation regression")
    def test_incomplete_information_scan_does_not_break_device_controls(self):
        probe = Path(__file__).with_name("conversation_device_probe.mjs")
        result = subprocess.run(["node", str(probe)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
