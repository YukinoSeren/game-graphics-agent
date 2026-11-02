"""DEV-T03: adopting a configuration, merging, replacing and restoring."""

from __future__ import annotations

import threading

import pytest
from pydantic import ValidationError

from game_app.adoption import AdoptionService
from game_app.contracts.enums import (
    ActivationState,
    AppliedOrigin,
    CheckStatus,
    ConfigurationRegistrationKind,
    ValueState,
)
from game_app.contracts.configuration import AdoptConfigurationInput
from game_app.contracts.enums import LegacyRecordState
from game_app.errors import ContractError, ErrorCode
from game_app.files import PublishHooks
from game_app.repository import canonical_json
from tests.product.support.configuration import (
    Definitions,
    USER,
    adopt_request,
    context_key,
    declaration,
    setup,
    store_raw,
    uid,
)


def _service(repository, files) -> AdoptionService:
    return AdoptionService(repository, files)


def _count(repository, entity_type) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def _adopt(service, request, *, key="key-1", hooks=None):
    return service.adopt_configuration(request, context_key(key), hooks=hooks)


def _baseline(service, ctx, device, definition, value, *, key="baseline-1", activation="unknown"):
    return _adopt(
        service,
        adopt_request(
            ctx.installation_id, ctx.context_id, device,
            declared_settings=[declaration(definition, value=value, unit="u",
                                           activation=activation)],
        ),
        key=key,
    )


class _Ctx:
    def __init__(self, installation_id, context_id):
        self.installation_id = installation_id
        self.context_id = context_id


def test_baseline_advances_the_pointer_and_records_the_declaration(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _series = definitions.new("设置甲")
    service = _service(repository, files)

    result = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            declared_settings=[declaration(definition, value=3, unit="tier")],
        ),
    )

    assert [ref.entity_type for ref in result.refs] == [
        "PlayerDeclaration", "AppliedConfiguration", "ManagedFile",
    ]
    applied_id = result.refs[1].entity_id
    applied = service.get_applied(applied_id)
    assert applied.origin is AppliedOrigin.baseline
    assert applied.parent_applied_id is None
    assert applied.version == 1
    assert applied.registration_kind is ConfigurationRegistrationKind.current_adoption
    assert applied.activation_state is ActivationState.unknown
    assert applied.settings[0].value.value_state is ValueState.known
    assert applied.settings[0].value.value == 3
    assert applied.settings[0].unit == "tier"
    assert applied.settings[0].confirmation_method.value == "user_provided"
    assert service.get_current_applied(USER, installation_id) == (applied_id, 1)
    with repository.unit_of_work() as uow:
        events = uow.query("SELECT * FROM current_applied_events")
    assert len(events) == 1
    assert events[0]["previous_applied_id"] is None
    assert events[0]["next_applied_id"] == applied_id


def test_second_baseline_links_to_the_current_version(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    first_def, _ = definitions.new("甲")
    second_def, _ = definitions.new("乙")
    service = _service(repository, files)
    ctx = _Ctx(installation_id, context_id)

    first = _baseline(service, ctx, device_id, first_def, 1)
    first_id = first.refs[1].entity_id
    second = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            expected_current_applied_id=first_id,
            base_applied_id=first_id,
            declared_settings=[declaration(second_def, value=2)],
        ),
        key="baseline-2",
    )

    applied = service.get_applied(second.refs[1].entity_id)
    assert applied.parent_applied_id == first_id
    assert applied.version == 2
    assert applied.series_id == service.get_applied(first_id).series_id


def test_stale_expected_current_conflicts_with_zero_partial_write(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)
    ctx = _Ctx(installation_id, context_id)
    first = _baseline(service, ctx, device_id, definition, 1)
    before = (_count(repository, "AppliedConfiguration"), _count(repository, "PlayerDeclaration"))

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                expected_current_applied_id=uid(555),
                declared_settings=[declaration(definition, value=2)],
            ),
            key="stale",
        )

    assert caught.value.code is ErrorCode.CURRENT_CONFIGURATION_CONFLICT
    assert (_count(repository, "AppliedConfiguration"), _count(repository, "PlayerDeclaration")) == before
    assert service.get_current_applied(USER, installation_id) == (first.refs[1].entity_id, 1)
    assert repository.find_idempotency(
        USER, "POST", "/api/applied-configurations", "stale"
    ) is None


