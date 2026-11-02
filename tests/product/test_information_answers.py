"""DEV-T04: answer normalisation, waiting-task continuation and amendments."""

from __future__ import annotations

import hashlib
import json
import threading

import pytest
from pydantic import ValidationError

from game_app.answers import AmendmentService, AnswerService
from game_app.contracts.common import (
    EvidenceRef,
    FieldCandidate,
    FieldValue,
    MutationContext,
    Provenance,
)
from game_app.contracts.device import (
    AdapterCapabilities,
    DeviceSnapshot,
    DisplayContext,
    GpuSnapshot,
    Resolution,
)
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    AnswerState,
    CheckStatus,
    InformationContinuationMode,
    InformationRequestState,
    TaskState,
    ValueState,
)
from game_app.contracts.information import (
    InformationRequest,
    InformationResponseInput,
    RequestedField,
)
from game_app.contracts.primitives import (
    InformationRequestRevisionRef,
    ObjectRef,
    VersionRef,
    utc_now,
)
from game_app.device import DeviceService, ProbeReading
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService
from game_app.repository import ProductRepository, canonical_json
from game_app.tasks import TaskService
from tests.product.support.spies import SpyProvider

USER = "a" * 32


def _uid(value: int) -> str:
    return f"{value:032x}"


def _ctx(key: str = "answer-1") -> MutationContext:
    return MutationContext(user_scope_id=USER, idempotency_key=key, request_id=_uid(991))


class StubProbe:
    def __init__(self, values=None, failures=None):
        self.values = values or {}
        self.failures = failures or {}
        self.calls = []

    def probe(self, field_paths, *, timeout):
        self.calls.append(list(field_paths))
        readings = []
        for path in field_paths:
            if path in self.failures:
                readings.append(ProbeReading(
                    field_path=path, status="failed", raw=b"",
                    error_code=self.failures[path],
                ))
                continue
            value = self.values.get(path)
            if value is None:
                readings.append(ProbeReading(
                    field_path=path, status="failed", raw=b"",
                    error_code="PROBE_UNAVAILABLE",
                ))
            else:
                readings.append(ProbeReading(
                    field_path=path, status="succeeded",
                    raw=str(value).encode("utf-8"), text=str(value),
                ))
        return readings


def _env(tmp_path, probe):
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    files = FileService(repository, tmp_path / "managed")
    device = DeviceService(repository, files, probe)
    tasks = TaskService(repository, provider=SpyProvider(), files=files)
    tasks.handlers["device_scan"] = device.handle
    answers = AnswerService(repository, files, tasks=tasks)
    amendments = AmendmentService(repository, files)
    return repository, files, device, tasks, answers, amendments


def _scan(tasks, field_paths, *, key="scan-key"):
    return tasks.register_task(
        {"kind": "device_scan", "game_context_id": None, "field_paths": field_paths},
        _ctx(key),
    )


def _request_ids(repository, task_id):
    with repository.unit_of_work() as uow:
        row = uow.query("SELECT * FROM tasks WHERE task_id=?", (task_id,))[0]
    refs = json.loads(row["result_refs"] or "[]")
    return [ref["entity_id"] for ref in refs if ref["entity_type"] == "InformationRequest"]


def _request(repository, request_id) -> InformationRequest:
    with repository.unit_of_work() as uow:
        head = uow.get_head("InformationRequest", request_id)
        if head is None:
            stored = uow.find_object("InformationRequest", request_id)
            return InformationRequest.model_validate_json(stored.payload_bytes)
        payload = uow.get_snapshot("InformationRequest", request_id, head)
    return InformationRequest.model_validate_json(canonical_json(payload))


def _task_row(repository, task_id):
    with repository.unit_of_work() as uow:
        return uow.query("SELECT * FROM tasks WHERE task_id=?", (task_id,))[0]


def _counts(repository, task_id):
    with repository.unit_of_work() as uow:
        inputs = uow.query(
            "SELECT COUNT(*) AS n FROM task_inputs WHERE task_id=?", (task_id,)
        )[0]["n"]
        events = uow.query(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id=?", (task_id,)
        )[0]["n"]
        row = uow.query("SELECT revision FROM tasks WHERE task_id=?", (task_id,))[0]
    return inputs, events, row["revision"]


def _answer_input(request, answers=None, *, skipped=None, selections=None,
                  version=None, context=None, raw_text=""):
    return InformationResponseInput.model_validate_json(canonical_json({
        "request_id": request.id,
        "expected_request_version": request.version if version is None else version,
        "expected_context_revision": (
            request.expected_context_revision if context is None else context
        ),
        "answers": answers or {},
        "skipped_fields": skipped or [],
        "raw_text": raw_text,
        "conflict_selections": selections or {},
    }))


def _answer(service, request, *, key="answer-1", **kwargs):
    return service.answer_information(_answer_input(request, **kwargs), _ctx(key))


