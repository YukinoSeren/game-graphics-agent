"""Simulated capture samples exercise the intake reader — never G-FORMAT.

These files imitate a real tool's header shape (``PresentRuntime``,
``TimeInSeconds``, ``PresentMode``) so the reader is tested against something
closer to reality than the offline fixture.  They are hand-authored: they can
never satisfy G-FORMAT, register a format, or make the registry claim support
for any tool.  Every test below asserts that boundary as well as the reading.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from game_app.adapters.presentmon import (
    OFFLINE_FORMAT_ID,
    SIMULATED_PRESENTMON_FORMAT_ID,
    default_registry,
    inspect_sample,
    parse_log,
    simulated_registry,
)
from game_app.contracts.common import EvidenceRef
from game_app.contracts.enums import FrameStreamKind, LogQuality, ValueState
from game_app.errors import ContractError
from tests.product.support.fixtures import FixtureLoader

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "product"
SAMPLE_DIR = FIXTURES / "simulated-capture" / "v1"


def _sample(name: str, *, origin: str = "simulated"):
    return inspect_sample(
        (SAMPLE_DIR / name).read_bytes(), source_name=name, declared_origin=origin,
    )


def _parse(name: str):
    evidence = EvidenceRef(
        kind="artifact", file_id="b" * 32, content_hash="c" * 64,
        locator=f"objects/{'b' * 32}",
    )
    return parse_log(
        (SAMPLE_DIR / name).read_bytes(),
        registry=simulated_registry(),
        evidence=evidence,
    )


# -- the fixtures are labelled as simulated ----------------------------------


def test_the_simulated_fixture_manifest_is_explicit():
    manifest = FixtureLoader(FIXTURES).load_fixture("simulated-capture", 1)
    # FixtureManifest is a strict contract model (08 §4), so the "simulated"
    # marker lives in the annotation method and the limitations, not an extra
    # field that the contract would reject.
    assert manifest.annotation_method.startswith("synthetic")
    assert "simulated" in manifest.annotation_method
    assert "no tool support claimed" in manifest.annotation_method
    assert any(
        "never satisfy G-FORMAT" in limitation
        for limitation in manifest.known_limitations
    )
    for entry in manifest.files:
        assert (SAMPLE_DIR / entry.relative_name).is_file()
        assert hashlib.sha256(
            (SAMPLE_DIR / entry.relative_name).read_bytes()
        ).hexdigest() == entry.sha256


# -- the reader describes a real-tool-shaped header --------------------------


def test_a_presentmon_shaped_sample_is_read_without_registering_anything():
    inspection = _sample("presentmon-2x-shape.csv")

    assert inspection.registration_ready
    assert inspection.blockers == ()
    # PresentMon writes TimeInSeconds with no unit suffix.
    assert inspection.time_column == "TimeInSeconds"
    assert inspection.time_monotonic is True
    assert inspection.process_ids == ("4242",)
    assert inspection.stream_values == ("Hardware: Independent Flip",)
    assert inspection.row_count == 12

    registry = default_registry()
    assert tuple(item.format_id for item in registry.formats) == (OFFLINE_FORMAT_ID,)
    by_id = {check.check_id: check for check in registry.formats[0].checks}
    assert by_id["format.real_tool_support"].status.value == "not_run"


def test_present_runtime_is_not_mistaken_for_the_clock():
    """``PresentRuntime`` contains "time"; the clock is ``TimeInSeconds``."""

    inspection = _sample("presentmon-runtime-column.csv")
    assert inspection.time_column == "TimeInSeconds"
    roles = {column.name: column.role for column in inspection.columns}
    assert roles["PresentRuntime"] == "other"
    assert roles["TimeInSeconds"] == "time"


def test_a_utf8_bom_and_crlf_line_endings_are_handled():
    data = (SAMPLE_DIR / "presentmon-2x-shape.csv").read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in data
    inspection = inspect_sample(data, source_name="bom.csv", declared_origin="simulated")
    assert inspection.malformed_rows == 0
    assert inspection.header[0] == "Application"


def test_two_present_modes_are_both_reported():
    inspection = _sample("presentmon-multistream.csv")
    assert inspection.stream_values == (
        "Composed: Flip", "Hardware: Independent Flip",
    )
    assert inspection.registration_ready


def test_a_backwards_clock_is_a_blocker():
    inspection = _sample("presentmon-backwards-clock.csv")
    assert inspection.time_monotonic is False
    assert inspection.registration_ready is False
    assert any("monotonic" in blocker for blocker in inspection.blockers)


# -- the boundary: simulated samples are not evidence ------------------------


def test_a_simulated_sample_is_never_citable_for_g_format():
    assert _sample("presentmon-2x-shape.csv", origin="simulated").citable_for_g_format is False
    assert _sample("presentmon-2x-shape.csv", origin="unknown").citable_for_g_format is False
    # Only a real capture can be cited, and only when nothing blocks it.
    assert _sample("presentmon-2x-shape.csv", origin="real").citable_for_g_format is True
    assert _sample("presentmon-backwards-clock.csv", origin="real").citable_for_g_format is False


def test_an_unknown_origin_is_refused():
    with pytest.raises(ValueError):
        inspect_sample(b"a,b\n1,2\n", source_name="x.csv", declared_origin="probably-real")


def test_the_registry_still_refuses_a_presentmon_shaped_header():
    """A plausible header is not a registered format, however realistic it looks."""

    inspection = _sample("presentmon-2x-shape.csv")
    with pytest.raises(ContractError) as caught:
        default_registry().match(list(inspection.header))
    assert caught.value.code.value == "UNSUPPORTED_ARTIFACT_FORMAT"


def test_no_simulated_sample_changes_the_enabled_format_set():
    before = tuple(item.format_id for item in default_registry().formats)
    for name in (
        "presentmon-2x-shape.csv",
        "presentmon-multistream.csv",
        "presentmon-runtime-column.csv",
        "presentmon-backwards-clock.csv",
    ):
        _sample(name)
    assert tuple(item.format_id for item in default_registry().formats) == before


# -- the offline parse path for a PresentMon-shaped registration --------------


def test_the_simulated_registration_is_separate_from_the_product_registry():
    assert SIMULATED_PRESENTMON_FORMAT_ID not in {
        item.format_id for item in default_registry().formats
    }
    simulated = {item.format_id for item in simulated_registry().formats}
    assert simulated == {OFFLINE_FORMAT_ID, SIMULATED_PRESENTMON_FORMAT_ID}


def test_a_unitless_seconds_clock_is_read_with_the_registered_meaning():
    result = _parse("presentmon-2x-shape.csv")

    assert result.format_id == SIMULATED_PRESENTMON_FORMAT_ID
    # PresentMon writes TimeInSeconds with no unit; the registration supplies
    # the meaning, so the scale is 1.0 seconds and no conversion is claimed.
    assert result.time_mapping.scale == 1.0
    assert result.time_mapping.source_unit == "s"
    assert result.source_ranges and result.source_ranges[0].unit == "s"
    assert len(result.frames) == 12
    assert len(result.valid_intervals_ms) == 11
    assert result.frames[1].interval_ms == 16.7


def test_absent_frame_generation_keeps_the_stream_unconfirmed():
    result = _parse("presentmon-2x-shape.csv")

    assert result.frame_generation.value_state is ValueState.unknown
    assert result.frame_stream_kind is FrameStreamKind.unknown
    assert any(
        "frame generation is unknown" in limitation
        for limitation in result.limitations
    )


def test_a_present_mode_token_outside_the_vocabulary_stays_unknown():
    """An unregistered token never inherits another token's meaning."""

    result = _parse("presentmon-runtime-column.csv")
    assert result.frame_stream_kind is FrameStreamKind.unknown


