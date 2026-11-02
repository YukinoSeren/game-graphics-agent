"""DEV-T01: read-only acquisition, unit handling, failure questions.

Every probe is a stub: no hardware is queried and no game is started.
"""

import pytest

from game_app.contracts.common import MutationContext
from game_app.contracts.device import DeviceSnapshot
from game_app.contracts.enums import AcquisitionStatus, CheckStatus, ValueState
from game_app.contracts.information import InformationRequest
from game_app.contracts.primitives import utc_now
from game_app.device import DeviceService, ProbeReading
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService
from game_app.repository import ProductRepository, canonical_json
from game_app.tasks import TaskService
from tests.product.support.spies import SpyProvider


def _uid(value: int) -> str:
    return f"{value:032x}"


def _ctx(key: str = "scan-key") -> MutationContext:
    return MutationContext(user_scope_id=_uid(90), idempotency_key=key, request_id=_uid(91))


class StubProbe:
    """Deterministic probe: per-path values, failures or timeouts."""

    def __init__(self, values=None, failures=None, timeouts=()):
        self.values = values or {}
        self.failures = failures or {}
        self.timeouts = set(timeouts)
        self.calls: list[list[str]] = []

    def probe(self, field_paths, *, timeout):
        self.calls.append(list(field_paths))
        readings = []
        for path in field_paths:
            if path in self.timeouts:
                raise TimeoutError(f"simulated probe timeout for {path}")
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
                    field_path=path, status="succeeded", raw=str(value).encode("utf-8"),
                    text=str(value),
                ))
        return readings


def _env(tmp_path, probe):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    files = FileService(repo, tmp_path / "managed")
    device = DeviceService(repo, files, probe)
    tasks = TaskService(repo, provider=SpyProvider(), files=files)
    tasks.handlers["device_scan"] = device.handle
    return repo, files, device, tasks


def _scan(tasks, field_paths, key="scan-key", game_context_id=None):
    return tasks.register_task(
        {
            "kind": "device_scan",
            "game_context_id": game_context_id,
            "field_paths": field_paths,
        },
        _ctx(key),
    )


def _snapshot(repo, task_id):
    with repo.unit_of_work() as uow:
        row = uow.query("SELECT * FROM tasks WHERE task_id=?", (task_id,))[0]
    refs = __import__("json").loads(row["result_refs"] or "[]")
    snapshot_ids = [ref["entity_id"] for ref in refs if ref["entity_type"] == "DeviceSnapshot"]
    if not snapshot_ids:
        return None
    return DeviceSnapshot.model_validate_json(
        repo.get_object("DeviceSnapshot", snapshot_ids[0]).payload_bytes
    )


def _request(repo, task_id):
    with repo.unit_of_work() as uow:
        row = uow.query("SELECT * FROM tasks WHERE task_id=?", (task_id,))[0]
    refs = __import__("json").loads(row["result_refs"] or "[]")
    ids = [ref["entity_id"] for ref in refs if ref["entity_type"] == "InformationRequest"]
    if not ids:
        return None
    return InformationRequest.model_validate_json(
        repo.get_object("InformationRequest", ids[0]).payload_bytes
    )


def test_successful_scan_records_known_values_with_published_evidence(tmp_path):
    probe = StubProbe(values={
        "cpu": "Example CPU 8-Core",
        "memory_bytes": "17179869184",
        "os_version": "10.0.26200",
    })
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "memory_bytes", "os_version"])
    outcome = tasks.run_once()

    assert outcome.state.value == "completed"
    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.cpu.value_state is ValueState.known
    assert snapshot.cpu.value == "Example CPU 8-Core"
    assert snapshot.memory_bytes.value == 17179869184
    assert snapshot.os_version.value == "10.0.26200"
    assert snapshot.gpus == []
    assert snapshot.selected_gpu_id is None
    # Every known value cites a published original that is actually readable.
    evidence = snapshot.cpu.provenance[0].evidence_refs[0]
    assert evidence.kind == "artifact"
    assert files.read_managed_file(evidence.file_id) == b"Example CPU 8-Core"


