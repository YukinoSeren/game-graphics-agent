"""PERF-T03 B-1: deterministic analysis and immutable summaries (04 §5—§9).

The log fixtures are hand-authored synthetic CSVs; no capture tool, game or
model is started and every test asserts that ``external_calls`` stays empty.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from game_app.analyzer import (
    ANALYZER_VERSION,
    CLEANING_PROTOCOL_VERSION,
    adaptive_threshold_ms,
    long_frame_statistics,
    slowest_tail_inverse_mean,
)
from game_app.contracts.common import MutationContext
from game_app.contracts.performance import (
    CaptureArtifact,
    ColumnMapping,
    EndSegmentRequest,
    LogAssociationRequest,
    PerformanceSummary,
    TimeMapping,
)
from game_app.contracts.primitives import BindingRef, TimeRange, utc_now
from game_app.contracts.tasks import AnalysisInput
from game_app.errors import ContractError, ErrorCode
from game_app.performance import PerformanceService
from game_app.repository import canonical_json
from game_app.tasks import TaskService
from tests.product.support.configuration import USER, context_key, setup, store_raw, uid
from tests.product.test_sessions import FakeClock

SCHEMA = "offline-frame-csv/v1"
HEADER = (
    "EventNumber,ProcessId,Time (ms),MsBetweenPresents (ms),FrameType,"
    "MsGPUActive (ms),FrameStream,FrameGeneration"
)


def _wired(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    tasks = TaskService(repository, files=files, clock=clock)
    service = PerformanceService(repository, files, clock=clock, tasks=tasks)
    tasks.handlers["analysis"] = service.handle_analysis
    return repository, files, service, tasks, installation_id, context_id, device_id, clock


def _csv(intervals: list[float | str | None], *, stream: str = "application_present",
         frame_generation: str = "off") -> bytes:
    lines = [HEADER]
    timestamps: list[float] = []
    moment = 0.0
    lines.append(f"1,4242,0.0,,Application,0.0,{stream},{frame_generation}")
    timestamps.append(0.0)
    for index, interval in enumerate(intervals, start=2):
        if interval is None:
            moment = moment
        elif isinstance(interval, str):
            moment += 10.0
        else:
            moment += interval
        cell = "" if interval is None else str(interval)
        lines.append(f"{index},4242,{moment},{cell},Application,7.5,{stream},{frame_generation}")
        timestamps.append(moment)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _publish(files, data: bytes, key: str, *, user: str = USER) -> tuple[str, str]:
    published = files.publish(
        kind="import",
        files={f"logs/{key}.csv": data},
        user_scope_id=user,
        method="POST",
        canonical_route="/api/log-files",
        key=key,
        request_schema_version="product-contract/0.4",
        request_hash=hashlib.sha256(data).hexdigest(),
    )
    return published.file_ids[0], hashlib.sha256(data).hexdigest()


def _association_payload(installation_id, context_id, device_id, file_id, sha, stream_key):
    inspection_range = None
    return {
        "file_id": file_id,
        "content_hash": sha,
        "source_schema": SCHEMA,
        "source_range": inspection_range,
        "stream_key": stream_key,
        "source_anchor": None,
        "target_anchor_s": 0.0,
        "target": {
            "mode": "historical_session",
            "installation_id": installation_id,
            "device_snapshot_id": device_id,
            "game_context_id": context_id,
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
            "content_hash": sha,
            "locator": "log.csv",
        }],
        "raw_text": "",
    }


def _artifact_from_csv(service, files, installation_id, context_id, device_id, data, key):
    file_id, sha = _publish(files, data, key)
    inspection = service.inspect_log(file_id, SCHEMA, USER)
    assert inspection.source_ranges
    source = inspection.source_ranges[0]
    payload = _association_payload(
        installation_id, context_id, device_id, file_id, sha,
        inspection.streams[0].stream_key,
    )
    payload["source_range"] = source.model_dump(mode="json")
    payload["source_anchor"] = source.start
    request = LogAssociationRequest.model_validate_json(canonical_json(payload))
    result = service.associate_log(request, context_key(f"{key}-assoc"))
    artifact_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "CaptureArtifact"
    )
    artifact = _load(service, "CaptureArtifact", artifact_id)
    with service.repository.unit_of_work() as uow:
        row = uow.query(
            "SELECT * FROM session_segments WHERE segment_id=?",
            (artifact.segment_ids[0],),
        )[0]
    return artifact, BindingRef(
        segment_id=row["segment_id"],
        segment_revision=row["revision"],
        binding_revision=row["binding_revision"],
    )


def _load(service, entity_type, object_id):
    from game_app.contracts.performance import (
        CaptureArtifact as Artifact,
        PerformanceSummary as Summary,
    )

    model = Artifact if entity_type == "CaptureArtifact" else Summary
    with service.repository.unit_of_work() as uow:
        return model.model_validate_json(
            uow.find_object(entity_type, object_id).payload_bytes
        )


def _input(artifact_id, binding_ref, **overrides):
    payload = {
        "kind": "analysis",
        "artifact_id": artifact_id,
        "bindings": [binding_ref.model_dump(mode="json")],
        "analyzer_version": ANALYZER_VERSION,
        "cleaning_protocol_version": CLEANING_PROTOCOL_VERSION,
    }
    payload.update(overrides)
    return AnalysisInput.model_validate_json(canonical_json(payload))


def _run(service, tasks, request, key="analysis-1"):
    accepted = service.analyze(request, context_key(key))
    outcome = tasks.run_once()
    assert outcome is not None and outcome.task_id == accepted.task_id
    return accepted, tasks.get_task(accepted.task_id)


def _summary(service, task_view) -> PerformanceSummary:
    ref = next(
        item for item in task_view.task.result_refs
        if item.entity_type == "PerformanceSummary"
    )
    return _load(service, "PerformanceSummary", ref.entity_id)


def _metric(summary, key: str):
    from game_app import metrics

    rule = next(item for item in metrics.RULES if item.metric_key == key)
    definition_id = _definition_id(rule)
    return next(item for item in summary.metrics if item.definition_id == definition_id)


def _definition_id(rule) -> str:
    from game_app.performance import _metric_definition_id

    return _metric_definition_id(rule)


def _external_calls(repository) -> int:
    with repository.unit_of_work() as uow:
        return uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"]


def _summary_count(repository) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects("PerformanceSummary"))


def test_original_formula_sequence_and_tail_gate(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    artifact, binding = _artifact_from_csv(
        service, files, installation, context, device, _csv([10, 20, 30, 40]), "ten"
    )

    _, view = _run(service, tasks, _input(artifact.id, binding))
    summary = _summary(service, view)

    assert summary.valid_interval_count == 4
    assert summary.effective_duration_s == pytest.approx(0.1)
    assert summary.quality.value == "limited"
    assert _metric(summary, "average_present_rate").numeric.value == 40.0
    assert _metric(summary, "interval_p95_nearest_rank").numeric.value == 40.0
    assert _metric(summary, "interval_p99_nearest_rank").numeric.value == 40.0
    # The algorithm's raw value is 25 fps, but one tail sample is far below the
    # project's 20-sample display gate, so the user-facing value is unknown.
    assert slowest_tail_inverse_mean([10, 20, 30, 40]) == 25.0
    tail = _metric(summary, "slowest_1pct_inverse_mean")
    assert tail.numeric.number_kind == "unknown"
    assert tail.numeric.value is None
    assert tail.numeric.unknown_reason == "not_provided"
    assert _external_calls(repository) == 0


def test_spike_sequence_is_kept_and_adaptive_threshold_is_per_run(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    sequence = [60, 70, 16, 50, 80]
    artifact, binding = _artifact_from_csv(
        service, files, installation, context, device, _csv(sequence), "spikes"
    )

    _, view = _run(service, tasks, _input(artifact.id, binding))
    summary = _summary(service, view)

    # Every declared interval is retained: a real spike is never an outlier.
    assert summary.valid_interval_count == 5
    assert summary.effective_duration_s == pytest.approx(sum(sequence) / 1000.0)
    assert summary.quality.value == "limited"
    assert len(summary.adaptive_long_frames) == 1
    adaptive = summary.adaptive_long_frames[0]
    assert adaptive.threshold_ms == 120.0  # max(50, 2 × median 60)
    assert adaptive.interval_count == 0
    # The comparison's common-threshold rule reuses the same primitive: 60/70/80
    # are three long intervals in two clusters; 50 is not "> 50".
    assert long_frame_statistics(sequence, 50.0) == (2, 3, 210.0)
    assert 210.0 / (1000.0 * summary.effective_duration_s) == pytest.approx(210 / 276)


def test_bad_duplicate_backwards_and_gap_break_the_chain(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    # frame 3 is not-a-number, frame 4 duplicates frame 3's timestamp, and
    # frame 2's interval is missing. No interval may cross any of them.
    data = "\n".join([
        HEADER,
        "1,4242,0.0,,Application,0.0,application_present,off",
        "2,4242,10.0,10.0,Application,7.5,application_present,off",
        "3,4242,20.0,not-a-number,Application,7.5,application_present,off",
        "4,4242,20.0,10.0,Application,7.5,application_present,off",
        "5,4242,30.0,10.0,Application,7.5,application_present,off",
        "6,4242,40.0,10.0,Application,7.5,application_present,off",
    ]) + "\n"
    artifact, binding = _artifact_from_csv(
        service, files, installation, context, device, data.encode("utf-8"), "broken"
    )

    _, view = _run(service, tasks, _input(artifact.id, binding))
    summary = _summary(service, view)

    # 10ms between frames 1-2, then 10ms between 5-6 and 6-7 after the break.
    assert summary.valid_interval_count == 3
    assert summary.effective_duration_s == pytest.approx(0.03)
    assert _metric(summary, "average_present_rate").numeric.value == pytest.approx(100.0)
    checks = {item.check_id: item for item in summary.checks}
    assert "break" in checks["analysis.cleaning"].note


def test_conflicting_streams_are_not_merged(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    data = "\n".join([
        HEADER,
        "1,4242,0.0,,Application,0.0,application_present,off",
        "2,4242,16.7,16.7,Application,7.5,displayed,off",
        "3,4242,33.4,16.7,Application,8.1,application_present,off",
    ]) + "\n"
    artifact, binding = _artifact_from_csv(
        service, files, installation, context, device, data.encode("utf-8"), "streams"
    )

    _, view = _run(service, tasks, _input(artifact.id, binding))
    summary = _summary(service, view)

    assert summary.quality.value == "invalid"
    assert view.task.domain_result.code == "invalid"
    assert all(item.numeric.number_kind == "unknown" for item in summary.metrics)
    assert all(item.numeric.value is None for item in summary.metrics)
    assert summary.adaptive_long_frames == []


def test_no_valid_interval_is_unknown_not_zero(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    data = "\n".join([
        HEADER,
        "1,4242,0.0,,Application,0.0,application_present,off",
        "2,4242,10.0,not-a-number,Application,7.5,application_present,off",
    ]) + "\n"
    artifact, binding = _artifact_from_csv(
        service, files, installation, context, device, data.encode("utf-8"), "empty"
    )

    _, view = _run(service, tasks, _input(artifact.id, binding))
    summary = _summary(service, view)

    assert summary.valid_interval_count == 0
    assert summary.effective_duration_s == 0.0
    assert summary.quality.value == "invalid"
    assert view.task.state.value == "partial"
    assert all(item.numeric.number_kind == "unknown" for item in summary.metrics)
    assert all(item.numeric.value is None for item in summary.metrics)


def _open_session_artifact(repository, files, service, installation, context, device, data, key):
    from game_app.contracts.performance import CreateSessionRequest

    request = CreateSessionRequest.model_validate_json(canonical_json({
        "installation_id": installation,
        "device_snapshot_id": device,
        "game_context_id": context,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation,
            "fields": {},
            "raw_text": "",
        },
        "test_protocol_id": None,
        "recommendation_id": None,
    }))
    result = service.create_session(request, context_key(f"{key}-session"))
    session_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "PlaySession"
    )
    segment_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "SessionSegment"
    )
    file_id, sha = _publish(files, data, key)
    artifact = _craft_artifact(
        repository, session_id=session_id, segment_id=segment_id,
        file_id=file_id, offset=0.0, scale=0.001,
    )
    return artifact, BindingRef(
        segment_id=segment_id, segment_revision=1, binding_revision=1,
    )


def _craft_artifact(repository, *, session_id, segment_id, file_id, offset, scale,
                    segment_ids=None):
    payload = {
        "id": uid(abs(hash((session_id, segment_id, file_id))) % 10**30 or 1),
        "schema_version": "product-contract/0.4",
        "created_at": utc_now(),
        "created_by_task_id": None,
        "session_id": session_id,
        "segment_ids": segment_ids or [segment_id],
        "file_id": file_id,
        "tool_name": "offline-fixture",
        "tool_version": "1",
        "options": {},
        "source_schema": SCHEMA,
        "source_column_map": [{
            "source_column": "Time",
            "target_field": "timestamp",
            "source_unit": "ms",
            "target_unit": "ms",
            "conversion": "identity",
        }],
        "time_mapping": {
            "source_basis": "log_relative",
            "target_basis": "session",
            "source_unit": "ms",
            "target_unit": "s",
            "offset": offset,
            "scale": scale,
            "checks": [],
        },
        "quality": "limited",
        "checks": [],
    }
    artifact = CaptureArtifact.model_validate_json(canonical_json(payload))
    store_raw(repository, "CaptureArtifact", artifact.model_dump(mode="json"))
    return artifact


def test_open_segment_is_refused_without_a_summary(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    artifact, binding = _open_session_artifact(
        repository, files, service, installation, context, device,
        _csv([10, 20, 30, 40]), "open",
    )

    with pytest.raises(ContractError) as caught:
        service.analyze(_input(artifact.id, binding), context_key("open-analysis"))

    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert _summary_count(repository) == 0
    assert repository.find_idempotency(
        USER, "POST", "/api/analyses", "open-analysis"
    ) is None


def test_foreign_artifact_is_not_visible(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    data = _csv([10, 20, 30, 40])
    file_id, _ = _publish(files, data, "foreign", user="b" * 32)
    artifact, binding = _open_session_artifact(
        repository, files, service, installation, context, device, data, "owner",
    )
    # Replace the artifact's file with one published by another user.
    artifact = _craft_artifact(
        repository, session_id=_session_of(repository, artifact), segment_id=binding.segment_id,
        file_id=file_id, offset=0.0, scale=0.001,
    )

    with pytest.raises(ContractError) as caught:
        service.analyze(_input(artifact.id, binding), context_key("foreign-analysis"))

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _summary_count(repository) == 0


def _session_of(repository, artifact) -> str:
    return artifact.session_id


def test_same_key_replays_the_same_summary_and_different_content_conflicts(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    artifact, binding = _artifact_from_csv(
        service, files, installation, context, device, _csv([10, 20, 30, 40]), "idem"
    )
    request = _input(artifact.id, binding)

    other, other_binding = _artifact_from_csv(
        service, files, installation, context, device, _csv([10, 20, 30, 40]), "idem-other"
    )
    first, view = _run(service, tasks, request, key="same")
    summary_id = _summary(service, view).id
    replay = service.analyze(request, context_key("same"))
    assert replay.task_id == first.task_id
    assert _summary(service, tasks.get_task(replay.task_id)).id == summary_id

    with pytest.raises(ContractError) as caught:
        service.analyze(_input(other.id, other_binding), context_key("same"))
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert _summary_count(repository) == 1


def test_pure_conflict_does_not_consume_the_key_then_same_key_succeeds(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    artifact, binding = _open_session_artifact(
        repository, files, service, installation, context, device,
        _csv([10, 20, 30, 40]), "retry",
    )
    key = "retry-analysis"
    clock.advance(60)

    with pytest.raises(ContractError) as caught:
        service.analyze(_input(artifact.id, binding), context_key(key))
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert repository.find_idempotency(USER, "POST", "/api/analyses", key) is None

    # Ending the segment closes both revisions; the same key may now succeed.
    end = EndSegmentRequest.model_validate_json(canonical_json({
        "segment_id": binding.segment_id,
        "expected_segment_revision": 1,
        "expected_binding_revision": 1,
    }))
    service.end_segment(end, context_key("retry-end"))
    accepted = service.analyze(
        _input(artifact.id, BindingRef(
            segment_id=binding.segment_id, segment_revision=2, binding_revision=2,
        )),
        context_key(key),
    )
    assert accepted.task_id
    assert tasks.run_once().task_id == accepted.task_id
    assert _summary_count(repository) == 1


def test_artifact_and_binding_must_name_the_same_segment(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    first, first_binding = _artifact_from_csv(
        service, files, installation, context, device, _csv([10, 20, 30, 40]), "one"
    )
    second, second_binding = _artifact_from_csv(
        service, files, installation, context, device, _csv([10, 20, 30, 40]), "two"
    )

    with pytest.raises(ContractError) as caught:
        service.analyze(_input(first.id, second_binding), context_key("mismatch"))

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _summary_count(repository) == 0


def test_second_analysis_creates_a_new_summary_without_touching_the_old(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = _wired(tmp_path)
    artifact, binding = _artifact_from_csv(
        service, files, installation, context, device, _csv([10, 20, 30, 40]), "twice"
    )
    request = _input(artifact.id, binding)

    _, first_view = _run(service, tasks, request, key="first")
    first = _summary(service, first_view)
    with repository.unit_of_work() as uow:
        first_bytes = uow.find_object("PerformanceSummary", first.id).payload_bytes

    _, second_view = _run(service, tasks, request, key="second")
    second = _summary(service, second_view)

    assert second.id != first.id
    with repository.unit_of_work() as uow:
        assert uow.find_object("PerformanceSummary", first.id).payload_bytes == first_bytes
        assert uow.query("SELECT COUNT(*) AS n FROM current_applied")[0]["n"] == 0
    assert _summary_count(repository) == 2
