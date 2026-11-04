"""FBK-T02: the next action, a legal ChangeSet and the loop guard.

Everything here is offline: synthetic installation, session, local settings and
a crafted recommendation version whose action chain is the only source of an
``adjust`` direction.  The program, not the model, decides the primary action.
"""

from __future__ import annotations

import json

import pytest

from game_app.application import Application
from game_app.contracts.common import MutationContext
from game_app.contracts.enums import (
    AcquisitionStatus,
    CheckStatus,
    NextActionKind,
    ValueState,
)
from game_app.contracts.feedback import NextActionInput
from game_app.contracts.performance import CreateSessionRequest
from game_app.errors import ContractError, ErrorCode
from game_app.repository import canonical_json
from tests.product.support.configuration import (
    USER,
    Definitions,
    adopt_request,
    context_key,
    declaration,
    setup,
    store_raw,
    uid,
)
from tests.product.test_feedback import FeedbackProvider
from tests.product.test_sessions import FakeClock

SCHEMA = "product-contract/0.4"
OTHER = "c" * 32


# --- fixtures ---------------------------------------------------------------


class World:
    def __init__(self, repository, application, provider, clock, installation,
                 context_id, device_id, session_id, definitions):
        self.repository = repository
        self.application = application
        self.provider = provider
        self.clock = clock
        self.installation = installation
        self.context_id = context_id
        self.device_id = device_id
        self.session_id = session_id
        self.definitions = definitions


def _world(tmp_path, *, provider=True, reply=None) -> World:
    repository, files, installation, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    stub = FeedbackProvider(reply=reply) if provider else None
    application = Application(
        repository, file_root=str(tmp_path / "managed"), provider=stub, clock=clock,
    )
    session_id, _ = _session(application, installation, context_id, device_id)
    return World(
        repository, application, stub, clock, installation, context_id,
        device_id, session_id, Definitions(repository, installation),
    )