def test_unknown_field_path_is_rejected_before_any_probe(tmp_path):
    probe = StubProbe(values={})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "gpus.0.fake_field"])
    tasks.run_once()

    assert probe.calls == []
    with repo.unit_of_work() as uow:
        row = uow.query("SELECT * FROM tasks WHERE task_id=?", (accepted.task_id,))[0]
    assert row["state"] == "failed"
    assert row["last_error_code"] == ErrorCode.INPUT_VALIDATION_FAILED.value


def test_empty_field_list_is_rejected(tmp_path):
    probe = StubProbe(values={})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, [])
    tasks.run_once()
    assert probe.calls == []
    with repo.unit_of_work() as uow:
        row = uow.query("SELECT * FROM tasks WHERE task_id=?", (accepted.task_id,))[0]
    assert row["last_error_code"] == ErrorCode.INPUT_VALIDATION_FAILED.value


def test_gpu_failure_asks_instead_of_claiming_the_machine_has_no_gpu(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={"gpus.0.raw_name": "PROBE_FAILED"},
    )
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "gpus.0.raw_name", "gpus.0.vram_bytes"])
    outcome = tasks.run_once()

    assert outcome.state.value == "waiting_input"
    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.gpus == []
    # The empty list is explained by a failed check, never "no GPU present".
    assert any(check.status is CheckStatus.fail for check in snapshot.checks)

    request = _request(repo, accepted.task_id)
    assert request is not None
    assert request.state.value == "open"
    paths = [field.field_path for field in request.fields]
    assert "gpus.0.raw_name" in paths
    gpu_field = next(field for field in request.fields if field.field_path == "gpus.0.raw_name")
    assert gpu_field.required_for
    assert gpu_field.answer_state.value == "unanswered"


def test_critical_and_noncritical_failures_are_planned_separately(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={
            "gpus.0.raw_name": "PROBE_FAILED",
            "device_form": "PROBE_FAILED",
            "memory_bytes": "PROBE_FAILED",
        },
    )
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(
        tasks, ["cpu", "gpus.0.raw_name", "device_form", "memory_bytes"]
    )
    tasks.run_once()

    request = _request(repo, accepted.task_id)
    critical = [f for f in request.fields if f.required_for]
    noncritical = [f for f in request.fields if not f.required_for]
    assert {f.field_path for f in critical} == {"gpus.0.raw_name", "device_form"}
    assert {f.field_path for f in noncritical} == {"memory_bytes"}
    # A known field never appears in the question.
    assert "cpu" not in {f.field_path for f in request.fields}


def test_noncritical_failure_completes_with_a_limited_result(tmp_path):
    probe = StubProbe(
        values={"cpu": "Example CPU"},
        failures={"memory_bytes": "PROBE_FAILED"},
    )
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "memory_bytes"])
    outcome = tasks.run_once()

    assert outcome.state.value == "partial"
    with repo.unit_of_work() as uow:
        row = uow.query("SELECT * FROM tasks WHERE task_id=?", (accepted.task_id,))[0]
    assert __import__("json").loads(row["domain_result"])["code"] == "limited"
    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.cpu.value_state is ValueState.known
    assert snapshot.memory_bytes.value is None
    assert snapshot.memory_bytes.acquisition_status is AcquisitionStatus.failed


