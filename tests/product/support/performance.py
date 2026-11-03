"""Shared offline fixtures for the performance slices (no product writes).

The helpers build sessions, managed logs and artifacts through the real
product entries wherever possible; the few crafted records exist only for
states the product intentionally refuses to create (open-revision summaries).
"""

from __future__ import annotations

import hashlib

from game_app.analyzer import ANALYZER_VERSION, CLEANING_PROTOCOL_VERSION
from game_app.contracts.performance import (
    CaptureArtifact,
    CreateSessionRequest,
    LogAssociationRequest,
    PerformanceSummary,
)
from game_app.contracts.primitives import BindingRef, utc_now
from game_app.contracts.tasks import AnalysisInput
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


def wired(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    tasks = TaskService(repository, files=files, clock=clock)
    service = PerformanceService(repository, files, clock=clock, tasks=tasks)
    tasks.handlers["analysis"] = service.handle_analysis
    return (
        repository, files, service, tasks,
        installation_id, context_id, device_id, clock,
    )


def csv_bytes(
    intervals: list[float | str | None],
    *,
    stream: str = "application_present",
    frame_generation: str = "off",
) -> bytes:
    lines = [HEADER]
    moment = 0.0
    lines.append(f"1,4242,0.0,,Application,0.0,{stream},{frame_generation}")
    for index, interval in enumerate(intervals, start=2):
        if isinstance(interval, str):
            moment += 10.0
        elif interval is not None:
            moment += interval
        cell = "" if interval is None else str(interval)
        lines.append(
            f"{index},4242,{moment},{cell},Application,7.5,{stream},{frame_generation}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def publish_log(files, data: bytes, key: str, *, user: str = USER) -> tuple[str, str]:
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


def artifact_from_csv(
    service, files, installation_id, context_id, device_id, data, key,
) -> tuple[CaptureArtifact, BindingRef]:
    file_id, sha = publish_log(files, data, key)
    inspection = service.inspect_log(file_id, SCHEMA, USER)
    assert inspection.source_ranges
    source = inspection.source_ranges[0]
    payload = {
        "file_id": file_id,
        "content_hash": sha,
        "source_schema": SCHEMA,
        "source_range": source.model_dump(mode="json"),
        "stream_key": inspection.streams[0].stream_key,
        "source_anchor": source.start,
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
    request = LogAssociationRequest.model_validate_json(canonical_json(payload))
    result = service.associate_log(request, context_key(f"{key}-assoc"))
    artifact_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "CaptureArtifact"
    )
    artifact = load_object(service, "CaptureArtifact", artifact_id, CaptureArtifact)
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


def load_object(service, entity_type, object_id, model):
    with service.repository.unit_of_work() as uow:
        stored = uow.find_object(entity_type, object_id)
    return model.model_validate_json(stored.payload_bytes)


def analysis_input(artifact_id, binding_ref, **overrides) -> AnalysisInput:
    payload = {
        "kind": "analysis",
        "artifact_id": artifact_id,
        "bindings": [binding_ref.model_dump(mode="json")],
        "analyzer_version": ANALYZER_VERSION,
        "cleaning_protocol_version": CLEANING_PROTOCOL_VERSION,
    }
    payload.update(overrides)
    return AnalysisInput.model_validate_json(canonical_json(payload))


def run_analysis(service, tasks, request, key="analysis-1"):
    accepted = service.analyze(request, context_key(key))
    outcome = tasks.run_once()
    assert outcome is not None and outcome.task_id == accepted.task_id
    return accepted, tasks.get_task(accepted.task_id)


def summary_of(service, task_view) -> PerformanceSummary:
    ref = next(
        item for item in task_view.task.result_refs
        if item.entity_type == "PerformanceSummary"
    )
    return load_object(service, "PerformanceSummary", ref.entity_id, PerformanceSummary)


def definition_id(rule) -> str:
    from game_app.performance import _metric_definition_id

    return _metric_definition_id(rule)


def metric_of(summary, key: str):
    from game_app import metrics

    rule = next(item for item in metrics.RULES if item.metric_key == key)
    return next(
        item for item in summary.metrics if item.definition_id == definition_id(rule)
    )


def external_calls(repository) -> int:
    with repository.unit_of_work() as uow:
        return uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"]


def count_objects(repository, entity_type: str) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def craft_artifact(
    repository, *, session_id, segment_id, file_id, offset, scale,
    segment_ids=None,
) -> CaptureArtifact:
    payload = {
        "id": uid(int(hashlib.sha256(
            f"{session_id}:{segment_id}:{file_id}".encode("utf-8")
        ).hexdigest()[:12], 16)),
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


def open_session_artifact(
    repository, files, service, installation, context, device, data, key,
) -> tuple[CaptureArtifact, BindingRef]:
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
    file_id, _ = publish_log(files, data, key)
    artifact = craft_artifact(
        repository, session_id=session_id, segment_id=segment_id,
        file_id=file_id, offset=0.0, scale=0.001,
    )
    return artifact, BindingRef(
        segment_id=segment_id, segment_revision=1, binding_revision=1,
    )


def crafted_summary(
    repository, *, artifact: CaptureArtifact, binding_refs: list[BindingRef],
    effective_duration_s: float = 0.1, quality: str = "limited",
) -> PerformanceSummary:
    """A summary with an open/foreign revision, for refusal tests only."""
    payload = {
        "id": uid(int(hashlib.sha256(
            f"summary:{artifact.id}:{binding_refs!r}".encode("utf-8")
        ).hexdigest()[:12], 16)),
        "schema_version": "product-contract/0.4",
        "created_at": utc_now(),
        "created_by_task_id": None,
        "artifact_id": artifact.id,
        "bindings": [item.model_dump(mode="json") for item in binding_refs],
        "analyzed_ranges": [{
            "start": 0.0,
            "end": max(effective_duration_s, 0.001),
            "time_basis": "session",
            "unit": "s",
        }],
        "analyzer_version": ANALYZER_VERSION,
        "cleaning_protocol_version": CLEANING_PROTOCOL_VERSION,
        "manifest_file_id": uid(int(hashlib.sha256(
            f"manifest:{artifact.id}".encode("utf-8")
        ).hexdigest()[:12], 16)),
        "valid_interval_count": 1 if effective_duration_s > 0 else 0,
        "effective_duration_s": effective_duration_s,
        "metrics": [],
        "adaptive_long_frames": [],
        "quality": quality,
        "checks": [],
    }
    summary = PerformanceSummary.model_validate_json(canonical_json(payload))
    store_raw(repository, "PerformanceSummary", summary.model_dump(mode="json"))
    return summary
