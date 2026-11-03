"""PERF-T03 B-2: frozen comparison protocols and common-threshold comparisons.

Every fixture is offline: two synthetic recordings, no capture tool, no game and
no provider call.  The tests assert both the arithmetic and the refusals.
"""

from __future__ import annotations

import pytest

from game_app.contracts.common import MutationContext
from game_app.contracts.performance import (
    CaptureArtifact,
    CompareInput,
    CreateComparisonProtocolRequest,
    CreateTestProtocolRequest,
    EndSegmentRequest,
    PerformanceSummary,
)
from game_app.contracts.primitives import BindingRef
from game_app.errors import ContractError, ErrorCode
from game_app.repository import canonical_json
from tests.product.support.configuration import USER, context_key
from tests.product.support.performance import (
    analysis_input,
    artifact_from_csv,
    count_objects,
    crafted_summary,
    csv_bytes,
    external_calls,
    load_object,
    open_session_artifact,
    run_analysis,
    summary_of,
    wired,
)

CLEANING = "perf-cleaning/1"
CLUSTERING = "perf-long-frame-clustering/1"
DENOMINATOR = "perf-long-frame-denominator/1"


def _ctx(key: str) -> MutationContext:
    return context_key(key)


def _protocol_payload(**overrides) -> dict:
    payload = {
        "threshold_mode": "fixed",
        "threshold_ms": 50.0,
        "baseline_summary_ids": [],
        "baseline_binding_refs": [],
        "cleaning_definition": CLEANING,
        "clustering_definition": CLUSTERING,
        "denominator_definition": DENOMINATOR,
        "exploratory": True,
    }
    payload.update(overrides)
    return payload


def _protocol_request(**overrides) -> CreateComparisonProtocolRequest:
    return CreateComparisonProtocolRequest.model_validate_json(
        canonical_json(_protocol_payload(**overrides))
    )


def _create_protocol(service, request, key: str):
    result = service.create_comparison_protocol(request, _ctx(key))
    return service.get_comparison_protocol(result.refs[0].entity_id, USER)


def _pair(tmp_path, left_intervals, right_intervals, *,
          left_data: bytes | None = None, right_data: bytes | None = None):
    repository, files, service, tasks, installation, context, device, clock = wired(tmp_path)
    left_artifact, left_binding = artifact_from_csv(
        service, files, installation, context, device,
        left_data if left_data is not None else csv_bytes(left_intervals), "left",
    )
    right_artifact, right_binding = artifact_from_csv(
        service, files, installation, context, device,
        right_data if right_data is not None else csv_bytes(right_intervals), "right",
    )
    _, left_view = run_analysis(
        service, tasks, analysis_input(left_artifact.id, left_binding), key="left-analysis",
    )
    _, right_view = run_analysis(
        service, tasks, analysis_input(right_artifact.id, right_binding), key="right-analysis",
    )
    return (
        repository, files, service, tasks, clock,
        summary_of(service, left_view), summary_of(service, right_view),
    )


def _compare(service, left: PerformanceSummary, right: PerformanceSummary, protocol, key: str):
    result = service.compare(
        CompareInput(summary_ids=[left.id, right.id], protocol_id=protocol.id),
        _ctx(key),
    )
    return service.get_comparison(result.refs[0].entity_id, USER)


def _threshold_metrics(comparison):
    return [
        item for item in comparison.per_metric
        if item.left is not None and item.left.unit in {"clusters/min", "ratio"}
    ]


def test_empty_library_fixed_defaults_to_50_and_rejects_bad_thresholds(tmp_path):
    repository, files, service, tasks, clock, left, right = _pair(
        tmp_path, [10, 20, 30, 40], [10, 20, 30, 40]
    )
    assert count_objects(repository, "ComparisonProtocol") == 0

    protocol = _create_protocol(service, _protocol_request(threshold_ms=None), "default")
    assert protocol.threshold_ms == 50.0
    assert protocol.threshold_mode.value == "fixed"

    for bad in (0, -5.0, float("nan")):
        with pytest.raises(ContractError) as caught:
            service.create_comparison_protocol(
                _protocol_payload(threshold_ms=bad), _ctx(f"bad-{bad}")
            )
        assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert count_objects(repository, "ComparisonProtocol") == 1


