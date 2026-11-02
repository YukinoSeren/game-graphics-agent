"""PERF-T02 A-2: sessions, immutable bindings and closed segments (04 §4)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from game_app.adoption import AdoptionService
from game_app.contracts.configuration import HistoricalConfigurationInput
from game_app.contracts.enums import CheckStatus
from game_app.contracts.primitives import BindingRef
from game_app.contracts.performance import (
    BindConfigurationRequest,
    CreateSessionRequest,
    EndSegmentRequest,
    EndSessionRequest,
    SplitSegmentRequest,
)
from game_app.errors import ContractError, ErrorCode
from game_app.performance import PerformanceService
from game_app.repository import ProductRepository, canonical_json
from tests.product.support.configuration import USER, context_key, setup, store_raw, uid


class FakeClock:
    def __init__(self) -> None:
        self.moment = datetime.fromisoformat("2026-11-01T12:00:00+00:00")

    def __call__(self) -> str:
        return self.moment.isoformat()

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


def _service(repository, files, clock):
    return PerformanceService(repository, files, clock=clock)


def _request(installation_id, context_id, device_id, *, fields=None, state="unverified",
             applied_id=None, test_protocol_id=None, declaration_installation=None):
    payload = {
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "applied_id": applied_id,
        "configuration_state": state,
        "run_declaration": {
            "installation_id": declaration_installation or installation_id,
            "fields": fields if fields is not None else {},
            "raw_text": "",
        },
        "test_protocol_id": test_protocol_id,
        "recommendation_id": None,
    }
    return CreateSessionRequest.model_validate_json(canonical_json(payload))


def _end(segment_id, segment_revision=1, binding_revision=1):
    payload = {
        "segment_id": segment_id,
        "expected_segment_revision": segment_revision,
        "expected_binding_revision": binding_revision,
    }
    return EndSegmentRequest.model_validate_json(canonical_json(payload))


def _count(repository, entity_type) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def test_create_without_configuration_opens_a_segment_and_first_binding(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)

    result = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )

    assert [ref.entity_type for ref in result.refs] == [
        "PlayerDeclaration", "PlaySession", "SessionSegment", "ConfigurationBinding", "ManagedFile",
    ]
    session = service.get_session(result.refs[1].entity_id)
    segment = service.get_segment(result.refs[2].entity_id)
    binding = service.get_binding(segment.id, 1)
    assert session.revision == 1
    assert session.lifecycle.value == "active"
    assert session.ended_at is None
    assert session.configuration_state.value == "unverified"
    assert segment.end_s is None
    assert segment.lifecycle.value == "active"
    assert segment.start_s == 0.0
    assert segment.binding_revision == 1
    assert segment.revision == 1
    assert binding.binding_revision == 1
    assert binding.effective_range.start == 0.0
    assert binding.effective_range.end is None
    assert binding.applied_id is None
    assert binding.supersedes_revision is None

    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0


def test_null_applied_cannot_be_declared_bound(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)

    with pytest.raises(ContractError) as caught:
        service.create_session(
            _request(installation_id, context_id, device_id, state="bound"),
            context_key("bad-state"),
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert caught.value.field_errors[0].path == "configuration_state"
    assert _count(repository, "PlaySession") == 0
    assert _count(repository, "SessionSegment") == 0


def test_end_segment_closes_with_a_new_binding_revision_and_keeps_the_old_one(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(12.5)

    result = service.end_segment(_end(segment_id), context_key("end-1"))

    assert [ref.entity_type for ref in result.refs] == ["SessionSegment", "ConfigurationBinding"]
    segment = service.get_segment(segment_id)
    assert segment.end_s == 12.5
    assert segment.lifecycle.value == "ended"
    assert segment.revision == 2
    assert segment.binding_revision == 2
    assert service.get_binding(segment_id, 1).effective_range.end is None
    closed = service.get_binding(segment_id, 2)
    assert closed.effective_range.start == 0.0
    assert closed.effective_range.end == 12.5
    assert closed.supersedes_revision == 1


def test_stale_segment_revision_conflicts_with_zero_writes(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(5)

    with pytest.raises(ContractError) as caught:
        service.end_segment(_end(segment_id, segment_revision=99, binding_revision=1), context_key("stale-seg"))

    assert caught.value.code is ErrorCode.BINDING_REVISION_CONFLICT
    assert caught.value.field_errors[0].path == "segment_revision"
    assert service.get_segment(segment_id).revision == 1
    assert service.get_segment(segment_id).end_s is None
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM idempotency")[0]["n"] == 1  # creation only


def test_stale_binding_revision_points_at_the_binding_dimension(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(5)

    with pytest.raises(ContractError) as caught:
        service.end_segment(_end(segment_id, segment_revision=1, binding_revision=9), context_key("stale-bind"))

    assert caught.value.code is ErrorCode.BINDING_REVISION_CONFLICT
    assert caught.value.field_errors[0].path == "binding_revision"
    with repository.unit_of_work() as uow:
        rows = uow.query("SELECT * FROM configuration_bindings WHERE segment_id=?", (segment_id,))
    assert [row["binding_revision"] for row in rows] == [1]


def test_a_pure_conflict_does_not_consume_the_key(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(3)

    with pytest.raises(ContractError):
        service.end_segment(_end(segment_id, segment_revision=7), context_key("retry-key"))
    result = service.end_segment(_end(segment_id), context_key("retry-key"))

    assert [ref.entity_type for ref in result.refs] == ["SessionSegment", "ConfigurationBinding"]


def test_split_shares_the_boundary_and_freezes_the_old_segment(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    old_id = created.refs[2].entity_id
    clock.advance(10)

    payload = {
        "ending": {
            "segment_id": old_id,
            "expected_segment_revision": 1,
            "expected_binding_revision": 1,
        },
        "new_applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {"installation_id": installation_id, "fields": {}, "raw_text": ""},
    }
    result = service.split_segment(
        SplitSegmentRequest.model_validate_json(canonical_json(payload)), context_key("split-1")
    )

    old = service.get_segment(old_id)
    new_id = result.refs[2].entity_id
    new = service.get_segment(new_id)
    assert old.end_s == 10
    assert old.lifecycle.value == "ended"
    assert new.start_s == old.end_s
    assert new.end_s is None
    assert new.revision == 1
    assert new.binding_revision == 1
    assert new.id != old.id

    clock.advance(2)
    with pytest.raises(ContractError) as caught:
        service.end_segment(_end(old_id), context_key("old-again"))
    assert caught.value.code is ErrorCode.BINDING_REVISION_CONFLICT


def test_end_session_requires_the_exact_active_segment_set(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(4)
    payload = {
        "session_id": created.refs[1].entity_id,
        "expected_session_revision": 1,
        "active_segments": [],
    }

    with pytest.raises(ContractError) as caught:
        service.end_session(
            EndSessionRequest.model_validate_json(canonical_json(payload)), context_key("end-session-bad")
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert service.get_segment(segment_id).end_s is None
    assert service.get_session(created.refs[1].entity_id).lifecycle.value == "active"


def test_end_session_closes_every_segment_and_writes_one_session_event(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    session_id = created.refs[1].entity_id
    first = created.refs[2].entity_id
    clock.advance(10)
    split_payload = {
        "ending": {"segment_id": first, "expected_segment_revision": 1, "expected_binding_revision": 1},
        "new_applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {"installation_id": installation_id, "fields": {}, "raw_text": ""},
    }
    split = service.split_segment(
        SplitSegmentRequest.model_validate_json(canonical_json(split_payload)), context_key("split-1")
    )
    second = split.refs[2].entity_id
    clock.advance(7)
    payload = {
        "session_id": session_id,
        "expected_session_revision": 1,
        "active_segments": [{
            "segment_id": second,
            "expected_segment_revision": 1,
            "expected_binding_revision": 1,
        }],
    }
    result = service.end_session(
        EndSessionRequest.model_validate_json(canonical_json(payload)), context_key("end-session")
    )

    assert result.refs[0].entity_type == "PlaySession"
    session = service.get_session(session_id)
    assert session.lifecycle.value == "ended"
    assert session.revision == 2
    assert session.ended_at is not None
    assert service.get_segment(first).end_s == 10
    assert service.get_segment(second).end_s == 17
    with repository.unit_of_work() as uow:
        events = uow.query(
            "SELECT * FROM domain_events WHERE target_type='PlaySession' AND target_id=?",
            (session_id,),
        )
    assert [row["cause"] for row in events] == ["session_created", "session_ended"]
    assert events[-1]["previous_state"] == "active"
    assert events[-1]["next_state"] == "ended"


def test_bind_configuration_appends_without_moving_the_current_pointer(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    session_id = created.refs[1].entity_id
    segment_id = created.refs[2].entity_id
    declaration_file_id = created.refs[-1].entity_id
    before_bytes = files.read_managed_file(declaration_file_id)
    clock.advance(20)
    service.end_segment(_end(segment_id), context_key("end-1"))
    applied_id = store_raw(repository, "AppliedConfiguration", {
        "id": uid(500),
        "user_scope_id": USER,
        "installation_id": installation_id,
        "series_id": uid(501),
        "version": 1,
    })
    evidence_file = files.store_artifact(b"historical basis\n", relative_name="basis.txt", stable_key="basis-1")
    evidence_hash = repository.get_object("ManagedFile", evidence_file).payload["manifest"]["sha256"]
    payload = {
        "segment_id": segment_id,
        "expected_segment_revision": 2,
        "expected_binding_revision": 2,
        "applied_id": applied_id,
        "effective_range": {"start": 2.0, "end": 9.0, "time_basis": "session", "unit": "s"},
        "evidence_refs": [{
            "kind": "artifact",
            "file_id": evidence_file,
            "content_hash": evidence_hash,
            "locator": "basis.txt",
        }],
    }
    request = BindConfigurationRequest.model_validate_json(canonical_json(payload))

    result = service.bind_configuration(request, context_key("bind-1"))

    assert [ref.entity_type for ref in result.refs] == ["ConfigurationBinding", "SessionSegment"]
    segment = service.get_segment(segment_id)
    assert segment.revision == 3
    assert segment.binding_revision == 3
    assert segment.end_s == 20
    bound = service.get_binding(segment_id, 3)
    assert bound.applied_id == applied_id
    assert bound.configuration_state.value == "bound"
    assert bound.effective_range.start == 2.0
    assert bound.effective_range.end == 9.0
    assert bound.supersedes_revision == 2
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM current_applied")[0]["n"] == 0
    assert files.read_managed_file(declaration_file_id) == before_bytes
    assert service.get_session(session_id).revision == 1


def test_an_open_revision_cannot_be_analyzed(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id

    with pytest.raises(ContractError) as caught:
        service.require_analyzeable(BindingRef(segment_id=segment_id, segment_revision=1, binding_revision=1))
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED

    clock.advance(5)
    service.end_segment(_end(segment_id), context_key("end-1"))
    service.require_analyzeable(BindingRef(segment_id=segment_id, segment_revision=2, binding_revision=2))


def test_create_session_replays_the_same_response_and_rejects_changed_content(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    request = _request(installation_id, context_id, device_id, fields={"upscaling": "balanced"})

    first = service.create_session(request, context_key("same-key"))
    clock.advance(30)
    replay = service.create_session(request, context_key("same-key"))

    assert [ref.model_dump() for ref in replay.refs] == [ref.model_dump() for ref in first.refs]
    assert _count(repository, "PlaySession") == 1
    assert _count(repository, "ManagedFile") == 1

    with pytest.raises(ContractError) as caught:
        service.create_session(
            _request(installation_id, context_id, device_id, fields={"upscaling": "quality"}),
            context_key("same-key"),
        )
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert _count(repository, "PlaySession") == 1


def test_run_declaration_must_name_the_session_installation(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)

    with pytest.raises(ContractError) as caught:
        service.create_session(
            _request(installation_id, context_id, device_id, declaration_installation=uid(777)),
            context_key("wrong-install"),
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "PlaySession") == 0
    assert _count(repository, "SessionSegment") == 0
    assert _count(repository, "PlayerDeclaration") == 0
    assert _count(repository, "ManagedFile") == 0


def test_undeclared_run_conditions_stay_unknown(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)

    created = service.create_session(
        _request(installation_id, context_id, device_id, fields={"scenario": "commute"}),
        context_key("create-1"),
    )

    session = service.get_session(created.refs[1].entity_id)
    conditions = session.run_conditions
    assert conditions.output_resolution.value_state.value == "unknown"
    assert conditions.output_resolution.value is None
    assert conditions.output_resolution.unknown_reason == "not_provided"
    assert conditions.output_resolution.acquisition_status.value == "not_attempted"
    assert conditions.frame_generation.value_state.value == "unknown"
    assert conditions.frame_generation.value is None
    assert conditions.upscaling.value_state.value == "unknown"
    assert conditions.scene_conditions == {}
    assert session.scenario.value_state.value == "known"
    assert session.scenario.value == "commute"


def test_another_users_segment_is_not_found(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(3)
    other = context_key("other-user").model_copy(update={"user_scope_id": uid(999)})

    with pytest.raises(ContractError) as caught:
        service.end_segment(_end(segment_id), other)

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert service.get_segment(segment_id).end_s is None


def test_concurrent_end_segment_lets_exactly_one_write(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(6)

    second = ProductRepository(repository.path)
    second_service = _service(second, files, clock)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def close(worker, key):
        barrier.wait()
        try:
            worker.end_segment(_end(segment_id), context_key(key))
            outcomes.append("ok")
        except ContractError as error:
            outcomes.append(error.code.value)

    threads = [
        threading.Thread(target=close, args=(service, "th-1")),
        threading.Thread(target=close, args=(second_service, "th-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    second.close()

    assert sorted(outcomes) == ["BINDING_REVISION_CONFLICT", "ok"]
    assert service.get_segment(segment_id).revision == 2
    with repository.unit_of_work() as uow:
        assert uow.query(
            "SELECT COUNT(*) AS n FROM configuration_bindings WHERE segment_id=?", (segment_id,)
        )[0]["n"] == 2


# --- additional guards -------------------------------------------------------


def test_end_session_requires_the_current_session_revision(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(2)
    payload = {
        "session_id": created.refs[1].entity_id,
        "expected_session_revision": 7,
        "active_segments": [{
            "segment_id": segment_id,
            "expected_segment_revision": 1,
            "expected_binding_revision": 1,
        }],
    }

    with pytest.raises(ContractError) as caught:
        service.end_session(
            EndSessionRequest.model_validate_json(canonical_json(payload)), context_key("end-bad-rev")
        )

    assert caught.value.code is ErrorCode.BINDING_REVISION_CONFLICT
    assert caught.value.field_errors[0].path == "session_revision"
    assert service.get_segment(segment_id).end_s is None
    assert service.get_session(created.refs[1].entity_id).lifecycle.value == "active"


def test_end_session_rejects_a_duplicate_or_unknown_segment(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    item = {
        "segment_id": segment_id,
        "expected_segment_revision": 1,
        "expected_binding_revision": 1,
    }
    payload = {
        "session_id": created.refs[1].entity_id,
        "expected_session_revision": 1,
        "active_segments": [item, dict(item)],
    }
    with pytest.raises(ContractError) as caught:
        service.end_session(
            EndSessionRequest.model_validate_json(canonical_json(payload)), context_key("dup")
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert caught.value.field_errors[0].path == "active_segments"


def test_another_users_session_is_not_found_for_end_session(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    payload = {
        "session_id": created.refs[1].entity_id,
        "expected_session_revision": 1,
        "active_segments": [{
            "segment_id": segment_id,
            "expected_segment_revision": 1,
            "expected_binding_revision": 1,
        }],
    }
    other = context_key("other").model_copy(update={"user_scope_id": uid(999)})

    with pytest.raises(ContractError) as caught:
        service.end_session(
            EndSessionRequest.model_validate_json(canonical_json(payload)), other
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert service.get_segment(segment_id).end_s is None


def test_binding_needs_an_ended_segment_and_evidence(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    applied_id = store_raw(repository, "AppliedConfiguration", {
        "id": uid(510), "user_scope_id": USER, "installation_id": installation_id,
        "series_id": uid(511), "version": 1,
    })
    evidence_file = files.store_artifact(b"basis\n", relative_name="basis.txt", stable_key="basis-2")
    evidence_hash = repository.get_object("ManagedFile", evidence_file).payload["manifest"]["sha256"]
    evidence = {
        "kind": "artifact", "file_id": evidence_file,
        "content_hash": evidence_hash, "locator": "basis.txt",
    }
    base = {
        "segment_id": segment_id,
        "expected_segment_revision": 1,
        "expected_binding_revision": 1,
        "applied_id": applied_id,
        "effective_range": {"start": 0.0, "end": 1.0, "time_basis": "session", "unit": "s"},
    }

    with pytest.raises(ContractError) as caught:
        service.bind_configuration(
            BindConfigurationRequest.model_validate_json(canonical_json({**base, "evidence_refs": [evidence]})),
            context_key("bind-open"),
        )
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED

    clock.advance(5)
    service.end_segment(_end(segment_id), context_key("end-1"))
    with pytest.raises(ContractError) as caught:
        service.bind_configuration(
            BindConfigurationRequest.model_validate_json(canonical_json({
                **base, "expected_segment_revision": 2, "expected_binding_revision": 2,
                "evidence_refs": [],
            })),
            context_key("bind-no-evidence"),
        )
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED

    with pytest.raises(ContractError) as caught:
        service.bind_configuration(
            BindConfigurationRequest.model_validate_json(canonical_json({
                **base, "expected_segment_revision": 2, "expected_binding_revision": 2,
                "effective_range": {"start": 4.0, "end": 9.0, "time_basis": "session", "unit": "s"},
                "evidence_refs": [evidence],
            })),
            context_key("bind-outside"),
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert caught.value.field_errors[0].path == "effective_range"


def test_half_a_resolution_is_refused(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)

    with pytest.raises(ContractError) as caught:
        service.create_session(
            _request(installation_id, context_id, device_id, fields={"output_width": 1920}),
            context_key("half-res"),
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert "output_height" in caught.value.message
    assert _count(repository, "PlaySession") == 0


def test_a_string_frame_generation_is_not_coerced(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)

    with pytest.raises(ContractError) as caught:
        service.create_session(
            _request(installation_id, context_id, device_id, fields={"frame_generation": "false"}),
            context_key("string-fg"),
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED


def test_an_unknown_binding_revision_is_not_found(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id

    with pytest.raises(ContractError) as caught:
        service.get_binding(segment_id, 9)

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_split_declaration_must_name_the_session_installation(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(4)
    payload = {
        "ending": {
            "segment_id": segment_id,
            "expected_segment_revision": 1,
            "expected_binding_revision": 1,
        },
        "new_applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {"installation_id": uid(778), "fields": {}, "raw_text": ""},
    }

    with pytest.raises(ContractError) as caught:
        service.split_segment(
            SplitSegmentRequest.model_validate_json(canonical_json(payload)), context_key("split-bad")
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "SessionSegment") == 1
    assert _count(repository, "PlayerDeclaration") == 1
    assert service.get_segment(segment_id).end_s is None


def test_end_segment_replays_the_same_response(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(3)

    first = service.end_segment(_end(segment_id), context_key("end-key"))
    replay = service.end_segment(_end(segment_id), context_key("end-key"))

    assert [ref.model_dump() for ref in replay.refs] == [ref.model_dump() for ref in first.refs]
    assert service.get_segment(segment_id).revision == 2


# --- integration with the 01 historical registration path --------------------


def _historical(installation_id, context_id, device_id, segment_ref, evidence):
    payload = {
        "kind": "historical_configuration",
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "segment_ref": segment_ref,
        "effective_range": {"start": 0.0, "end": 10.0, "time_basis": "session", "unit": "s"},
        "adopted_at": "2026-10-01T12:00:00+00:00",
        "declared_settings": [],
        "preset_reference": "profiles/past",
        "claimed_activation": "unknown",
        "evidence_refs": [evidence],
    }
    return HistoricalConfigurationInput.model_validate_json(canonical_json(payload))


def test_a_historical_configuration_reads_the_real_segment_projection(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    created = service.create_session(
        _request(installation_id, context_id, device_id), context_key("create-1")
    )
    segment_id = created.refs[2].entity_id
    clock.advance(10)
    service.end_segment(_end(segment_id), context_key("end-1"))
    evidence_file = files.store_artifact(b"past basis\n", relative_name="past.txt", stable_key="past-1")
    evidence_hash = repository.get_object("ManagedFile", evidence_file).payload["manifest"]["sha256"]
    evidence = {
        "kind": "artifact", "file_id": evidence_file,
        "content_hash": evidence_hash, "locator": "past.txt",
    }

    result = AdoptionService(repository, files).record_historical_configuration(
        _historical(
            installation_id, context_id, device_id,
            {"segment_id": segment_id, "segment_revision": 2, "binding_revision": 2},
            evidence,
        ),
        context_key("hist-1"),
    )

    applied = AdoptionService(repository, files).get_applied(result.refs[1].entity_id)
    check = next(item for item in applied.checks if item.check_id == "applied.segment")
    assert check.status is CheckStatus.pass_

    with pytest.raises(ContractError) as caught:
        AdoptionService(repository, files).record_historical_configuration(
            _historical(
                installation_id, context_id, device_id,
                {"segment_id": segment_id, "segment_revision": 1, "binding_revision": 2},
                evidence,
            ),
            context_key("hist-stale"),
        )
    assert caught.value.code is ErrorCode.BINDING_REVISION_CONFLICT
    assert caught.value.field_errors[0].path == "segment_ref.segment_revision"


def test_the_application_layer_exposes_the_performance_entries(tmp_path):
    from game_app.application import Application
    from game_app.contracts.performance import MetricCatalogQuery

    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    app = Application(repository, file_root=str(tmp_path / "app-managed"))

    created = app.create_session(
        _request(installation_id, context_id, device_id), context_key("app-session")
    )

    assert [ref.entity_type for ref in created.refs] == [
        "PlayerDeclaration", "PlaySession", "SessionSegment", "ConfigurationBinding", "ManagedFile",
    ]
    assert app.get_session(created.refs[1].entity_id).id == created.refs[1].entity_id
    assert app.get_segment(created.refs[2].entity_id).id == created.refs[2].entity_id
    page = app.list_metric_definitions(MetricCatalogQuery(frame_stream_kind=None))
    assert len(page.choices) == 4



