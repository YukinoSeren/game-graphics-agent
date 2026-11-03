"""PERF-T04 B-3: owned capture, leases and safe stop (04 §3/§4.3/§5/§7).

The only adapter is an inert stub: no process is launched, no privilege is
requested and no external session is stopped.  The tests assert launch counts
explicitly and keep ``external_calls`` at zero.
"""

from __future__ import annotations

import pytest

from game_app.adapters.capture import CaptureAdapterRegistry, StubCaptureAdapter
from game_app.analyzer import ANALYZER_VERSION, CLEANING_PROTOCOL_VERSION
from game_app.application import Application
from game_app.contracts.common import MutationContext
from game_app.contracts.performance import CreateSessionRequest, StopCaptureInput
from game_app.contracts.primitives import BindingRef
from game_app.contracts.tasks import AnalysisInput
from game_app.errors import ContractError, ErrorCode
from game_app.repository import canonical_json
from tests.product.support.configuration import context_key, setup
from tests.product.support.performance import count_objects, csv_bytes
from tests.product.test_sessions import FakeClock


def _ctx(key: str) -> MutationContext:
    return context_key(key)


def _stack(tmp_path, *, verified: bool = True, **stub_options):
    repository, files, installation, context, device = setup(tmp_path)
    clock = FakeClock()
    stub = StubCaptureAdapter(clock=clock, **stub_options)
    registry = CaptureAdapterRegistry()
    registry.register(stub, verified=verified)
    application = Application(
        repository, file_root=str(tmp_path / "managed"),
        capture_registry=registry, clock=clock,
    )
    return repository, application, stub, clock, installation, context, device


def _session(application, installation, context, device, key="session") -> tuple[str, str]:
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
    result = application.create_session(request, _ctx(key))
    session_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "PlaySession"
    )
    segment_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "SessionSegment"
    )
    return session_id, segment_id


def _capture_payload(session_id: str, segment_id: str, **overrides) -> dict:
    payload = {
        "kind": "capture",
        "session_id": session_id,
        "segment_ref": {
            "segment_id": segment_id,
            "segment_revision": 1,
            "binding_revision": 1,
        },
        "adapter_id": "stub-capture",
        "adapter_version": "1",
        "protocol_id": None,
    }
    payload.update(overrides)
    return payload


def _lease(repository, task_id):
    with repository.unit_of_work() as uow:
        rows = uow.query("SELECT * FROM capture_leases WHERE task_id=?", (task_id,))
    return None if not rows else rows[0]


def _lease_events(repository, task_id):
    with repository.unit_of_work() as uow:
        return uow.query(
            "SELECT sequence, previous_state, next_state, cause, process_pid"
            " FROM capture_lease_events WHERE task_id=? ORDER BY sequence",
            (task_id,),
        )


def _task_events(repository, task_id):
    with repository.unit_of_work() as uow:
        return uow.query(
            "SELECT cause, from_state, to_state FROM task_events"
            " WHERE task_id=? ORDER BY sequence",
            (task_id,),
        )


def _count(repository, table: str) -> int:
    with repository.unit_of_work() as uow:
        return uow.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def _started(application, session_id, segment_id, key="capture"):
    accepted = application.capture(
        _capture_payload(session_id, segment_id), _ctx(key)
    )
    outcome = application.run_once()
    assert outcome is not None and outcome.state.value == "running"
    return accepted


def _completed(repository, application, stub, clock, session_id, segment_id):
    accepted = _started(application, session_id, segment_id)
    clock.advance(30)
    application.stop_capture(
        StopCaptureInput(task_id=accepted.task_id), _ctx("stop")
    )
    outcome = application.run_once()
    assert outcome is not None and outcome.state.value == "completed"
    return accepted


