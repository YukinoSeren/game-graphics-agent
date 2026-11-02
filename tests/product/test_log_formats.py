"""PERF-T01: versioned log formats, units, clocks and streams.

No real capture tool has been sampled, so the only enabled format is an offline
fixture shape.  These tests assert what the parser refuses to guess: unknown
columns, foreign units, an undetermined frame stream, frame generation state
and an unanchored clock.
"""

from __future__ import annotations

import hashlib

import pytest

from game_app.adapters.presentmon import (
    OFFLINE_FORMAT_ID,
    FormatRegistry,
    LogColumn,
    LogFormat,
    default_registry,
    parse_log,
)
from game_app.contracts.enums import CheckStatus, FrameStreamKind, LogQuality
from game_app.contracts.performance import ColumnMapping, TimeMapping
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService
from game_app.repository import ProductRepository
from tests.product.support.fixtures import FixtureLoader

FIXTURES = __import__("pathlib").Path(__file__).resolve().parent.parent / "fixtures" / "product"
SAMPLE_DIR = FIXTURES / "offline-log-basic" / "v1"
SCOPE = "a" * 32

EVIDENCE_FILE_ID = "b" * 32
EVIDENCE_HASH = "c" * 64


def _evidence():
    from game_app.contracts.common import EvidenceRef

    return EvidenceRef(
        kind="artifact",
        file_id=EVIDENCE_FILE_ID,
        content_hash=EVIDENCE_HASH,
        locator=f"objects/{EVIDENCE_FILE_ID}",
    )


def _parse(name: str, *, registry: FormatRegistry | None = None, **kwargs):
    data = (SAMPLE_DIR / name).read_bytes()
    return parse_log(data, registry=registry or default_registry(), evidence=_evidence(), **kwargs)


def _codes(result) -> str:
    return " | ".join(result.limitations)


# --- the fixture manifest is the evidence for the enabled format ------------


def test_fixture_matches_its_recorded_manifest(fixture_loader: FixtureLoader):
    manifest = fixture_loader.load_fixture("offline-log-basic", 1)
    assert "basic.csv" in {entry.relative_name for entry in manifest.files}


def test_enabled_registry_ships_exactly_the_fixture_format():
    registry = default_registry()
    assert [fmt.format_id for fmt in registry.formats] == [OFFLINE_FORMAT_ID]
    registration = registry.formats[0]
    assert registration.tool_name == "offline-fixture"
    # No real tool is claimed: the fixture check passes, the real-tool check
    # was never run.
    by_id = {check.check_id: check for check in registration.checks}
    assert by_id["format.fixture_verified"].status is CheckStatus.pass_
    assert "offline_fixture" in by_id["format.fixture_verified"].method
    assert by_id["format.real_tool_support"].status is CheckStatus.not_run


def test_registration_names_the_sample_it_was_verified_against(fixture_loader: FixtureLoader):
    manifest = fixture_loader.load_fixture("offline-log-basic", 1)
    basic = next(entry for entry in manifest.files if entry.relative_name == "basic.csv")
    assert default_registry().formats[0].fixture_hashes == (basic.sha256,)


def test_registration_declares_the_mapping_rule_and_column_map():
    registration = default_registry().formats[0]
    assert registration.time_mapping_rule.startswith("unit_scale_only")
    assert {mapping.source_column for mapping in registration.column_map} == set(
        registration.names
    )


# --- the happy path ---------------------------------------------------------


def test_registered_format_is_parsed_into_normalized_frames():
    result = _parse("basic.csv")

    assert result.format_id == OFFLINE_FORMAT_ID
    assert result.source_schema == OFFLINE_FORMAT_ID
    assert result.quality is LogQuality.valid
    assert result.limitations == []
    assert len(result.frames) == 5

    first, second = result.frames[0], result.frames[1]
    assert first.event_index == 1
    # The first row has no preceding frame, so no interval exists yet.
    assert first.interval_ms is None
    assert second.interval_ms == 16.7
    assert second.timestamp == 16.7
    assert second.validity == "valid"
    assert second.frame_type == "Application"
    assert second.gpu_busy_ms == 7.5
    assert second.cpu_busy_ms is None
    assert second.swapchain_id is None
    # Process attribution comes from the log; start time is not invented.
    assert second.process.pid == 4242
    assert second.process.started_at is None
    assert second.process.provenance[0].evidence_refs[0].file_id == EVIDENCE_FILE_ID

    assert result.valid_intervals_ms == [16.7, 16.7, 16.7, 16.7]
    assert result.frame_stream_kind is FrameStreamKind.application_present
    assert result.frame_generation.value_state.value == "known"
    assert result.frame_generation.value is False
    quality_check = next(check for check in result.checks if check.check_id == "log.quality")
    assert quality_check.status is CheckStatus.pass_


