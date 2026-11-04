"""T20: the demo stays offline, self-contained, and clear of formal artifacts."""

from __future__ import annotations

import ast
import hashlib
import pathlib
import re
import socket
import unittest
from unittest import mock
from urllib.parse import urlparse

from demo.tests.support import DemoCase
from demo.app.contracts import DemoError

DEMO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = DEMO_ROOT.parent
FORMAL_TREES = ("game_app", "game_collector", "tests", "runs")
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "www.w3.org"}
URL_RE = re.compile(r"https?://[^\s\"'<>)]+")


def _python_files() -> list[pathlib.Path]:
    return sorted(DEMO_ROOT.rglob("*.py"))


def _import_roots(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_state(root: pathlib.Path) -> dict[str, tuple[str, int]]:
    state: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if "__pycache__" in path.parts:
            continue
        stat = path.stat()
        state[str(path.relative_to(root))] = (_file_digest(path), stat.st_size)
    return state


def _top_level_state(root: pathlib.Path) -> dict[str, int]:
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)): path.stat().st_mtime_ns
        for path in sorted(root.iterdir())
    }


class IsolationTests(unittest.TestCase):
    def test_demo_sources_never_import_the_formal_packages(self):
        offenders = {}
        for path in _python_files():
            roots = _import_roots(path)
            bad = roots & {"game_app", "game_collector"}
            if bad:
                offenders[str(path.relative_to(DEMO_ROOT))] = sorted(bad)
        self.assertEqual(offenders, {})

    def test_static_assets_have_no_external_references(self):
        external = []
        for path in sorted(DEMO_ROOT.joinpath("static").rglob("*")):
            if not path.is_file() or path.suffix not in {".html", ".css", ".js", ".svg"}:
                continue
            text = path.read_text(encoding="utf-8")
            for match in URL_RE.findall(text):
                host = urlparse(match).hostname
                if host not in ALLOWED_HOSTS:
                    external.append(f"{path.relative_to(DEMO_ROOT)}: {match}")
        self.assertEqual(external, [])

    def test_static_shell_has_no_remote_script_or_style(self):
        html = (DEMO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(html, r"<script[^>]+src=\"https?://")
        self.assertNotRegex(html, r"<link[^>]+href=\"https?://")


class IsolationStoryTests(DemoCase):
    def test_fixtures_are_never_modified_by_a_story(self):
        before = _tree_state(DEMO_ROOT / "fixtures")
        self.h.story(accepted=True)
        self.h.upload(b"timestamp_ms,interval_ms\n0,16.6\n", filename="isolated.csv")
        reply = self.h.command("export.create", {"format": "markdown"})
        self.assertTrue(reply.body["refs"])
        after = _tree_state(DEMO_ROOT / "fixtures")
        self.assertEqual(before, after)

    def test_runtime_files_stay_inside_the_runtime_root(self):
        static_before = _top_level_state(DEMO_ROOT / "static")
        formal_before = {
            name: _top_level_state(REPO_ROOT / name) for name in FORMAL_TREES
        }
        self.h.story(accepted=True)
        self.h.upload(b"timestamp_ms,interval_ms\n0,16.6\n", filename="isolated.csv")
        self.h.command("export.create", {"format": "json"})
        self.assertEqual(static_before, _top_level_state(DEMO_ROOT / "static"))
        for name in FORMAL_TREES:
            self.assertEqual(formal_before[name], _top_level_state(REPO_ROOT / name), name)
        for path in self.h.runtime_root.rglob("*"):
            if path.is_file():
                resolved = path.resolve()
                self.assertTrue(
                    str(resolved).startswith(str(self.h.runtime_root.resolve())),
                    str(resolved),
                )

    def test_a_full_story_runs_with_outbound_network_blocked(self):
        real_connect = socket.socket.connect
        real_getaddrinfo = socket.getaddrinfo

        def guarded_connect(sock, address):
            host = address[0] if isinstance(address, tuple) else address
            if isinstance(host, str) and host not in ALLOWED_HOSTS:
                raise AssertionError(f"demo attempted an outbound connection to {host}")
            return real_connect(sock, address)

        def guarded_getaddrinfo(host, *args, **kwargs):
            if isinstance(host, str) and host not in ALLOWED_HOSTS:
                raise AssertionError(f"demo attempted to resolve {host}")
            return real_getaddrinfo(host, *args, **kwargs)

        with mock.patch.object(socket.socket, "connect", guarded_connect), \
                mock.patch.object(socket, "getaddrinfo", guarded_getaddrinfo):
            story = self.h.story(accepted=True)
            with self.assertRaises(DemoError) as archived:
                self.h.knowledge(scenario="fh6-rtx5090")
            self.assertEqual(archived.exception.code, "unsupported_game")
        self.assertIs(self.h.entity(story["outcome_id"])["accepted"], True)

    def test_export_is_marked_as_demo_data(self):
        self.h.story(accepted=True)
        reply = self.h.command("export.create", {"format": "markdown"})
        file_id = self.h.ref(reply, "File")
        body = self.h.service.files.read(file_id).decode("utf-8")
        self.assertIn("演示报告，非真实实测", body)
        self.assertIn("模拟", body)
        self.assertIn("使命召唤", body)
        self.assertNotIn("实测通过", body)