def _session(application, installation, context_id, device_id, key="session-1"):
    request = CreateSessionRequest.model_validate_json(canonical_json({
        "installation_id": installation,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
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
    result = application.create_session(request, context_key(key))
    return (
        next(ref.entity_id for ref in result.refs if ref.entity_type == "PlaySession"),
        next(ref.entity_id for ref in result.refs if ref.entity_type == "SessionSegment"),
    )


def _known(value):
    return {
        "value_state": ValueState.known.value,
        "value": value,
        "unknown_reason": None,
        "acquisition_status": AcquisitionStatus.succeeded.value,
        "provenance": [{
            "method": "user_provided",
            "observed_at": None,
            "recorded_at": "2026-11-01T11:00:00+00:00",
            "evidence_refs": [_knowledge_evidence()],
            "note": "offline fixture",
        }],
    }


def _unknown():
    return {
        "value_state": ValueState.unknown.value,
        "value": None,
        "unknown_reason": "not_provided",
        "acquisition_status": AcquisitionStatus.not_attempted.value,
        "provenance": [],
    }


def _knowledge_evidence() -> dict:
    return {
        "kind": "knowledge",
        "record": {
            "package_id": uid(900),
            "record_snapshot_id": uid(901),
            "source_revision_id": uid(902),
            "capture_scope_id": "scope-1",
            "local_record_id": "set-0001",
            "record_type": "setting",
            "payload_hash": "d" * 64,
        },
    }


def _action(
    definition_id, old_value, new_value, *, evidence=True, group=None,
    restart_known=True, action="set_value",
) -> dict:
    return {
        "action_id": uid(int(definition_id[-4:], 16) + 500),
        "setting_ref": {"kind": "local", "local_setting_id": definition_id},
        "setting_identity_ref": None,
        "action": action,
        "value": {
            "value_kind": "fixed", "value": new_value, "alternatives": [],
            "branches": [], "unknown_reason": None, "unit": None,
        },
        "current_value": _known(old_value) if old_value is not None else _unknown(),
        "menu_path": ["图形", "quality"],
        "reason": "the package records a higher value for this setting",
        "evidence_refs": [_knowledge_evidence()] if evidence else [],
        "dependency_group_id": group,
        "restart_required": (
            _known(True) if restart_known else _unknown()
        ),
        "checks": [],
    }


def _recommendation(
    repository, context_id, device_id, actions, *,
    version_id=None, created_at="2026-11-01T11:00:00+00:00",
) -> str:
    version_id = version_id or uid(800)
    store_raw(repository, "RecommendationVersion", {
        "id": version_id,
        "schema_version": SCHEMA,
        "created_at": created_at,
        "created_by_task_id": None,
        "series_id": version_id,
        "version": 1,
        "parent_id": None,
        "input": {
            "kind": "recommendation",
            "game_context_id": context_id,
            "device_snapshot_id": device_id,
            "package_id": uid(902),
            "preference_ids": [],
            "applied_id": None,
            "history_summary_ids": [],
        },
        "base_configuration_ref": None,
        "actions": actions,
        "alternatives": [],
        "target_proposal": None,
        "reference_results": [],
        "declared_scope": "synthetic offline fixture",
        "readiness": "actionable",
        "checks": [],
        "candidate_manifest_file_id": uid(903),
    })
    return version_id


def _adopt(world: World, recommendation_id, settings, *, key="adopt-1", expected=None,
           preset=None):
    request = adopt_request(
        world.installation, world.context_id, world.device_id,
        origin="recommendation",
        expected_current_applied_id=expected,
        recommendation_id=recommendation_id,
        declared_settings=settings,
        preset_reference=preset,
    )
    result = world.application.adopt_configuration(request, context_key(key))
    return next(
        ref.entity_id for ref in result.refs
        if ref.entity_type == "AppliedConfiguration"
    )


def _feedback(world: World, selections, *, raw_text="", feedback_id=None) -> str:
    feedback_id = feedback_id or uid(int(len(raw_text) or 1) + 700)
    store_raw(world.repository, "Feedback", {
        "id": feedback_id,
        "schema_version": SCHEMA,
        "created_at": world.clock(),
        "created_by_task_id": None,
        "session_id": world.session_id,
        "segment_id": None,
        "raw_text": raw_text,
        "selections": selections,
        "symptoms": [],
        "interpretation_status": "unparsed",
    })
    return feedback_id


def _input(world: World, feedback_ids, *, applied_id=None, **overrides):
    payload = {
        "kind": "next_action",
        "session_id": world.session_id,
        "bindings": [],
        "summary_ids": [],
        "feedback_ids": list(feedback_ids),
        "current_applied_id": applied_id,
        "package_id": None,
    }
    payload.update(overrides)
    return NextActionInput.model_validate_json(canonical_json(payload))


def _plan(world: World, task_input, *, key="next-1", scope=USER):
    context = MutationContext(
        user_scope_id=scope, idempotency_key=key, request_id=uid(999),
    )
    accepted = world.application.plan_next_action(task_input, context)
    outcome = world.application.run_once()
    return accepted, outcome, world.application.get_task(accepted.task_id)


def _objects(repository, entity_type):
    with repository.unit_of_work() as uow:
        return [json.loads(row["payload"]) for row in uow.query(
            "SELECT payload FROM objects WHERE entity_type=? ORDER BY created_at, rowid",
            (entity_type,),
        )]


def _the_diagnosis(world, view):
    ref = next(item for item in view.result_refs if item.entity_type == "Diagnosis")
    return next(item for item in _objects(world.repository, "Diagnosis")
                if item["id"] == ref.entity_id)


def _checks(diagnosis):
    return {item["check_id"]: item["status"] for item in diagnosis["checks"]}


def _external_calls(repository):
    with repository.unit_of_work() as uow:
        return uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"]


def _preference(world, *, explicitness="explicit", content="keep motion blur on"):
    feedback_id = _feedback(world, {"feeling": "satisfied"}, raw_text="motion blur")
    store_raw(world.repository, "Preference", {
        "id": uid(710),
        "schema_version": SCHEMA,
        "created_at": world.clock(),
        "created_by_task_id": None,
        "series_id": uid(710),
        "version": 1,
        "parent_id": None,
        "scope": "global",
        "scope_id": None,
        "content": content,
        "explicitness": explicitness,
        "lifecycle": "active",
        "feedback_id": feedback_id,
    })
    return uid(710)


# --- E-1: FBK-T02 ------------------------------------------------------------


def test_explicit_satisfaction_keeps_without_a_change_set(tmp_path):
    world = _world(tmp_path)
    feedback_id = _feedback(world, {"feeling": "satisfied"}, raw_text="looks great")

    accepted, outcome, view = _plan(world, _input(world, [feedback_id]))

    assert accepted.state.value == "queued"
    assert outcome.state.value == "completed"
    diagnosis = _the_diagnosis(world, view)
    assert diagnosis["primary_action"] == NextActionKind.keep.value
    assert diagnosis["change_set_id"] is None
    assert view.task.domain_result.code == NextActionKind.keep.value
    assert _objects(world.repository, "ChangeSet") == []


def test_words_without_a_baseline_only_measure_and_never_fabricate_a_base(tmp_path):
    world = _world(tmp_path)
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="the scene load stutters",
    )

    _, outcome, view = _plan(world, _input(world, [feedback_id]))

    diagnosis = _the_diagnosis(world, view)
    assert diagnosis["primary_action"] in {
        NextActionKind.measure.value, NextActionKind.insufficient_evidence.value,
        NextActionKind.research.value,
    }
    assert diagnosis["change_set_id"] is None
    assert diagnosis["input"]["current_applied_id"] is None
    assert _objects(world.repository, "ChangeSet") == []
    assert outcome.state.value == "completed"


def test_a_legal_adjust_writes_a_change_set_without_moving_the_pointer(tmp_path):
    world = _world(tmp_path)
    changed, _ = world.definitions.new("阴影质量")
    recommendation_id = _recommendation(
        world.repository, world.context_id, world.device_id,
        [_action(changed, 1, 2)],
    )
    applied_id = _adopt(
        world, recommendation_id, [declaration(changed, value=1)],
    )
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="shadows cost frames",
    )

    _, outcome, view = _plan(
        world, _input(world, [feedback_id], applied_id=applied_id),
    )

    diagnosis = _the_diagnosis(world, view)
    assert diagnosis["primary_action"] == NextActionKind.adjust.value
    assert diagnosis["change_set_id"] is not None
    change_set = next(
        item for item in _objects(world.repository, "ChangeSet")
        if item["id"] == diagnosis["change_set_id"]
    )
    assert change_set["base_applied_id"] == applied_id
    assert change_set["installation_id"] == world.installation
    assert change_set["hypothesis_id"] in {
        item["id"] for item in diagnosis["hypotheses"]
    }
    assert change_set["expected_direction"] == {f"local:{changed}": "increase"}
    assert {item["status"] for item in change_set["checks"]} <= {
        CheckStatus.pass_.value,
    }
    assert outcome.state.value == "completed"
    # The next action never advances the current pointer by itself.
    current, _ = world.application.adoption.get_current_applied(USER, world.installation)
    assert current == applied_id
    assert view.task.domain_result.code == NextActionKind.adjust.value


def test_unknown_old_value_saves_a_diagnosis_but_no_change_set(tmp_path):
    world = _world(tmp_path)
    changed, _ = world.definitions.new("阴影质量")
    recommendation_id = _recommendation(
        world.repository, world.context_id, world.device_id,
        [_action(changed, 1, 2)],
    )
    applied_id = _adopt(
        world, recommendation_id, [declaration(changed)],
        preset="Synthetic Preset",
    )
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="shadows look flat",
    )

    _, _, view = _plan(world, _input(world, [feedback_id], applied_id=applied_id))

    diagnosis = _the_diagnosis(world, view)
    assert diagnosis["primary_action"] is not NextActionKind.adjust.value
    assert diagnosis["change_set_id"] is None
    assert _objects(world.repository, "ChangeSet") == []
    checks = _checks(diagnosis)
    assert checks["change_set.old_value"] in {
        CheckStatus.unknown.value, CheckStatus.fail.value,
    }
    assert checks["change_set.rollback"] in {
        CheckStatus.unknown.value, CheckStatus.fail.value,
    }


