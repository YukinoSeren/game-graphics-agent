"""G-WIN evidence: the packaged read-only probe, opt-in only.

The default suite stays offline.  With ``GAME_APP_REAL_PROBE=1`` and an
explicit authorization flag, the test runs the packaged queries on this machine
and records what each field reported.  It asserts only structural honesty:

* every field returns a stable status, and a failure keeps its reason;
* no command outside the whitelist can be requested;
* the adapter never fabricates a value for a failed field.

It never starts a game, a capture tool or a model call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from game_app.adapters.windows_probe import WindowsProbeAdapter, query_for

REPO_ROOT = Path(__file__).resolve().parents[2]
ENABLED = os.environ.get("GAME_APP_REAL_PROBE") == "1"

requires_probe = pytest.mark.skipif(
    not ENABLED,
    reason="real local probe is opt-in: set GAME_APP_REAL_PROBE=1 after authorizing it",
)

BASE_FIELDS = (
    "cpu",
    "memory_bytes",
    "os_version",
    "device_form",
    "display.desktop_resolution.width",
    "display.desktop_resolution.height",
    "display.refresh_hz",
)


def test_unregistered_probe_paths_are_refused():
    for path in ("gpu.0.name", "cpu; Remove-Item", "gpus.0.name", "gpus.99.raw_name"):
        with pytest.raises(ValueError):
            query_for(path)


@requires_probe
def test_the_packaged_probe_reports_a_status_for_every_field():
    fields = list(BASE_FIELDS) + ["gpus.0.raw_name", "gpus.0.vram_bytes"]
    readings = WindowsProbeAdapter().probe(fields, timeout=30.0)

    assert [reading.field_path for reading in readings] == fields
    for reading in readings:
        assert reading.status in {"succeeded", "failed"}
        if reading.status == "succeeded":
            assert reading.text
            assert reading.error_code is None
        else:
            # A failed field keeps its reason and invents no value.
            assert reading.error_code
            assert reading.text is None


def test_the_probe_script_refuses_without_an_explicit_authorization(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-B", str(REPO_ROOT / "scripts" / "probe_device_fields.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 2
    assert "authorize" in completed.stderr


@requires_probe
def test_the_authorized_probe_records_what_this_machine_reported(tmp_path):
    destination = tmp_path / "gwin.json"
    completed = subprocess.run(
        [
            sys.executable, "-B",
            str(REPO_ROOT / "scripts" / "probe_device_fields.py"),
            "--i-authorize-read-only-probe",
            "--out", str(destination),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr

    report = json.loads(destination.read_text(encoding="utf-8"))
    assert report["kind"] == "gwin_read_only_probe"
    assert report["authorized"] is True
    by_field = {item["field_path"]: item for item in report["fields"]}
    for field in BASE_FIELDS:
        assert field in by_field
        assert by_field[field]["status"] in {"succeeded", "failed"}


@requires_probe
def test_a_real_scan_produces_an_honest_snapshot(tmp_path):
    """The full device scan against this machine, on a temporary store.

    This is the only place real hardware reaches a product snapshot.  It asserts
    honesty rather than a particular machine: every adapter the machine reports
    is kept, none is silently selected, a saturated VRAM reading stays unknown,
    and a choice question is raised instead of picking the first item.
    """

    import json as _json

    from game_app.contracts.common import MutationContext
    from game_app.contracts.device import DeviceSnapshot
    from game_app.contracts.information import InformationRequest
    from game_app.device import DeviceService
    from game_app.files import FileService
    from game_app.repository import ProductRepository
    from game_app.tasks import TaskService
    from tests.product.support.spies import SpyProvider

    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    files = FileService(repository, tmp_path / "managed")
    device = DeviceService(repository, files, WindowsProbeAdapter(), timeout=30.0)
    tasks = TaskService(repository, provider=SpyProvider(), files=files)
    tasks.handlers["device_scan"] = device.handle

    fields = list(BASE_FIELDS) + [
        f"gpus.{index}.{attribute}"
        for index in range(4)
        for attribute in ("raw_name", "vram_bytes", "driver_version")
    ]
    accepted = tasks.register_task(
        {"kind": "device_scan", "game_context_id": None, "field_paths": fields},
        MutationContext(
            user_scope_id="a" * 32, idempotency_key="real-scan",
            request_id="b" * 32,
        ),
    )
    tasks.run_once()

    with repository.unit_of_work() as uow:
        row = uow.query("SELECT result_refs FROM tasks WHERE task_id=?", (accepted.task_id,))[0]
    refs = _json.loads(row["result_refs"] or "[]")
    snapshot_ids = [
        ref["entity_id"] for ref in refs if ref["entity_type"] == "DeviceSnapshot"
    ]
    assert snapshot_ids, f"a scan produced no snapshot: {refs}"

    snapshot = DeviceSnapshot.model_validate_json(
        repository.get_object("DeviceSnapshot", snapshot_ids[0]).payload_bytes
    )
    assert snapshot.gpus, "this machine reports at least one graphics adapter"
    assert snapshot.selected_gpu_id is None  # never picks one silently

    known_names = {
        gpu.raw_name.value for gpu in snapshot.gpus
        if gpu.raw_name.value_state.value == "known"
    }
    assert known_names, "at least one adapter name must be readable"

    # A 32-bit saturated AdapterRAM is not a measurement; it must stay unknown.
    for gpu in snapshot.gpus:
        if gpu.vram_bytes.value_state.value == "known":
            assert gpu.vram_bytes.value < 4 * 1024 ** 3 - 1024 ** 2

    if len(snapshot.gpus) > 1:
        request_ids = [
            ref["entity_id"] for ref in refs
            if ref["entity_type"] == "InformationRequest"
        ]
        assert request_ids, "several adapters must raise a choice question"
        request = InformationRequest.model_validate_json(
            repository.get_object("InformationRequest", request_ids[0]).payload_bytes
        )
        assert request.state is not None