def test_timeout_records_a_failed_attempt_without_fabricating_a_value(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"}, timeouts={"memory_bytes"})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "memory_bytes"])
    tasks.run_once()

    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.memory_bytes.value is None
    assert snapshot.memory_bytes.value_state is ValueState.unknown
    assert snapshot.memory_bytes.acquisition_status is AcquisitionStatus.failed
    assert snapshot.memory_bytes.attempts
    assert snapshot.memory_bytes.attempts[0].error_code == "PROBE_TIMEOUT"


def test_identical_gpu_names_stay_separate_and_none_is_selected(tmp_path):
    probe = StubProbe(values={
        "gpus.0.raw_name": "Generic GPU",
        "gpus.1.raw_name": "Generic GPU",
        "gpus.0.vram_bytes": "8589934592",
        "gpus.1.vram_bytes": "17179869184",
    })
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(
        tasks,
        ["gpus.0.raw_name", "gpus.1.raw_name", "gpus.0.vram_bytes", "gpus.1.vram_bytes"],
    )
    tasks.run_once()

    snapshot = _snapshot(repo, accepted.task_id)
    assert len(snapshot.gpus) == 2
    assert snapshot.gpus[0].id != snapshot.gpus[1].id
    assert [gpu.raw_name.value for gpu in snapshot.gpus] == ["Generic GPU", "Generic GPU"]
    assert [gpu.vram_bytes.value for gpu in snapshot.gpus] == [8589934592, 17179869184]
    assert snapshot.selected_gpu_id is None


def test_missing_display_values_do_not_default_to_1080p_or_60hz(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu"])
    tasks.run_once()

    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.display.desktop_resolution.value_state is ValueState.unknown
    assert snapshot.display.desktop_resolution.value is None
    assert snapshot.display.refresh_hz.value is None


def test_memory_and_refresh_units_are_parsed_strictly(tmp_path):
    probe = StubProbe(values={
        "memory_bytes": "16 GB",
        "display.refresh_hz": "165",
    })
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["memory_bytes", "display.refresh_hz"])
    tasks.run_once()

    snapshot = _snapshot(repo, accepted.task_id)
    # "16 GB" is not converted: the adapter must report bytes, not a unit guess.
    assert snapshot.memory_bytes.value is None
    assert snapshot.memory_bytes.value_state is ValueState.unknown
    assert snapshot.memory_bytes.unknown_reason == "parse_failed"
    assert snapshot.display.refresh_hz.value == 165.0


def test_adapter_receives_exactly_the_requested_whitelisted_paths(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU", "os_version": "10.0"})
    repo, files, device, tasks = _env(tmp_path, probe)
    _scan(tasks, ["cpu", "os_version"])
    tasks.run_once()
    assert probe.calls == [["cpu", "os_version"]]


def test_resolution_is_only_built_from_a_complete_positive_pair(tmp_path):
    probe = StubProbe(values={
        "display.desktop_resolution.width": "2560",
        "display.desktop_resolution.height": "1440",
    })
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(
        tasks, ["display.desktop_resolution.width", "display.desktop_resolution.height"]
    )
    tasks.run_once()

    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.display.desktop_resolution.value_state is ValueState.known
    assert snapshot.display.desktop_resolution.value.width == 2560
    assert snapshot.display.desktop_resolution.value.height == 1440


def test_zero_resolution_is_not_accepted_as_known(tmp_path):
    probe = StubProbe(values={
        "display.desktop_resolution.width": "0",
        "display.desktop_resolution.height": "1440",
    })
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(
        tasks, ["display.desktop_resolution.width", "display.desktop_resolution.height"]
    )
    tasks.run_once()
    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.display.desktop_resolution.value is None


def test_probe_original_is_published_even_when_parsing_fails(tmp_path):
    probe = StubProbe(values={"memory_bytes": "not-a-number"})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["memory_bytes"])
    tasks.run_once()

    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.memory_bytes.value_state is ValueState.unknown
    evidence = snapshot.memory_bytes.provenance[0].evidence_refs[0]
    assert files.read_managed_file(evidence.file_id) == b"not-a-number"


def test_device_service_refuses_to_run_without_a_managed_file_store(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    with pytest.raises(ContractError) as caught:
        DeviceService(repo, None, StubProbe(values={}))  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED


def test_windows_adapter_refuses_unregistered_paths_without_running_anything():
    from game_app.adapters.windows_probe import query_for

    for bad in (
        "gpus.0.name; Remove-Item C:\\",
        "cpu | Format-List",
        "gpus.99.raw_name",
        "$(Get-Process)",
    ):
        with pytest.raises(ValueError):
            query_for(bad)
    # Whitelisted paths map to constant, read-only queries.
    assert "Win32_VideoController" in query_for("gpus.1.raw_name")
    assert "Win32_Processor" in query_for("cpu")


# --- review fix pass ---------------------------------------------------------

def test_a_sparse_gpu_index_is_matched_by_index_not_list_position(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU", "gpus.1.raw_name": "GPU B"})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu", "gpus.1.raw_name"])
    outcome = tasks.run_once()

    # The successfully probed GPU must not be asked about or park the task.
    assert outcome.state.value == "completed"
    assert _request(repo, accepted.task_id) is None
    snapshot = _snapshot(repo, accepted.task_id)
    assert [gpu.raw_name.value for gpu in snapshot.gpus] == ["GPU B"]


def test_each_adapter_keeps_its_probe_index(tmp_path):
    """The stored list is sparse, so the adapter's own position is recorded."""
    probe = StubProbe(
        values={"gpus.1.raw_name": "GPU B", "gpus.3.raw_name": "GPU D"},
        failures={"gpus.0.raw_name": "PROBE_FAILED"},
    )
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(
        tasks, ["gpus.0.raw_name", "gpus.1.raw_name", "gpus.3.raw_name"]
    )
    tasks.run_once()

    snapshot = _snapshot(repo, accepted.task_id)
    assert [gpu.probe_index for gpu in snapshot.gpus] == [1, 3]
    assert [gpu.raw_name.value for gpu in snapshot.gpus] == ["GPU B", "GPU D"]


def test_a_failed_gpu_index_is_asked_about_even_below_a_known_one(tmp_path):
    probe = StubProbe(
        values={"gpus.1.raw_name": "GPU B"},
        failures={"gpus.0.raw_name": "PROBE_FAILED"},
    )
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["gpus.0.raw_name", "gpus.1.raw_name"])
    outcome = tasks.run_once()

    assert outcome.state.value == "waiting_input"
    request = _request(repo, accepted.task_id)
    assert [field.field_path for field in request.fields] == ["gpus.0.raw_name"]


