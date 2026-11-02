"""DEV-T03: historical configuration registration never moves the current pointer."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from game_app.adoption import AdoptionService
from game_app.contracts.configuration import HistoricalConfigurationInput
from game_app.contracts.enums import (
    AppliedOrigin,
    CheckStatus,
    ConfigurationRegistrationKind,
)
from game_app.errors import ContractError, ErrorCode
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


def _evidence(files):
    data = b"player note about the old configuration"
    file_id = files.store_artifact(
        data, relative_name="history/note.txt", stable_key="history:note"
    )
    return {
        "kind": "artifact",
        "file_id": file_id,
        "content_hash": hashlib.sha256(data).hexdigest(),
        "locator": "history/note.txt",
    }


def _historical(installation_id, context_id, device_id, evidence, **overrides):
    payload = {
        "kind": "historical_configuration",
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "segment_ref": {
            "segment_id": uid(800),
            "segment_revision": 1,
            "binding_revision": 1,
        },
        "effective_range": {
            "start": 0.0, "end": 600.0, "time_basis": "session", "unit": "s",
        },
        "adopted_at": "2026-10-01T12:00:00+00:00",
        "declared_settings": [],
        "preset_reference": None,
        "claimed_activation": "unknown",
        "evidence_refs": [evidence],
    }
    payload.update(overrides)
    return HistoricalConfigurationInput.model_validate_json(
        canonical_json(payload)
    )


def _count(repository, entity_type) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def test_historical_record_is_an_independent_family_and_keeps_the_pointer(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    current_def, _ = definitions.new("当前")
    historical_def, _ = definitions.new("过去")
    service = AdoptionService(repository, files)
    current = service.adopt_configuration(
        adopt_request(
            installation_id, context_id, device_id,
            declared_settings=[declaration(current_def, value=1)],
        ),
        context_key("current"),
    )
    current_id = current.refs[1].entity_id
    current_applied = service.get_applied(current_id)
    pointer_before = service.get_current_applied(USER, installation_id)
    with repository.unit_of_work() as uow:
        events_before = len(uow.query("SELECT 1 FROM current_applied_events"))

    result = service.record_historical_configuration(
        _historical(
            installation_id, context_id, device_id, _evidence(files),
            declared_settings=[declaration(historical_def, value=2)],
        ),
        context_key("historical"),
    )

    historical = service.get_applied(result.refs[1].entity_id)
    assert historical.registration_kind is ConfigurationRegistrationKind.historical_record
    assert historical.origin is AppliedOrigin.baseline
    assert historical.version == 1
    assert historical.series_id != current_applied.series_id
    assert historical.recommendation_id is None
    assert historical.parent_applied_id is None
    assert historical.change_set_id is None
    assert historical.restored_from_applied_id is None
    assert historical.adopted_at == "2026-10-01T12:00:00+00:00"
    assert historical.created_at >= "2026-11-01"
    assert service.get_current_applied(USER, installation_id) == pointer_before
    with repository.unit_of_work() as uow:
        assert len(uow.query("SELECT 1 FROM current_applied_events")) == events_before
    assert service.get_applied(current_id) == current_applied


def test_historical_adopted_at_stays_null_when_there_is_no_basis(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("过去")
    service = AdoptionService(repository, files)

    result = service.record_historical_configuration(
        _historical(
            installation_id, context_id, device_id, _evidence(files),
            adopted_at=None,
            declared_settings=[declaration(definition, value=2)],
        ),
        context_key("historical-null"),
    )

    applied = service.get_applied(result.refs[1].entity_id)
    assert applied.adopted_at is None
    assert applied.created_at is not None


def test_historical_record_needs_evidence_for_its_period(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("过去")
    service = AdoptionService(repository, files)

    with pytest.raises(ValidationError):
        _historical(
            installation_id, context_id, device_id, _evidence(files),
            evidence_refs=[],
            declared_settings=[declaration(definition, value=2)],
        )

    missing = _evidence(files)
    missing["file_id"] = uid(999)
    with pytest.raises(ContractError) as caught:
        service.record_historical_configuration(
            _historical(
                installation_id, context_id, device_id, missing,
                declared_settings=[declaration(definition, value=2)],
            ),
            context_key("missing-evidence"),
        )

    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert _count(repository, "AppliedConfiguration") == 0
    assert _count(repository, "PlayerDeclaration") == 0


def test_missing_segment_storage_is_recorded_as_not_run(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("过去")
    service = AdoptionService(repository, files)

    result = service.record_historical_configuration(
        _historical(
            installation_id, context_id, device_id, _evidence(files),
            declared_settings=[declaration(definition, value=2)],
        ),
        context_key("no-segment"),
    )

    applied = service.get_applied(result.refs[1].entity_id)
    segment = next(check for check in applied.checks if check.check_id == "applied.segment")
    assert segment.status is CheckStatus.not_run
    assert "PERF-T02" in (segment.note or "")
    assert segment.checked_at is None


def test_an_active_segment_cannot_back_a_historical_record(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("过去")
    service = AdoptionService(repository, files)
    store_raw(repository, "SessionSegment", {
        "id": uid(800), "version": 1, "lifecycle": "active",
    })

    with pytest.raises(ContractError) as caught:
        service.record_historical_configuration(
            _historical(
                installation_id, context_id, device_id, _evidence(files),
                declared_settings=[declaration(definition, value=2)],
            ),
            context_key("active-segment"),
        )

    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert _count(repository, "AppliedConfiguration") == 0


def test_two_historical_registrations_are_separate_families(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    first_def, _ = definitions.new("过去甲")
    second_def, _ = definitions.new("过去乙")
    service = AdoptionService(repository, files)

    first = service.record_historical_configuration(
        _historical(
            installation_id, context_id, device_id, _evidence(files),
            declared_settings=[declaration(first_def, value=1)],
        ),
        context_key("h-1"),
    )
    second = service.record_historical_configuration(
        _historical(
            installation_id, context_id, device_id, _evidence(files),
            declared_settings=[declaration(second_def, value=2)],
        ),
        context_key("h-2"),
    )

    left = service.get_applied(first.refs[1].entity_id)
    right = service.get_applied(second.refs[1].entity_id)
    assert left.series_id != right.series_id
    assert left.version == right.version == 1
    assert service.get_current_applied(USER, installation_id) == (None, None)


def test_artifact_evidence_hash_must_match_the_managed_file(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("过去")
    service = AdoptionService(repository, files)
    evidence = _evidence(files)
    evidence["content_hash"] = "0" * 64

    with pytest.raises(ContractError) as caught:
        service.record_historical_configuration(
            _historical(
                installation_id, context_id, device_id, evidence,
                declared_settings=[declaration(definition, value=2)],
            ),
            context_key("bad-hash"),
        )

    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert _count(repository, "AppliedConfiguration") == 0


def test_period_coverage_is_not_claimed_from_mere_existence(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("过去")
    service = AdoptionService(repository, files)

    result = service.record_historical_configuration(
        _historical(
            installation_id, context_id, device_id, _evidence(files),
            declared_settings=[declaration(definition, value=2)],
        ),
        context_key("period"),
    )

    applied = service.get_applied(result.refs[1].entity_id)
    coverage = next(
        check for check in applied.checks
        if check.check_id == "applied.period_coverage"
    )
    # Nothing proved the declaration covers the stated period, so it is not a pass.
    assert coverage.status is CheckStatus.unknown


def test_contract_shaped_segment_revision_is_accepted(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("过去")
    service = AdoptionService(repository, files)
    store_raw(repository, "SessionSegment", {
        "id": uid(800), "revision": 1, "lifecycle": "ended",
    })

    result = service.record_historical_configuration(
        _historical(
            installation_id, context_id, device_id, _evidence(files),
            declared_settings=[declaration(definition, value=2)],
        ),
        context_key("segment"),
    )

    applied = service.get_applied(result.refs[1].entity_id)
    segment = next(check for check in applied.checks if check.check_id == "applied.segment")
    assert segment.status is CheckStatus.pass_


def test_historical_registration_replays_the_same_ids(tmp_path):
    repository, files, installation_id, context_id, device_id = setup(tmp_path)
    definitions = Definitions(repository, installation_id)
    definition, _ = definitions.new("过去")
    service = AdoptionService(repository, files)
    request = _historical(
        installation_id, context_id, device_id, _evidence(files),
        declared_settings=[declaration(definition, value=2)],
    )

    first = service.record_historical_configuration(request, context_key("same"))
    second = service.record_historical_configuration(request, context_key("same"))

    assert first.refs[1].entity_id == second.refs[1].entity_id
    assert _count(repository, "AppliedConfiguration") == 1