def test_derived_threshold_is_frozen_and_replays_without_recompute(tmp_path):
    repository, files, service, tasks, clock, baseline, later = _pair(
        tmp_path, [40, 40, 40, 40], [70, 70, 70, 70]
    )
    request = _protocol_request(
        threshold_mode="baseline_derived",
        threshold_ms=None,
        baseline_summary_ids=[baseline.id],
        baseline_binding_refs=[item.model_dump(mode="json") for item in baseline.bindings],
    )
    protocol = _create_protocol(service, request, "derived")
    assert protocol.threshold_ms == 80.0  # max(50, 2 × median 40)
    assert protocol.frozen_at == clock()

    replay = _create_protocol(service, request, "derived")
    assert replay.id == protocol.id
    assert replay.frozen_at == protocol.frozen_at
    assert count_objects(repository, "ComparisonProtocol") == 1

    # A deleted baseline original cannot change a replay: the threshold and
    # frozen_at come from the committed protocol, not from a new calculation.
    artifact = load_object(service, "CaptureArtifact", baseline.artifact_id, CaptureArtifact)
    (files.root / "objects" / artifact.file_id).unlink()
    still = _create_protocol(service, request, "derived")
    assert still.id == protocol.id
    assert still.frozen_at == protocol.frozen_at
    assert still.threshold_ms == 80.0

    with pytest.raises(ContractError) as caught:
        service.create_comparison_protocol(
            _protocol_request(
                threshold_mode="baseline_derived",
                threshold_ms=None,
                baseline_summary_ids=[baseline.id],
                baseline_binding_refs=[
                    item.model_dump(mode="json") for item in baseline.bindings
                ],
                exploratory=False,
            ),
            _ctx("derived"),
        )
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert count_objects(repository, "ComparisonProtocol") == 1