def test_columns_are_mapped_with_registered_units_and_no_conversion():
    result = _parse("basic.csv")
    by_source = {mapping.source_column: mapping for mapping in result.column_map}
    assert by_source["Time"].target_field == "timestamp"
    assert by_source["Time"].source_unit == "ms"
    assert by_source["Time"].target_unit == "ms"
    assert by_source["Time"].conversion == "identity"
    assert all(isinstance(mapping, ColumnMapping) for mapping in result.column_map)


def test_source_range_spans_the_parsed_rows():
    result = _parse("basic.csv")
    assert len(result.source_ranges) == 1
    source_range = result.source_ranges[0]
    assert (source_range.start, source_range.end) == (0.0, 66.8)
    assert source_range.unit == "ms"
    assert source_range.time_basis == "log_relative"


# --- refusals: never guess a column, a unit or a stream ---------------------


def test_unknown_column_is_not_parsed_but_rows_are_kept():
    result = _parse("unknown-column.csv")

    assert result.quality is LogQuality.limited
    assert any("SomeNewMetric" in item for item in result.limitations)
    # The row count is preserved: an unregistered column does not drop rows.
    assert len(result.frames) == 3
    assert result.valid_intervals_ms == [16.7, 16.7]
    assert "SomeNewMetric" not in {
        mapping.source_column for mapping in result.column_map
    }
    quality_check = next(check for check in result.checks if check.check_id == "log.quality")
    assert quality_check.status is CheckStatus.unknown


def test_unit_mismatch_is_not_converted_and_stays_unknown():
    result = _parse("unit-mismatch.csv")

    assert result.quality is LogQuality.limited
    assert any("MsBetweenPresents" in item and "unit" in item for item in result.limitations)
    # 0.0167 s is never silently reinterpreted as 0.0167 ms or 16.7 ms.
    assert result.valid_intervals_ms == []
    assert all(frame.interval_ms is None for frame in result.frames)
    assert len(result.frames) == 3
    mapping = next(m for m in result.column_map if m.source_column == "MsBetweenPresents")
    assert mapping.source_unit == "s"
    assert mapping.conversion == "none"


def test_missing_stream_column_leaves_the_stream_unknown():
    result = _parse("no-stream.csv")

    assert result.quality is LogQuality.limited
    assert result.frame_stream_kind is FrameStreamKind.unknown
    assert any("stream" in item for item in result.limitations)
    assert len(result.frames) == 3


def test_conflicting_streams_are_not_merged_into_one_result():
    result = _parse("conflicting-streams.csv")

    assert result.quality is LogQuality.invalid
    assert result.frame_stream_kind is FrameStreamKind.unknown
    observed = {stream.frame_stream_kind for stream in result.observed_streams}
    assert observed == {FrameStreamKind.application_present, FrameStreamKind.displayed}
    assert any("stream" in item for item in result.limitations)


def test_unknown_frame_generation_never_becomes_application_present():
    result = _parse("fg-unknown.csv")

    assert result.frame_generation.value_state.value == "unknown"
    assert result.frame_generation.value is None
    assert result.frame_stream_kind is FrameStreamKind.unknown
    assert result.quality is not LogQuality.valid
    assert any("frame generation" in item.lower() for item in result.limitations)


def test_a_stream_the_registration_did_not_verify_is_reported_and_flagged():
    body = (
        "EventNumber,ProcessId,Time (ms),MsBetweenPresents (ms),FrameType,"
        "MsGPUActive (ms),FrameStream,FrameGeneration\n"
        "1,4242,0.0,,Application,0.0,displayed,off\n"
        "2,4242,16.7,16.7,Application,7.5,displayed,off\n"
    )
    result = parse_log(
        body.encode("utf-8"), registry=default_registry(), evidence=_evidence()
    )

    assert result.frame_stream_kind is FrameStreamKind.displayed
    assert result.quality is LogQuality.limited
    assert any("displayed" in item for item in result.limitations)


def test_unregistered_format_is_refused_and_the_original_stays_readable(tmp_path):
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    service = FileService(repository, tmp_path / "managed")
    published = service.publish(
        kind="import",
        files={"logs/unregistered.csv": (SAMPLE_DIR / "unregistered.csv").read_bytes()},
        user_scope_id=SCOPE,
        method="POST",
        canonical_route="/api/log-imports",
        key="log-1",
        request_schema_version="product-contract/0.4",
        request_hash=hashlib.sha256(b"unregistered").hexdigest(),
    )
    file_id = published.file_ids[0]
    original = service.read_managed_file(file_id)

    from game_app.contracts.common import EvidenceRef

    evidence = EvidenceRef(
        kind="artifact",
        file_id=file_id,
        content_hash=hashlib.sha256(original).hexdigest(),
        locator=f"objects/{file_id}",
    )
    with pytest.raises(ContractError) as caught:
        parse_log(original, registry=default_registry(), evidence=evidence)
    assert caught.value.code is ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT
    # The original is retained byte for byte; nothing was rewritten.
    assert service.read_managed_file(file_id) == original
    repository.close()


