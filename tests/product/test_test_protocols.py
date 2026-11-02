"""PERF-T02 A-3/A-5: metric catalog and test protocol creation (04 §4.4)."""

from __future__ import annotations

import json
import threading

import pytest
from pydantic import ValidationError

from game_app.contracts.device import GameInstallation
from game_app.contracts.enums import FrameStreamKind
from game_app.contracts.performance import (
    CreateSessionRequest,
    CreateTestProtocolRequest,
    MetricCatalogQuery,
)
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService, PublishHooks
from game_app.metrics import REGISTRY_VERSION, registry_manifest_hash
from game_app.performance import PerformanceService
from game_app.contracts.primitives import utc_now
from game_app.repository import ProductRepository, canonical_json
from tests.product.support.configuration import (
    GAME_ID,
    context_key,
    setup,
    store_raw,
    uid,
    unknown_platform,
)
from tests.product.test_sessions import FakeClock

ALL_KEYS = [
    "average_present_rate/v1",
    "interval_p95_nearest_rank/v1",
    "interval_p99_nearest_rank/v1",
    "slowest_1pct_inverse_mean/v1",
]


def _service(repository, files, clock=None) -> PerformanceService:
    if clock is None:
        return PerformanceService(repository, files)
    return PerformanceService(repository, files, clock=clock)


def _protocol(context_id: str, **overrides) -> CreateTestProtocolRequest:
    payload = {
        "game_context_id": context_id,
        "parent_protocol_id": None,
        "scenario": None,
        "condition_fields": {},
        "controlled_variables": [],
        "metric_registry_version": REGISTRY_VERSION,
        "metric_keys": ["average_present_rate/v1"],
        "warmup_seconds": None,
        "sample_duration_seconds": None,
        "repeat_count": None,
        "note": None,
    }
    payload.update(overrides)
    return CreateTestProtocolRequest.model_validate_json(canonical_json(payload))


def _count(repository, entity_type) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def _rule_rows(repository) -> int:
    with repository.unit_of_work() as uow:
        return uow.query("SELECT COUNT(*) AS n FROM metric_definition_rules")[0]["n"]


def test_empty_catalog_names_every_rule_and_writes_nothing(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)
    before = _count(repository, "MetricDefinition") + _rule_rows(repository)

    page = service.list_metric_definitions(MetricCatalogQuery(frame_stream_kind=None))

    assert page.registry_version == "project-metrics/1"
    assert [choice.key for choice in page.choices] == ALL_KEYS
    assert {choice.definition_id for choice in page.choices} == {None}
    assert all(
        choice.frame_stream_kind is FrameStreamKind.application_present
        for choice in page.choices
    )
    assert _count(repository, "MetricDefinition") == 0
    assert _rule_rows(repository) == 0
    assert before == 0

    displayed = service.list_metric_definitions(
        MetricCatalogQuery(frame_stream_kind=FrameStreamKind.displayed)
    )
    assert displayed.choices == []


