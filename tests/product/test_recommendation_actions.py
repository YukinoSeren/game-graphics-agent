"""REC-T02: baseline, actions, alternatives, target and readiness.

The same offline fixtures as REC-T01: a real verified game, context, device and
an imported package.  No provider, network, image or game is involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from game_app.contracts.enums import (
    CheckStatus,
    RecommendationActionKind,
    RecommendationReadiness,
    ValueState,
)
from game_app.contracts.recommendation import TargetProposal
from game_app.errors import ContractError, ErrorCode
from game_app.knowledge import KnowledgeService
from game_app.recommendation import (
    InputGate,
    assess_candidates,
    build_actions,
    build_alternatives,
    build_content,
    build_target,
    reduce_readiness,
    select_baseline,
    validate_actions,
)
from tests.product.support.recommendation import (
    SCOPE,
    build_package,
    check,
    confirmed_setting,
    count_objects,
    dataset,
    knowledge_setting_ref,
    package_records,
    record,
    setup,
    store_applied,
    store_context,
    store_device,
    store_game,
    target_evidence,
    uid,
)


# --- fixtures ---------------------------------------------------------------


def _input(context, device, package, **overrides):
    from game_app.contracts.tasks import RecommendationInput

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


def _world(
    tmp_path: Path,
    *,
    records=None,
    setting_extra: dict | None = None,
    configs: int = 1,
    values: tuple[str, ...] | None = None,
):
    repository, files = setup(tmp_path)
    game = store_game(repository)
    context = store_context(repository, files, game)
    device = store_device(repository, files)
    package = build_package(
        repository, files, game, context,
        records=records if records is not None
        else dataset(
            configs=configs, values=values, setting_extra=setting_extra,
        ),
    )
    knowledge = KnowledgeService(repository, files)
    task_input = _input(context, device, package)
    assessments = assess_candidates(
        task_input, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    return repository, files, context, device, package, knowledge, task_input, assessments


def _content(
    repository, knowledge, task_input, assessments, *,
    applied=None, overrides=None, clock=None,
):
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    return build_content(
        task_input, assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE, applied=applied, overrides=overrides,
        recommendation_id=uid(999), **kwargs,
    )


def _by_id(checks):
    return {item.check_id: item for item in checks}


def _applicable(records):
    for item in records:
        if item["record_type"] == "configurations":
            item["applicability"] = "applicable"
    return records


# --- 1..3: baseline ---------------------------------------------------------


def test_the_baseline_keeps_the_author_choice_and_an_explainable_order(tmp_path):
    records = _applicable(dataset(configs=2, values=("High", "Ultra")))
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    assert selection.base_configuration_ref is not None
    assert selection.base_configuration_ref.local_record_id == "cfg-0001"
    assert len(selection.ranked) == 2
    assert selection.checks[0].status is CheckStatus.pass_


def test_an_incomplete_configuration_is_never_a_baseline(tmp_path):
    records = dataset(configs=1)
    records[-1]["payload"]["assignments"] = []
    records[-1]["payload"]["preset_reference"] = "Synthetic Preset"
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    assert selection.base_configuration_ref is None
    assert selection.checks[0].status is CheckStatus.fail
    assert any("no baseline" in item for item in selection.limitations)
    content = _content(repository, knowledge, task_input, assessments)
    assert content.base_configuration_ref is None
    assert content.readiness is RecommendationReadiness.blocked
    assert content.actions == []


def test_each_action_keeps_its_source_and_no_gain_is_split_out(tmp_path):
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=_applicable(dataset()),
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    built = build_actions(
        task_input, selection, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    assert len(built.actions) == 1
    action = built.actions[0]
    assert action.action is RecommendationActionKind.set_value
    assert action.evidence_refs and action.evidence_refs[0].kind == "knowledge"
    assert "%" not in action.reason


# --- 4..7: branches, groups, validation -------------------------------------


def test_an_unresolved_branch_is_conditional_not_a_fixed_value(tmp_path):
    records = dataset(configs=1)
    records[-1]["payload"]["assignments"][0]["value"] = {
        "value_kind": "alternatives",
        "value": None,
        "alternatives": ["DLSS", "FSR"],
        "branches": [],
        "unknown_reason": None,
        "unit": None,
    }
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    built = build_actions(
        task_input, selection, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    assert built.actions[0].action is RecommendationActionKind.conditional_strategy
    assert built.actions[0].value.value_kind == "alternatives"


def test_two_mutually_exclusive_branches_are_not_both_set(tmp_path):
    records = dataset(
        configs=1,
        setting_extra={"branch_group": "upscaling"},
    )
    records.append(record("set-0002", "settings", {
        "id": "set-0002", "raw_name": "Upscaler", "canonical_name": "Upscaler",
        "aliases": [], "unit": None, "branch_group": "upscaling",
        "proof": {"url": "https://example.invalid/src-0001"},
    }))
    records[-2]["payload"]["assignments"].append({
        "setting_id": "set-0002",
        "value": {"value_kind": "fixed", "value": "FSR"},
        "evidence_ids": ["evi-0001"],
    })
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    content = _content(repository, knowledge, task_input, assessments)
    assert any(
        item.check_id == "branch.exclusion" and item.status is CheckStatus.fail
        for item in content.checks
    )
    assert content.readiness is RecommendationReadiness.blocked
    assert not any(
        action.action is RecommendationActionKind.set_value
        for action in content.actions
    )


def test_a_dependency_group_missing_a_member_blocks_the_plan(tmp_path):
    records = dataset(
        configs=1,
        setting_extra={
            "dependency_group": uid(801),
            "dependency_group_members": ["set-0001", "set-0002"],
        },
    )
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    content = _content(repository, knowledge, task_input, assessments)
    assert any(
        item.check_id == "combination.dependency_group"
        and item.status is CheckStatus.fail
        for item in content.checks
    )
    assert content.readiness is RecommendationReadiness.blocked
    assert not any(
        action.action is RecommendationActionKind.set_value
        for action in content.actions
    )


def test_a_self_invented_menu_value_and_an_illegal_reference_fail_validation(tmp_path):
    records = dataset(
        configs=1, values=("Ultra",),
        setting_extra={"allowed_values": ["High", "Medium"]},
    )
    repository, _, _, _, package, knowledge, task_input, _ = _world(
        tmp_path, records=records,
    )
    setting_ref = knowledge_setting_ref(repository, package, "set-0001")
    actions = [{
        "action_id": uid(900),
        "setting_ref": setting_ref,
        "action": "set_value",
        "value": {
            "value_kind": "fixed", "value": "Ultra", "alternatives": [],
            "branches": [], "unknown_reason": None, "unit": None,
        },
        "current_value": None,
        "menu_path": ["Video"],
        "reason": "invented",
        "evidence_refs": [{
            "kind": "knowledge",
            "record": {
                "package_id": package.id,
                "record_snapshot_id": uid(779),
                "source_revision_id": uid(780),
                "capture_scope_id": "src-0001",
                "local_record_id": "evi-0001",
                "record_type": "evidence",
                "payload_hash": "0" * 64,
            },
        }],
        "dependency_group_id": None,
        "restart_required": None,
        "checks": [],
    }]
    checks = _by_id(validate_actions(
        task_input, actions, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    ))
    assert checks["value.membership"].status is CheckStatus.fail
    assert checks["reference.setting"].status is CheckStatus.pass_
    assert checks["reference.evidence"].status is CheckStatus.fail


# --- 8..11: restart, current values, alternatives ---------------------------


def test_an_unknown_restart_requirement_is_unknown_not_false(tmp_path):
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=_applicable(dataset()),
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    built = build_actions(
        task_input, selection, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    field = built.actions[0].restart_required
    assert field.value_state.value == "unknown"
    assert field.value is not False


def test_an_unknown_current_value_is_never_claimed_as_keep_current(tmp_path):
    repository, files, context, device, package, knowledge, task_input, assessments = (
        _world(tmp_path, records=_applicable(dataset()))
    )
    applied = store_applied(
        repository, uid_value=910,
        settings=[confirmed_setting(
            repository, files, package, "set-0001", "High", known_value=False,
        )],
    )
    content = _content(
        repository, knowledge, task_input, assessments, applied=applied,
    )
    action = content.actions[0]
    assert action.action is not RecommendationActionKind.keep_current
    assert action.current_value.value_state.value == "unknown"


def test_without_evidence_the_alternatives_stay_empty(tmp_path):
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=_applicable(dataset()),
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    built = build_actions(
        task_input, selection, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    alternatives, _, _ = build_alternatives(
        task_input, built.actions, [],
        recommendation_id=uid(999), repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    assert alternatives == []
    invented, checks, limitations = build_alternatives(
        task_input, built.actions,
        [{"label": "invented", "overrides": [{
            "action_id": built.actions[0].action_id,
            "setting_ref": built.actions[0].setting_ref.model_dump(mode="json"),
            "action": "set_value",
            "value": {"value_kind": "fixed", "value": "Ultra",
                      "alternatives": [], "branches": [],
                      "unknown_reason": None, "unit": None},
            "current_value": None,
            "menu_path": [],
            "reason": "no evidence",
            "evidence_refs": [],
            "dependency_group_id": None,
            "restart_required": None,
            "checks": [],
        }]}],
        recommendation_id=uid(999), repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    assert invented == []
    assert any("revalidation" in item for item in limitations)


def test_an_override_is_revalidated_and_illegal_ones_are_dropped(tmp_path):
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=_applicable(dataset()),
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    built = build_actions(
        task_input, selection, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    base = built.actions[0]
    valid = base.model_dump(mode="json")
    valid["value"]["value"] = "Medium"
    alternatives, _, limitations = build_alternatives(
        task_input, built.actions,
        [
            {"label": "less inference", "overrides": [valid]},
            {"label": "illegal", "overrides": [{
                "action_id": uid(777), "action": "set_value",
            }]},
        ],
        recommendation_id=uid(999), repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    assert len(alternatives) == 1
    assert "integrated configuration" in alternatives[0].label
    assert any("does not name a main action" in item for item in limitations)


# --- 12..16: target and sources ---------------------------------------------


def test_an_interval_target_stays_an_interval(tmp_path):
    records = dataset(configs=1)
    records.append(target_evidence(numeric={
        "number_kind": "interval", "value": None, "lower": 50, "upper": 60,
        "unknown_reason": None,
    }))
    repository, _, _, _, package, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    content = _content(repository, knowledge, task_input, assessments)
    proposal = content.target_proposal
    assert proposal is not None
    assert proposal.value.number_kind == "interval"
    assert proposal.value.value is None
    assert proposal.value.lower == 50 and proposal.value.upper == 60
    assert any("interval" in item for item in content.limitations)


def test_without_a_basis_there_is_no_default_sixty(tmp_path):
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=_applicable(dataset()),
    )
    content = _content(repository, knowledge, task_input, assessments)
    assert content.target_proposal is None
    assert any("no default frame rate" in item for item in content.limitations)


def test_a_not_detected_hdr_observation_stays_an_observation(tmp_path):
    records = dataset(configs=1)
    records.append(record("obs-0001", "evidence", {
        "id": "obs-0001", "kind": "observation", "origin": "source_author",
        "statement": "HDR: not detected in the captured settings",
        "detected_state": "not_detected", "setting_ids": [], "input_ids": [],
    }))
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    content = _content(repository, knowledge, task_input, assessments)
    assert all("HDR" not in action.reason for action in content.actions)
    assert all(
        "hdr" not in str(action.setting_ref.model_dump()).lower()
        for action in content.actions
    )


def test_public_measurements_are_not_written_as_local_results(tmp_path):
    records = dataset(configs=1)
    records.append(target_evidence())
    repository, files, _, _, package, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    applied = store_applied(repository, uid_value=920, settings=[
        confirmed_setting(repository, files, package, "set-0001", "High"),
    ])
    content = _content(
        repository, knowledge, task_input, assessments, applied=applied,
    )
    members = set(package.record_snapshot_ids)
    assert content.reference_results
    assert all(item.record_snapshot_id in members for item in content.reference_results)
    assert all(
        item.kind == "knowledge" for item in content.target_proposal.basis_refs
    )
    assert uid(920) not in {
        item.record_snapshot_id for item in content.reference_results
    }


def test_conflicting_sources_keep_both_candidates_and_their_limits(tmp_path):
    records = _applicable(dataset(configs=2, values=("High", "Ultra")))
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    assert len(selection.ranked) == 2
    assert selection.provisional is True
    assert any("provisional" in item for item in selection.limitations)


# --- 17..22: readiness, preference protection, target immutability -----------


def test_an_unknown_critical_check_is_provisional(tmp_path):
    checks = [
        check("identity.context", CheckStatus.pass_),
        check("identity.game", CheckStatus.unknown),
        check("reference.base", CheckStatus.pass_),
        check("operation.menu", CheckStatus.pass_),
    ]
    assert reduce_readiness(checks, "recommendation.configuration") is (
        RecommendationReadiness.provisional
    )


def test_all_critical_checks_passing_is_actionable(tmp_path):
    checks = [
        check("identity.context", CheckStatus.pass_),
        check("reference.base", CheckStatus.pass_),
        check("value.shape", CheckStatus.pass_),
        check("evidence.present", CheckStatus.pass_),
        check("combination.dependency_group", CheckStatus.pass_),
        check("branch.exclusion", CheckStatus.pass_),
        check("operation.menu", CheckStatus.pass_),
        check("restart.required", CheckStatus.not_applicable),
        check("target.present", CheckStatus.not_run),
    ]
    assert reduce_readiness(checks, "recommendation.configuration") is (
        RecommendationReadiness.actionable
    )


def test_an_accepted_record_without_checks_is_not_actionable(tmp_path):
    assert reduce_readiness([], "recommendation.configuration") is (
        RecommendationReadiness.blocked
    )
    assert reduce_readiness(
        [check("candidate.applicability", CheckStatus.pass_)],
        "recommendation.configuration",
    ) is RecommendationReadiness.blocked


def test_an_unknown_preference_id_is_not_found(tmp_path):
    repository, files, context, device, package, knowledge, task_input, _ = _world(
        tmp_path, records=_applicable(dataset()),
    )
    gate = InputGate(repository, knowledge).check(task_input, user_scope_id=SCOPE)
    preference = _by_id(gate.checks)["preference.explicit"]
    assert preference.status is CheckStatus.not_applicable
    assert count_objects(repository, "Preference") == 0
    with pytest.raises(ContractError) as error:
        InputGate(repository, knowledge).check(
            task_input.model_copy(update={"preference_ids": [uid(700)]}),
            user_scope_id=SCOPE,
        )
    assert error.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_a_blocked_plan_downgrades_every_executable_action(tmp_path):
    records = dataset(
        configs=1,
        setting_extra={
            "dependency_group": uid(802),
            "dependency_group_members": ["set-0001", "set-0002"],
        },
    )
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    content = _content(repository, knowledge, task_input, assessments)
    assert content.readiness is RecommendationReadiness.blocked
    assert content.actions
    assert all(
        action.action is RecommendationActionKind.defer
        for action in content.actions
    )
    assert all("deferred" in action.reason for action in content.actions)


def test_a_target_is_immutable_and_writes_no_acceptance(tmp_path):
    records = dataset(configs=1)
    records.append(target_evidence())
    repository, _, _, _, package, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    members = package_records(repository, package)
    first, _, _ = build_target(
        package, members, clock=lambda: "2026-11-03T01:00:00+00:00",
    )
    second, _, _ = build_target(
        package, members, clock=lambda: "2026-11-04T01:00:00+00:00",
    )
    assert isinstance(first, TargetProposal)
    assert first.id == second.id
    assert first.value == second.value
    with pytest.raises(ValidationError):
        first.note = "rewritten"
    assert count_objects(repository, "TargetAcceptance") == 0


def test_two_assignments_for_one_setting_get_distinct_action_ids(tmp_path):
    records = _applicable(dataset())
    records[-1]["payload"]["assignments"] = [
        {"setting_id": "set-0001", "value": {"value_kind": "fixed", "value": "High"},
         "evidence_ids": ["evi-0001"]},
        {"setting_id": "set-0001", "value": {"value_kind": "fixed", "value": "Ultra"},
         "evidence_ids": ["evi-0001"]},
    ]
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )

    built = build_actions(
        task_input, selection, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )

    assert len(built.actions) == 2
    assert len({action.action_id for action in built.actions}) == 2


def test_a_sourced_restart_fact_is_known_and_an_unsourced_one_stays_unknown(tmp_path):
    sourced = _applicable(dataset(
        setting_extra={
            "restart_required": True,
            "restart_proof": {"url": "https://example.invalid/src-0001"},
        },
    ))
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=sourced,
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    built = build_actions(
        task_input, selection, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )

    restart = built.actions[0].restart_required
    assert restart.value_state is ValueState.known
    assert restart.value is True
    assert restart.provenance and restart.provenance[0].evidence_refs
    content = _content(repository, knowledge, task_input, assessments)
    restart_check = _by_id(content.checks)["restart.required"]
    assert restart_check.status is CheckStatus.pass_

    # The same boolean without its proof is not a known fact.
    unsourced = _applicable(dataset(
        setting_extra={"restart_required": True},
    ))
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path / "unsourced", records=unsourced,
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    built = build_actions(
        task_input, selection, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    assert built.actions[0].restart_required.value_state is ValueState.unknown


def test_a_non_executable_action_never_claims_a_known_menu(tmp_path):
    records = _applicable(dataset())
    records[-1]["payload"]["assignments"] = [
        {"setting_id": "set-0001", "value": {"value_kind": "fixed", "value": "High"},
         "evidence_ids": ["evi-0001"]},
        {"setting_id": "set-0001",
         "value": {"value_kind": "unknown", "unknown_reason": "not_provided"},
         "evidence_ids": ["evi-0001"]},
    ]
    repository, _, _, _, _, knowledge, task_input, assessments = _world(
        tmp_path, records=records,
    )
    selection = select_baseline(
        assessments, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    built = build_actions(
        task_input, selection, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    checked = next(
        action for action in built.actions
        if action.action is RecommendationActionKind.check_in_game
    )
    assert checked.menu_path == []

    checks = validate_actions(
        task_input, built.actions, repository=repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )

    menu = _by_id(checks)["operation.menu"]
    assert menu.status is CheckStatus.not_applicable
    assert "known" not in (menu.note or "")