def test_two_clients_with_the_same_expected_current_only_one_advances(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    first_def, _ = definitions.new("甲")
    second_def, _ = definitions.new("乙")
    service = _service(repository, files)
    ctx = _Ctx(installation_id, context_id)
    base = _baseline(service, ctx, device_id, first_def, 1)
    base_id = base.refs[1].entity_id

    results: list[str] = []
    errors: list[ContractError] = []
    barrier = threading.Barrier(2)

    def submit(key: str):
        try:
            barrier.wait(timeout=5)
            result = _adopt(
                service,
                adopt_request(
                    installation_id, context_id, device_id,
                    expected_current_applied_id=base_id,
                    base_applied_id=base_id,
                    declared_settings=[declaration(second_def, value=2)],
                ),
                key=key,
            )
            results.append(result.refs[1].entity_id)
        except ContractError as error:
            errors.append(error)

    threads = [
        threading.Thread(target=submit, args=("race-1",)),
        threading.Thread(target=submit, args=("race-2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0].code is ErrorCode.CURRENT_CONFIGURATION_CONFLICT
    assert service.get_current_applied(USER, installation_id)[0] == results[0]
    assert _count(repository, "PlayerDeclaration") == 2  # base + the winner only


def test_rollback_creates_a_new_version_that_restores_the_target(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    first_def, _ = definitions.new("甲")
    second_def, _ = definitions.new("乙")
    service = _service(repository, files)
    ctx = _Ctx(installation_id, context_id)
    a = _baseline(service, ctx, device_id, first_def, 1)
    a_id = a.refs[1].entity_id
    b = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            expected_current_applied_id=a_id, base_applied_id=a_id,
            declared_settings=[declaration(second_def, value=2)],
        ),
        key="to-b",
    )
    b_id = b.refs[1].entity_id
    a_before = service.get_applied(a_id)

    result = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            origin="rollback",
            expected_current_applied_id=b_id,
            restored_from_applied_id=a_id,
        ),
        key="rollback",
    )

    restored = service.get_applied(result.refs[1].entity_id)
    assert restored.id != a_id
    assert restored.parent_applied_id == b_id
    assert restored.restored_from_applied_id == a_id
    assert restored.origin is AppliedOrigin.rollback
    assert restored.settings == a_before.settings
    assert service.get_current_applied(USER, installation_id)[0] == restored.id
    # The restored target keeps its own immutable identity.
    assert service.get_applied(a_id) == a_before


def test_change_set_merges_from_the_frozen_base_and_keeps_unknown_entries(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    changed, _ = definitions.new("被改")
    untouched, _ = definitions.new("未知项")
    service = _service(repository, files)
    ctx = _Ctx(installation_id, context_id)
    base = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            declared_settings=[
                declaration(changed, value=1),
                declaration(untouched),
            ],
        ),
    )
    base_id = base.refs[1].entity_id
    change_set_id = store_raw(repository, "ChangeSet", {
        "id": uid(301),
        "base_applied_id": base_id,
        "installation_id": installation_id,
        "changes": [{
            "setting_ref": {"kind": "local", "local_setting_id": changed},
            "new_value": 5,
        }],
    })

    result = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            origin="change_set",
            expected_current_applied_id=base_id,
            base_applied_id=base_id,
            change_set_id=change_set_id,
            declared_settings=[declaration(changed, value=5)],
        ),
        key="change-set",
    )

    applied = service.get_applied(result.refs[1].entity_id)
    values = {setting.setting_ref.local_setting_id: setting for setting in applied.settings}
    assert values[changed].value.value == 5
    assert values[untouched].value.value_state is ValueState.unknown
    assert applied.parent_applied_id == base_id
    assert applied.change_set_id == change_set_id


def test_change_set_that_does_not_reflect_its_change_is_refused(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    changed, _ = definitions.new("被改")
    service = _service(repository, files)
    ctx = _Ctx(installation_id, context_id)
    base = _baseline(service, ctx, device_id, changed, 1)
    base_id = base.refs[1].entity_id
    change_set_id = store_raw(repository, "ChangeSet", {
        "id": uid(302),
        "base_applied_id": base_id,
        "changes": [{
            "setting_ref": {"kind": "local", "local_setting_id": changed},
            "new_value": 9,
        }],
    })

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                origin="change_set",
                expected_current_applied_id=base_id,
                base_applied_id=base_id,
                change_set_id=change_set_id,
                declared_settings=[],
            ),
            key="bad-merge",
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "AppliedConfiguration") == 1