def test_derived_refuses_self_reported_threshold_empty_baselines_and_open_bindings(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = wired(tmp_path)
    open_artifact, open_binding = open_session_artifact(
        repository, files, service, installation, context, device,
        csv_bytes([10, 20, 30, 40]), "open-derived",
    )
    open_summary = crafted_summary(
        repository, artifact=open_artifact, binding_refs=[open_binding],
    )

    with pytest.raises(ContractError) as reported:
        service.create_comparison_protocol(
            _protocol_payload(
                threshold_mode="baseline_derived", threshold_ms=50.0,
            ),
            _ctx("self-reported"),
        )
    assert reported.value.code is ErrorCode.INPUT_VALIDATION_FAILED

    with pytest.raises(ContractError) as empty_summaries:
        service.create_comparison_protocol(
            _protocol_payload(
                threshold_mode="baseline_derived", threshold_ms=None,
                baseline_binding_refs=[open_binding.model_dump(mode="json")],
            ),
            _ctx("empty-summaries"),
        )
    assert empty_summaries.value.code is ErrorCode.INPUT_VALIDATION_FAILED

    with pytest.raises(ContractError) as empty_bindings:
        service.create_comparison_protocol(
            _protocol_payload(
                threshold_mode="baseline_derived", threshold_ms=None,
                baseline_summary_ids=[open_summary.id],
            ),
            _ctx("empty-bindings"),
        )
    assert empty_bindings.value.code is ErrorCode.INPUT_VALIDATION_FAILED

    with pytest.raises(ContractError) as open_binding_error:
        service.create_comparison_protocol(
            _protocol_payload(
                threshold_mode="baseline_derived",
                threshold_ms=None,
                baseline_summary_ids=[open_summary.id],
                baseline_binding_refs=[open_binding.model_dump(mode="json")],
            ),
            _ctx("open-binding"),
        )
    assert open_binding_error.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert count_objects(repository, "ComparisonProtocol") == 0


def test_duplicate_summary_and_duplicate_original_are_rejected(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = wired(tmp_path)
    artifact, binding = artifact_from_csv(
        service, files, installation, context, device, csv_bytes([10, 20, 30, 40]), "dup",
    )
    _, first_view = run_analysis(
        service, tasks, analysis_input(artifact.id, binding), key="dup-1",
    )
    _, second_view = run_analysis(
        service, tasks, analysis_input(artifact.id, binding), key="dup-2",
    )
    first = summary_of(service, first_view)
    second = summary_of(service, second_view)
    refs = [item.model_dump(mode="json") for item in first.bindings]

    with pytest.raises(ContractError) as duplicate_id:
        service.create_comparison_protocol(
            _protocol_payload(
                threshold_mode="baseline_derived", threshold_ms=None,
                baseline_summary_ids=[first.id, first.id],
                baseline_binding_refs=refs,
            ),
            _ctx("duplicate-id"),
        )
    assert duplicate_id.value.code is ErrorCode.INPUT_VALIDATION_FAILED

    with pytest.raises(ContractError) as duplicate_original:
        service.create_comparison_protocol(
            _protocol_payload(
                threshold_mode="baseline_derived", threshold_ms=None,
                baseline_summary_ids=[first.id, second.id],
                baseline_binding_refs=refs,
            ),
            _ctx("duplicate-original"),
        )
    assert duplicate_original.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert count_objects(repository, "ComparisonProtocol") == 0


def test_common_threshold_recomputes_both_sides(tmp_path):
    sequence = [60, 70, 16, 50, 80]
    repository, files, service, tasks, clock, left, right = _pair(
        tmp_path, sequence, sequence
    )
    protocol = _create_protocol(service, _protocol_request(threshold_ms=50.0), "common")
    comparison = _compare(service, left, right, protocol, "common-compare")

    rate, share = _threshold_metrics(comparison)
    duration = left.effective_duration_s
    assert rate.status.value == "pass"
    assert rate.left.numeric.value == pytest.approx(60 * 2 / duration)
    assert rate.right.numeric.value == pytest.approx(60 * 2 / duration)
    assert rate.delta.numeric.value == pytest.approx(0.0)
    assert share.left.numeric.value == pytest.approx(210.0 / (1000.0 * duration))
    assert share.right.numeric.value == pytest.approx(210.0 / (1000.0 * duration))
    assert comparison.level.value == "descriptive"  # historical, unverified config
    assert _count_status(comparison, "comparison.configuration") == "unknown"


def _count_status(comparison, check_id: str) -> str:
    return next(
        item.status.value for item in comparison.checks if item.check_id == check_id
    )


def test_cluster_rate_is_per_minute_and_zero_duration_is_unknown(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = wired(tmp_path)
    # 50ms is *equal* to the threshold and therefore not long; 80ms is.
    base = [50.0] * 1200
    base[100] = 80.0
    base[300] = 80.0
    doubled = base + base
    minute_artifact, minute_binding = artifact_from_csv(
        service, files, installation, context, device, csv_bytes(base), "minute",
    )
    doubled_artifact, doubled_binding = artifact_from_csv(
        service, files, installation, context, device, csv_bytes(doubled), "doubled",
    )
    _, minute_view = run_analysis(
        service, tasks, analysis_input(minute_artifact.id, minute_binding), key="minute-a",
    )
    _, doubled_view = run_analysis(
        service, tasks, analysis_input(doubled_artifact.id, doubled_binding), key="doubled-a",
    )
    minute = summary_of(service, minute_view)
    two_minutes = summary_of(service, doubled_view)

    protocol = _create_protocol(service, _protocol_request(threshold_ms=50.0), "rate")
    comparison = _compare(service, minute, two_minutes, protocol, "rate-compare")
    rate, _ = _threshold_metrics(comparison)
    assert rate.left.numeric.value == pytest.approx(rate.right.numeric.value, rel=1e-9)
    assert rate.left.numeric.value == pytest.approx(60 * 2 / minute.effective_duration_s)

    # A summary with no valid interval has no duration; the threshold metric is
    # unknown with a reason, never a division by zero and never 0.
    zero_data = "\n".join([
        "EventNumber,ProcessId,Time (ms),MsBetweenPresents (ms),FrameType,"
        "MsGPUActive (ms),FrameStream,FrameGeneration",
        "1,4242,0.0,,Application,0.0,application_present,off",
        "2,4242,10.0,not-a-number,Application,7.5,application_present,off",
    ]).encode("utf-8") + b"\n"
    zero_artifact, zero_binding = artifact_from_csv(
        service, files, installation, context, device, zero_data, "zero",
    )
    _, zero_view = run_analysis(
        service, tasks, analysis_input(zero_artifact.id, zero_binding), key="zero-analysis",
    )
    zero = summary_of(service, zero_view)
    zero_comparison = _compare(service, zero, minute, protocol, "zero-compare")
    zero_rate, _ = _threshold_metrics(zero_comparison)
    assert zero_rate.status.value == "unknown"
    assert zero_rate.delta is None
    assert zero_rate.reason


def test_missing_original_degrades_only_the_threshold_metrics(tmp_path):
    repository, files, service, tasks, clock, left, right = _pair(
        tmp_path, [60, 70, 16, 50, 80], [60, 70, 16, 50, 80]
    )
    protocol = _create_protocol(service, _protocol_request(threshold_ms=50.0), "missing")
    artifacts = {
        item.definition_id: item for item in left.metrics
    }
    assert artifacts
    left_artifact = load_object(service, "CaptureArtifact", left.artifact_id, CaptureArtifact)
    (files.root / "objects" / left_artifact.file_id).unlink()

    comparison = _compare(service, left, right, protocol, "missing-compare")
    stored = [item for item in comparison.per_metric if item.left.unit in {"fps", "ms"}]
    # Average, P95 and P99 compare from the frozen summaries; the slowest-1%
    # metric was already unknown because of the tail-sample gate, not the file.
    assert sum(item.status.value == "pass" for item in stored) == 3
    threshold_metrics = _threshold_metrics(comparison)
    assert len(threshold_metrics) == 2
    assert all(item.status.value == "unknown" for item in threshold_metrics)
    assert all(item.delta is None for item in threshold_metrics)
    assert all(item.reason for item in threshold_metrics)


def test_post_hoc_non_exploratory_is_rejected_and_exploratory_is_allowed(tmp_path):
    repository, files, service, tasks, clock, left, right = _pair(
        tmp_path, [10, 20, 30, 40], [10, 20, 30, 40]
    )
    with pytest.raises(ContractError) as caught:
        planned = _create_protocol(
            service, _protocol_request(threshold_ms=50.0, exploratory=False), "post-hoc",
        )
        _compare(service, left, right, planned, "post-hoc-compare")
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED

    exploratory = _create_protocol(
        service, _protocol_request(threshold_ms=50.0, exploratory=True), "exploratory",
    )
    comparison = _compare(service, left, right, exploratory, "exploratory-compare")
    assert any("exploratory" in note for note in comparison.limitations)
    reloaded = service.get_comparison_protocol(exploratory.id, USER)
    assert reloaded.frozen_at == exploratory.frozen_at


def test_frozen_before_sampling_allows_a_planned_comparison(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = wired(tmp_path)
    planned = _create_protocol(
        service, _protocol_request(threshold_ms=50.0, exploratory=False), "planned",
    )

    summaries = []
    for index in range(2):
        clock.advance(60)
        artifact, binding = open_session_artifact(
            repository, files, service, installation, context, device,
            csv_bytes([10, 20, 30, 40]), f"planned-{index}",
        )
        clock.advance(30)
        service.end_segment(
            EndSegmentRequest.model_validate_json(canonical_json({
                "segment_id": binding.segment_id,
                "expected_segment_revision": 1,
                "expected_binding_revision": 1,
            })),
            _ctx(f"planned-end-{index}"),
        )
        _, view = run_analysis(
            service, tasks,
            analysis_input(artifact.id, BindingRef(
                segment_id=binding.segment_id, segment_revision=2, binding_revision=2,
            )),
            key=f"planned-analysis-{index}",
        )
        summaries.append(summary_of(service, view))

    comparison = _compare(service, summaries[0], summaries[1], planned, "planned-compare")
    assert comparison.summary_ids == [summaries[0].id, summaries[1].id]
    assert not any("exploratory" in note for note in comparison.limitations)


def test_unknown_configuration_is_never_controlled(tmp_path):
    repository, files, service, tasks, clock, left, right = _pair(
        tmp_path, [10, 20, 30, 40], [10, 20, 30, 40]
    )
    protocol = _create_protocol(service, _protocol_request(), "level")
    comparison = _compare(service, left, right, protocol, "level-compare")
    assert comparison.level.value == "descriptive"
    assert _count_status(comparison, "comparison.configuration") == "unknown"


def test_test_protocol_id_is_wrong_type_and_the_frozen_version_is_used(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = wired(tmp_path)
    artifact, binding = artifact_from_csv(
        service, files, installation, context, device, csv_bytes([60, 70, 80]), "typing",
    )
    other, other_binding = artifact_from_csv(
        service, files, installation, context, device, csv_bytes([60, 70, 80]), "typing-2",
    )
    _, view = run_analysis(
        service, tasks, analysis_input(artifact.id, binding), key="typing-analysis",
    )
    _, other_view = run_analysis(
        service, tasks, analysis_input(other.id, other_binding), key="typing-analysis-2",
    )
    summary = summary_of(service, view)
    other_summary = summary_of(service, other_view)

    test_protocol = service.create_test_protocol(
        CreateTestProtocolRequest.model_validate_json(canonical_json({
            "game_context_id": context,
            "parent_protocol_id": None,
            "scenario": "route",
            "condition_fields": {},
            "controlled_variables": [],
            "metric_registry_version": "project-metrics/1",
            "metric_keys": ["average_present_rate/v1"],
            "warmup_seconds": None,
            "sample_duration_seconds": None,
            "repeat_count": None,
            "note": None,
        })),
        _ctx("test-protocol"),
    )
    with pytest.raises(ContractError) as caught:
        service.compare(
            CompareInput(
                summary_ids=[summary.id, other_summary.id],
                protocol_id=test_protocol.refs[0].entity_id,
            ),
            _ctx("wrong-type"),
        )
    assert caught.value.code is ErrorCode.REFERENCE_INVALID


def test_each_protocol_version_keeps_its_own_threshold(tmp_path):
    repository, files, service, tasks, clock, left, right = _pair(
        tmp_path, [60, 10, 80], [60, 10, 80]
    )
    fifty = _create_protocol(service, _protocol_request(threshold_ms=50.0), "fifty")
    seventy = _create_protocol(service, _protocol_request(threshold_ms=70.0), "seventy")
    assert fifty.id != seventy.id

    at_fifty = _compare(service, left, right, fifty, "at-fifty")
    at_seventy = _compare(service, left, right, seventy, "at-seventy")
    rate_fifty, _ = _threshold_metrics(at_fifty)
    rate_seventy, _ = _threshold_metrics(at_seventy)
    assert rate_fifty.left.numeric.value != rate_seventy.left.numeric.value


def test_comparison_leaves_summaries_and_pointer_untouched(tmp_path):
    repository, files, service, tasks, clock, left, right = _pair(
        tmp_path, [10, 20, 30, 40], [10, 20, 30, 40]
    )
    protocol = _create_protocol(service, _protocol_request(), "untouched")
    with repository.unit_of_work() as uow:
        before_left = uow.find_object("PerformanceSummary", left.id).payload_bytes
        before_right = uow.find_object("PerformanceSummary", right.id).payload_bytes

    _compare(service, left, right, protocol, "untouched-compare")

    with repository.unit_of_work() as uow:
        assert uow.find_object("PerformanceSummary", left.id).payload_bytes == before_left
        assert uow.find_object("PerformanceSummary", right.id).payload_bytes == before_right
        assert uow.query("SELECT COUNT(*) AS n FROM current_applied")[0]["n"] == 0
    assert external_calls(repository) == 0