def _snapshot_ref(request) -> VersionRef:
    return next(
        ref for ref in request.context_refs if ref.entity_type == "DeviceSnapshot"
    )


def _snapshot(repository, request) -> DeviceSnapshot:
    stored = repository.get_object("DeviceSnapshot", _snapshot_ref(request).entity_id)
    return DeviceSnapshot.model_validate_json(stored.payload_bytes)


def _latest_snapshot(repository, series_id) -> DeviceSnapshot:
    with repository.unit_of_work() as uow:
        stored = uow.latest_for_series("DeviceSnapshot", series_id)
    return DeviceSnapshot.model_validate_json(stored.payload_bytes)


# --- answers -----------------------------------------------------------------


def test_partial_answer_keeps_unanswered_fields_and_the_task_waiting(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={
            "gpus.0.raw_name": "PROBE_FAILED",
            "device_form": "PROBE_FAILED",
            "memory_bytes": "PROBE_FAILED",
        },
    )
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "gpus.0.raw_name", "device_form", "memory_bytes"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])

    _answer(answers, request, answers={"gpus.0.raw_name": "Synthetic GPU"})

    updated = _request(repository, request.id)
    states = {field.field_path: field.answer_state for field in updated.fields}
    assert states["gpus.0.raw_name"] is AnswerState.answered
    assert states["device_form"] is AnswerState.unanswered
    assert states["memory_bytes"] is AnswerState.unanswered
    assert updated.state is InformationRequestState.partially_answered
    assert updated.version == 2
    assert updated.expected_context_revision == 2
    assert _task_row(repository, accepted.task_id)["state"] == "waiting_input"
    snapshot = _latest_snapshot(repository, _snapshot(repository, request).series_id)
    assert snapshot.version == 2
    assert snapshot.gpus[0].raw_name.value == "Synthetic GPU"


def test_a_value_that_cannot_be_normalised_stays_unresolved(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={"memory_bytes": "PROBE_FAILED"},
    )
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "memory_bytes"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])

    _answer(answers, request, answers={"memory_bytes": "16 GB"})

    updated = _request(repository, request.id)
    field = next(item for item in updated.fields if item.field_path == "memory_bytes")
    assert field.answer_state is AnswerState.unresolved
    assert field.value.value_state is ValueState.unknown
    assert field.value.value is None
    # The player's words are preserved in the response, never in a known field.
    with repository.unit_of_work() as uow:
        stored = uow.iter_objects("InformationResponse")[-1]
    assert stored.payload["answers"]["memory_bytes"] == "16 GB"
    assert updated.state is InformationRequestState.partially_answered