def test_an_explicit_preference_blocks_adjust_and_names_itself(tmp_path):
    world = _world(tmp_path)
    changed, _ = world.definitions.new("运动模糊")
    recommendation_id = _recommendation(
        world.repository, world.context_id, world.device_id,
        [_action(changed, 1, 0)],
    )
    applied_id = _adopt(world, recommendation_id, [declaration(changed, value=1)])
    preference_id = _preference(world)
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="motion blur smears",
    )

    _, _, view = _plan(world, _input(world, [feedback_id], applied_id=applied_id))

    diagnosis = _the_diagnosis(world, view)
    assert diagnosis["change_set_id"] is None
    assert _objects(world.repository, "ChangeSet") == []
    preference_check = next(
        item for item in diagnosis["checks"]
        if item["check_id"] == "change_set.preference"
    )
    assert preference_check["status"] == CheckStatus.unknown.value
    assert preference_id in preference_check["note"]


def test_a_tentative_preference_does_not_block_adjust(tmp_path):
    world = _world(tmp_path)
    changed, _ = world.definitions.new("阴影质量")
    recommendation_id = _recommendation(
        world.repository, world.context_id, world.device_id,
        [_action(changed, 1, 2)],
    )
    applied_id = _adopt(world, recommendation_id, [declaration(changed, value=1)])
    _preference(world, explicitness="tentative")
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="shadows cost frames",
    )

    _, _, view = _plan(world, _input(world, [feedback_id], applied_id=applied_id))

    diagnosis = _the_diagnosis(world, view)
    assert diagnosis["primary_action"] == NextActionKind.adjust.value
    assert diagnosis["change_set_id"] is not None