def test_a_second_capture_with_another_key_is_refused(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(tmp_path)
    session_id, segment_id = _session(application, installation, context, device)
    accepted = _started(application, session_id, segment_id)

    with pytest.raises(ContractError) as caught:
        application.capture(
            _capture_payload(session_id, segment_id), _ctx("second")
        )

    assert caught.value.code is ErrorCode.ACTIVE_CAPTURE_EXISTS
    assert [ref.entity_id for ref in caught.value.resource_refs] == [accepted.task_id]
    assert _count(repository, "tasks") == 1
    assert _count(repository, "capture_leases") == 1
    assert stub.starts == 1


def test_an_unverified_adapter_is_refused_without_a_task_or_lease(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, verified=False
    )
    session_id, segment_id = _session(application, installation, context, device)

    with pytest.raises(ContractError) as caught:
        application.capture(
            _capture_payload(session_id, segment_id), _ctx("unverified")
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert [(item.path, item.code) for item in caught.value.field_errors] == [
        ("adapter_id", "enum")
    ]
    assert _count(repository, "tasks") == 0
    assert _count(repository, "capture_leases") == 0
    assert stub.starts == 0


def test_prepared_owned_finalized_is_one_event_per_change(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=csv_bytes([10, 20, 30, 40])
    )
    session_id, segment_id = _session(application, installation, context, device)

    accepted = application.capture(
        _capture_payload(session_id, segment_id), _ctx("lifecycle")
    )
    lease = _lease(repository, accepted.task_id)
    assert lease["state"] == "prepared"
    assert lease["process_pid"] is None
    assert [(row["previous_state"], row["next_state"]) for row in
            _lease_events(repository, accepted.task_id)] == [(None, "prepared")]

    assert application.run_once().state.value == "running"
    lease = _lease(repository, accepted.task_id)
    assert lease["state"] == "owned"
    assert lease["process_pid"] is not None
    assert lease["process_started_at"] is not None
    assert [(row["previous_state"], row["next_state"]) for row in
            _lease_events(repository, accepted.task_id)] == [
        (None, "prepared"), ("prepared", "owned"), ("owned", "owned"),
    ]
    # Nothing is consumable before finalization.
    assert count_objects(repository, "CaptureArtifact") == 0
    assert repository.find_object("ManagedFile", lease["file_id"]) is None

    clock.advance(30)
    application.stop_capture(
        StopCaptureInput(task_id=accepted.task_id), _ctx("lifecycle-stop")
    )
    running = application.get_task(accepted.task_id).task
    assert running.state.value == "running"
    assert running.cancel_requested is False

    outcome = application.run_once()
    assert outcome.state.value == "completed"
    lease = _lease(repository, accepted.task_id)
    assert lease["state"] == "finalized"
    assert count_objects(repository, "CaptureArtifact") == 1
    with repository.unit_of_work() as uow:
        segment = uow.query("SELECT * FROM session_segments")[0]
    assert segment["lifecycle"] == "ended"
    assert segment["end_s"] == pytest.approx(30.0)
    assert segment["revision"] == 2
    assert segment["binding_revision"] == 2
    states = [(row["previous_state"], row["next_state"]) for row in
              _lease_events(repository, accepted.task_id)]
    assert states[-1] == ("owned", "finalized")
    assert stub.starts == 1 and stub.stops == 1
    assert _count(repository, "external_calls") == 0


def test_a_running_stop_only_records_the_intent_once(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=csv_bytes([10, 20, 30, 40])
    )
    session_id, segment_id = _session(application, installation, context, device)
    accepted = _started(application, session_id, segment_id)

    application.stop_capture(
        StopCaptureInput(task_id=accepted.task_id), _ctx("stop-1")
    )
    application.stop_capture(
        StopCaptureInput(task_id=accepted.task_id), _ctx("stop-2")
    )

    stops = [row for row in _task_events(repository, accepted.task_id)
             if row["cause"] == "capture_stop_requested"]
    assert len(stops) == 1
    task = application.get_task(accepted.task_id).task
    assert task.state.value == "running"
    assert task.cancel_requested is False


def test_a_queued_stop_cancels_and_frees_the_lease(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=csv_bytes([10, 20, 30, 40])
    )
    session_id, segment_id = _session(application, installation, context, device)
    accepted = application.capture(
        _capture_payload(session_id, segment_id), _ctx("queued")
    )

    application.stop_capture(
        StopCaptureInput(task_id=accepted.task_id), _ctx("queued-stop")
    )

    task = application.get_task(accepted.task_id).task
    assert task.state.value == "cancelled"
    assert _lease(repository, accepted.task_id)["state"] == "finalized"
    assert stub.starts == 0
    with repository.unit_of_work() as uow:
        assert uow.query(
            "SELECT COUNT(*) AS n FROM capture_leases WHERE state <> 'finalized'"
        )[0]["n"] == 0

    second = application.capture(
        _capture_payload(session_id, segment_id), _ctx("after-cancel")
    )
    assert second.task_id != accepted.task_id
    assert _count(repository, "tasks") == 2


def test_a_terminal_task_returns_its_result_without_a_new_event(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=csv_bytes([10, 20, 30, 40])
    )
    session_id, segment_id = _session(application, installation, context, device)
    accepted = _completed(
        repository, application, stub, clock, session_id, segment_id
    )
    task = application.get_task(accepted.task_id).task
    before = len(_task_events(repository, accepted.task_id))

    result = application.stop_capture(
        StopCaptureInput(task_id=accepted.task_id), _ctx("terminal-stop")
    )

    assert [ref.entity_id for ref in result.refs] == [
        ref.entity_id for ref in task.result_refs
    ]
    assert len(_task_events(repository, accepted.task_id)) == before
    assert application.get_task(accepted.task_id).task.state.value == "completed"


def test_restart_with_a_reused_pid_is_unresolved_and_blocks_a_launch(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=csv_bytes([10, 20, 30, 40])
    )
    session_id, segment_id = _session(application, installation, context, device)
    accepted = _started(application, session_id, segment_id)
    with repository.unit_of_work() as uow:
        uow.execute(
            "UPDATE capture_leases SET process_started_at=?"
            " WHERE task_id=?",
            ("1999-01-01T00:00:00+00:00", accepted.task_id),
        )

    application.recover_on_startup()

    assert application.get_task(accepted.task_id).task.state.value == "interrupted"
    lease = _lease(repository, accepted.task_id)
    assert lease["state"] == "unresolved"
    with pytest.raises(ContractError) as caught:
        application.capture(
            _capture_payload(session_id, segment_id), _ctx("after-restart")
        )
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert stub.starts == 1


def test_unverifiable_ownership_never_launches_a_second_capture(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=csv_bytes([10, 20, 30, 40]), ownership_verifiable=False,
    )
    session_id, segment_id = _session(application, installation, context, device)
    accepted = _started(application, session_id, segment_id)

    application.recover_on_startup()

    assert _lease(repository, accepted.task_id)["state"] == "unresolved"
    with pytest.raises(ContractError) as caught:
        application.capture(
            _capture_payload(session_id, segment_id), _ctx("after-unknown")
        )
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert stub.starts == 1


def test_missing_output_keeps_the_segment_open_and_blocks_again(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=None
    )
    session_id, segment_id = _session(application, installation, context, device)
    accepted = _started(application, session_id, segment_id)
    clock.advance(30)
    application.stop_capture(
        StopCaptureInput(task_id=accepted.task_id), _ctx("no-output-stop")
    )

    outcome = application.run_once()

    assert outcome.state.value == "partial"
    task = application.get_task(accepted.task_id).task
    assert task.domain_result.code == "retained_only"
    assert _lease(repository, accepted.task_id)["state"] == "unresolved"
    assert count_objects(repository, "CaptureArtifact") == 0
    with repository.unit_of_work() as uow:
        segment = uow.query("SELECT * FROM session_segments")[0]
    assert segment["lifecycle"] == "active"
    assert segment["end_s"] is None
    # The terminal task did not clear the lease.
    with pytest.raises(ContractError) as caught:
        application.capture(
            _capture_payload(session_id, segment_id), _ctx("after-no-output")
        )
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED


def test_same_key_replays_and_different_content_conflicts(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=csv_bytes([10, 20, 30, 40])
    )
    first_session, first_segment = _session(
        application, installation, context, device, "s-1"
    )
    second_session, second_segment = _session(
        application, installation, context, device, "s-2"
    )
    payload = _capture_payload(first_session, first_segment)

    first = application.capture(payload, _ctx("same"))
    replay = application.capture(payload, _ctx("same"))
    assert replay.task_id == first.task_id
    assert _count(repository, "tasks") == 1
    assert _count(repository, "capture_leases") == 1

    with pytest.raises(ContractError) as caught:
        application.capture(
            _capture_payload(second_session, second_segment), _ctx("same")
        )
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert _count(repository, "tasks") == 1


def test_capture_output_is_analyzable_and_uses_no_provider(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=csv_bytes([10, 20, 30, 40])
    )
    session_id, segment_id = _session(application, installation, context, device)
    accepted = _completed(
        repository, application, stub, clock, session_id, segment_id
    )
    refs = application.get_task(accepted.task_id).task.result_refs
    artifact_id = next(
        ref.entity_id for ref in refs if ref.entity_type == "CaptureArtifact"
    )
    analysis = AnalysisInput(
        kind="analysis",
        artifact_id=artifact_id,
        bindings=[BindingRef(
            segment_id=segment_id, segment_revision=2, binding_revision=2,
        )],
        analyzer_version=ANALYZER_VERSION,
        cleaning_protocol_version=CLEANING_PROTOCOL_VERSION,
    )
    accepted_analysis = application.analyze(analysis, _ctx("capture-analysis"))
    outcome = application.run_once()

    assert outcome.state.value == "completed"
    assert application.get_task(accepted_analysis.task_id).task.domain_result.code == "limited"
    assert _count(repository, "external_calls") == 0
    assert stub.starts == 1 and stub.stops == 1


def test_cancelling_a_running_capture_saves_the_original_first(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=csv_bytes([10, 20, 30, 40])
    )
    session_id, segment_id = _session(application, installation, context, device)
    accepted = _started(application, session_id, segment_id)
    clock.advance(30)

    application.cancel_task(accepted.task_id, _ctx("cancel-running"))
    outcome = application.run_once()

    assert outcome.state.value == "cancelled"
    task = application.get_task(accepted.task_id).task
    assert task.state.value == "cancelled"
    assert _lease(repository, accepted.task_id)["state"] == "finalized"
    assert count_objects(repository, "CaptureArtifact") == 1
    assert stub.starts == 1 and stub.stops == 1


def test_a_queued_task_interrupted_before_launch_finalizes_the_lease(tmp_path):
    repository, application, stub, clock, installation, context, device = _stack(
        tmp_path, output=csv_bytes([10, 20, 30, 40])
    )
    session_id, segment_id = _session(application, installation, context, device)
    accepted = application.capture(
        _capture_payload(session_id, segment_id), _ctx("queued-restart")
    )
    assert _lease(repository, accepted.task_id)["state"] == "prepared"

    application.recover_on_startup()

    assert application.get_task(accepted.task_id).task.state.value == "interrupted"
    assert _lease(repository, accepted.task_id)["state"] == "finalized"
    assert stub.starts == 0
    second = application.capture(
        _capture_payload(session_id, segment_id), _ctx("after-queued-restart")
    )
    assert second.task_id != accepted.task_id