def test_manual_edit_copies_the_current_configuration(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    edited, _ = definitions.new("甲")
    kept, _ = definitions.new("乙")
    service = _service(repository, files)
    ctx = _Ctx(installation_id, context_id)
    base = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            declared_settings=[declaration(edited, value=1), declaration(kept, value=2)],
        ),
    )
    base_id = base.refs[1].entity_id

    result = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            origin="manual_edit",
            expected_current_applied_id=base_id,
            base_applied_id=base_id,
            declared_settings=[declaration(edited, value=7)],
        ),
        key="manual",
    )

    applied = service.get_applied(result.refs[1].entity_id)
    values = {setting.setting_ref.local_setting_id: setting for setting in applied.settings}
    assert values[edited].value.value == 7
    assert values[kept].value.value == 2
    assert applied.parent_applied_id == base_id


def test_manual_edit_requires_a_current_configuration(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                origin="manual_edit",
                declared_settings=[declaration(definition, value=1)],
            ),
        )

    assert caught.value.code is ErrorCode.BASELINE_REQUIRED
    assert _count(repository, "AppliedConfiguration") == 0


def test_replacement_old_value_must_match_the_scalar_type_too(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    old_id, series_id = definitions.new("设置甲")
    service = _service(repository, files)
    base = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            declared_settings=[declaration(old_id, value=1)],
        ),
    )
    base_id = base.refs[1].entity_id
    new_id = definitions.correct(old_id, series_id, "设置甲（新）")

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                origin="manual_edit",
                expected_current_applied_id=base_id,
                base_applied_id=base_id,
                declared_settings=[declaration(new_id, value=4)],
                setting_ref_replacements=[{
                    "old_local_setting_id": old_id,
                    "new_local_setting_id": new_id,
                    # True == 1 in Python; the contract keeps the types apart.
                    "expected_old_value": True,
                    "expected_old_unit": None,
                }],
            ),
            key="bool-old",
        )

    assert caught.value.code is ErrorCode.LOCAL_SETTING_CONFLICT


def test_a_declared_definition_removed_before_the_commit_is_refused(tmp_path):
    """The writing transaction re-checks what it is about to reference."""
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)

    def remove_definition(_path):
        with repository.unit_of_work() as uow:
            uow.execute("DELETE FROM objects WHERE id=?", (definition,))

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                declared_settings=[declaration(definition, value=1)],
            ),
            key="vanished",
            hooks=PublishHooks(after_rename=remove_definition),
        )

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _count(repository, "AppliedConfiguration") == 0


# --- explicit reference replacement ------------------------------------------


def _replacement_setup(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    old_id, series_id = definitions.new("设置甲")
    other_id, _ = definitions.new("其他")
    service = _service(repository, files)
    ctx = _Ctx(installation_id, context_id)
    base = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            declared_settings=[
                declaration(old_id, value=3, unit="tier"),
                declaration(other_id, value=1),
            ],
        ),
    )
    base_id = base.refs[1].entity_id
    new_id = definitions.correct(old_id, series_id, "设置甲（新）")
    return (
        repository, files, installation_id, context_id, device_id,
        service, base_id, old_id, new_id, other_id,
    )


def test_replacement_swaps_only_the_mapped_definition(tmp_path):
    (
        repository, files, installation_id, context_id, device_id,
        service, base_id, old_id, new_id, other_id,
    ) = _replacement_setup(tmp_path)

    result = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            origin="manual_edit",
            expected_current_applied_id=base_id,
            base_applied_id=base_id,
            declared_settings=[declaration(new_id, value=4, unit="tier")],
            setting_ref_replacements=[{
                "old_local_setting_id": old_id,
                "new_local_setting_id": new_id,
                "expected_old_value": 3,
                "expected_old_unit": "tier",
            }],
        ),
        key="replace",
    )

    applied = service.get_applied(result.refs[1].entity_id)
    values = {setting.setting_ref.local_setting_id: setting for setting in applied.settings}
    assert new_id in values
    assert old_id not in values
    assert values[new_id].value.value == 4
    assert values[new_id].unit == "tier"
    assert values[other_id].value.value == 1  # untouched entries survive
    assert applied.parent_applied_id == base_id


