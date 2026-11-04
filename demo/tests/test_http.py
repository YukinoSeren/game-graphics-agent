"""T04: loopback HTTP surface, static shell, and request limits."""

import hashlib
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from demo.app.http import _header_text
from demo.tests.support import DemoCase, running_http


def request(base, path, *, method="GET", data=None, headers=None):
    req = Request(base + path, data=data, headers=headers or {}, method=method)
    try:
        with urlopen(req, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def post_json(base, path, document, *, headers=None):
    body = json.dumps(document).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    return request(base, path, method="POST", data=body, headers=merged)


class HttpTests(DemoCase):
    def test_health_announces_current_contract_and_single_active_scenario(self):
        with running_http(self.h.service) as base:
            with urlopen(base + "/api/demo/v1/health") as response:
                health = json.load(response)
            with urlopen(base + "/api/demo/v1/bootstrap") as response:
                bootstrap = json.load(response)
        self.assertEqual(health["service"], "demo/1.3")
        self.assertEqual(health["ui_contract"], "conversation/2")
        self.assertIn("feedback.review", health["capabilities"])
        self.assertEqual(bootstrap["ui_contract"], health["ui_contract"])
        self.assertEqual(bootstrap["scenarios"], [{"scenario_id": "mw2-rtx4060"}])

    def test_static_shell_has_assistant_navigation_without_demo_badge_or_case_selector(self):
        with running_http(self.h.service) as base:
            status, body, headers = request(base, "/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertNotIn("演示模式", text)
        self.assertIn("PC 游戏画质优化助手", text)
        self.assertIn("page-root", text)
        self.assertNotIn("scenario-select", text)
        self.assertIn("page-list", text)
        self.assertIn("text/html", headers["Content-Type"])

    def test_index_has_no_external_resource_references(self):
        with running_http(self.h.service) as base:
            _, body, _ = request(base, "/")
        text = body.decode("utf-8")
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("cdn", text.lower())

    def test_unknown_action_returns_400_without_changing_revision(self):
        with running_http(self.h.service) as base:
            status, body, _ = post_json(
                base,
                "/api/demo/v1/commands",
                {
                    "request_id": "http-unknown",
                    "scenario_id": "mw2-rtx4060",
                    "workspace_epoch": 1,
                    "expected_revision": 1,
                    "action": "not.registered",
                    "payload": {},
                },
            )
        payload = json.loads(body)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "unknown_action")
        self.assertEqual(self.h.workspace()["revision"], 1)

    def test_get_queries_do_not_advance_the_workspace(self):
        with running_http(self.h.service) as base:
            for _ in range(3):
                status, _, _ = request(base, "/api/demo/v1/workspaces/mw2-rtx4060")
                self.assertEqual(status, 200)
        self.assertEqual(self.h.workspace()["revision"], 1)

    def test_cross_origin_post_is_rejected(self):
        with running_http(self.h.service) as base:
            status, body, _ = post_json(
                base,
                "/api/demo/v1/commands",
                {
                    "request_id": "x",
                    "scenario_id": "mw2-rtx4060",
                    "workspace_epoch": 1,
                    "expected_revision": 1,
                    "action": "not.registered",
                    "payload": {},
                },
                headers={"Origin": "http://evil.example"},
            )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "cross_origin")

    def test_oversized_json_body_is_rejected(self):
        with running_http(self.h.service) as base:
            status, body, _ = request(
                base,
                "/api/demo/v1/commands",
                method="POST",
                data=b"{" + b" " * (1024 * 1024 + 16) + b"}",
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "request_too_large")

    def test_malformed_json_is_rejected(self):
        with running_http(self.h.service) as base:
            status, body, _ = request(
                base,
                "/api/demo/v1/commands",
                method="POST",
                data=b"{not json",
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_json")

    def test_path_traversal_cannot_read_outside_static(self):
        with running_http(self.h.service) as base:
            status, body, _ = request(base, "/../demo/app/store.py")
            status2, body2, _ = request(base, "/..%2F..%2Fdemo%2Fapp%2Fstore.py")
        self.assertIn(status, (404, 403))
        self.assertIn(status2, (404, 403))
        self.assertNotIn(b"DemoStore", body + body2)

    def test_unknown_api_route_is_404(self):
        with running_http(self.h.service) as base:
            status, body, _ = request(base, "/api/demo/v1/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_multipart_upload_registers_a_downloadable_file(self):
        data = "demo_only,scenario_id\n".encode("utf-8")
        boundary = "----demoBoundary"
        command = {
            "request_id": "upload-http-1",
            "scenario_id": "mw2-rtx4060",
            "workspace_epoch": 1,
            "expected_revision": 1,
            "action": "file.upload",
            "payload": {
                "media_type": "text/csv",
                "filename": "sample.csv",
                "size": len(data),
                "content_hash": hashlib.sha256(data).hexdigest(),
            },
        }
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="command"\r\n'
            "Content-Type: application/json\r\n\r\n"
            + json.dumps(command)
            + f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="sample.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        with running_http(self.h.service) as base:
            status, response_body, _ = request(
                base,
                "/api/demo/v1/uploads",
                method="POST",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            payload = json.loads(response_body)
            file_id = payload["refs"][0]["id"]
            download_status, download, headers = request(
                base,
                f"/api/demo/v1/files/{file_id}?scenario_id=mw2-rtx4060&workspace_epoch=1",
            )
        self.assertEqual(status, 201)
        self.assertEqual(download_status, 200)
        self.assertEqual(download, data)
        self.assertEqual(headers["Content-Type"], "text/csv")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="sample.csv"')

    def test_download_header_values_are_sanitized(self):
        self.assertEqual(_header_text('a"\r\nX-Evil: 1.csv', "demo-file"), "a___X-Evil: 1.csv")
        self.assertEqual(_header_text("   ", "demo-file"), "demo-file")
        self.assertEqual(_header_text(None, "demo-file"), "demo-file")
        self.assertEqual(len(_header_text("x" * 500, "demo-file")), 120)


if __name__ == "__main__":
    unittest.main()