# --- registry is versioned and distinguishes column sets --------------------


def _column(source: str, target: str, unit: str, conversion: str = "identity") -> LogColumn:
    return LogColumn(
        mapping=ColumnMapping(
            source_column=source,
            target_field=target,
            source_unit=unit,
            target_unit=unit,
            conversion=conversion,
        ),
        required=False,
    )


def _variant_registry() -> FormatRegistry:
    base = default_registry().formats[0]
    registry = FormatRegistry()
    registry.register(base)
    registry.register(LogFormat(
        format_id="offline-frame-csv/no-frame-type",
        tool_name=base.tool_name,
        tool_version=base.tool_version,
        header=tuple(token for token in base.header if token != "FrameType"),
        columns=tuple(c for c in base.columns if c.mapping.source_column != "FrameType"),
        time_basis=base.time_basis,
        time_mapping_rule=base.time_mapping_rule,
        frame_stream_kind=base.frame_stream_kind,
        options_fingerprint=base.options_fingerprint,
        fixture_hashes=base.fixture_hashes,
        checks=base.checks,
    ))
    return registry


def test_same_tool_version_with_different_columns_is_a_different_schema():
    registry = _variant_registry()
    assert len(registry.formats) == 2
    assert registry.formats[0].format_id != registry.formats[1].format_id

    basic = _parse("basic.csv", registry=registry)
    assert basic.format_id == OFFLINE_FORMAT_ID

    stripped_header = ",".join(
        token for token in default_registry().formats[0].header if token != "FrameType"
    )
    rows = [
        "1,4242,0.0,,Application,0.0,application_present,off",
        "2,4242,16.7,16.7,Application,7.5,application_present,off",
    ]
    body = stripped_header + "\n" + "\n".join(rows) + "\n"
    variant = parse_log(body.encode("utf-8"), registry=registry, evidence=_evidence())
    assert variant.format_id == "offline-frame-csv/no-frame-type"
    assert all(frame.frame_type is None for frame in variant.frames)


# --- clock: unknown offset/scale never forces a binding ---------------------


def test_unanchored_clock_keeps_offset_unknown_and_is_not_bindable():
    result = _parse("basic.csv")

    assert isinstance(result.time_mapping, TimeMapping)
    assert result.time_mapping.offset is None
    assert result.time_mapping.scale == 0.001
    assert result.time_mapping.bindable is False
    assert result.time_mapping.source_basis == "log_relative"
    assert result.time_mapping.target_basis == "session_relative_s"
    anchor_check = next(
        check for check in result.time_mapping.checks if check.check_id == "time.anchor"
    )
    assert anchor_check.status is CheckStatus.not_run


def test_time_column_without_a_unit_has_no_scale_and_no_source_range():
    result = _parse("time-no-unit.csv")

    assert result.time_mapping.scale is None
    assert result.time_mapping.bindable is False
    assert result.source_ranges == []
    assert result.quality is LogQuality.limited
    assert any("Time" in item and "unit" in item for item in result.limitations)


# --- malformed rows and the bounded window ----------------------------------


def test_broken_rows_break_the_chain_instead_of_reconnecting_it():
    result = _parse("row-problems.csv")

    validities = [frame.validity for frame in result.frames]
    assert validities[0] == "valid"
    assert validities[1] == "break"
    assert validities[2] == "break"
    # The row after the break is readable, but its interval must not be joined
    # to the earlier run.
    assert validities[3] == "valid"
    assert result.valid_intervals_ms == []
    assert result.quality is LogQuality.limited
    assert any("break" in item for item in result.limitations)


def test_parsing_stops_at_the_deadline_and_keeps_nothing_partial():
    result = _parse("basic.csv", deadline_seconds=0.0)

    assert result.quality is LogQuality.invalid
    assert result.frames == []
    assert any("time budget" in item for item in result.limitations)


def test_parse_result_fills_the_capture_artifact_contract():
    from game_app.contracts.enums import LogQuality as Quality
    from game_app.contracts.performance import CaptureArtifact

    result = _parse("basic.csv")
    artifact = CaptureArtifact(
        id="d" * 32,
        schema_version="product-contract/0.4",
        created_at="2026-11-02T00:00:00+00:00",
        created_by_task_id=None,
        session_id="e" * 32,
        segment_ids=["f" * 32],
        file_id=EVIDENCE_FILE_ID,
        tool_name=result.tool_name,
        tool_version=result.tool_version,
        options={},
        source_schema=result.source_schema,
        source_column_map=result.column_map,
        time_mapping=result.time_mapping,
        quality=result.quality,
        checks=result.checks,
    )
    assert artifact.source_schema == OFFLINE_FORMAT_ID
    assert artifact.time_mapping == result.time_mapping
    assert artifact.quality is Quality.valid


def test_parse_needs_no_repository_and_reports_zero_provider_calls():
    result = _parse("basic.csv")
    # The parser is a pure function: no database, no model, no network.
    assert result.provider_send_count == 0
