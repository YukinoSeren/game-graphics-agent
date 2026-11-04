"""T16-T19: server-side journeys that the workbench calls."""

import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from demo.app.contracts import Scope
from demo.tests.support import DemoCase, running_http


def post(base, document, headers=None):
    body = json.dumps(document).encode("utf-8")
    request = Request(
        base + "/api/demo/v1/commands",
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


class WebFlowTests(DemoCase):
    def test_same_request_twice_over_http_creates_one_result(self):
        command = {
            "request_id": "web-plan-1",
            "scenario_id": "mw2-rtx4060",
            "workspace_epoch": 1,
            "expected_revision": 1,
            "action": "plan.save",
            "payload": {"scene": "交战场景甲"},
        }
        with running_http(self.h.service) as base:
            first_status, first = post(base, command)
            second_status, second = post(base, command)
        self.assertEqual((first_status, second_status), (201, 201))
        self.assertEqual(first, second)
        workspace = self.h.workspace()
        self.assertEqual(workspace["revision"], 2)
        scope = Scope("mw2-rtx4060", workspace["workspace_epoch"])
        self.assertEqual(len(self.h.service.store.find(scope, "TestPlan")), 1)

    def test_same_request_key_with_different_content_conflicts_over_http(self):
        command = {
            "request_id": "web-plan-2",
            "scenario_id": "mw2-rtx4060",
            "workspace_epoch": 1,
            "expected_revision": 1,
            "action": "plan.save",
            "payload": {"scene": "甲"},
        }
        with running_http(self.h.service) as base:
            first_status, _ = post(base, command)
            changed = dict(command, payload={"scene": "乙"}, expected_revision=2)
            second_status, second = post(base, changed)
        self.assertEqual(first_status, 201)
        self.assertEqual(second_status, 409)
        self.assertEqual(second["error"]["code"], "request_id_conflict")

    def test_entities_can_be_listed_by_kind_inside_one_case(self):
        story = self.h.story(accepted=True)
        workspace = self.h.workspace()
        query = urlencode(
            {
                "scenario_id": "mw2-rtx4060",
                "workspace_epoch": workspace["workspace_epoch"],
                "kind": "AppliedConfiguration",
            }
        )
        foreign = urlencode(
            {
                "scenario_id": "fh6-rtx5090",
                "workspace_epoch": 1,
                "kind": "AppliedConfiguration",
            }
        )
        with running_http(self.h.service) as base:
            with urlopen(base + "/api/demo/v1/entities?" + query, timeout=10) as response:
                listed = json.load(response)["entities"]
            with self.assertRaises(HTTPError) as rejected:
                urlopen(base + "/api/demo/v1/entities?" + foreign, timeout=10)
            self.assertEqual(rejected.exception.code, 404)
            other = json.load(rejected.exception)
        ids = {item["id"] for item in listed}
        self.assertEqual(ids, {story["a0_id"], story["a1_id"], story["a2_id"]})
        self.assertEqual(other["error"]["code"], "unsupported_game")

    def test_entity_listing_requires_a_kind(self):
        workspace = self.h.workspace()
        query = urlencode(
            {"scenario_id": "mw2-rtx4060", "workspace_epoch": workspace["workspace_epoch"]}
        )
        with running_http(self.h.service) as base:
            try:
                urlopen(base + "/api/demo/v1/entities?" + query, timeout=10)
            except HTTPError as error:
                self.assertEqual(error.code, 400)
                self.assertEqual(json.load(error)["error"]["code"], "invalid_kind")
            else:
                self.fail("listing entities without kind should fail")

    def test_static_modules_are_served(self):
        with running_http(self.h.service) as base:
            for path in (
                "/",
                "/styles.css",
                "/js/app.js",
                "/js/pages/overview.js",
                "/js/pages/knowledge.js",
                "/js/pages/recommendation.js",
                "/js/pages/performance.js",
                "/js/pages/feedback.js",
                "/js/pages/history.js",
            ):
                with urlopen(base + path, timeout=10) as response:
                    self.assertEqual(response.status, 200, path)
                    self.assertGreater(len(response.read()), 0, path)

    def test_accepting_a_story_keeps_the_current_version(self):
        story = self.h.story(accepted=True)
        outcome = self.h.entity(story["outcome_id"])
        self.assertIs(outcome["accepted"], True)
        current = self.h.entity(self.h.workspace()["current_applied_id"])
        self.assertEqual(current["id"], story["a2_id"])
        self.assertEqual(self.h.entity(story["comparison_id"])["comparable"], True)

    def test_rejecting_the_visual_result_keeps_the_comparison_and_adds_rollback(self):
        story = self.h.story(accepted=False)
        outcome = self.h.entity(story["outcome_id"])
        current = self.h.entity(self.h.workspace()["current_applied_id"])
        self.assertIs(outcome["accepted"], False)
        self.assertEqual(current["rollback_target_id"], story["a1_id"])
        self.assertEqual(current["parent_id"], story["a2_id"])
        self.assertTrue(self.h.entity(story["comparison_id"])["demo_only"])

    def test_archived_http_command_does_not_change_the_completed_active_story(self):
        story = self.h.story(accepted=True)
        before = self.h.workspace()
        command = {"request_id": "archive-plan", "scenario_id": "fh6-rtx5090",
                   "workspace_epoch": 1, "expected_revision": 1,
                   "action": "plan.save", "payload": {"scene": "archived"}}
        with running_http(self.h.service) as base:
            status, response = post(base, command)
        self.assertEqual(status, 404)
        self.assertEqual(response["error"]["code"], "unsupported_game")
        self.assertEqual(self.h.workspace(), before)
        self.assertEqual(self.h.entity(story["outcome_id"])["scenario_id"], "mw2-rtx4060")
        self.assertTrue(self.h.entity(story["outcome_id"])["accepted"])
        self.assertEqual(self.h.service.store.find(Scope("fh6-rtx5090", 1), "TestPlan"), [])