def test_create_returns_the_protocol_and_a_readable_declaration(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)
    request = _protocol(
        context_id,
        scenario="玩家声明的固定路线甲",
        condition_fields={
            "output_width": 1920,
            "output_height": 1080,
            "frame_generation": False,
        },
        controlled_variables=["scenario", "output_resolution", "frame_generation"],
        metric_keys=["average_present_rate/v1", "interval_p99_nearest_rank/v1"],
        sample_duration_seconds=60,
        repeat_count=3,
    )

    result = service.create_test_protocol(request, context_key("protocol-1"))

    assert [ref.entity_type for ref in result.refs] == ["TestProtocol", "ManagedFile"]
    protocol = service.get_test_protocol(result.refs[0].entity_id)
    assert protocol.game_id == GAME_ID
    assert protocol.version == 1
    assert protocol.parent_protocol_id is None
    assert protocol.controlled_variables == ["scenario", "output_resolution", "frame_generation"]
    assert protocol.warmup_seconds is None
    assert protocol.sample_duration_seconds == 60
    assert protocol.repeat_count == 3
    assert protocol.scenario.value == "玩家声明的固定路线甲"
    assert protocol.conditions.output_resolution.value.width == 1920
    assert protocol.conditions.output_resolution.value.height == 1080
    assert protocol.conditions.frame_generation.value is False
    assert protocol.conditions.upscaling.value_state.value == "unknown"
    assert len(protocol.metric_definition_ids) == 2
    assert protocol.declaration_file_id == result.refs[1].entity_id

    declaration = json.loads(files.read_managed_file(protocol.declaration_file_id))
    assert declaration["kind"] == "test_protocol_declaration"
    assert declaration["game_context_id"] == context_id
    assert declaration["game_id"] == GAME_ID
    assert declaration["protocol_id"] == protocol.id
    assert declaration["registry_manifest_hash"] == registry_manifest_hash()
    assert declaration["input"]["metric_keys"] == [
        "average_present_rate/v1", "interval_p99_nearest_rank/v1",
    ]
    assert declaration["metric_definitions"] == {
        choice.key: choice.definition_id
        for choice in service.list_metric_definitions(
            MetricCatalogQuery(frame_stream_kind=None)
        ).choices
        if choice.definition_id is not None
    }
    assert set(declaration["metric_definitions"]) == {
        "average_present_rate/v1", "interval_p99_nearest_rank/v1",
    }

    # Only the two selected rules are registered, once each.
    assert _count(repository, "MetricDefinition") == 2
    assert _rule_rows(repository) == 2
    page = service.list_metric_definitions(MetricCatalogQuery(frame_stream_kind=None))
    known = {choice.key: choice.definition_id for choice in page.choices}
    assert known["average_present_rate/v1"] is not None
    assert known["interval_p99_nearest_rank/v1"] is not None
    assert known["interval_p95_nearest_rank/v1"] is None
    assert known["slowest_1pct_inverse_mean/v1"] is None

    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0


def test_replay_returns_the_same_protocol_and_changed_content_conflicts(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)
    request = _protocol(context_id)

    first = service.create_test_protocol(request, context_key("same"))
    replay = service.create_test_protocol(request, context_key("same"))

    assert [ref.model_dump() for ref in replay.refs] == [ref.model_dump() for ref in first.refs]
    assert _count(repository, "TestProtocol") == 1
    assert _count(repository, "ManagedFile") == 1

    with pytest.raises(ContractError) as caught:
        service.create_test_protocol(
            _protocol(context_id, metric_keys=["interval_p95_nearest_rank/v1"]),
            context_key("same"),
        )
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert _count(repository, "TestProtocol") == 1
    assert _rule_rows(repository) == 1


def test_two_concurrent_keys_share_the_registered_definitions(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    second_repository = ProductRepository(repository.path)
    second_files = FileService(second_repository, files.root)
    services = [_service(repository, files), _service(second_repository, second_files)]
    barrier = threading.Barrier(2)
    results: list = []
    errors: list[BaseException] = []

    def create(service, key):
        barrier.wait()
        try:
            results.append(
                service.create_test_protocol(_protocol(context_id), context_key(key))
            )
        except BaseException as error:  # surfaced below
            errors.append(error)

    threads = [
        threading.Thread(target=create, args=(services[index], f"conc-{index}"))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    second_repository.close()

    assert errors == []
    assert len(results) == 2
    protocols = [
        services[0].get_test_protocol(result.refs[0].entity_id) for result in results
    ]
    assert protocols[0].metric_definition_ids == protocols[1].metric_definition_ids
    assert _count(repository, "MetricDefinition") == 1
    assert _rule_rows(repository) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"repeat_count": True},
        {"repeat_count": 0},
        {"sample_duration_seconds": 0},
        {"sample_duration_seconds": -1},
        {"warmup_seconds": -1},
    ],
)
def test_invalid_sampling_parameters_are_refused(tmp_path, override):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)

    with pytest.raises(ValidationError):
        _protocol(context_id, **override)


def test_half_a_resolution_is_refused(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)

    with pytest.raises(ContractError) as caught:
        service.create_test_protocol(
            _protocol(context_id, condition_fields={"output_width": 1920}),
            context_key("half"),
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "TestProtocol") == 0