def test_zero_or_negative_measurements_are_not_known(tmp_path):
    probe = StubProbe(values={
        "memory_bytes": "0",
        "display.refresh_hz": "-1",
        "gpus.0.raw_name": "GPU A",
        "gpus.0.vram_bytes": "0",
    })
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(
        tasks,
        ["memory_bytes", "display.refresh_hz", "gpus.0.raw_name", "gpus.0.vram_bytes"],
    )
    tasks.run_once()

    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.memory_bytes.value is None
    assert snapshot.memory_bytes.unknown_reason == "parse_failed"
    assert snapshot.display.refresh_hz.value is None
    assert snapshot.gpus[0].vram_bytes.value is None


def test_unknown_game_context_is_rejected_before_probing(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu"], game_context_id=_uid(999))
    tasks.run_once()

    assert probe.calls == []
    with repo.unit_of_work() as uow:
        row = uow.query("SELECT * FROM tasks WHERE task_id=?", (accepted.task_id,))[0]
        snapshots = uow.query(
            "SELECT COUNT(*) AS n FROM objects WHERE entity_type='DeviceSnapshot'"
        )[0]["n"]
    assert row["last_error_code"] == ErrorCode.REFERENCE_INVALID.value
    assert snapshots == 0


def test_device_questions_reference_the_snapshot_they_are_about(tmp_path):
    probe = StubProbe(failures={"gpus.0.raw_name": "PROBE_FAILED"})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["gpus.0.raw_name"])
    tasks.run_once()

    request = _request(repo, accepted.task_id)
    snapshot = _snapshot(repo, accepted.task_id)
    assert [ref.entity_type for ref in request.context_refs] == ["DeviceSnapshot"]
    assert request.context_refs[0].entity_id == snapshot.id
    assert request.context_refs[0].version == snapshot.version


# --- review fix pass 2: per-field resolution honesty -------------------------


def test_a_known_resolution_axis_is_not_asked_again(tmp_path):
    probe = StubProbe(
        values={"display.desktop_resolution.width": "2560"},
        failures={"display.desktop_resolution.height": "PROBE_FAILED"},
    )
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(
        tasks,
        ["display.desktop_resolution.width", "display.desktop_resolution.height"],
    )
    outcome = tasks.run_once()

    assert outcome.state.value == "partial"
    request = _request(repo, accepted.task_id)
    assert [field.field_path for field in request.fields] == [
        "display.desktop_resolution.height"
    ]
    snapshot = _snapshot(repo, accepted.task_id)
    # The composite resolution still needs both halves.
    assert snapshot.display.desktop_resolution.value is None


def test_a_single_known_resolution_axis_completes_without_a_question(tmp_path):
    probe = StubProbe(values={"display.desktop_resolution.width": "2560"})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["display.desktop_resolution.width"])
    outcome = tasks.run_once()

    assert outcome.state.value == "completed"
    assert _request(repo, accepted.task_id) is None


