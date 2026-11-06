"""The CLI surface, end to end.

Q1 fixed the product as CLI-only, so the command line *is* the product.  These
tests drive ``game_app.cli`` the way a player would: every id a later step needs
comes from an earlier command's printed response, and no step reaches into the
repository to shortcut a rule.

Two journeys:

* from an empty database, up to the identity gate the product itself declares;
* from a verified game, through import, package, session, adoption and analysis.

Device acquisition is the one place a stub is injected, through the same
``--probe`` seam the real build uses: a real probe is opt-in and machine
dependent.  Real hardware is covered separately by ``test_real_probe.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from game_app.analyzer import ANALYZER_VERSION, CLEANING_PROTOCOL_VERSION
from game_app.cli import main as cli_main
from game_app.device import ProbeReading
from game_app.files import FileService
from game_app.repository import ProductRepository
from tests.product.support.performance import csv_bytes
from tests.product.support.recommendation import (
    GAME_LEGACY_ID,
    SCOPE,
    store_context,
    store_device,
    store_game,
    store_installation,
)

LOG_SCHEMA = "offline-frame-csv/v1"


class StubProbe:
    """Deterministic readings for the whitelisted device paths."""

    VALUES = {
        "cpu": "Synthetic CPU",
        "memory_bytes": "17179869184",
        "os_version": "10.0.26200",
        "device_form": "2",
        "display.desktop_resolution.width": "2560",
        "display.desktop_resolution.height": "1600",
        "display.refresh_hz": "165",
        "gpus.0.raw_name": "Synthetic Laptop GPU",
        "gpus.0.vram_bytes": "3221225472",
        "gpus.0.driver_version": "32.0.16.2101",
    }

    def probe(self, field_paths, *, timeout):
        readings = []
        for path in field_paths:
            value = self.VALUES.get(path)
            if value is None:
                readings.append(ProbeReading(
                    field_path=path, status="failed", raw=b"",
                    error_code="PROBE_UNAVAILABLE",
                ))
            else:
                readings.append(ProbeReading(
                    field_path=path, status="succeeded",
                    raw=value.encode("utf-8"), text=value,
                ))
        return readings


class Runner:
    def __init__(self, tmp_path: Path, capsys, database: Path, managed: Path) -> None:
        self.tmp_path = tmp_path
        self.capsys = capsys
        self.database = database
        self.managed = managed
        self.inputs = tmp_path / "inputs"
        self.inputs.mkdir()

    def run(self, *argv, expect: int = 0):
        self.capsys.readouterr()
        code = cli_main([
            *argv,
            "--db", str(self.database),
            "--files", str(self.managed),
            "--user-scope", SCOPE,
        ])
        captured = self.capsys.readouterr()
        payload = json.loads(captured.out) if captured.out.strip() else None
        error = json.loads(captured.err) if captured.err.strip() else None
        assert code == expect, f"{argv} -> {code}\n{captured.err}"
        return payload if expect == 0 else error

    def document(self, name: str, payload: dict) -> str:
        path = self.inputs / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)


@pytest.fixture
def cli(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("game_app.cli.WindowsProbeAdapter", lambda: StubProbe())
    database = tmp_path / "product.sqlite3"
    managed = tmp_path / "managed"
    return Runner(tmp_path, capsys, database, managed)


def _one(payload: dict, entity_type: str, *, from_task: bool = False) -> str:
    refs = payload["task"]["result_refs"] if from_task else payload["refs"]
    ids = [ref["entity_id"] for ref in refs if ref["entity_type"] == entity_type]
    assert ids, f"no {entity_type} in {payload}"
    return ids[0]


def _upload(runner: Runner, name: str, data: bytes, key: str) -> str:
    path = runner.tmp_path / name
    path.write_bytes(data)
    stored = runner.run("file", "upload", str(path), "--name", name, "--key", key)
    assert stored["relative_name"] == name
    return stored["file_id"]


def test_an_empty_database_reaches_the_identity_gate(cli):
    """The first journey stops exactly where the product says it must."""

    page = cli.run("game", "list")
    assert page["choices"] == []

    selected = cli.run(
        "game", "select",
        "--input", cli.document("select.json", {
            "raw_name": "Synthetic Game",
            "selected_game_revision_id": None,
            "platform": None,
            "installation_id": None,
            "expected_installation_revision": None,
        }),
        "--key", "select-1",
    )
    game_id = _one(selected, "Game")
    context_id = _one(selected, "GameContext")

    installation = cli.run(
        "installation", "register",
        "--input", cli.document("installation.json", {
            "game_context_id": context_id,
            "display_label": "Synthetic install",
        }),
        "--key", "install-1",
    )
    installation_context = _one(installation, "GameContext")

    accepted = cli.run(
        "device", "scan", "--probe", "windows",
        "--input", cli.document("scan.json", {
            "kind": "device_scan",
            "game_context_id": installation_context,
            "field_paths": [
                "cpu", "memory_bytes", "os_version", "device_form",
                "display.desktop_resolution.width",
                "display.desktop_resolution.height",
                "display.refresh_hz",
                "gpus.0.raw_name", "gpus.0.vram_bytes", "gpus.0.driver_version",
            ],
        }),
        "--key", "scan-1", "--wait",
    )
    assert accepted["task"]["state"] == "completed"
    _one(accepted, "DeviceSnapshot", from_task=True)

    from tests.product.support.recommendation import dataset, run_files

    file_ids = [
        _upload(cli, name, data, f"upload-{index}")
        for index, (name, data) in enumerate(sorted(run_files(dataset()).items()))
    ]
    mapping = cli.run(
        "import", "declare-identity",
        "--input", cli.document("identity.json", {
            "run_id": "run-1",
            "legacy_game_id": GAME_LEGACY_ID,
            "game_revision_id": game_id,
        }),
        "--key", "identity-1",
    )
    mapping_id = _one(mapping, "IdentityMapping")
    imported = cli.run(
        "import", "run",
        "--input", cli.document("import.json", {
            "kind": "knowledge_import",
            "file_ids": file_ids,
            "game_context_id": installation_context,
            "mapping_version": "collector-import/1",
            "identity_mapping_ids": [mapping_id],
        }),
        "--key", "import-1", "--wait",
    )
    assert imported["task"]["state"] == "completed"
    snapshot_id = _one(imported, "ImportSnapshot", from_task=True)

    # A provisional identity cannot become a matched package (KNW-V06).  The
    # CLI reports the owner's refusal verbatim instead of inventing a package.
    refusal = cli.run(
        "package", "build",
        "--input", cli.document("package.json", {
            "import_snapshot_ids": [snapshot_id],
            "prior_package_id": None,
            "declared_scope": "collection.public_graphics",
            "game_context_id": installation_context,
        }),
        "--key", "package-1",
        expect=2,
    )
    assert refusal["code"] == "PRECONDITION_UNVERIFIED"
    assert "game identity is not verified" in refusal["message"]

    # The reads still work and stay scoped to the caller.  An installation is
    # queried by game *series*: the catalog names both ids, so no id is guessed.
    games = cli.run("game", "list")
    assert games["choices"]
    series_id = games["choices"][0]["game_id"]
    assert cli.run("installation", "list", "--game-id", series_id)["choices"]
    assert cli.run("metric", "list")["choices"]


def test_a_verified_game_walks_the_rest_of_the_journey(cli):
    """Import -> package -> session -> adoption -> log -> analysis, all via CLI."""

    repository = ProductRepository(cli.database)
    repository.migrate()
    files = FileService(repository, cli.managed)
    game = store_game(repository)
    context = store_context(
        repository, files, game, uid_value=300, installation=301,
    )
    # ``store_context`` creates the installation it names; reusing that id keeps
    # the seeded facts consistent instead of inserting a second installation.
    installation_id = context.installation_id
    device = store_device(repository, files)
    repository.close()

    from tests.product.support.recommendation import dataset, run_files

    file_ids = [
        _upload(cli, name, data, f"upload-{index}")
        for index, (name, data) in enumerate(sorted(run_files(dataset()).items()))
    ]
    mapping = cli.run(
        "import", "declare-identity",
        "--input", cli.document("identity.json", {
            "run_id": "run-1",
            "legacy_game_id": GAME_LEGACY_ID,
            "game_revision_id": game.id,
        }),
        "--key", "identity-1",
    )
    imported = cli.run(
        "import", "run",
        "--input", cli.document("import.json", {
            "kind": "knowledge_import",
            "file_ids": file_ids,
            "game_context_id": context.id,
            "mapping_version": "collector-import/1",
            "identity_mapping_ids": [_one(mapping, "IdentityMapping")],
        }),
        "--key", "import-1", "--wait",
    )
    snapshot_id = _one(imported, "ImportSnapshot", from_task=True)

    package = cli.run(
        "package", "build",
        "--input", cli.document("package.json", {
            "import_snapshot_ids": [snapshot_id],
            "prior_package_id": None,
            "declared_scope": "collection.public_graphics",
            "game_context_id": context.id,
        }),
        "--key", "package-1",
    )
    package_id = _one(package, "KnowledgePackage")
    assert cli.run("package", "get", package_id)["id"] == package_id

    session = cli.run(
        "session", "create",
        "--input", cli.document("session.json", {
            "installation_id": installation_id,
            "device_snapshot_id": device.id,
            "game_context_id": context.id,
            "applied_id": None,
            "configuration_state": "unverified",
            "run_declaration": {
                "installation_id": installation_id,
                "fields": {},
                "raw_text": "",
            },
            "test_protocol_id": None,
            "recommendation_id": None,
        }),
        "--key", "session-1",
    )
    session_id = _one(session, "PlaySession")
    segment_id = _one(session, "SessionSegment")

    adoption = cli.run(
        "adopt",
        "--input", cli.document("adopt.json", {
            "kind": "adopt_configuration",
            "origin": "baseline",
            "installation_id": installation_id,
            "device_snapshot_id": device.id,
            "game_context_id": context.id,
            "expected_current_applied_id": None,
            "recommendation_id": None,
            "base_applied_id": None,
            "change_set_id": None,
            "restored_from_applied_id": None,
            "declared_settings": [],
            "preset_reference": "Extreme",
            "claimed_activation": "unknown",
        }),
        "--key", "adopt-1",
    )
    _one(adoption, "AppliedConfiguration")

    closed = cli.run(
        "session", "end-segment",
        "--input", cli.document("end-segment.json", {
            "segment_id": segment_id,
            "expected_segment_revision": 1,
            "expected_binding_revision": 1,
        }),
        "--key", "end-segment-1",
    )
    assert _one(closed, "SessionSegment") == segment_id
    assert cli.run("session", "get", session_id)["id"] == session_id

    # A log: upload, read-only preview, explicit association, then analysis.
    log_bytes = csv_bytes([10, 20, 30, 40])
    file_id = _upload(cli, "log.csv", log_bytes, "upload-log")
    inspection = cli.run("log", "inspect", file_id, "--schema", LOG_SCHEMA)
    assert inspection["source_ranges"]
    source = inspection["source_ranges"][0]

    association = cli.run(
        "log", "associate",
        "--input", cli.document("associate.json", {
            "file_id": file_id,
            "content_hash": inspection["content_hash"],
            "source_schema": LOG_SCHEMA,
            "source_range": source,
            "stream_key": inspection["streams"][0]["stream_key"],
            "source_anchor": source["start"],
            "target_anchor_s": 0.0,
            "target": {
                "mode": "historical_session",
                "installation_id": installation_id,
                "device_snapshot_id": device.id,
                "game_context_id": context.id,
                "started_at": "2026-10-01T12:00:00+00:00",
                "run_declaration": {
                    "installation_id": installation_id,
                    "fields": {"upscaling": "off"},
                    "raw_text": "synthetic fixture run",
                },
                "test_protocol_id": None,
            },
            "evidence_refs": [{
                "kind": "artifact",
                "file_id": file_id,
                "content_hash": inspection["content_hash"],
                "locator": "log.csv",
            }],
            "raw_text": "",
        }),
        "--key", "associate-1",
    )
    artifact_id = _one(association, "CaptureArtifact")

    # The artifact names its own historical segment; read it back through the
    # CLI rather than guessing a revision.
    artifact = cli.run("artifact", "get", artifact_id)
    historical_segment = cli.run("segment", "get", artifact["segment_ids"][0])
    assert historical_segment["lifecycle"] == "ended"

    analyzed = cli.run(
        "analyze",
        "--input", cli.document("analyze.json", {
            "kind": "analysis",
            "artifact_id": artifact_id,
            "bindings": [{
                "segment_id": historical_segment["id"],
                "segment_revision": historical_segment["revision"],
                "binding_revision": historical_segment["binding_revision"],
            }],
            "analyzer_version": ANALYZER_VERSION,
            "cleaning_protocol_version": CLEANING_PROTOCOL_VERSION,
        }),
        "--key", "analyze-1", "--wait",
    )
    assert analyzed["task"]["state"] == "completed"
    summary_id = _one(analyzed, "PerformanceSummary", from_task=True)
    assert cli.run("summary", "get", summary_id)["id"] == summary_id

    # Reads that need an id the actor does not own are indistinguishable from a
    # missing one.
    missing = cli.run("summary", "get", "0" * 32, expect=4)
    assert missing["code"] == "RESOURCE_NOT_FOUND"