def test_two_present_modes_are_never_merged_into_one_stream():
    result = _parse("presentmon-multistream.csv")

    assert {item.stream_key for item in result.observed_streams} == {
        "Composed: Flip", "Hardware: Independent Flip",
    }
    assert result.frame_stream_kind is FrameStreamKind.unknown
    assert result.quality is LogQuality.invalid
    assert any("not merged" in limitation for limitation in result.limitations)


def test_a_stated_frame_generation_state_confirms_the_stream():
    result = _parse("presentmon-with-framegen.csv")

    assert result.frame_generation.value_state is ValueState.known
    assert result.frame_generation.value is False
    assert result.frame_stream_kind is FrameStreamKind.application_present


def test_a_backwards_clock_breaks_the_chain_and_loses_no_row():
    result = _parse("presentmon-backwards-clock.csv")

    assert len(result.frames) == 12  # every row is kept, including the break
    assert len(result.valid_intervals_ms) < 11
    assert any("broke the interval chain" in item for item in result.limitations)


def test_unregistered_columns_are_reported_not_silently_dropped():
    result = _parse("presentmon-2x-shape.csv")
    reported = " ".join(result.limitations)
    for name in ("Application", "SwapChainAddress", "PresentRuntime", "SyncInterval"):
        assert f"column {name} is not registered" in reported