def test_replacements_require_exact_old_values_and_units(tmp_path):
    (
        repository, files, installation_id, context_id, device_id,
        service, base_id, old_id, new_id, _other,
    ) = _replacement_setup(tmp_path)

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                origin="manual_edit",
                expected_current_applied_id=base_id,
                base_applied_id=base_id,
                declared_settings=[declaration(new_id, value=4, unit="tier")],
                setting_ref_replacements=[{
                    "old_local_setting_id": old_id,
                    "new_local_setting_id": new_id,
                    "expected_old_value": 99,
                    "expected_old_unit": "tier",
                }],
            ),
            key="bad-old",
        )
    assert caught.value.code is ErrorCode.LOCAL_SETTING_CONFLICT

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                origin="manual_edit",
                expected_current_applied_id=base_id,
                base_applied_id=base_id,
                declared_settings=[declaration(new_id, value=4, unit="tier")],
                setting_ref_replacements=[{
                    "old_local_setting_id": old_id,
                    "new_local_setting_id": new_id,
                    "expected_old_value": 3,
                    "expected_old_unit": "tier",
                }, {
                    "old_local_setting_id": new_id,
                    "new_local_setting_id": old_id,
                    "expected_old_value": 4,
                    "expected_old_unit": "tier",
                }],
            ),
            key="chain",
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED


def test_two_definitions_from_one_series_are_refused_for_any_origin(tmp_path):
    """One configuration can hold only one executable value per local series."""
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    old_id, series_id = definitions.new("设置甲")
    new_id = definitions.correct(old_id, series_id, "设置甲（新）")
    service = _service(repository, files)

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                declared_settings=[
                    declaration(old_id, value=1),
                    declaration(new_id, value=2),
                ],
            ),
        )

    assert caught.value.code is ErrorCode.LOCAL_SETTING_CONFLICT
    assert _count(repository, "AppliedConfiguration") == 0
    assert service.get_current_applied(USER, installation_id) == (None, None)


def _store_imported_record(
    repository, *, record_type: str, local_id: str = "set-0001", serial: int = 880
):
    import hashlib

    from game_app.contracts.knowledge import ImportedRecord, RecordExtension
    from game_app.contracts.primitives import utc_now

    payload = {"id": local_id, "raw_name": "Synthetic setting"}
    payload_hash = hashlib.sha256(canonical_json(payload)).hexdigest()
    record = ImportedRecord(
        id=uid(serial),
        schema_version="product-contract/0.4",
        created_at=utc_now(),
        created_by_task_id=None,
        record_snapshot_id=uid(serial),
        import_snapshot_id=uid(serial + 1),
        capture_scope_id="src-0001",
        local_record_id=local_id,
        legacy_record_id=f"src-0001:{local_id}",
        source_revision_id=uid(serial + 2),
        record_type=record_type,
        payload=payload,
        payload_hash=payload_hash,
        upstream_status=LegacyRecordState.accepted,
        upstream_envelope={},
        checks=[],
        extension=RecordExtension(
            boundary_semantics=None, setting_identity_ref=None, checks=[]
        ),
    )
    with repository.unit_of_work() as uow:
        uow.insert_object("ImportedRecord", record)
        uow.claim_imported_record_key(
            record_snapshot_id=record.id,
            import_snapshot_id=record.import_snapshot_id,
            capture_scope_id=record.capture_scope_id,
            record_type=record.record_type,
            local_record_id=record.local_record_id,
            usable=True,
        )
    return record


def _knowledge_ref(record, **overrides):
    ref = {
        "package_id": uid(883),
        "record_snapshot_id": record.id,
        "source_revision_id": record.source_revision_id,
        "capture_scope_id": record.capture_scope_id,
        "local_record_id": record.local_record_id,
        "record_type": "settings",
        "payload_hash": record.payload_hash,
    }
    ref.update(overrides)
    return {"kind": "knowledge", "record": ref, "local_setting_id": None}