def test_conflict_selection_resolves_the_value_and_keeps_old_evidence(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()

    evidence_data = b"two conflicting probe originals"
    file_id = files.store_artifact(
        evidence_data, relative_name="probe/conflict.txt", stable_key="probe:conflict"
    )
    evidence = EvidenceRef(
        kind="artifact",
        file_id=file_id,
        content_hash=hashlib.sha256(evidence_data).hexdigest(),
        locator="probe/conflict.txt",
    )
    provenance = Provenance(
        method=AcquisitionMethod.system_probe,
        observed_at=None,
        recorded_at=utc_now(),
        evidence_refs=[evidence],
        note="probe reading",
    )
    conflicting = FieldValue[str](
        value_state=ValueState.conflicting,
        value=None,
        candidates=[
            FieldCandidate[str](value="Desktop PC", provenance=[provenance]),
            FieldCandidate[str](value="Laptop", provenance=[provenance]),
        ],
        provenance=[],
        acquisition_status=AcquisitionStatus.succeeded,
    )
    unknown = FieldValue[str](
        value_state=ValueState.unknown,
        value=None,
        unknown_reason="not_provided",
        acquisition_status=AcquisitionStatus.not_attempted,
    )
    snapshot = DeviceSnapshot(
        id=_uid(600),
        schema_version="product-contract/0.4",
        created_at=utc_now(),
        created_by_task_id=None,
        series_id=_uid(601),
        version=1,
        device_series_id=_uid(602),
        user_scope_id=USER,
        captured_at=utc_now(),
        gpus=[],
        selected_gpu_id=None,
        cpu=unknown,
        memory_bytes=unknown,
        device_form=conflicting,
        os_version=unknown,
        display=DisplayContext(
            desktop_resolution=FieldValue[Resolution](
                value_state=ValueState.unknown, value=None,
                unknown_reason="not_provided",
                acquisition_status=AcquisitionStatus.not_attempted,
            ),
            desktop_resolution_axes={},
            refresh_hz=FieldValue[float](
                value_state=ValueState.unknown, value=None,
                unknown_reason="not_provided",
                acquisition_status=AcquisitionStatus.not_attempted,
            ),
        ),
        power_context={},
        checks=[],
    )
    request = InformationRequest(
        id=_uid(603),
        schema_version="product-contract/0.4",
        created_at=utc_now(),
        created_by_task_id=None,
        source_task_id=accepted.task_id,
        version=1,
        context_refs=[VersionRef(
            entity_type="DeviceSnapshot", entity_id=snapshot.id,
            series_id=snapshot.series_id, version=snapshot.version,
        )],
        expected_context_revision=1,
        fields=[RequestedField(
            field_path="device_form",
            purpose="device form conflict",
            required_for=[],
            candidates=["Desktop PC", "Laptop"],
            answer_state=AnswerState.unanswered,
            value=conflicting,
        )],
        state=InformationRequestState.open,
        result_refs=[],
        superseded_by_request_id=None,
        continuation_mode=InformationContinuationMode.source_task,
        amends_request_id=None,
        amendment_root_ref=None,
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("DeviceSnapshot", snapshot)
        uow.insert_object("InformationRequest", request)

    _answer(answers, request, answers={"device_form": "ignored raw text"},
            selections={"device_form": 1})

    response = _last_response(repository)
    assert response["answers"]["device_form"] == "ignored raw text"
    # The normalised value is the selected candidate, not the submitted text.
    assert response["answered_values"] == {"device_form": "Laptop"}

    updated = _request(repository, request.id)
    field = updated.fields[0]
    assert field.answer_state is AnswerState.answered
    assert field.value.value_state is ValueState.known
    assert field.value.value == "Laptop"
    assert len(field.value.candidates) == 2
    assert field.value.resolution is not None
    assert field.value.resolution.prior_object_ref.entity_id == snapshot.id
    assert field.value.resolution.selected_provenance_indexes == [0]
    derived = _latest_snapshot(repository, snapshot.series_id)
    assert derived.version == 2
    assert derived.device_form.value == "Laptop"
    assert derived.device_form.resolution is not None
    assert len(derived.device_form.candidates) == 2


def test_skipping_the_last_field_marks_skipped_and_wakes_the_task(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"},
                      failures={"gpus.0.raw_name": "PROBE_FAILED",
                                "memory_bytes": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "gpus.0.raw_name", "memory_bytes"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    inputs_before, events_before, revision_before = _counts(repository, accepted.task_id)

    _answer(
        answers, request,
        skipped=["gpus.0.raw_name", "memory_bytes"],
    )

    updated = _request(repository, request.id)
    assert updated.state is InformationRequestState.skipped
    assert all(field.answer_state is AnswerState.skipped for field in updated.fields)
    row = _task_row(repository, accepted.task_id)
    assert row["state"] == "queued"
    inputs_after, events_after, revision_after = _counts(repository, accepted.task_id)
    assert inputs_after == inputs_before + 1
    assert events_after == events_before + 1
    assert revision_after == revision_before + 1
    with repository.unit_of_work() as uow:
        newest = uow.query(
            "SELECT * FROM task_inputs WHERE task_id=? ORDER BY input_revision DESC LIMIT 1",
            (accepted.task_id,),
        )[0]
        event = uow.query(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY sequence DESC LIMIT 1",
            (accepted.task_id,),
        )[0]
    assert newest["input_revision"] == inputs_before + 1
    assert newest["supersedes_revision"] == inputs_before
    assert json.loads(newest["cause_ref"])["entity_type"] == "InformationResponse"
    assert event["from_state"] == "waiting_input"
    assert event["to_state"] == "queued"


def test_stale_versions_are_rejected_without_any_write(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])

    for overrides, expected in (
        ({"version": 99}, ErrorCode.INFORMATION_REQUEST_VERSION_CONFLICT),
        ({"context": 99}, ErrorCode.INFORMATION_CONTEXT_CHANGED),
    ):
        with pytest.raises(ContractError) as caught:
            _answer(answers, request, answers={"device_form": "Desktop PC"}, **overrides)
        assert caught.value.code is expected

    assert _request(repository, request.id) == request
    with repository.unit_of_work() as uow:
        assert len(uow.iter_objects("InformationResponse")) == 0
    assert _task_row(repository, accepted.task_id)["state"] == "waiting_input"


def test_answers_and_skips_cannot_cross_or_name_unknown_fields(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])

    with pytest.raises(ValidationError):
        _answer_input(
            request,
            answers={"device_form": "Desktop PC"},
            skipped=["device_form"],
        )
    with pytest.raises(ContractError) as caught:
        _answer(answers, request, answers={"gpus.0.raw_name": "GPU"})
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED


def test_answering_first_then_cancelling_stops_the_queued_task(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"], key="scan-a")
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])

    _answer(answers, request, answers={"device_form": "Desktop PC"}, key="answer-a")
    assert _task_row(repository, accepted.task_id)["state"] == "queued"

    tasks.cancel_task(accepted.task_id, _ctx("cancel-a"))
    assert _task_row(repository, accepted.task_id)["state"] == "cancelled"


def test_cancel_first_then_late_answer_does_not_revive_the_task(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"], key="scan-b")
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    tasks.cancel_task(accepted.task_id, _ctx("cancel-b"))
    before = _counts(repository, accepted.task_id)

    _answer(answers, request, answers={"device_form": "Desktop PC"}, key="answer-b")

    row = _task_row(repository, accepted.task_id)
    assert row["state"] == "cancelled"
    assert _counts(repository, accepted.task_id) == before
    updated = _request(repository, request.id)
    assert updated.fields[0].answer_state is AnswerState.answered


def test_answering_a_sparse_gpu_index_does_not_rename_another_adapter(tmp_path):
    """Question paths use the probe's own index; the snapshot list is compacted."""
    probe = StubProbe(
        values={"gpus.1.raw_name": "GPU B"},
        failures={"gpus.0.raw_name": "PROBE_FAILED"},
    )
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["gpus.0.raw_name", "gpus.1.raw_name"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    before = _snapshot(repository, request)
    assert [gpu.raw_name.value for gpu in before.gpus] == ["GPU B"]
    second_id = before.gpus[0].id

    _answer(answers, request, answers={"gpus.0.raw_name": "GPU A"})

    after = _latest_snapshot(repository, before.series_id)
    by_index = {gpu.probe_index: gpu for gpu in after.gpus}
    assert set(by_index) == {0, 1}
    assert by_index[0].raw_name.value == "GPU A"
    # The adapter that was already known keeps its identity and its name.
    assert by_index[1].id == second_id
    assert by_index[1].raw_name.value == "GPU B"


def test_a_resolved_request_rejects_a_direct_answer(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(answers, request, answers={"device_form": "Desktop PC"}, key="answer-root")
    resolved = _request(repository, request.id)
    assert resolved.state is InformationRequestState.resolved

    with pytest.raises(ContractError) as caught:
        _answer(answers, resolved, answers={"device_form": "Laptop"}, key="answer-again")

    assert caught.value.code is ErrorCode.INFORMATION_REQUEST_STATE_CONFLICT
    assert _request(repository, request.id) == resolved


def test_a_skipped_request_rejects_a_direct_answer(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(answers, request, skipped=["device_form"], key="skip-root")
    skipped = _request(repository, request.id)
    assert skipped.state is InformationRequestState.skipped

    with pytest.raises(ContractError) as caught:
        _answer(answers, skipped, answers={"device_form": "Desktop PC"}, key="answer-again")

    assert caught.value.code is ErrorCode.INFORMATION_REQUEST_STATE_CONFLICT
    assert _request(repository, request.id) == skipped


def test_answering_a_field_keeps_the_failed_attempt_history(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    before = _snapshot(repository, request)
    prior = before.device_form
    assert prior.acquisition_status is AcquisitionStatus.failed
    assert len(prior.attempts) == 1
    assert prior.provenance

    _answer(answers, request, answers={"device_form": "Desktop PC"})

    after = _latest_snapshot(repository, before.series_id)
    assert after.device_form.value == "Desktop PC"
    # The derived snapshot cites the response that produced the value.
    assert after.device_form.attempts[-1].information_response_id == _last_response(
        repository
    )["id"]
    # The failed probe attempt is history: the answer appends to it.
    statuses = [attempt.status for attempt in after.device_form.attempts]
    assert statuses[0] is AcquisitionStatus.failed
    assert statuses[-1] is AcquisitionStatus.succeeded
    assert any(
        item.method is AcquisitionMethod.system_probe
        for item in after.device_form.provenance
    )


def test_answering_the_missing_axis_completes_the_resolution(tmp_path):
    probe = StubProbe(
        values={
            "display.desktop_resolution.width": "2560",
        },
        failures={"display.desktop_resolution.height": "PROBE_FAILED"},
    )
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(
        tasks,
        ["display.desktop_resolution.width", "display.desktop_resolution.height"],
    )
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    before = _snapshot(repository, request)
    assert before.display.desktop_resolution.value_state is ValueState.unknown

    _answer(answers, request, answers={"display.desktop_resolution.height": 1440})

    after = _latest_snapshot(repository, before.series_id)
    assert after.display.desktop_resolution.value_state is ValueState.known
    assert after.display.desktop_resolution.value.width == 2560
    assert after.display.desktop_resolution.value.height == 1440


def test_another_users_question_cannot_be_answered(tmp_path):
    """Answering someone else's question must not wake their task either."""
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    before = _counts(repository, accepted.task_id)
    stranger = MutationContext(
        user_scope_id="b" * 32, idempotency_key="stranger", request_id=_uid(998),
    )

    with pytest.raises(ContractError) as caught:
        answers.answer_information(
            _answer_input(request, answers={"device_form": "Desktop PC"}), stranger
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    with repository.unit_of_work() as uow:
        assert len(uow.iter_objects("InformationResponse")) == 0
    assert _counts(repository, accepted.task_id) == before


def test_another_users_handled_question_cannot_be_amended(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, amendments = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(answers, request, answers={"device_form": "Desktop PC"}, key="answer-root")
    resolved = _request(repository, request.id)
    stranger = MutationContext(
        user_scope_id="b" * 32, idempotency_key="stranger", request_id=_uid(997),
    )

    with pytest.raises(ContractError) as caught:
        amendments.create_information_amendment(
            {
                "source_request_id": resolved.id,
                "expected_request_version": resolved.version,
                "expected_context_revision": resolved.expected_context_revision,
                "field_paths": ["device_form"],
                "reason": "not my question",
            },
            stranger,
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    with repository.unit_of_work() as uow:
        assert len(uow.iter_objects("InformationAmendment")) == 0


def test_answer_replays_the_same_key(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    response_input = _answer_input(request, answers={"device_form": "Desktop PC"})

    first = answers.answer_information(response_input, _ctx("same"))
    second = answers.answer_information(response_input, _ctx("same"))

    assert [ref.entity_id for ref in first.refs] == [ref.entity_id for ref in second.refs]
    with repository.unit_of_work() as uow:
        assert len(uow.iter_objects("InformationResponse")) == 1


# --- amendments --------------------------------------------------------------


def test_facts_only_amendment_writes_no_source_task_rows(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"},
                      failures={"gpus.0.raw_name": "PROBE_FAILED",
                                "gpus.0.vram_bytes": "PROBE_FAILED"})
    repository, files, device, tasks, answers, amendments = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "gpus.0.raw_name", "gpus.0.vram_bytes"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(
        answers, request,
        answers={"gpus.0.raw_name": "Synthetic GPU", "gpus.0.vram_bytes": 8},
        key="answer-root",
    )
    resolved = _request(repository, request.id)
    assert resolved.state is InformationRequestState.resolved
    before = _counts(repository, accepted.task_id)

    amendment = amendments.create_information_amendment(
        {
            "source_request_id": resolved.id,
            "expected_request_version": resolved.version,
            "expected_context_revision": resolved.expected_context_revision,
            "field_paths": ["gpus.0.raw_name"],
            "reason": "clarify the GPU name",
        },
        _ctx("amend-1"),
    )

    assert _counts(repository, accepted.task_id) == before
    new_request_id = amendment.refs[1].entity_id
    new_request = _request(repository, new_request_id)
    assert new_request.version == 1
    assert new_request.expected_context_revision == 1
    assert new_request.continuation_mode is InformationContinuationMode.facts_only
    assert new_request.amends_request_id == resolved.id
    assert new_request.source_task_id == accepted.task_id
    assert new_request.amendment_root_ref == InformationRequestRevisionRef(
        request_id=resolved.id, version=resolved.version
    )
    assert new_request.state is InformationRequestState.open
    assert new_request.fields[0].answer_state is AnswerState.unanswered

    _answer(answers, new_request, answers={"gpus.0.raw_name": "Synthetic GPU"}, key="answer-2")

    assert _counts(repository, accepted.task_id) == before


def test_two_round_amendment_reaches_fields_outside_the_direct_source(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"},
                      failures={"gpus.0.raw_name": "PROBE_FAILED",
                                "gpus.0.vram_bytes": "PROBE_FAILED"})
    repository, files, device, tasks, answers, amendments = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "gpus.0.raw_name", "gpus.0.vram_bytes"])
    tasks.run_once()
    root = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(
        answers, root,
        answers={"gpus.0.raw_name": "Synthetic GPU", "gpus.0.vram_bytes": 8},
        key="answer-root",
    )
    root = _request(repository, root.id)
    first = amendments.create_information_amendment(
        {
            "source_request_id": root.id,
            "expected_request_version": root.version,
            "expected_context_revision": root.expected_context_revision,
            "field_paths": ["gpus.0.raw_name"],
            "reason": "correct the GPU name",
        },
        _ctx("amend-a"),
    )
    first_request = _request(repository, first.refs[1].entity_id)
    _answer(answers, first_request,
            answers={"gpus.0.raw_name": "Corrected GPU"}, key="answer-a")
    first_request = _request(repository, first_request.id)

    second = amendments.create_information_amendment(
        {
            "source_request_id": first_request.id,
            "expected_request_version": first_request.version,
            "expected_context_revision": first_request.expected_context_revision,
            "field_paths": ["gpus.0.vram_bytes"],
            "reason": "add the VRAM figure",
        },
        _ctx("amend-b"),
    )

    second_request = _request(repository, second.refs[1].entity_id)
    assert [field.field_path for field in second_request.fields] == ["gpus.0.vram_bytes"]
    # The root domain is still the original question, so the second round is
    # reachable even though the direct source only asked about the GPU name.
    assert second_request.amendment_root_ref == InformationRequestRevisionRef(
        request_id=root.id, version=root.version
    )
    assert {field.field_path for field in _request(repository, root.id).fields} == {
        "gpus.0.raw_name", "gpus.0.vram_bytes",
    }
    # The first correction still stands as its own fact.
    assert _request(repository, first_request.id).fields[0].answer_state is AnswerState.answered


def test_one_direct_successor_per_source_request(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, amendments = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(answers, request, answers={"device_form": "Desktop PC"}, key="answer-root")
    resolved = _request(repository, request.id)

    payload = {
        "source_request_id": resolved.id,
        "expected_request_version": resolved.version,
        "expected_context_revision": resolved.expected_context_revision,
        "field_paths": ["device_form"],
        "reason": "correct the device form",
    }
    successes: list[str] = []
    failures: list[ContractError] = []
    barrier = threading.Barrier(2)

    def submit(key: str):
        try:
            barrier.wait(timeout=5)
            result = amendments.create_information_amendment(payload, _ctx(key))
            successes.append(result.refs[1].entity_id)
        except ContractError as error:
            failures.append(error)

    threads = [
        threading.Thread(target=submit, args=("amend-1",)),
        threading.Thread(target=submit, args=("amend-2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code is ErrorCode.INFORMATION_CONTEXT_CHANGED
    assert failures[0].details.replacement_resource_ref is not None
    assert failures[0].details.replacement_resource_ref.entity_id == successes[0]
    with repository.unit_of_work() as uow:
        assert len(uow.iter_objects("InformationAmendment")) == 1
        assert len(uow.query("SELECT 1 FROM information_amendment_links")) == 1


def test_amendment_requires_a_handled_source_request(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, amendments = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])

    with pytest.raises(ContractError) as caught:
        amendments.create_information_amendment(
            {
                "source_request_id": request.id,
                "expected_request_version": request.version,
                "expected_context_revision": request.expected_context_revision,
                "field_paths": ["device_form"],
                "reason": "too early",
            },
            _ctx("amend-open"),
        )
    assert caught.value.code is ErrorCode.INFORMATION_REQUEST_STATE_CONFLICT


def test_amendment_field_must_belong_to_the_root_domain(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, amendments = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(answers, request, answers={"device_form": "Desktop PC"}, key="answer-root")
    resolved = _request(repository, request.id)

    with pytest.raises(ContractError) as caught:
        amendments.create_information_amendment(
            {
                "source_request_id": resolved.id,
                "expected_request_version": resolved.version,
                "expected_context_revision": resolved.expected_context_revision,
                "field_paths": ["gpus.0.raw_name"],
                "reason": "not asked here",
            },
            _ctx("amend-domain"),
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED


def test_an_old_chain_without_a_root_snapshot_is_refused(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, amendments = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])

    with repository.unit_of_work() as uow:
        uow.insert_object_bytes(
            "InformationRequest",
            schema_version="product-contract/0.3",
            raw=canonical_json({
                "id": _uid(700),
                "schema_version": "product-contract/0.3",
                "created_at": utc_now(),
                "created_by_task_id": None,
                "source_task_id": accepted.task_id,
                "version": 2,
                "context_refs": [ref.model_dump(mode="json") for ref in request.context_refs],
                "expected_context_revision": 2,
                "fields": [field.model_dump(mode="json") for field in request.fields],
                "state": "resolved",
                "result_refs": [],
                "superseded_by_request_id": None,
                "continuation_mode": "facts_only",
                "amends_request_id": request.id,
                "amendment_root_ref": None,
            }),
            object_id=_uid(700),
        )

    with pytest.raises(ContractError) as caught:
        amendments.create_information_amendment(
            {
                "source_request_id": _uid(700),
                "expected_request_version": 2,
                "expected_context_revision": 2,
                "field_paths": ["device_form"],
                "reason": "legacy chain",
            },
            _ctx("amend-legacy"),
        )
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    with repository.unit_of_work() as uow:
        assert len(uow.iter_objects("InformationAmendment")) == 0


# --- 2D-B: answers are consumed by a rerun -----------------------------------


def _requeue(repository, task_id):
    """Test-only scheduler nudge: put the same task back in the claim slot."""
    with repository.unit_of_work() as uow:
        row = uow.query("SELECT revision FROM tasks WHERE task_id=?", (task_id,))[0]
        uow.execute(
            "UPDATE tasks SET state='queued', revision=?, updated_at=? WHERE task_id=?",
            (row["revision"] + 1, utc_now(), task_id),
        )


def _request_count(repository, task_id):
    with repository.unit_of_work() as uow:
        return sum(
            1 for stored in uow.iter_objects("InformationRequest")
            if stored.payload.get("source_task_id") == task_id
            and stored.payload.get("continuation_mode") == "source_task"
        )


def _latest_source_request(repository, task_id):
    with repository.unit_of_work() as uow:
        rows = uow.query(
            "SELECT id FROM objects WHERE entity_type='InformationRequest'"
            " ORDER BY created_at DESC, rowid DESC"
        )
        for row in rows:
            stored = uow.find_object("InformationRequest", row["id"])
            if stored is None:
                continue
            request = InformationRequest.model_validate_json(stored.payload_bytes)
            if (
                request.source_task_id != task_id
                or request.continuation_mode is not InformationContinuationMode.source_task
            ):
                continue
            head = uow.get_head("InformationRequest", request.id)
            if head is not None:
                request = InformationRequest.model_validate_json(
                    canonical_json(uow.get_snapshot("InformationRequest", request.id, head))
                )
            return request
    return None


def _last_response(repository):
    with repository.unit_of_work() as uow:
        return uow.iter_objects("InformationResponse")[-1].payload


def test_answering_a_critical_field_lets_a_rerun_complete_without_reprobing(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(answers, request, answers={"device_form": "Desktop PC"})
    assert _task_row(repository, accepted.task_id)["state"] == "queued"
    probe.calls.clear()

    outcome = tasks.run_once()

    assert outcome.state.value == "completed"
    assert probe.calls == [["cpu"]]  # the answered path is not probed again
    assert _request_count(repository, accepted.task_id) == 1
    snapshot = _latest_snapshot(repository, _snapshot(repository, request).series_id)
    assert snapshot.device_form.value == "Desktop PC"
    assert any(
        attempt.information_response_id for attempt in snapshot.device_form.attempts
    )


def test_a_partial_answer_without_cause_ref_is_consumed_on_a_rerun(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={
            "gpus.0.raw_name": "PROBE_FAILED",
            "device_form": "PROBE_FAILED",
            "memory_bytes": "PROBE_FAILED",
        },
    )
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(
        tasks, ["cpu", "gpus.0.raw_name", "device_form", "memory_bytes"]
    )
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(answers, request, answers={"memory_bytes": 17179869184})
    # The critical fields are still open, so the answer wrote no cause_ref and
    # the task was not woken.
    assert _task_row(repository, accepted.task_id)["state"] == "waiting_input"
    probe.calls.clear()

    _requeue(repository, accepted.task_id)
    tasks.run_once()

    assert probe.calls and "memory_bytes" not in probe.calls[0]
    newest = _latest_source_request(repository, accepted.task_id)
    assert {field.field_path for field in newest.fields} == {
        "gpus.0.raw_name", "device_form",
    }
    snapshot = _latest_snapshot(repository, _snapshot(repository, request).series_id)
    assert snapshot.memory_bytes.value == 17179869184


def test_a_rerun_with_no_progress_does_not_ask_again(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, failures={"device_form": "PROBE_FAILED"})
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form"])
    tasks.run_once()
    before = _request_count(repository, accepted.task_id)

    _requeue(repository, accepted.task_id)
    outcome = tasks.run_once()

    assert outcome.state.value == "failed"
    row = _task_row(repository, accepted.task_id)
    assert row["last_error_code"] == "PRECONDITION_UNVERIFIED"
    assert json.loads(row["stop_reason"]) == {
        "kind": "execution", "code": "input_unresolved",
    }
    assert _request_count(repository, accepted.task_id) == before


def test_the_response_states_what_it_answered_and_what_stayed_unresolved(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={"device_form": "PROBE_FAILED", "memory_bytes": "PROBE_FAILED"},
    )
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form", "memory_bytes"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])

    _answer(
        answers, request,
        answers={"device_form": "Desktop PC", "memory_bytes": "16 GB"},
    )

    response = _last_response(repository)
    assert response["answered_fields"] == ["device_form"]
    assert response["unresolved_fields"] == ["memory_bytes"]
    assert response["answered_values"] == {"device_form": "Desktop PC"}
    # The player's words stay in answers; they never become a known value.
    assert response["answers"]["memory_bytes"] == "16 GB"
    assert set(response["answered_fields"]) & set(response["unresolved_fields"]) == set()


def test_a_later_unresolved_answer_withdraws_the_earlier_value(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={"device_form": "PROBE_FAILED", "memory_bytes": "PROBE_FAILED"},
    )
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form", "memory_bytes"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(answers, request, answers={"device_form": "Desktop PC"}, key="answer-1")
    updated = _request(repository, request.id)
    _answer(
        answers, updated,
        answers={"device_form": 123, "memory_bytes": 17179869184},
        key="answer-2",
    )

    from game_app.tasks import TaskContext

    answered = TaskContext(tasks, accepted.task_id).answered_values
    assert "device_form" not in answered
    assert answered["memory_bytes"].value == 17179869184

    tasks.run_once()
    newest = _latest_source_request(repository, accepted.task_id)
    assert {field.field_path for field in newest.fields} == {"device_form"}


def test_a_facts_only_answer_does_not_associate_with_the_source_task(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"},
                      failures={"gpus.0.raw_name": "PROBE_FAILED",
                                "gpus.0.vram_bytes": "PROBE_FAILED"})
    repository, files, device, tasks, answers, amendments = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "gpus.0.raw_name", "gpus.0.vram_bytes"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(
        answers, request,
        answers={"gpus.0.raw_name": "Synthetic GPU", "gpus.0.vram_bytes": 8},
        key="answer-root",
    )
    resolved = _request(repository, request.id)
    with repository.unit_of_work() as uow:
        before = uow.query(
            "SELECT COUNT(*) AS n FROM task_responses WHERE task_id=?", (accepted.task_id,)
        )[0]["n"]
    assert before == 1

    amendment = amendments.create_information_amendment(
        {
            "source_request_id": resolved.id,
            "expected_request_version": resolved.version,
            "expected_context_revision": resolved.expected_context_revision,
            "field_paths": ["gpus.0.raw_name"],
            "reason": "clarify the GPU name",
        },
        _ctx("amend-task-response"),
    )
    new_request = _request(repository, amendment.refs[1].entity_id)
    _answer(answers, new_request, answers={"gpus.0.raw_name": "Other GPU"}, key="answer-2")

    with repository.unit_of_work() as uow:
        after = uow.query(
            "SELECT COUNT(*) AS n FROM task_responses WHERE task_id=?", (accepted.task_id,)
        )[0]["n"]
    assert after == before


def test_an_identical_question_is_not_asked_a_third_time(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={"device_form": "PROBE_FAILED", "memory_bytes": "PROBE_FAILED"},
    )
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form", "memory_bytes"])
    tasks.run_once()
    first = _request(repository, _request_ids(repository, accepted.task_id)[0])
    # device_form is answered and critical, so the task wakes; memory stays
    # unresolved, so the rerun asks about it alone.
    _answer(
        answers, first,
        answers={"device_form": "Desktop PC", "memory_bytes": "16 GB"},
        key="answer-1",
    )
    tasks.run_once()
    second = _latest_source_request(repository, accepted.task_id)
    assert [field.field_path for field in second.fields] == ["memory_bytes"]
    before = _request_count(repository, accepted.task_id)

    _answer(answers, second, answers={"memory_bytes": "still 16 GB"}, key="answer-2")
    _requeue(repository, accepted.task_id)
    outcome = tasks.run_once()

    assert outcome.state.value == "failed"
    row = _task_row(repository, accepted.task_id)
    assert row["last_error_code"] == "PRECONDITION_UNVERIFIED"
    assert _request_count(repository, accepted.task_id) == before


# --- review fix pass: a response describes its own submission -----------------


def _responses(repository) -> list:
    from game_app.contracts.information import InformationResponse

    with repository.unit_of_work() as uow:
        return [
            InformationResponse.model_validate_json(stored.payload_bytes)
            for stored in uow.iter_objects("InformationResponse")
        ]


def test_a_second_answer_reports_only_the_fields_it_submitted(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={"device_form": "PROBE_FAILED", "memory_bytes": "PROBE_FAILED"},
    )
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form", "memory_bytes"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(answers, request, answers={"device_form": "Desktop PC"}, key="answer-1")
    updated = _request(repository, request.id)

    _answer(answers, updated, answers={"memory_bytes": 17179869184}, key="answer-2")

    second = _responses(repository)[-1]
    # The earlier answer is part of the request projection, not this submission.
    assert second.answered_fields == ["memory_bytes"]
    assert second.answered_values == {"memory_bytes": 17179869184}
    assert "device_form" not in second.unresolved_fields


def test_re_answering_an_older_request_does_not_overwrite_a_newer_probe(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={"device_form": "PROBE_FAILED", "memory_bytes": "PROBE_FAILED"},
    )
    repository, files, device, tasks, answers, _ = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "device_form", "memory_bytes"])
    tasks.run_once()
    request = _request(repository, _request_ids(repository, accepted.task_id)[0])
    _answer(answers, request, answers={"device_form": "Desktop PC"}, key="answer-1")
    tasks.run_once()  # probes memory, applies the stored answer

    # A later scan on the same device series reads the form successfully.
    probe.failures.pop("device_form")
    probe.values["device_form"] = "Probe Form"
    later = _scan(tasks, ["cpu", "device_form"], key="scan-2")
    assert tasks.run_once().state.value == "completed"
    series_id = _snapshot(repository, _request(repository, request.id)).series_id
    assert _latest_snapshot(repository, series_id).device_form.value == "Probe Form"

    # The first question is still partially answered; answering it again must
    # not replay the stale answer over the newer probe.
    _answer(
        answers, _request(repository, request.id),
        answers={"memory_bytes": 17179869184}, key="answer-2",
    )

    after = _latest_snapshot(repository, series_id)
    assert after.device_form.value == "Probe Form"
    form_attempts = [
        attempt for attempt in after.device_form.attempts
        if attempt.information_response_id is not None
    ]
    # Exactly one response ever answered this field.
    assert len(form_attempts) == 1