def test_a_controlled_variable_must_be_declared_and_complete(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)

    with pytest.raises(ContractError) as caught:
        service.create_test_protocol(
            _protocol(context_id, controlled_variables=["scene.weather"]),
            context_key("missing-scene"),
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert caught.value.field_errors[0].path == "controlled_variables"

    with pytest.raises(ContractError) as caught:
        service.create_test_protocol(
            _protocol(
                context_id,
                condition_fields={"output_width": 1920},
                controlled_variables=["output_resolution"],
            ),
            context_key("half-controlled"),
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED

    created = service.create_test_protocol(
        _protocol(
            context_id,
            condition_fields={"scene.weather": "rain"},
            controlled_variables=["scene.weather"],
        ),
        context_key("scene-ok"),
    )
    assert created.refs[0].entity_type == "TestProtocol"


def test_a_string_frame_generation_is_refused(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)

    with pytest.raises(ContractError) as caught:
        service.create_test_protocol(
            _protocol(context_id, condition_fields={"frame_generation": "false"}),
            context_key("string-fg"),
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "TestProtocol") == 0


def test_unknown_metric_and_registry_versions_are_separated(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)

    with pytest.raises(ContractError) as caught:
        service.create_test_protocol(
            _protocol(context_id, metric_keys=["made_up/v1"]), context_key("bad-key")
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert caught.value.field_errors[0].path == "metric_keys"

    with pytest.raises(ContractError) as caught:
        service.create_test_protocol(
            _protocol(context_id, metric_registry_version="project-metrics/99"),
            context_key("bad-registry"),
        )
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert _count(repository, "TestProtocol") == 0


def test_a_child_version_keeps_the_parent_and_old_session_reference(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)
    parent_result = service.create_test_protocol(_protocol(context_id), context_key("parent"))
    parent = service.get_test_protocol(parent_result.refs[0].entity_id)
    before = repository.get_object("TestProtocol", parent.id).payload_bytes
    session_payload = {
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation_id, "fields": {}, "raw_text": "",
        },
        "test_protocol_id": parent.id,
        "recommendation_id": None,
    }
    session = service.create_session(
        CreateSessionRequest.model_validate_json(canonical_json(session_payload)),
        context_key("session-parent"),
    )

    child_result = service.create_test_protocol(
        _protocol(context_id, parent_protocol_id=parent.id, note="corrected plan"),
        context_key("child"),
    )

    child = service.get_test_protocol(child_result.refs[0].entity_id)
    assert child.version == 2
    assert child.parent_protocol_id == parent.id
    assert child.series_id == parent.series_id
    assert child.note == "corrected plan"
    assert repository.get_object("TestProtocol", parent.id).payload_bytes == before
    assert service.get_session(session.refs[1].entity_id).test_protocol_id == parent.id


def test_a_parent_from_another_game_is_refused(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)
    other_context = store_raw(repository, "GameContext", {
        "id": uid(720), "installation_id": None, "game_id": uid(721),
    })
    other = service.create_test_protocol(
        _protocol(other_context), context_key("other-game")
    )

    with pytest.raises(ContractError) as caught:
        service.create_test_protocol(
            _protocol(context_id, parent_protocol_id=other.refs[0].entity_id),
            context_key("cross-game"),
        )

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _count(repository, "TestProtocol") == 1


def test_test_and_comparison_protocol_ids_are_not_interchangeable(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)
    comparison_id = store_raw(repository, "ComparisonProtocol", {
        "id": uid(730), "game_id": GAME_ID, "series_id": uid(731), "version": 1,
    })
    session_payload = {
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation_id, "fields": {}, "raw_text": "",
        },
        "test_protocol_id": comparison_id,
        "recommendation_id": None,
    }
    with pytest.raises(ContractError) as caught:
        service.create_session(
            CreateSessionRequest.model_validate_json(canonical_json(session_payload)),
            context_key("wrong-type-session"),
        )
    assert caught.value.code is ErrorCode.REFERENCE_INVALID

    protocol = service.create_test_protocol(_protocol(context_id), context_key("test"))
    with pytest.raises(ContractError) as caught:
        service.require_comparison_protocol(protocol.refs[0].entity_id)
    assert caught.value.code is ErrorCode.REFERENCE_INVALID


@pytest.mark.parametrize(
    "extra",
    [
        {"controlled": True},
        {"executed": True},
        {"created_at": "2026-11-01T00:00:00+00:00"},
        {"provenance": []},
    ],
)
def test_the_request_schema_accepts_no_self_reported_state(tmp_path, extra):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    payload = {
        "game_context_id": context_id,
        "parent_protocol_id": None,
        "scenario": None,
        "condition_fields": {},
        "controlled_variables": [],
        "metric_registry_version": REGISTRY_VERSION,
        "metric_keys": ["average_present_rate/v1"],
        "warmup_seconds": None,
        "sample_duration_seconds": None,
        "repeat_count": None,
        "note": None,
        **extra,
    }
    with pytest.raises(ValidationError):
        CreateTestProtocolRequest.model_validate_json(canonical_json(payload))


def test_creation_writes_no_execution_or_controlled_claim(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)

    result = service.create_test_protocol(_protocol(context_id), context_key("plain"))

    stored = repository.get_object("TestProtocol", result.refs[0].entity_id).payload
    assert "checks" not in stored
    assert "controlled" not in stored
    assert "executed" not in stored
    declaration = json.loads(files.read_managed_file(result.refs[1].entity_id))
    assert "controlled" not in declaration["input"]
    assert "executed" not in declaration["input"]
    assert not any(
        check.get("status") == "pass"
        for check in declaration.get("checks", [])
    )


# --- idempotent retries must not be defeated by a live clock -----------------


def test_a_retry_after_a_failed_commit_returns_the_same_protocol(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    service = _service(repository, files, clock)
    request = _protocol(context_id)

    def fail():
        raise RuntimeError("commit window failure")

    with pytest.raises(RuntimeError):
        service.create_test_protocol(
            request, context_key("retry"), hooks=PublishHooks(before_commit=fail)
        )
    clock.advance(42)

    result = service.create_test_protocol(request, context_key("retry"))

    assert result.refs[0].entity_type == "TestProtocol"
    assert _count(repository, "TestProtocol") == 1
    assert _count(repository, "ManagedFile") == 1


def test_two_same_key_submissions_share_one_protocol(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    second_repository = ProductRepository(repository.path)
    second_files = FileService(second_repository, files.root)
    first_clock = FakeClock()
    second_clock = FakeClock()
    second_clock.advance(3)
    services = [
        _service(repository, files, first_clock),
        _service(second_repository, second_files, second_clock),
    ]
    barrier = threading.Barrier(2)
    results: list = []
    errors: list[BaseException] = []

    def create(service):
        barrier.wait()
        try:
            results.append(
                service.create_test_protocol(_protocol(context_id), context_key("double"))
            )
        except BaseException as error:  # surfaced below
            errors.append(error)

    threads = [threading.Thread(target=create, args=(item,)) for item in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    second_repository.close()

    assert errors == []
    assert {item.refs[0].entity_id for item in results} == {
        results[0].refs[0].entity_id
    }
    assert _count(repository, "TestProtocol") == 1


# --- a parent protocol must be visible to this user -------------------------


def _second_user_setup(repository):
    """A same-game installation/context owned by another user."""
    other = uid(940)
    installation_id = uid(941)
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
            game_id=GAME_ID,
            origin="user_provided",
            platform=unknown_platform(),
            private_path_ref=None,
        ))
    context_id = store_raw(repository, "GameContext", {
        "id": uid(942), "installation_id": installation_id, "game_id": GAME_ID,
    })
    return other, installation_id, context_id


def test_another_users_protocol_cannot_be_a_parent(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)
    mine = service.create_test_protocol(_protocol(context_id), context_key("mine"))
    other, other_installation, other_context = _second_user_setup(repository)
    other_key = context_key("other-parent").model_copy(update={"user_scope_id": other})

    with pytest.raises(ContractError) as caught:
        service.create_test_protocol(
            _protocol(other_context, parent_protocol_id=mine.refs[0].entity_id),
            other_key,
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _count(repository, "TestProtocol") == 1
    series_id = repository.get_object(
        "TestProtocol", mine.refs[0].entity_id
    ).series_id
    with repository.unit_of_work() as uow:
        latest = uow.latest_for_series("TestProtocol", series_id)
    assert latest.version == 1