def test_a_knowledge_setting_ref_must_match_a_setting_record_exactly(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)
    configuration_record = _store_imported_record(
        repository, record_type="configurations"
    )
    setting_declaration = {
        "setting_ref": _knowledge_ref(configuration_record),
        "value": 1,
        "unknown_reason": None,
        "unit": None,
        "claimed_activation": "unknown",
        "activation_note": None,
        "observation_id": None,
    }

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                declared_settings=[setting_declaration],
            ),
            key="wrong-type",
        )
    assert caught.value.code is ErrorCode.REFERENCE_INVALID

    setting_record = _store_imported_record(
        repository, record_type="settings", local_id="set-0002", serial=890
    )
    for overrides in (
        {"local_record_id": "other-id"},
        {"payload_hash": "0" * 64},
        {"capture_scope_id": "another-scope"},
    ):
        bad = dict(setting_declaration)
        bad["setting_ref"] = _knowledge_ref(setting_record, **overrides)
        with pytest.raises(ContractError) as caught:
            _adopt(
                service,
                adopt_request(
                    installation_id, context_id, device_id,
                    declared_settings=[bad],
                ),
                key=f"bad-ref-{next(iter(overrides))}",
            )
        assert caught.value.code is ErrorCode.REFERENCE_INVALID


def test_a_matching_knowledge_setting_ref_is_accepted(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)
    record = _store_imported_record(repository, record_type="settings")

    result = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            declared_settings=[{
                "setting_ref": _knowledge_ref(record),
                "value": 1,
                "unknown_reason": None,
                "unit": None,
                "claimed_activation": "unknown",
                "activation_note": None,
                "observation_id": None,
            }],
        ),
        key="knowledge-ok",
    )

    applied = service.get_applied(result.refs[1].entity_id)
    assert applied.settings[0].setting_ref.record.record_snapshot_id == record.id


def test_same_series_change_without_a_mapping_is_refused(tmp_path):
    (
        repository, files, installation_id, context_id, device_id,
        service, base_id, old_id, new_id, _other,
    ) = _replacement_setup(tmp_path)

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                origin="manual_edit",
                expected_current_applied_id=base_id,
                base_applied_id=base_id,
                declared_settings=[declaration(new_id, value=4, unit="tier")],
            ),
            key="no-map",
        )

    assert caught.value.code is ErrorCode.LOCAL_SETTING_CONFLICT
    assert _count(repository, "AppliedConfiguration") == 1


def test_replacement_against_another_installation_is_a_reference_error(tmp_path):
    (
        repository, files, installation_id, context_id, device_id,
        service, base_id, old_id, new_id, _other,
    ) = _replacement_setup(tmp_path)
    foreign_id = uid(900)
    with repository.unit_of_work() as uow:
        uow.register_local_setting(
            installation_id=uid(901),
            namespace="player",
            raw_name="其他安装",
            menu_path=["图形", "其他安装"],
            local_setting_id=foreign_id,
            series_id=uid(902),
        )

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                origin="manual_edit",
                expected_current_applied_id=base_id,
                base_applied_id=base_id,
                declared_settings=[declaration(foreign_id, value=1)],
                setting_ref_replacements=[{
                    "old_local_setting_id": old_id,
                    "new_local_setting_id": foreign_id,
                    "expected_old_value": 3,
                    "expected_old_unit": "tier",
                }],
            ),
            key="foreign",
        )

    assert caught.value.code is ErrorCode.REFERENCE_INVALID


def test_non_manual_origin_cannot_carry_replacements():
    with pytest.raises(ValidationError):
        AdoptConfigurationInput.model_validate_json(canonical_json({
            "kind": "adopt_configuration",
            "origin": "baseline",
            "installation_id": uid(1),
            "device_snapshot_id": uid(2),
            "game_context_id": uid(3),
            "expected_current_applied_id": None,
            "recommendation_id": None,
            "base_applied_id": None,
            "change_set_id": None,
            "restored_from_applied_id": None,
            "declared_settings": [],
            "preset_reference": "预设 P",
            "claimed_activation": "unknown",
            "setting_ref_replacements": [{
                "old_local_setting_id": uid(4),
                "new_local_setting_id": uid(5),
                "expected_old_value": 1,
                "expected_old_unit": None,
            }],
        }))


# --- guards and idempotency --------------------------------------------------


