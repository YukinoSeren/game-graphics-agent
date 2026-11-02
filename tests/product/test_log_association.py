"""PERF-T02 A-4: bounded log preview and historical association (04 §4.3)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from game_app.contracts.performance import (
    CreateSessionRequest,
    EndSegmentRequest,
    LogAssociationRequest,
)
from game_app.contracts.primitives import BindingRef
from game_app.errors import ContractError, ErrorCode
from game_app.files import PublishHooks
from game_app.performance import PerformanceService
from game_app.repository import canonical_json
from tests.product.support.configuration import USER, context_key, setup, store_raw, uid
from tests.product.test_sessions import FakeClock

SAMPLE_DIR = (
    Path(__file__).resolve().parent.parent / "fixtures" / "product" / "offline-log-basic" / "v1"
)
SOURCE_SCHEMA = "offline-frame-csv/v1"


def _service(repository, files, clock):
    return PerformanceService(repository, files, clock=clock)


def _log_file(files, *, user_scope_id: str = USER, key: str = "fixture-log") -> tuple[str, str]:
    data = (SAMPLE_DIR / "basic.csv").read_bytes()
    published = files.publish(
        kind="import",
        files={f"logs/{key}.csv": data},
        user_scope_id=user_scope_id,
        method="POST",
        canonical_route="/api/log-files",
        key=key,
        request_schema_version="product-contract/0.4",
        request_hash=hashlib.sha256(data).hexdigest(),
    )
    return published.file_ids[0], hashlib.sha256(data).hexdigest()


def _count(repository, entity_type) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def _evidence(file_id: str, sha: str) -> dict:
    return {
        "kind": "artifact",
        "file_id": file_id,
        "content_hash": sha,
        "locator": "basic.csv",
    }


def _historical_payload(installation_id, context_id, device_id, file_id, sha, **overrides):
    payload = {
        "file_id": file_id,
        "content_hash": sha,
        "source_schema": SOURCE_SCHEMA,
        "source_range": {"start": 0.0, "end": 66.8, "time_basis": "log_relative", "unit": "ms"},
        "stream_key": overrides.pop("stream_key", None),
        "source_anchor": 0.0,
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
                "raw_text": "historical run",
            },
            "test_protocol_id": None,
        },
        "evidence_refs": [_evidence(file_id, sha)],
        "raw_text": "关联到 10 月 1 日的记录",
    }
    payload.update(overrides)
    return payload


def _existing_payload(session_id, segment_id, segment_revision, binding_revision,
                      file_id, sha, *, stream_key, close_at_s):
    return {
        "file_id": file_id,
        "content_hash": sha,
        "source_schema": SOURCE_SCHEMA,
        "source_range": {"start": 0.0, "end": 66.8, "time_basis": "log_relative", "unit": "ms"},
        "stream_key": stream_key,
        "source_anchor": 0.0,
        "target_anchor_s": 0.0,
        "target": {
            "mode": "existing_segment",
            "session_id": session_id,
            "expected_session_revision": 1,
            "segment_ref": {
                "segment_id": segment_id,
                "segment_revision": segment_revision,
                "binding_revision": binding_revision,
            },
            "close_at_s": close_at_s,
        },
        "evidence_refs": [_evidence(file_id, sha)],
        "raw_text": "",
    }


def _request(payload) -> LogAssociationRequest:
    return LogAssociationRequest.model_validate_json(canonical_json(payload))


def _inspection_stream(service, file_id, user_scope_id=USER):
    inspection = service.inspect_log(file_id, SOURCE_SCHEMA, user_scope_id)
    return inspection, inspection.streams[0].stream_key


# --- preview ----------------------------------------------------------------


def test_inspect_log_is_bounded_and_read_only(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files, FakeClock())
    file_id, sha = _log_file(files)
    before = _count(repository, "PlaySession") + _count(repository, "CaptureArtifact")

    inspection = service.inspect_log(file_id, SOURCE_SCHEMA, USER)

    assert inspection.file_id == file_id
    assert inspection.content_hash == sha
    assert inspection.source_schema == SOURCE_SCHEMA
    assert len(inspection.source_ranges) == 1
    assert inspection.source_ranges[0].start == 0.0
    assert inspection.source_ranges[0].end == 66.8
    assert inspection.source_ranges[0].unit == "ms"
    assert len(inspection.streams) == 1
    assert inspection.streams[0].process.pid == 4242
    assert inspection.streams[0].process.started_at is None
    assert inspection.streams[0].frame_stream_kind.value == "application_present"
    assert inspection.limitations == []
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0
    assert _count(repository, "PlaySession") + _count(repository, "CaptureArtifact") == before


def test_inspect_log_refuses_a_schema_the_file_does_not_match(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files, FakeClock())
    file_id, _ = _log_file(files)

    with pytest.raises(ContractError) as caught:
        service.inspect_log(file_id, "made-up/v9", USER)

    assert caught.value.code is ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT
    # The original is retained and still consumable.
    assert files.read_managed_file(file_id)


def test_inspect_log_refuses_an_unregistered_header(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files, FakeClock())
    data = (SAMPLE_DIR / "unregistered.csv").read_bytes()
    file_id, _ = _publish_log(files, data, key="log-unreg")

    with pytest.raises(ContractError) as caught:
        service.inspect_log(file_id, SOURCE_SCHEMA, USER)

    assert caught.value.code is ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT
    assert files.read_managed_file(file_id) == data


# --- historical session -----------------------------------------------------


def test_historical_association_creates_an_ended_session_and_closed_binding(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files, FakeClock())
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)

    result = service.associate_log(
        _request(_historical_payload(
            installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
        )),
        context_key("assoc-1"),
    )

    assert [ref.entity_type for ref in result.refs] == ["CaptureArtifact", "ManagedFile"]
    artifact = repository.get_object("CaptureArtifact", result.refs[0].entity_id).payload
    assert artifact["file_id"] == file_id
    assert artifact["source_schema"] == SOURCE_SCHEMA
    assert artifact["time_mapping"]["scale"] == 0.001
    assert artifact["time_mapping"]["offset"] == 0.0
    assert artifact["quality"] == "valid"
    assert artifact["options"] == {}
    session = service.get_session(artifact["session_id"], USER)
    assert session.lifecycle.value == "ended"
    assert session.started_at == "2026-10-01T12:00:00+00:00"
    assert session.ended_at == "2026-10-01T12:00:00.066800+00:00"
    assert session.configuration_state.value == "unverified"
    assert session.initial_applied_id is None
    segment = service.get_segment(artifact["segment_ids"][0], USER)
    assert segment.lifecycle.value == "ended"
    assert segment.start_s == 0.0
    assert abs(segment.end_s - 0.0668) < 1e-9
    assert segment.configuration_state.value == "unverified"
    assert segment.stream_selection.frame_stream_kind.value == "application_present"
    binding = service.get_binding(segment.id, 1, USER)
    assert binding.applied_id is None
    assert binding.effective_range.end == segment.end_s
    assert binding.supersedes_revision is None
    service.require_analyzeable(BindingRef(
        segment_id=segment.id, segment_revision=1, binding_revision=1,
    ), USER)
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM current_applied")[0]["n"] == 0
        events = uow.query(
            "SELECT * FROM domain_events WHERE target_id=?", (session.id,),
        )
    assert events[0]["previous_state"] is None
    assert events[0]["next_state"] == "ended"
    assert events[0]["cause"] == "historical_log_registered"


def test_association_replays_and_conflicts_by_key(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files, FakeClock())
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)
    payload = _historical_payload(
        installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
    )

    first = service.associate_log(_request(payload), context_key("same"))
    replay = service.associate_log(_request(payload), context_key("same"))

    assert [ref.model_dump() for ref in replay.refs] == [ref.model_dump() for ref in first.refs]
    assert _count(repository, "CaptureArtifact") == 1
    assert _count(repository, "PlaySession") == 1

    changed = dict(payload)
    changed["target"] = {**payload["target"], "started_at": "2026-10-02T12:00:00+00:00"}
    with pytest.raises(ContractError) as caught:
        service.associate_log(_request(changed), context_key("same"))
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert _count(repository, "CaptureArtifact") == 1


def test_historical_target_needs_the_anchor_at_the_range_start(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files, FakeClock())
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)
    payload = _historical_payload(
        installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
    )
    payload["source_anchor"] = 16.7

    with pytest.raises(ContractError) as caught:
        service.associate_log(_request(payload), context_key("bad-anchor"))

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "PlaySession") == 0
    assert _count(repository, "CaptureArtifact") == 0


def test_historical_target_needs_evidence_and_a_matching_installation(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files, FakeClock())
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)
    payload = _historical_payload(
        installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
    )
    payload["evidence_refs"] = []

    with pytest.raises(ContractError) as caught:
        service.associate_log(_request(payload), context_key("no-evidence"))
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED

    payload = _historical_payload(
        installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
    )
    payload["target"]["run_declaration"]["installation_id"] = uid(901)
    with pytest.raises(ContractError) as caught:
        service.associate_log(_request(payload), context_key("wrong-install"))
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "PlaySession") == 0


def test_an_unknown_stream_key_or_range_is_refused(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files, FakeClock())
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)

    payload = _historical_payload(
        installation_id, context_id, device_id, file_id, sha,
        stream_key="not-a-stream",
    )
    with pytest.raises(ContractError) as caught:
        service.associate_log(_request(payload), context_key("bad-stream"))
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert caught.value.field_errors[0].path == "stream_key"

    payload = _historical_payload(
        installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
    )
    payload["source_range"] = {"start": 0.0, "end": 1.0, "time_basis": "log_relative", "unit": "s"}
    with pytest.raises(ContractError) as caught:
        service.associate_log(_request(payload), context_key("bad-range"))
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "CaptureArtifact") == 0


def test_a_stale_content_hash_writes_nothing(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files, FakeClock())
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)
    payload = _historical_payload(
        installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
    )
    payload["content_hash"] = "f" * 64

    with pytest.raises(ContractError) as caught:
        service.associate_log(_request(payload), context_key("bad-hash"))

    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert _count(repository, "PlaySession") == 0
    assert _count(repository, "CaptureArtifact") == 0


# --- existing segment -------------------------------------------------------


def _session(repository, files, clock):
    installation_id = None
    repository, files, installation_id, context_id, device_id = setup(tmp_path=Path(
        repository.path.parent
    ))
    return repository, files, installation_id, context_id, device_id


def test_existing_open_segment_closes_at_the_declared_point(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    payload = {
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation_id, "fields": {}, "raw_text": "",
        },
        "test_protocol_id": None,
        "recommendation_id": None,
    }
    session = service.create_session(
        CreateSessionRequest.model_validate_json(canonical_json(payload)),
        context_key("session-1"),
    )
    session_id = session.refs[1].entity_id
    segment_id = session.refs[2].entity_id
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)
    clock.advance(1)

    result = service.associate_log(
        _request(_existing_payload(
            session_id, segment_id, 1, 1, file_id, sha,
            stream_key=stream_key, close_at_s=0.5,
        )),
        context_key("assoc-existing"),
    )

    artifact = repository.get_object("CaptureArtifact", result.refs[0].entity_id).payload
    segment = service.get_segment(segment_id, USER)
    assert segment.lifecycle.value == "ended"
    assert segment.end_s == 0.5
    assert segment.revision == 2
    assert service.get_binding(segment_id, 2, USER).effective_range.end == 0.5
    assert artifact["session_id"] == session_id
    assert artifact["segment_ids"] == [segment_id]
    assert service.get_session(session_id, USER).lifecycle.value == "active"


def test_existing_closed_segment_keeps_its_boundary_and_rejects_a_close_point(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    payload = {
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation_id, "fields": {}, "raw_text": "",
        },
        "test_protocol_id": None,
        "recommendation_id": None,
    }
    session = service.create_session(
        CreateSessionRequest.model_validate_json(canonical_json(payload)),
        context_key("session-1"),
    )
    session_id = session.refs[1].entity_id
    segment_id = session.refs[2].entity_id
    clock.advance(1)
    service.end_segment(
        EndSegmentRequest.model_validate_json(canonical_json({
            "segment_id": segment_id,
            "expected_segment_revision": 1,
            "expected_binding_revision": 1,
        })),
        context_key("end-1"),
    )
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)

    result = service.associate_log(
        _request(_existing_payload(
            session_id, segment_id, 2, 2, file_id, sha,
            stream_key=stream_key, close_at_s=None,
        )),
        context_key("assoc-closed"),
    )

    assert result.refs[0].entity_type == "CaptureArtifact"
    segment = service.get_segment(segment_id, USER)
    assert segment.end_s == 1.0
    assert segment.revision == 2

    with pytest.raises(ContractError) as caught:
        service.associate_log(
            _request(_existing_payload(
                session_id, segment_id, 2, 2, file_id, sha,
                stream_key=stream_key, close_at_s=0.9,
            )),
            context_key("assoc-closed-bad"),
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED


def test_a_stale_segment_revision_writes_no_artifact(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    payload = {
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation_id, "fields": {}, "raw_text": "",
        },
        "test_protocol_id": None,
        "recommendation_id": None,
    }
    session = service.create_session(
        CreateSessionRequest.model_validate_json(canonical_json(payload)),
        context_key("session-1"),
    )
    session_id = session.refs[1].entity_id
    segment_id = session.refs[2].entity_id
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)
    clock.advance(1)

    with pytest.raises(ContractError) as caught:
        service.associate_log(
            _request(_existing_payload(
                session_id, segment_id, 7, 1, file_id, sha,
                stream_key=stream_key, close_at_s=0.5,
            )),
            context_key("stale"),
        )

    assert caught.value.code is ErrorCode.BINDING_REVISION_CONFLICT
    assert caught.value.field_errors[0].path == "segment_revision"
    assert _count(repository, "CaptureArtifact") == 0
    assert service.get_segment(segment_id, USER).revision == 1


def test_another_users_session_cannot_be_targeted(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    payload = {
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation_id, "fields": {}, "raw_text": "",
        },
        "test_protocol_id": None,
        "recommendation_id": None,
    }
    session = service.create_session(
        CreateSessionRequest.model_validate_json(canonical_json(payload)),
        context_key("session-1"),
    )
    session_id = session.refs[1].entity_id
    segment_id = session.refs[2].entity_id
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)
    other = context_key("other").model_copy(update={"user_scope_id": uid(999)})

    with pytest.raises(ContractError) as caught:
        service.associate_log(
            _request(_existing_payload(
                session_id, segment_id, 1, 1, file_id, sha,
                stream_key=stream_key, close_at_s=0.5,
            )),
            other,
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _count(repository, "CaptureArtifact") == 0


# --- idempotent retries must survive a commit-window failure -----------------


def test_a_retry_after_a_failed_commit_returns_the_same_artifact(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)
    request = _request(_historical_payload(
        installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
    ))

    def fail():
        raise RuntimeError("commit window failure")

    with pytest.raises(RuntimeError):
        service.associate_log(
            request, context_key("assoc-retry"), hooks=PublishHooks(before_commit=fail)
        )
    clock.advance(30)

    result = service.associate_log(request, context_key("assoc-retry"))

    assert result.refs[0].entity_type == "CaptureArtifact"
    assert _count(repository, "CaptureArtifact") == 1
    assert _count(repository, "PlaySession") == 1


# --- review follow-ups: ownership, in-transaction checks, honest selections --

from game_app.files import PublishHooks


def _publish_log(files, data: bytes, *, user_scope_id: str = USER, key: str = "log-publish"):
    published = files.publish(
        kind="import",
        files={"logs/basic.csv": data},
        user_scope_id=user_scope_id,
        method="POST",
        canonical_route="/api/log-files",
        key=key,
        request_schema_version="product-contract/0.4",
        request_hash=hashlib.sha256(data).hexdigest(),
    )
    return published.file_ids[0], hashlib.sha256(data).hexdigest()


def _other_user(repository, files, clock):
    """Another user's installation, context, device and an open session."""
    from game_app.contracts.device import GameInstallation
    from game_app.contracts.primitives import utc_now
    from game_app.contracts.performance import CreateSessionRequest
    from tests.product.support.configuration import unknown_platform

    other = uid(950)
    installation_id = uid(951)
    with repository.unit_of_work() as uow:
        uow.insert_versioned("GameInstallation", GameInstallation(
            id=installation_id,
            schema_version="product-contract/0.4",
            created_at=utc_now(),
            created_by_task_id=None,
            series_id=installation_id,
            version=1,
            revision=1,
            user_scope_id=other,
            game_id="b" * 32,
            origin="user_provided",
            platform=unknown_platform(),
            private_path_ref=None,
        ))
    context_id = store_raw(repository, "GameContext", {
        "id": uid(952), "installation_id": installation_id, "game_id": "b" * 32,
    })
    device_id = store_raw(repository, "DeviceSnapshot", {
        "id": uid(953), "user_scope_id": other,
    })
    service = PerformanceService(repository, files, clock=clock)
    created = service.create_session(
        CreateSessionRequest.model_validate_json(canonical_json({
            "installation_id": installation_id,
            "device_snapshot_id": device_id,
            "game_context_id": context_id,
            "applied_id": None,
            "configuration_state": "unverified",
            "run_declaration": {
                "installation_id": installation_id, "fields": {}, "raw_text": "",
            },
            "test_protocol_id": None,
            "recommendation_id": None,
        })),
        context_key("other-session", user=other),
    )
    return other, installation_id, context_id, device_id, created


