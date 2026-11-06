"""G-FORMAT intake: describing a capture sample never registers a format.

The reader must be strict about structure and silent about claims: it reports
what the file states (columns, units, process ids, streams, frame generation,
time monotonicity) and refuses to call any tool supported.  A real PresentMon
sample is not in the repository, so the tool-support check stays ``not_run``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from game_app.adapters.presentmon import (
    OFFLINE_FORMAT_ID,
    default_registry,
    inspect_sample,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "product" / "offline-log-basic" / "v1"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_a_well_formed_sample_is_described_without_claiming_support():
    inspection = inspect_sample(_fixture("basic.csv"), source_name="basic.csv")

    assert inspection.registration_ready
    assert inspection.blockers == ()
    assert inspection.row_count == 5
    assert inspection.process_ids == ("4242",)
    assert inspection.stream_values == ("application_present",)
    assert inspection.frame_generation_values == ("off",)
    assert inspection.time_column == "Time"
    assert inspection.time_monotonic is True

    # Describing a sample is not registering a format or claiming a tool.
    registry = default_registry()
    assert tuple(item.format_id for item in registry.formats) == (OFFLINE_FORMAT_ID,)
    assert "presentmon" not in OFFLINE_FORMAT_ID
    real_support = next(
        check for check in registry.formats[0].checks
        if check.check_id == "format.real_tool_support"
    )
    assert real_support.status.value == "not_run"


def test_a_missing_time_column_is_a_blocker_not_a_default():
    payload = b"EventNumber,ProcessId\n1,4242\n2,4242\n"
    inspection = inspect_sample(payload, source_name="no-time.csv")

    assert inspection.time_column is None
    assert inspection.registration_ready is False
    assert any("time" in blocker for blocker in inspection.blockers)


def test_a_non_monotonic_clock_is_recorded_as_a_blocker():
    payload = (
        b"ProcessId,Time (ms)\n"
        b"4242,0.0\n"
        b"4242,33.4\n"
        b"4242,16.7\n"
    )
    inspection = inspect_sample(payload, source_name="backwards.csv")

    assert inspection.time_monotonic is False
    assert any("monotonic" in blocker for blocker in inspection.blockers)


def test_a_malformed_row_is_counted_and_blocks_registration():
    payload = (
        b"ProcessId,Time (ms),MsBetweenPresents (ms)\n"
        b"4242,0.0,\n"
        b"4242,16.7\n"
    )
    inspection = inspect_sample(payload, source_name="short-row.csv")

    assert inspection.malformed_rows == 1
    assert any("column count" in blocker for blocker in inspection.blockers)


def test_a_non_utf8_or_empty_file_is_blocked():
    assert inspect_sample(b"\xff\xfe\x00", source_name="binary.csv").registration_ready is False
    assert inspect_sample(b"", source_name="empty.csv").registration_ready is False


def test_the_intake_script_reports_the_hash_and_claims_nothing(tmp_path):
    sample = tmp_path / "basic.csv"
    sample.write_bytes(_fixture("basic.csv"))
    report_path = tmp_path / "sample.json"

    completed = subprocess.run(
        [
            sys.executable, "-B",
            str(REPO_ROOT / "scripts" / "inspect_capture_sample.py"),
            str(sample),
            "--tool-name", "PresentMon",
            "--tool-version", "unverified",
            "--origin", "simulated",
            "--option=--process_name=SyntheticGame.exe",
            "--out", str(report_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["kind"] == "capture_sample_inspection"
    assert report["sha256"] == (
        "5bbb043e9a942525f4617a359fa25d89252715f4b422cb4bc7409fa9217fece4"
    )
    assert report["tool_support_claimed"] is False
    assert report["registration_ready"] is True
    # A simulated sample exercises the reader but is never G-FORMAT evidence.
    assert report["declared_origin"] == "simulated"
    assert report["citable_for_g_format"] is False
    assert report["tool"] == {"name": "PresentMon", "version": "unverified"}


def test_an_unregistered_header_is_still_described_without_matching():
    inspection = inspect_sample(_fixture("unregistered.csv"), source_name="unregistered.csv")

    # The reader describes it; the registry still refuses to parse it.
    assert inspection.header == ("alpha", "beta")
    assert inspection.registration_ready is False
    with pytest.raises(Exception):
        default_registry().match(list(inspection.header))