def test_an_unrequested_gpu_group_is_marked_not_checked(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu"])
    tasks.run_once()

    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.gpus == []
    group = [check for check in snapshot.checks if check.check_id == "probe.gpus"]
    assert group, "the empty GPU list must be explained by a check"
    assert group[0].status is CheckStatus.not_run


def test_a_failed_question_write_does_not_leave_an_orphan_snapshot(tmp_path, monkeypatch):
    from game_app.repository import UnitOfWork

    probe = StubProbe(failures={"gpus.0.raw_name": "PROBE_FAILED"})
    repo, files, device, tasks = _env(tmp_path, probe)
    original = UnitOfWork.insert_object

    def boom(self, entity_type, entity):
        if entity_type == "InformationRequest":
            raise RuntimeError("injected question write failure")
        return original(self, entity_type, entity)

    monkeypatch.setattr(UnitOfWork, "insert_object", boom)
    accepted = _scan(tasks, ["gpus.0.raw_name"])
    outcome = tasks.run_once()

    assert outcome.state.value == "failed"
    with repo.unit_of_work() as uow:
        snapshots = uow.query(
            "SELECT COUNT(*) AS n FROM objects WHERE entity_type='DeviceSnapshot'"
        )[0]["n"]
    assert snapshots == 0


# --- 2D-B: stable device series and snapshot ownership ----------------------


def _context_stub(repo, *, installation_id=None):
    context_id = _uid(720)
    with repo.unit_of_work() as uow:
        uow.insert_object_bytes(
            "GameContext",
            schema_version="product-contract/0.4",
            raw=canonical_json({
                "id": context_id,
                "series_id": context_id,
                "version": 1,
                "installation_id": installation_id,
            }),
            object_id=context_id,
            series_id=context_id,
            version=1,
        )
    return context_id


def _two_scans(repo, tasks, *, game_context_id):
    first = _scan(tasks, ["cpu"], key="scan-a", game_context_id=game_context_id)
    assert tasks.run_once().state.value == "completed"
    second = _scan(tasks, ["cpu"], key="scan-b", game_context_id=game_context_id)
    assert tasks.run_once().state.value == "completed"
    return _snapshot(repo, first.task_id), _snapshot(repo, second.task_id)


def _slot(repo):
    with repo.unit_of_work() as uow:
        rows = uow.query("SELECT * FROM device_series_slots")
    assert len(rows) == 1
    return rows[0]


def test_repeated_scans_share_one_device_series_and_increment_the_version(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"})
    repo, files, device, tasks = _env(tmp_path, probe)
    context_id = _context_stub(repo, installation_id=_uid(721))

    left, right = _two_scans(repo, tasks, game_context_id=context_id)

    assert left.series_id == right.series_id
    assert left.device_series_id == right.device_series_id
    assert (left.version, right.version) == (1, 2)
    slot = _slot(repo)
    assert slot["scope_key"] == f"installation:{_uid(721)}"
    assert slot["series_id"] == left.series_id
    assert slot["device_series_id"] == left.device_series_id


def test_a_context_without_an_installation_uses_the_context_slot(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"})
    repo, files, device, tasks = _env(tmp_path, probe)
    context_id = _context_stub(repo)

    left, right = _two_scans(repo, tasks, game_context_id=context_id)

    assert left.series_id == right.series_id
    assert _slot(repo)["scope_key"] == f"context:{context_id}"


def test_scans_without_a_context_use_the_user_slot(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"})
    repo, files, device, tasks = _env(tmp_path, probe)

    left, right = _two_scans(repo, tasks, game_context_id=None)

    assert left.series_id == right.series_id
    assert _slot(repo)["scope_key"] == "user"


def test_a_snapshot_records_the_task_owner(tmp_path):
    probe = StubProbe(values={"cpu": "Example CPU"})
    repo, files, device, tasks = _env(tmp_path, probe)
    accepted = _scan(tasks, ["cpu"])
    tasks.run_once()

    snapshot = _snapshot(repo, accepted.task_id)
    assert snapshot.user_scope_id == _uid(90)