def test_origin_guards_are_enforced(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)

    cases = [
        {"origin": "baseline", "recommendation_id": uid(1)},
        {"origin": "recommendation"},
        {"origin": "change_set"},
        {"origin": "rollback"},
    ]
    for index, overrides in enumerate(cases):
        with pytest.raises(ContractError) as caught:
            _adopt(
                service,
                adopt_request(
                    installation_id, context_id, device_id,
                    declared_settings=[declaration(definition, value=1)],
                    **overrides,
                ),
                key=f"guard-{index}",
            )
        assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "AppliedConfiguration") == 0


def test_an_adoption_with_nothing_confirmed_is_refused(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                declared_settings=[declaration(definition)],
            ),
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "AppliedConfiguration") == 0
    assert service.get_current_applied(USER, installation_id) == (None, None)


def test_a_preset_only_adoption_is_legal(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    service = _service(repository, files)

    result = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            preset_reference="玩家声明的预设 P（未展开）",
            claimed_activation="unknown",
        ),
    )

    applied = service.get_applied(result.refs[1].entity_id)
    assert applied.settings == []
    assert applied.preset_reference == "玩家声明的预设 P（未展开）"


def test_context_from_another_installation_is_refused(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)
    foreign_context = store_raw(repository, "GameContext", {
        "id": uid(950), "installation_id": uid(951), "game_id": uid(952),
    })

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, foreign_context, device_id,
                declared_settings=[declaration(definition, value=1)],
            ),
        )

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _count(repository, "AppliedConfiguration") == 0


def test_same_key_replays_the_original_response(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)
    request = adopt_request(
        installation_id, context_id, device_id,
        declared_settings=[declaration(definition, value=1)],
    )

    first = _adopt(service, request, key="same")
    second = _adopt(service, request, key="same")

    assert first.refs[1].entity_id == second.refs[1].entity_id
    assert _count(repository, "AppliedConfiguration") == 1


def test_same_key_with_different_content_conflicts(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)
    _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            declared_settings=[declaration(definition, value=1)],
        ),
        key="same",
    )

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, device_id,
                declared_settings=[declaration(definition, value=2)],
            ),
            key="same",
        )

    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_lost_response_after_commit_replays_the_same_ids(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)
    request = adopt_request(
        installation_id, context_id, device_id,
        declared_settings=[declaration(definition, value=1)],
    )

    def lose():
        raise RuntimeError("response lost after commit")

    with pytest.raises(RuntimeError):
        _adopt(service, request, key="lost", hooks=PublishHooks(after_commit=lose))

    replayed = _adopt(service, request, key="lost")

    assert _count(repository, "AppliedConfiguration") == 1
    assert replayed.refs[1].entity_id == service.get_current_applied(
        USER, installation_id
    )[0]


# --- 2D-B: snapshot ownership and unverified activation ---------------------


def test_a_foreign_device_snapshot_cannot_be_adopted(tmp_path):
    repository, files, installation_id, context_id, _device_id = setup(tmp_path)
    foreign_id = store_raw(
        repository, "DeviceSnapshot",
        {"id": uid(104), "user_scope_id": "b" * 32},
    )
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)

    with pytest.raises(ContractError) as caught:
        _adopt(
            service,
            adopt_request(
                installation_id, context_id, foreign_id,
                declared_settings=[declaration(definition, value=1)],
            ),
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _count(repository, "AppliedConfiguration") == 0
    assert _count(repository, "ManagedFile") == 0


def test_a_claimed_active_confirmation_stays_unverified_at_the_aggregate(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("甲")
    service = _service(repository, files)

    result = _adopt(
        service,
        adopt_request(
            installation_id, context_id, device_id,
            declared_settings=[declaration(
                definition, value=3, activation="active_confirmed",
            )],
        ),
    )

    applied = service.get_applied(result.refs[1].entity_id)
    # The single setting keeps the player's declaration; the aggregate does not
    # turn an unverified claim into confirmed activation.
    assert applied.settings[0].activation_state is ActivationState.active_confirmed
    assert applied.activation_state is ActivationState.unknown
    check = next(item for item in applied.checks if item.check_id == "applied.activation")
    assert check.status is CheckStatus.unknown


def test_get_current_applied_without_a_pointer_returns_two_nones(tmp_path):
    repository, files, installation_id, _context_id, _device_id = setup(tmp_path)
    service = _service(repository, files)

    applied_id, revision = service.get_current_applied(USER, installation_id)

    assert applied_id is None
    assert revision is None