def test_inspect_and_association_hide_another_users_file(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    data = (SAMPLE_DIR / "basic.csv").read_bytes()
    file_id, sha = _publish_log(files, data)
    _, stream_key = _inspection_stream(service, file_id)
    other, other_installation, other_context, other_device, _ = _other_user(
        repository, files, clock
    )

    with pytest.raises(ContractError) as caught:
        service.inspect_log(file_id, SOURCE_SCHEMA, other)
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND

    payload = _historical_payload(
        other_installation, other_context, other_device, file_id, sha, stream_key=stream_key,
    )
    with pytest.raises(ContractError) as caught:
        service.associate_log(
            _request(payload),
            context_key("foreign-file").model_copy(update={"user_scope_id": other}),
        )
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _count(repository, "CaptureArtifact") == 0


def test_a_file_without_a_recorded_owner_is_not_consumer_visible(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files, FakeClock())
    data = (SAMPLE_DIR / "basic.csv").read_bytes()
    file_id = files.store_artifact(data, relative_name="basic.csv", stable_key="anon-log")

    with pytest.raises(ContractError) as caught:
        service.inspect_log(file_id, SOURCE_SCHEMA, USER)

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_the_source_hash_is_rechecked_inside_the_write_transaction(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    data = (SAMPLE_DIR / "basic.csv").read_bytes()
    file_id, sha = _publish_log(files, data)
    _, stream_key = _inspection_stream(service, file_id)
    request = _request(_historical_payload(
        installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
    ))

    def tamper():
        (files.objects_dir / file_id).write_bytes(data + b"tampered\n")

    with pytest.raises(ContractError) as caught:
        service.associate_log(
            request, context_key("tampered"), hooks=PublishHooks(before_commit=tamper)
        )

    assert caught.value.code is ErrorCode.ARTIFACT_UNAVAILABLE
    assert _count(repository, "CaptureArtifact") == 0
    assert _count(repository, "PlaySession") == 0


def test_an_existing_target_records_the_selected_stream(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    session_payload = {
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation_id, "fields": {}, "raw_text": "",
        },
        "test_protocol_id": None,
        "recommendation_id": None,
    }
    session = service.create_session(
        CreateSessionRequest.model_validate_json(canonical_json(session_payload)),
        context_key("session-1"),
    )
    session_id = session.refs[1].entity_id
    segment_id = session.refs[2].entity_id
    data = (SAMPLE_DIR / "basic.csv").read_bytes()
    file_id, sha = _publish_log(files, data)
    _, stream_key = _inspection_stream(service, file_id)
    clock.advance(1)

    service.associate_log(
        _request(_existing_payload(
            session_id, segment_id, 1, 1, file_id, sha,
            stream_key=stream_key, close_at_s=0.5,
        )),
        context_key("assoc-existing"),
    )

    segment = service.get_segment(segment_id, USER)
    assert segment.stream_selection.frame_stream_kind.value == "application_present"
    assert segment.stream_selection.process.pid == 4242
    stream_check = next(
        check for check in segment.stream_selection.checks
        if check.check_id == "segment.stream"
    )
    assert stream_check.status.value == "pass"


def test_an_unknown_frame_stream_is_never_claimed_as_application_present(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    data = (SAMPLE_DIR / "fg-unknown.csv").read_bytes()
    file_id, sha = _publish_log(files, data)

    inspection = service.inspect_log(file_id, SOURCE_SCHEMA, USER)

    assert inspection.streams
    assert {item.frame_stream_kind.value for item in inspection.streams} == {"unknown"}
    assert any("frame generation" in note for note in inspection.limitations)


def test_the_artifact_cites_its_declaration_manifest(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)

    result = service.associate_log(
        _request(_historical_payload(
            installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
        )),
        context_key("assoc-manifest"),
    )

    artifact = repository.get_object("CaptureArtifact", result.refs[0].entity_id).payload
    manifest_id = result.refs[1].entity_id
    for check in artifact["checks"]:
        if check["check_id"] in ("log.association", "log.evidence"):
            assert any(
                ref["kind"] == "artifact" and ref["file_id"] == manifest_id
                for ref in check["evidence_refs"]
            ), check["check_id"]
    evidence_check = next(
        check for check in artifact["checks"] if check["check_id"] == "log.evidence"
    )
    assert evidence_check["status"] == "pass"
    assert evidence_check["evidence_refs"]


def test_foreign_identifiers_are_not_found_rather_than_conflicts(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        CreateSessionRequest.model_validate_json(canonical_json({
            "installation_id": installation_id,
            "device_snapshot_id": device_id,
            "game_context_id": context_id,
            "applied_id": None,
            "configuration_state": "unverified",
            "run_declaration": {
                "installation_id": installation_id, "fields": {}, "raw_text": "",
            },
            "test_protocol_id": None,
            "recommendation_id": None,
        })),
        context_key("my-session"),
    )
    my_segment = created.refs[2].entity_id
    other, other_installation, other_context, other_device, other_session = _other_user(
        repository, files, clock
    )
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)

    foreign_segment = _existing_payload(
        other_session.refs[1].entity_id, my_segment, 1, 1, file_id, sha,
        stream_key=stream_key, close_at_s=1.0,
    )
    with pytest.raises(ContractError) as caught:
        service.associate_log(
            _request(foreign_segment),
            context_key("foreign-segment").model_copy(update={"user_scope_id": other}),
        )
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND

    foreign_context = _historical_payload(
        other_installation, context_id, other_device, file_id, sha, stream_key=stream_key,
    )
    with pytest.raises(ContractError) as caught:
        service.associate_log(
            _request(foreign_context),
            context_key("foreign-context").model_copy(update={"user_scope_id": other}),
        )
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _count(repository, "CaptureArtifact") == 0


def test_a_close_point_cannot_lie_in_the_future(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    session = service.create_session(
        CreateSessionRequest.model_validate_json(canonical_json({
            "installation_id": installation_id,
            "device_snapshot_id": device_id,
            "game_context_id": context_id,
            "applied_id": None,
            "configuration_state": "unverified",
            "run_declaration": {
                "installation_id": installation_id, "fields": {}, "raw_text": "",
            },
            "test_protocol_id": None,
            "recommendation_id": None,
        })),
        context_key("session-1"),
    )
    session_id = session.refs[1].entity_id
    segment_id = session.refs[2].entity_id
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)
    clock.advance(1)

    with pytest.raises(ContractError) as caught:
        service.associate_log(
            _request(_existing_payload(
                session_id, segment_id, 1, 1, file_id, sha,
                stream_key=stream_key, close_at_s=100.0,
            )),
            context_key("future-close"),
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert service.get_segment(segment_id, USER).end_s is None
    assert _count(repository, "CaptureArtifact") == 0


def test_a_historical_end_cannot_lie_in_the_future(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    file_id, sha = _log_file(files)
    _, stream_key = _inspection_stream(service, file_id)
    payload = _historical_payload(
        installation_id, context_id, device_id, file_id, sha, stream_key=stream_key,
    )
    payload["target"]["started_at"] = clock()

    with pytest.raises(ContractError) as caught:
        service.associate_log(_request(payload), context_key("future-end"))

    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert _count(repository, "PlaySession") == 0
