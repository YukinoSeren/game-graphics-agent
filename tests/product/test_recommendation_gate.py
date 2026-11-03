"""REC-T01: the input gate, deterministic candidate pool and declarative rules.

Every fixture is real: a verified game, a real context, a real device snapshot
and a package published through the real importer.  No provider, network, image
or game is involved; ``external_calls`` stays at zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from game_app.adoption import AdoptionService
from game_app.contracts.enums import CheckStatus
from game_app.contracts.recommendation import CandidateAssessment
from game_app.contracts.tasks import RecommendationInput
from game_app.errors import ContractError, ErrorCode
from game_app.knowledge import KnowledgeService
from game_app.performance import PerformanceService
from game_app.recommendation import (
    CandidateBuilder,
    ConstraintEvaluator,
    InputGate,
    assess_candidates,
    assessment_manifest,
    resolve_setting_identities,
    validate_actions,
)
from tests.product.support.recommendation import (
    OTHER,
    SCOPE,
    build_package,
    count_objects,
    dataset,
    external_calls,
    record,
    setup,
    source,
    store_context,
    store_device,
    store_game,
    store_raw,
    stub_package,
    uid,
)
from tests.product.support.spies import SpyProvider


# --- helpers ----------------------------------------------------------------


def _world(
    tmp_path: Path,
    *,
    records=None,
    configs: int = 1,
    gpu: str = "Synthetic GPU",
    form: str = "desktop",
    resolution: tuple[int, int] | None = (1920, 1080),
    platform: str = "steam",
    version: str | None = "1.0",
    owner: str = SCOPE,
    context_uid: int = 300,
    device_uid: int = 400,
    installation_uid: int = 301,
    package_key: str = "pkg-1",
    run_id: str = "run-1",
    sources=None,
):
    repository, files = setup(tmp_path)
    game = store_game(repository)
    context = store_context(
        repository, files, game, uid_value=context_uid, owner=owner,
        installation=installation_uid, platform=platform, version=version,
    )
    device = store_device(
        repository, files, uid_value=device_uid, owner=owner, gpu=gpu,
        form=form, resolution=resolution,
    )
    package = build_package(
        repository, files, game, context, key=package_key, run_id=run_id,
        records=records if records is not None else dataset(configs=configs),
        user_scope_id=owner, sources=sources,
    )
    return repository, files, game, context, device, package


def _input(context, device, package, **overrides) -> RecommendationInput:
    payload = {
        "kind": "recommendation",
        "game_context_id": context.id,
        "device_snapshot_id": device.id,
        "package_id": package.id,
        "preference_ids": [],
        "applied_id": None,
        "history_summary_ids": [],
    }
    payload.update(overrides)
    return RecommendationInput.model_validate(payload)


def _assess(repository, files, task_input, *, user: str = SCOPE, **kwargs):
    return assess_candidates(
        task_input,
        repository=repository,
        knowledge=KnowledgeService(repository, files),
        user_scope_id=user,
        **kwargs,
    )


def _gate(repository, files, task_input, *, user: str = SCOPE):
    return InputGate(
        repository,
        KnowledgeService(repository, files),
        AdoptionService(repository, files),
        PerformanceService(repository, files),
    ).check(task_input, user_scope_id=user)


def _checks(assessment: CandidateAssessment) -> dict:
    return {item.check_id: item for item in assessment.condition_checks}


def _stored_summary(repository, *, summary_id: str, owner: str) -> str:
    """A well-shaped summary whose artifact is absent.

    Ownership of a real summary is enforced by ``PerformanceService.get_summary``
    (covered by the performance tests); this asserts the gate delegates to it and
    never reads a summary directly.  The artifact is missing, so ``get_summary``
    refuses with ``RESOURCE_NOT_FOUND`` before ownership is even reached.
    """
    from tests.product.support.recommendation import NOW

    store_raw(repository, "PerformanceSummary", {
        "id": summary_id,
        "schema_version": "product-contract/0.4",
        "created_at": NOW,
        "created_by_task_id": None,
        "artifact_id": uid(888),
        "bindings": [{
            "segment_id": uid(880), "segment_revision": 1, "binding_revision": 1,
        }],
        "analyzed_ranges": [{
            "start": 0.0, "end": 60.0, "time_basis": "capture_clock", "unit": "s",
        }],
        "analyzer_version": "analyzer/1",
        "cleaning_protocol_version": "cleaning/1",
        "manifest_file_id": uid(881),
        "valid_interval_count": 1,
        "effective_duration_s": 60.0,
        "metrics": [],
        "adaptive_long_frames": [],
        "quality": "valid",
        "checks": [],
    })
    return summary_id


# --- 1..5: input gate -------------------------------------------------------


def test_a_package_from_another_game_is_a_reference_invalid(tmp_path):
    repository, files, game, context, device, package = _world(tmp_path)
    other_game = store_game(repository, uid_value=500, series=501, name="Other Game")
    other_context = store_context(
        repository, files, other_game, uid_value=510, installation=511,
    )
    task_input = _input(other_context, device, package)
    with pytest.raises(ContractError) as error:
        _assess(repository, files, task_input)
    assert error.value.code is ErrorCode.REFERENCE_INVALID
    assert count_objects(repository, "RecommendationVersion") == 0


def test_an_own_unpublished_package_is_never_an_input(tmp_path):
    repository, files, _, context, device, package = _world(tmp_path)
    unpublished = stub_package(repository, package, publication_status="staging")
    task_input = _input(context, device, package, package_id=unpublished)
    with pytest.raises(ContractError) as error:
        _assess(repository, files, task_input)
    assert error.value.code is ErrorCode.REFERENCE_INVALID
    assert count_objects(repository, "RecommendationVersion") == 0


def test_another_players_resources_are_indistinguishable_from_missing(tmp_path):
    repository, files, game, context, device, package = _world(tmp_path)
    knowledge = KnowledgeService(repository, files)

    # a package imported and built by another player
    foreign_context = store_context(
        repository, files, game, uid_value=320, owner=OTHER, installation=321,
    )
    foreign_device = store_device(repository, files, uid_value=420, owner=OTHER)
    foreign_package = build_package(
        repository, files, game, foreign_context,
        key="foreign-pkg", run_id="foreign-run", user_scope_id=OTHER,
    )
    for task_input in (
        _input(context, device, foreign_package),
        _input(context, foreign_device, package),
    ):
        with pytest.raises(ContractError) as error:
            _assess(repository, files, task_input)
        assert error.value.code is ErrorCode.RESOURCE_NOT_FOUND

    summary_id = _stored_summary(repository, summary_id=uid(870), owner=OTHER)
    task_input = _input(
        context, device, package, history_summary_ids=[summary_id],
    )
    with pytest.raises(ContractError) as error:
        _assess(repository, files, task_input)
    assert error.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert knowledge.get_package(package.id, user_scope_id=SCOPE).id == package.id


def test_an_applied_configuration_from_another_game_or_device_series_is_invalid(tmp_path):
    repository, files, game, context, device, package = _world(tmp_path)
    from tests.product.support.recommendation import store_applied

    other_game = store_game(repository, uid_value=600, series=601, name="Other Game")
    wrong_game = store_applied(
        repository, uid_value=610, game_id=other_game.series_id,
        installation_id=uid(301), device_snapshot_id=device.id,
        game_context_id=context.id,
    )
    wrong_device = store_device(
        repository, files, uid_value=430, device_series=432,
    )
    other_series = store_applied(
        repository, uid_value=611, game_id=game.series_id,
        installation_id=uid(301), device_snapshot_id=wrong_device.id,
        game_context_id=context.id,
    )
    foreign = store_applied(
        repository, uid_value=612, owner=OTHER, game_id=game.series_id,
        installation_id=uid(321), device_snapshot_id=device.id,
        game_context_id=context.id,
    )
    for applied_id, code in (
        (wrong_game, ErrorCode.REFERENCE_INVALID),
        (other_series, ErrorCode.REFERENCE_INVALID),
        (foreign, ErrorCode.RESOURCE_NOT_FOUND),
    ):
        task_input = _input(context, device, package, applied_id=applied_id)
        with pytest.raises(ContractError) as error:
            _assess(repository, files, task_input)
        assert error.value.code is code

    # the same device series with an older snapshot is allowed, with a limitation
    older = store_device(
        repository, files, uid_value=441, device_series=402,
    )
    assert older.device_series_id == device.device_series_id
    old = store_applied(
        repository, uid_value=613, game_id=game.series_id,
        installation_id=uid(301), device_snapshot_id=older.id,
        game_context_id=context.id,
    )
    task_input = _input(context, device, package, applied_id=old)
    gate = _gate(repository, files, task_input)
    assert any("older device snapshot" in item for item in gate.limitations)


def test_non_empty_preference_ids_are_rejected_and_no_preference_is_written(tmp_path):
    repository, files, _, context, device, package = _world(tmp_path)
    task_input = _input(context, device, package, preference_ids=[uid(700)])
    with pytest.raises(ContractError) as error:
        _assess(repository, files, task_input)
    assert error.value.code is ErrorCode.REFERENCE_INVALID
    assert count_objects(repository, "Preference") == 0


# --- 6..9: candidate pool and match class -----------------------------------


def test_a_same_named_laptop_with_another_gpu_is_not_a_direct_match(tmp_path):
    records = dataset(configs=1, gpu="Desktop GPU", resolution="2560x1440")
    repository, files, _, context, device, package = _world(
        tmp_path, records=records, gpu="Laptop GPU", form="laptop",
        resolution=(1920, 1080),
    )
    assessments = _assess(repository, files, _input(context, device, package))
    assert len(assessments) == 1
    assert assessments[0].match_class != "direct"
    checks = _checks(assessments[0])
    assert checks["candidate.gpu"].status is CheckStatus.fail
    assert any("gpu" in item.lower() for item in assessments[0].limitations)


def test_without_a_sourced_neighbour_relation_the_match_is_general(tmp_path):
    records = dataset(configs=1, gpu="Other GPU")
    repository, files, _, context, device, package = _world(
        tmp_path, records=records, gpu="Synthetic GPU",
    )
    assessments = _assess(repository, files, _input(context, device, package))
    assert assessments[0].match_class == "general"
    assert _checks(assessments[0])["candidate.neighbor_relation"].status is not CheckStatus.pass_


def test_excluded_branches_and_failed_applicability_leave_the_pool(tmp_path):
    records = dataset(configs=3)
    records[-1]["recommendation_status"] = "excluded_branch"
    records[-2]["record_status"] = "accepted"
    records[-2]["applicability"] = "fail"
    repository, files, _, context, device, package = _world(
        tmp_path, records=records,
    )
    assessments = _assess(repository, files, _input(context, device, package))
    assert [item.configuration_ref.local_record_id for item in assessments] == ["cfg-0001"]


def test_needs_followup_and_unclassified_records_are_conditional_with_limitations(tmp_path):
    records = dataset(configs=3)
    records[-1]["recommendation_status"] = "needs_followup"
    records[-2]["recommendation_status"] = None
    repository, files, _, context, device, package = _world(
        tmp_path, records=records,
    )
    assessments = _assess(repository, files, _input(context, device, package))
    assert len(assessments) == 3
    for assessment in assessments:
        assert assessment.match_class != "direct"
        assert assessment.limitations


# --- 10..12: declarative values and constraints ----------------------------


def _action(
    action_id: str,
    kind: str,
    *,
    setting_ref: dict,
    value=None,
    evidence_refs=None,
    group: str | None = None,
) -> dict:
    return {
        "action_id": action_id,
        "setting_ref": setting_ref,
        "action": kind,
        "value": value,
        "current_value": None,
        "menu_path": [],
        "reason": "synthetic",
        "evidence_refs": evidence_refs or [],
        "dependency_group_id": group,
        "restart_required": None,
        "checks": [],
    }


def _package_records(repository, package):
    from game_app.contracts.knowledge import ImportedRecord

    with repository.unit_of_work() as uow:
        return [
            ImportedRecord.model_validate_json(
                uow.get_object("ImportedRecord", item).payload_bytes
            )
            for item in package.record_snapshot_ids
        ]


def _package_record(repository, package, *, local_id=None, record_type=None):
    for item in _package_records(repository, package):
        if local_id is not None and item.local_record_id != local_id:
            continue
        if record_type is not None and item.record_type != record_type:
            continue
        return item
    raise AssertionError("the fixture package has no such record")


def _setting_ref(repository, package, local_id: str = "set-0001") -> dict:
    from game_app.knowledge import _record_ref

    item = _package_record(repository, package, local_id=local_id)
    return {"kind": "knowledge", "record": _record_ref(package.id, item).model_dump(mode="json")}


def test_an_unknown_premise_never_becomes_a_fixed_action(tmp_path):
    repository, files, _, context, device, package = _world(tmp_path)
    setting_ref = _setting_ref(repository, package)
    value = {
        "value_kind": "unknown",
        "value": None,
        "alternatives": [],
        "branches": [],
        "unknown_reason": "not_provided",
        "unit": None,
    }
    actions = [
        _action("a-1", "set_value", setting_ref=setting_ref, value=value),
        _action("a-2", "check_in_game", setting_ref=setting_ref),
    ]
    checks = validate_actions(
        _input(context, device, package), actions,
        repository=repository, knowledge=KnowledgeService(repository, files),
        user_scope_id=SCOPE,
    )
    by_id = {item.check_id: item for item in checks}
    assert by_id["value.shape"].status is CheckStatus.fail
    assert by_id["operation.check_in_game"].status is CheckStatus.pass_


def test_numeric_thresholds_and_complex_versions_are_not_executed(tmp_path):
    constraints = [
        record("cns-0001", "constraints", {
            "id": "cns-0001", "evidence_id": "evi-0001", "kind": "exclusion",
            "when": {"vram_gb": 8}, "setting_id": "set-0001",
            "affected_values": ["Ultra"], "requires": {},
            "verified_scope": True, "scope_note": "synthetic numeric threshold",
        }),
        record("cns-0002", "constraints", {
            "id": "cns-0002", "evidence_id": "evi-0001", "kind": "exclusion",
            "when": {"game_version": ">=1.2"}, "setting_id": "set-0001",
            "affected_values": ["Ultra"], "requires": {},
            "verified_scope": True, "scope_note": "synthetic range",
        }),
        record("cns-0003", "constraints", {
            "id": "cns-0003", "evidence_id": "evi-0001", "kind": "dependency",
            "when": {"platform": "steam"}, "setting_id": "set-0001",
            "affected_values": [], "requires": {"gpu_tier": 2},
            "verified_scope": True, "scope_note": "synthetic dependency",
        }),
    ]
    records = dataset(configs=2) + constraints
    repository, files, _, context, device, package = _world(
        tmp_path, records=records, platform="steam",
    )
    knowledge = KnowledgeService(repository, files)
    gate = _gate(repository, files, _input(context, device, package))
    evaluation = ConstraintEvaluator().evaluate(
        _package_records(repository, package), gate.facts,
    )
    assert evaluation.exclusions == []
    assert [item.status for item in evaluation.checks] == [
        CheckStatus.unknown, CheckStatus.unknown, CheckStatus.unknown,
    ]
    assessments = _assess(repository, files, _input(context, device, package))
    assert len(assessments) == 2


def test_a_verified_equality_predicate_filters_candidates_deterministically(tmp_path):
    constraint = record("cns-0001", "constraints", {
        "id": "cns-0001", "evidence_id": "evi-0001", "kind": "exclusion",
        "when": {"platform": "steam"}, "setting_id": "set-0001",
        "affected_values": ["Ultra"], "requires": {},
        "verified_scope": True, "scope_note": "synthetic exclusion",
    })
    records = dataset(configs=2, values=("High", "Ultra")) + [constraint]
    repository, files, _, context, device, package = _world(
        tmp_path, records=records, platform="steam",
    )
    assessments = _assess(repository, files, _input(context, device, package))
    assert [item.configuration_ref.local_record_id for item in assessments] == ["cfg-0001"]


# --- 13..17: evidence, manifest, identity and side effects -----------------


def test_reposted_sources_do_not_vote_by_count(tmp_path):
    shared_hash = "a" * 64
    sources = [
        source("src-0001", "rev-0001", url="https://same.invalid/x", content_hash=shared_hash),
        source("src-0002", "rev-0002", url="https://same.invalid/x", content_hash=shared_hash),
    ]
    records = dataset(configs=1)
    records.append(record("set-0002", "settings", {
        "id": "set-0002", "raw_name": "Synthetic Shadow",
        "canonical_name": "Shadow", "aliases": [], "unit": None,
        "proof": {"url": "https://same.invalid/x"},
    }))
    records.append(record("evi-0002", "evidence", {
        "id": "evi-0002", "kind": "measurement",
        "origin": "source_author_measurement",
        "statement": "reposted average frame rate", "metric": "average_fps",
        "numeric": {"number_kind": "exact", "value": 61}, "unit": "fps",
        "setting_ids": ["set-0002"], "input_ids": [], "detected_state": None,
    }, source_id="src-0002", revision_id="rev-0002"))
    records[-3]["payload"]["assignments"].append({
        "setting_id": "set-0002",
        "value": {"value_kind": "fixed", "value": "On"},
        "evidence_ids": ["evi-0002"],
    })
    repository, files, _, context, device, package = _world(
        tmp_path, records=records, package_key="pkg-dup", run_id="run-dup",
        sources=sources,
    )
    assessments = _assess(repository, files, _input(context, device, package))
    assert len(assessments) == 1
    independence = assessments[0].independence_checks
    assert any(item.status is CheckStatus.fail for item in independence)
    assert any("duplicate" in item.lower() for item in assessments[0].limitations)


def test_the_model_cannot_add_a_candidate_outside_the_package(tmp_path):
    repository, files, _, context, device, package = _world(tmp_path)
    task_input = _input(context, device, package)
    gate = _gate(repository, files, task_input)
    builder = CandidateBuilder(repository, KnowledgeService(repository, files))
    result = builder.build(gate, model_candidates=[
        {"record_id": uid(777), "match_class": "direct", "reason": "made up"},
    ])
    assert len(result.assessments) == 1
    assert all(
        item.configuration_ref.record_snapshot_id != uid(777)
        for item in result.assessments
    )
    assert result.diagnostics
    assert any("outside the package" in item for item in result.diagnostics)


def test_the_candidate_manifest_is_byte_stable_for_the_same_input(tmp_path):
    repository, files, _, context, device, package = _world(tmp_path, configs=2)
    first = _assess(repository, files, _input(context, device, package))
    second = _assess(
        repository, files, _input(context, device, package),
        clock=lambda: "2026-11-03T12:34:56+00:00",
    )
    assert assessment_manifest(first) == assessment_manifest(second)
    changed = _assess(
        repository, files, _input(context, device, package),
    )
    changed[0] = changed[0].model_copy(update={"selection_reason": "different"})
    assert assessment_manifest(changed) != assessment_manifest(first)


def test_unmapped_settings_with_the_same_name_stay_independent(tmp_path):
    records = dataset(configs=1)
    records.append(record("set-0002", "settings", {
        "id": "set-0002", "raw_name": "Synthetic Quality",
        "canonical_name": "Quality", "aliases": [], "unit": None,
        "proof": {"url": "https://example.invalid/src-0001"},
    }))
    records[-2]["payload"]["assignments"].append({
        "setting_id": "set-0002",
        "value": {"value_kind": "fixed", "value": "High"},
        "evidence_ids": ["evi-0001"],
    })
    repository, files, _, context, device, package = _world(
        tmp_path, records=records,
    )
    members = [
        item for item in _package_records(repository, package)
        if item.record_type == "settings"
    ]
    keys = resolve_setting_identities(members, [])
    assert len(set(keys.values())) == len(members) == 2


def test_the_gate_makes_no_model_call(tmp_path):
    repository, files, _, context, device, package = _world(tmp_path)
    spy = SpyProvider()
    _assess(repository, files, _input(context, device, package))
    assert external_calls(repository) == []
    assert spy.sends == 0
