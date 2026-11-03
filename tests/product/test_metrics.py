"""PERF-T03 B-1: deterministic analysis and immutable summaries (04 §5—§9).

The log fixtures are hand-authored synthetic CSVs; no capture tool, game or
model is started and every test asserts that ``external_calls`` stays empty.
"""

from __future__ import annotations

import pytest

from game_app.analyzer import long_frame_statistics, slowest_tail_inverse_mean
from game_app.contracts.performance import EndSegmentRequest
from game_app.contracts.primitives import BindingRef
from game_app.errors import ContractError, ErrorCode
from game_app.repository import canonical_json
from tests.product.support.configuration import USER, context_key
from tests.product.support.performance import (
    HEADER,
    analysis_input as _input,
    artifact_from_csv as _artifact_from_csv,
    count_objects,
    craft_artifact as _craft_artifact,
    csv_bytes as _csv,
    external_calls as _external_calls,
    metric_of as _metric,
    open_session_artifact as _open_session_artifact,
    publish_log as _publish,
    run_analysis as _run,
    summary_of as _summary,
    wired as _wired,
)


def _summary_count(repository) -> int:
    return count_objects(repository, "PerformanceSummary")


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
        repository, session_id=artifact.session_id, segment_id=binding.segment_id,
        file_id=file_id, offset=0.0, scale=0.001,
    )

    with pytest.raises(ContractError) as caught:
        service.analyze(_input(artifact.id, binding), context_key("foreign-analysis"))

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _summary_count(repository) == 0


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