def test_an_open_dependency_group_is_refused(tmp_path):
    world = _world(tmp_path)
    changed, _ = world.definitions.new("阴影质量")
    dependent, _ = world.definitions.new("阴影距离")
    group = uid(770)
    recommendation_id = _recommendation(
        world.repository, world.context_id, world.device_id,
        [
            _action(changed, 1, 2, group=group),
            _action(dependent, None, 3, group=group),
        ],
    )
    applied_id = _adopt(
        world, recommendation_id,
        [declaration(changed, value=1), declaration(dependent)],
    )
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="shadows cost frames",
    )

    _, _, view = _plan(world, _input(world, [feedback_id], applied_id=applied_id))

    diagnosis = _the_diagnosis(world, view)
    assert diagnosis["change_set_id"] is None
    checks = _checks(diagnosis)
    assert checks["change_set.dependency_group"] in {
        CheckStatus.unknown.value, CheckStatus.fail.value,
    }
    assert _objects(world.repository, "ChangeSet") == []


def test_a_refused_direction_is_not_repeated_without_new_evidence(tmp_path):
    world = _world(tmp_path)
    changed, _ = world.definitions.new("阴影质量")
    recommendation_id = _recommendation(
        world.repository, world.context_id, world.device_id,
        [_action(changed, 1, 2)],
    )
    applied_id = _adopt(world, recommendation_id, [declaration(changed, value=1)])
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="shadows cost frames",
    )
    _prior_refusal(world, applied_id, changed)
    before = len(_objects(world.repository, "ChangeSet"))

    _, _, view = _plan(world, _input(world, [feedback_id], applied_id=applied_id))

    diagnosis = _the_diagnosis(world, view)
    assert diagnosis["primary_action"] == NextActionKind.insufficient_evidence.value
    assert diagnosis["change_set_id"] is None
    loop_check = next(
        item for item in diagnosis["checks"] if item["check_id"] == "loop_guard"
    )
    assert loop_check["status"] == CheckStatus.fail.value
    assert len(_objects(world.repository, "ChangeSet")) == before


def test_new_evidence_allows_the_direction_again_with_a_reason(tmp_path):
    world = _world(tmp_path)
    changed, _ = world.definitions.new("阴影质量")
    first = _recommendation(
        world.repository, world.context_id, world.device_id,
        [_action(changed, 1, 2)],
        version_id=uid(800), created_at="2026-11-01T11:00:00+00:00",
    )
    applied_id = _adopt(world, first, [declaration(changed, value=1)], key="adopt-1")
    _prior_refusal(world, applied_id, changed)
    second = _recommendation(
        world.repository, world.context_id, world.device_id,
        [_action(changed, 1, 2)],
        version_id=uid(801), created_at="2026-11-01T13:00:00+00:00",
    )
    newer_id = _adopt(
        world, second, [declaration(changed, value=1)], key="adopt-2",
        expected=applied_id,
    )
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="shadows cost frames",
    )

    _, _, view = _plan(world, _input(world, [feedback_id], applied_id=newer_id))

    diagnosis = _the_diagnosis(world, view)
    assert diagnosis["primary_action"] == NextActionKind.adjust.value
    assert diagnosis["change_set_id"] is not None
    loop_check = next(
        item for item in diagnosis["checks"] if item["check_id"] == "loop_guard"
    )
    assert loop_check["status"] == CheckStatus.pass_.value
    assert "new" in loop_check["note"].lower()


def _prior_refusal(world: World, applied_id: str, changed: str) -> None:
    change_set_id = store_raw(world.repository, "ChangeSet", {
        "id": uid(760),
        "schema_version": SCHEMA,
        "created_at": "2026-11-01T11:30:00+00:00",
        "created_by_task_id": None,
        "base_applied_id": applied_id,
        "installation_id": world.installation,
        "changes": [{
            "setting_ref": {"kind": "local", "local_setting_id": changed},
            "old_value": _known(1),
            "new_value": 2,
            "unit": None,
            "dependency_group_id": None,
            "restart_required": _known(True),
        }],
        "hypothesis_id": uid(750),
        "evidence_refs": [_knowledge_evidence()],
        "protected_preference_ids": [],
        "expected_direction": {f"local:{changed}": "increase"},
        "protocol_id": None,
        "rollback_ref": applied_id,
        "checks": [],
    })
    store_raw(world.repository, "ExperimentOutcome", {
        "id": uid(761),
        "schema_version": SCHEMA,
        "created_at": "2026-11-01T12:00:00+00:00",
        "created_by_task_id": None,
        "change_set_id": change_set_id,
        "base_applied_id": applied_id,
        "tested_applied_id": applied_id,
        "before_bindings": [],
        "after_bindings": [],
        "comparison_id": None,
        "legacy_outcome": "worse",
        "player_acceptance": "rejected",
        "effect_assessment": "worse",
        "feedback_ids": [],
        "evidence_refs": [_knowledge_evidence()],
        "limitations": ["offline fixture"],
    })


def test_the_model_cannot_choose_the_action_or_declare_controlled(tmp_path):
    world = _world(
        tmp_path,
        reply={
            "symptoms": [{"kind": "stutter", "interpretation": "inferred"}],
            "primary_action": "adjust",
            "controlled": True,
            "hypotheses": [{
                "statement": "the scene is loading too much at once",
                "support": [], "counter": [], "unknowns": ["loading time"],
                "test_action": "repeat the same route twice",
            }],
        },
    )
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="the scene load stutters",
    )

    _, _, view = _plan(world, _input(world, [feedback_id]))

    diagnosis = _the_diagnosis(world, view)
    # No baseline exists, so the program cannot adjust even though the reply asks.
    assert diagnosis["primary_action"] != NextActionKind.adjust.value
    assert diagnosis["change_set_id"] is None
    assert diagnosis["hypotheses"][0]["statement"] == (
        "the scene is loading too much at once"
    )
    assert view.task.domain_result.code == diagnosis["primary_action"]
    assert world.provider.sends == 1


def test_without_a_provider_no_model_evidence_is_fabricated(tmp_path):
    world = _world(tmp_path, provider=False)
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="the scene load stutters",
    )

    _, _, view = _plan(world, _input(world, [feedback_id]))

    diagnosis = _the_diagnosis(world, view)
    checks = _checks(diagnosis)
    assert checks["interpretation.model"] == CheckStatus.not_run.value
    assert _external_calls(world.repository) == 0
    assert diagnosis["primary_action"] in {
        NextActionKind.measure.value,
        NextActionKind.research.value,
        NextActionKind.insufficient_evidence.value,
    }


def test_with_a_provider_there_is_exactly_one_model_call(tmp_path):
    world = _world(tmp_path, reply={"hypotheses": []})
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="the scene load stutters",
    )

    _, _, view = _plan(world, _input(world, [feedback_id]))

    assert world.provider.sends == 1
    assert _external_calls(world.repository) == 1
    assert _checks(_the_diagnosis(world, view))["interpretation.model"] == (
        CheckStatus.pass_.value
    )


def test_the_same_key_replays_the_original_acceptance(tmp_path):
    world = _world(tmp_path, provider=False)
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="the scene load stutters",
    )
    task_input = _input(world, [feedback_id])

    first, _, _ = _plan(world, task_input, key="same-key")
    with world.repository.unit_of_work() as uow:
        before = len(uow.iter_objects("Diagnosis"))
    second, _, _ = _plan(world, task_input, key="same-key")

    assert second.task_id == first.task_id
    with world.repository.unit_of_work() as uow:
        after = len(uow.iter_objects("Diagnosis"))
    assert after == before


def test_another_players_session_is_indistinguishable_from_missing(tmp_path):
    world = _world(tmp_path)
    feedback_id = _feedback(
        world, {"feeling": "want_improvement"}, raw_text="the scene load stutters",
    )
    task_input = _input(world, [feedback_id])

    with pytest.raises(ContractError) as caught:
        world.application.plan_next_action(
            task_input,
            MutationContext(
                user_scope_id=OTHER, idempotency_key="foreign", request_id=uid(998),
            ),
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
