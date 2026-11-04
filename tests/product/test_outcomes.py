"""FBK-T03: the adopted chain, repeated fluctuation and the acceptance split.

Every fixture is offline: synthetic sessions, real CSV analyses and a crafted
adopted chain.  The effect is derived by the program from published summaries;
the client states acceptance only, and never an effect or a ``controlled`` flag.
"""

from __future__ import annotations

import json

import pytest

from game_app.application import Application
from game_app.contracts.common import MutationContext
from game_app.contracts.performance import (
    CompareInput,
    CreateComparisonProtocolRequest,
    CreateSessionRequest,
    LogAssociationRequest,
)
from game_app.contracts.primitives import BindingRef
from game_app.errors import ContractError, ErrorCode
from game_app.repository import canonical_json
from tests.product.support.configuration import (
    USER,
    context_key,
    setup,
    store_raw,
    uid,
)
from tests.product.support.performance import (
    analysis_input,
    crafted_summary,
    csv_bytes,
    open_session_artifact,
    publish_log,
    summary_of,
)
from tests.product.test_sessions import FakeClock

SCHEMA = "product-contract/0.4"
CLEANING = "perf-cleaning/1"
CLUSTERING = "perf-long-frame-clustering/1"
DENOMINATOR = "perf-long-frame-denominator/1"
NOW = "2026-11-01T12:00:00+00:00"
FIELDS = {
    "scenario": "route A",
    "output_width": 1920, "output_height": 1080,
    "render_width": 1920, "render_height": 1080,
    "frame_generation": False,
    "upscaling": "off",
}


# --- fixtures ---------------------------------------------------------------


class Stack:
    def __init__(self, repository, application, clock, installation, context_id,
                 device_id):
        self.repository = repository
        self.application = application
        self.clock = clock
        self.installation = installation
        self.context_id = context_id
        self.device_id = device_id


def _stack(tmp_path) -> Stack:
    repository, files, installation, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    application = Application(
        repository, file_root=str(tmp_path / "managed"), clock=clock,
    )
    return Stack(repository, application, clock, installation, context_id, device_id)


def _known(value):
    return {
        "value_state": "known",
        "value": value,
        "unknown_reason": None,
        "acquisition_status": "succeeded",
        "provenance": [{
            "method": "user_provided",
            "observed_at": None,
            "recorded_at": NOW,
            "evidence_refs": [{
                "kind": "local",
                "entity_type": "AppliedConfiguration",
                "entity_id": uid(1101),
            }],
            "note": "offline fixture",
        }],
    }


def _applied(
    stack: Stack, suffix: int, *, parent=None, change_set=None,
    activation="active_confirmed", origin="baseline", user=USER,
    installation=None, context=None, device=None,
) -> str:
    applied_id = uid(1000 + suffix)
    store_raw(stack.repository, "AppliedConfiguration", {
        "id": applied_id,
        "schema_version": SCHEMA,
        "created_at": NOW,
        "created_by_task_id": None,
        "series_id": applied_id,
        "version": 1,
        "user_scope_id": user,
        "installation_id": installation or stack.installation,
        "game_id": "b" * 32,
        "registration_kind": "current_adoption",
        "device_snapshot_id": device or stack.device_id,
        "game_context_id": context or stack.context_id,
        "origin": origin,
        "recommendation_id": None,
        "parent_applied_id": parent,
        "change_set_id": change_set,
        "restored_from_applied_id": None,
        "settings": [],
        "preset_reference": "Synthetic Preset",
        "adopted_at": NOW,
        "activation_state": activation,
        "checks": [],
        "declaration_id": uid(900 + suffix),
    })
    return applied_id


def _change_set(stack: Stack, base_id: str, *, suffix: int = 600) -> str:
    setting = uid(201)
    return store_raw(stack.repository, "ChangeSet", {
        "id": uid(suffix),
        "schema_version": SCHEMA,
        "created_at": NOW,
        "created_by_task_id": None,
        "base_applied_id": base_id,
        "installation_id": stack.installation,
        "changes": [{
            "setting_ref": {"kind": "local", "local_setting_id": setting},
            "old_value": _known(1),
            "new_value": 2,
            "unit": None,
            "dependency_group_id": None,
            "restart_required": _known(True),
        }],
        "hypothesis_id": uid(650),
        "evidence_refs": [],
        "protected_preference_ids": [],
        "expected_direction": {f"local:{setting}": "increase"},
        "protocol_id": None,
        "rollback_ref": base_id,
        "checks": [],
    })


def _run(
    stack: Stack, applied_id: str | None, key: str, intervals,
    *, context_id=None, device_id=None, user=USER, state="bound",
) -> tuple[BindingRef, object, str]:
    request = CreateSessionRequest.model_validate_json(canonical_json({
        "installation_id": stack.installation,
        "device_snapshot_id": device_id or stack.device_id,
        "game_context_id": context_id or stack.context_id,
        "applied_id": applied_id,
        "configuration_state": state,
        "run_declaration": {
            "installation_id": stack.installation,
            "fields": FIELDS,
            "raw_text": "",
        },
        "test_protocol_id": None,
        "recommendation_id": None,
    }))
    result = stack.application.create_session(request, context_key(f"{key}-s", user=user))
    session_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "PlaySession"
    )
    segment_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "SessionSegment"
    )
    file_id, sha = publish_log(
        stack.application.files, csv_bytes(intervals), f"{key}-log", user=user,
    )
    inspection = stack.application.performance.inspect_log(file_id, "offline-frame-csv/v1", user)
    source = inspection.source_ranges[0]
    association = LogAssociationRequest.model_validate_json(canonical_json({
        "file_id": file_id,
        "content_hash": sha,
        "source_schema": "offline-frame-csv/v1",
        "source_range": source.model_dump(mode="json"),
        "stream_key": inspection.streams[0].stream_key,
        "source_anchor": source.start,
        "target_anchor_s": 0.0,
        "target": {
            "mode": "existing_segment",
            "session_id": session_id,
            "expected_session_revision": 1,
            "segment_ref": {
                "segment_id": segment_id,
                "segment_revision": 1,
                "binding_revision": 1,
            },
            "close_at_s": 1.0,
        },
        "evidence_refs": [{
            "kind": "artifact",
            "file_id": file_id,
            "content_hash": sha,
            "locator": "log.csv",
        }],
        "raw_text": "",
    }))
    stack.clock.advance(10)
    associated = stack.application.associate_log(
        association, context_key(f"{key}-assoc", user=user),
    )
    artifact_id = next(
        ref.entity_id for ref in associated.refs if ref.entity_type == "CaptureArtifact"
    )
    binding = BindingRef(
        segment_id=segment_id, segment_revision=2, binding_revision=2,
    )
    accepted = stack.application.analyze(
        analysis_input(artifact_id, binding), context_key(f"{key}-a", user=user),
    )
    stack.application.run_once()
    view = stack.application.get_task(accepted.task_id)
    summary = summary_of(stack.application.performance, view)
    return binding, summary, artifact_id


def _analyze_again(stack: Stack, artifact_id: str, binding: BindingRef, key: str):
    """A second analysis of the same artifact: one physical sample, two rows."""
    accepted = stack.application.analyze(
        analysis_input(artifact_id, binding), context_key(f"{key}-again"),
    )
    stack.application.run_once()
    return summary_of(stack.application.performance,
                      stack.application.get_task(accepted.task_id))


def _protocol(stack: Stack, key: str, *, exploratory: bool = False):
    request = CreateComparisonProtocolRequest.model_validate_json(canonical_json({
        "threshold_mode": "fixed",
        "threshold_ms": 50.0,
        "baseline_summary_ids": [],
        "baseline_binding_refs": [],
        "cleaning_definition": CLEANING,
        "clustering_definition": CLUSTERING,
        "denominator_definition": DENOMINATOR,
        "exploratory": exploratory,
    }))
    result = stack.application.performance.create_comparison_protocol(
        request, context_key(key),
    )
    return stack.application.performance.get_comparison_protocol(
        result.refs[0].entity_id, USER,
    )


def _comparison(stack: Stack, left, right, protocol, key: str):
    result = stack.application.performance.compare(
        CompareInput(summary_ids=[left.id, right.id], protocol_id=protocol.id),
        context_key(key),
    )
    return stack.application.performance.get_comparison(
        result.refs[0].entity_id, USER,
    )


def _outcome(stack: Stack, payload, *, key="outcome-1", user=USER):
    return stack.application.record_outcome(
        payload, MutationContext(
            user_scope_id=user, idempotency_key=key, request_id=uid(999),
        ),
    )


def _outcome_payload(
    *, change_set_id, base, tested, before, after, comparison=None,
    acceptance="unknown", feedback_ids=(), **overrides,
):
    payload = {
        "change_set_id": change_set_id,
        "base_applied_id": base,
        "tested_applied_id": tested,
        "before_bindings": [item.model_dump(mode="json") for item in before],
        "after_bindings": [item.model_dump(mode="json") for item in after],
        "comparison_id": None if comparison is None else comparison.id,
        "player_acceptance": acceptance,
        "feedback_ids": list(feedback_ids),
    }
    payload.update(overrides)
    return payload


def _outcomes(stack: Stack):
    with stack.repository.unit_of_work() as uow:
        return [json.loads(row["payload"]) for row in uow.query(
            "SELECT payload FROM objects WHERE entity_type='ExperimentOutcome'"
            " ORDER BY created_at, rowid",
        )]


def _external_calls(stack: Stack) -> list[dict]:
    with stack.repository.unit_of_work() as uow:
        return [dict(row) for row in uow.query("SELECT * FROM external_calls")]


def _improving_run(stack: Stack):
    """Three improving rounds on each side, a frozen protocol and a comparison."""
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    tested = _applied(stack, 102, parent=base, change_set=change_set,
                      origin="change_set")
    before = [_run(stack, base, f"before-{index}", [20, 20, 20, 20])
              for index in range(3)]
    protocol = _protocol(stack, "protocol-1")
    stack.clock.advance(60)
    after = [_run(stack, tested, f"after-{index}", [10, 10, 10, 10])
             for index in range(3)]
    comparison = _comparison(stack, before[0][1], after[0][1], protocol, "cmp-1")
    return base, change_set, tested, before, after, comparison


# --- FBK-T03 ---------------------------------------------------------------


def test_a_tested_config_without_this_change_set_is_refused(tmp_path):
    stack = _stack(tmp_path)
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    other = _applied(stack, 103, parent=base)
    before = _run(stack, base, "before", [20, 20, 20, 20])
    after = _run(stack, other, "after", [10, 10, 10, 10])

    with pytest.raises(ContractError) as caught:
        _outcome(stack, _outcome_payload(
            change_set_id=change_set, base=base, tested=other,
            before=[before[0]], after=[after[0]], acceptance="accepted",
        ))

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _outcomes(stack) == []


def test_a_reversed_comparison_is_refused(tmp_path):
    stack = _stack(tmp_path)
    base, change_set, tested, before, after, _ = _improving_run(stack)
    protocol = _protocol(stack, "protocol-reversed", exploratory=True)
    stack.clock.advance(60)
    reversed_comparison = _comparison(
        stack, after[0][1], before[0][1], protocol, "cmp-reversed",
    )

    with pytest.raises(ContractError) as caught:
        _outcome(stack, _outcome_payload(
            change_set_id=change_set, base=base, tested=tested,
            before=[item[0] for item in before],
            after=[item[0] for item in after],
            comparison=reversed_comparison, acceptance="accepted",
        ))

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _outcomes(stack) == []


def test_a_comparison_with_another_experiment_is_refused(tmp_path):
    stack = _stack(tmp_path)
    base, change_set, tested, before, after, _ = _improving_run(stack)
    stranger = _applied(stack, 104)
    stranger_run = _run(stack, stranger, "stranger", [10, 10, 10, 10])
    protocol = _protocol(stack, "protocol-mixed", exploratory=True)
    stack.clock.advance(60)
    mixed = _comparison(stack, before[0][1], stranger_run[1], protocol, "cmp-mixed")

    with pytest.raises(ContractError) as caught:
        _outcome(stack, _outcome_payload(
            change_set_id=change_set, base=base, tested=tested,
            before=[item[0] for item in before],
            after=[item[0] for item in after],
            comparison=mixed, acceptance="accepted",
        ))

    assert caught.value.code is ErrorCode.REFERENCE_INVALID


def test_a_player_change_outside_the_change_set_downgrades_the_result(tmp_path):
    stack = _stack(tmp_path)
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    edited = _applied(stack, 105, parent=base, origin="manual_edit")
    tested = _applied(stack, 102, parent=edited, change_set=change_set,
                      origin="change_set")
    before = [_run(stack, base, f"x-before-{index}", [20, 20, 20, 20])
              for index in range(3)]
    protocol = _protocol(stack, "protocol-extra")
    stack.clock.advance(60)
    after = [_run(stack, tested, f"x-after-{index}", [10, 10, 10, 10])
             for index in range(3)]
    comparison = _comparison(stack, before[0][1], after[0][1], protocol, "cmp-extra")

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="accepted",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "inconclusive"
    assert any("outside this ChangeSet" in note for note in outcome.limitations)


def test_accepted_without_a_comparison_is_accepted_and_not_measured(tmp_path):
    stack = _stack(tmp_path)
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    tested = _applied(stack, 102, parent=base, change_set=change_set,
                      origin="change_set")
    before = _run(stack, base, "plain-before", [20, 20, 20, 20])
    after = _run(stack, tested, "plain-after", [10, 10, 10, 10])

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[before[0]], after=[after[0]], comparison=None,
        acceptance="accepted",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "not_measured"
    assert outcome.legacy_outcome.value == "accepted"
    assert outcome.player_acceptance.value == "accepted"


def test_better_but_rejected_keeps_the_players_words(tmp_path):
    stack = _stack(tmp_path)
    base, change_set, tested, before, after, comparison = _improving_run(stack)
    session_id = stack.application.get_segment(
        before[0][0].segment_id, USER,
    ).session_id
    feedback_id = _feedback(stack, "I still do not like how it looks", session_id)

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="rejected",
        feedback_ids=[feedback_id],
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "improved"
    assert outcome.legacy_outcome.value == "improved_but_rejected"
    assert outcome.feedback_ids == [feedback_id]


def _feedback(stack: Stack, words: str, session_id: str) -> str:
    feedback_id = uid(720)
    store_raw(stack.repository, "Feedback", {
        "id": feedback_id,
        "schema_version": SCHEMA,
        "created_at": NOW,
        "created_by_task_id": None,
        "session_id": session_id,
        "segment_id": None,
        "raw_text": words,
        "selections": {},
        "symptoms": [],
        "interpretation_status": "unparsed",
    })
    return feedback_id


def test_three_rounds_within_fluctuation_are_no_clear_change(tmp_path):
    stack = _stack(tmp_path)
    base, change_set, tested, before, after, comparison = _flat_run(stack)

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="unknown",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "no_clear_change"
    assert outcome.legacy_outcome.value == "no_clear_change"


def test_three_consistent_improvements_are_improved(tmp_path):
    stack = _stack(tmp_path)
    base, change_set, tested, before, after, comparison = _improving_run(stack)

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="accepted",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "improved"
    assert outcome.legacy_outcome.value == "accepted"
    assert _external_calls(stack) == []


def test_average_up_but_tail_down_is_inconclusive(tmp_path):
    stack = _stack(tmp_path)
    base, change_set, tested, before, after, comparison = _mixed_run(stack)

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="accepted",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "inconclusive"
    assert any("tradeoff" in note for note in outcome.limitations)


def test_an_exploratory_protocol_is_never_improved(tmp_path):
    stack = _stack(tmp_path)
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    tested = _applied(stack, 102, parent=base, change_set=change_set,
                      origin="change_set")
    before = [_run(stack, base, f"e-before-{i}", [20, 20, 20, 20]) for i in range(3)]
    protocol = _protocol(stack, "protocol-exploratory", exploratory=True)
    stack.clock.advance(60)
    after = [_run(stack, tested, f"e-after-{i}", [10, 10, 10, 10]) for i in range(3)]
    comparison = _comparison(stack, before[0][1], after[0][1], protocol, "cmp-e")

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="accepted",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "inconclusive"
    assert any("exploratory" in note for note in outcome.limitations)


def test_conflicting_summaries_for_one_binding_are_inconclusive(tmp_path):
    stack = _stack(tmp_path)
    base, change_set, tested, before, after, comparison = _improving_run(stack)
    original = after[0][1]
    payload = original.model_dump(mode="json")
    payload["id"] = uid(880)
    for metric in payload["metrics"]:
        if metric["numeric"]["value"] is not None:
            metric["numeric"]["value"] += 1
    store_raw(stack.repository, "PerformanceSummary", payload)

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="accepted",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "inconclusive"
    assert any("conflict" in note for note in outcome.limitations)


def test_a_missing_manifest_is_artifact_unavailable(tmp_path):
    stack = _stack(tmp_path)
    base, change_set, tested, before, after, comparison = _improving_run(stack)
    selected = stack.application.performance.get_summary(
        comparison.summary_ids[0], USER,
    )
    (stack.application.files.objects_dir / selected.manifest_file_id).unlink()

    with pytest.raises(ContractError) as caught:
        _outcome(stack, _outcome_payload(
            change_set_id=change_set, base=base, tested=tested,
            before=[item[0] for item in before], after=[item[0] for item in after],
            comparison=comparison, acceptance="accepted",
        ))

    assert caught.value.code is ErrorCode.ARTIFACT_UNAVAILABLE
    assert _outcomes(stack) == []


def test_the_same_key_replays_the_same_outcome_and_manifest(tmp_path):
    stack = _stack(tmp_path)
    base, change_set, tested, before, after, comparison = _improving_run(stack)
    payload = _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="accepted",
    )

    first = _outcome(stack, payload, key="same-outcome")
    with stack.repository.unit_of_work() as uow:
        before_count = len(uow.iter_objects("ExperimentOutcome"))
    second = _outcome(stack, payload, key="same-outcome")
    with stack.repository.unit_of_work() as uow:
        after_count = len(uow.iter_objects("ExperimentOutcome"))

    assert second.refs[0].entity_id == first.refs[0].entity_id
    assert before_count == after_count == 1
    first_outcome = stack.application.get_outcome(first.refs[0].entity_id, USER)
    artifact = next(
        ref for ref in first_outcome.evidence_refs if ref.kind == "artifact"
    )
    manifest = json.loads(
        stack.application.files.read_managed_file(artifact.file_id)
    )
    assert manifest["effect_policy_version"] == "effect-policy/1"
    assert manifest["selected_summary_ids"] == comparison.summary_ids
    assert manifest["outcome_id"] == first_outcome.id


def test_another_players_outcome_is_not_visible(tmp_path):
    stack = _stack(tmp_path)
    base, change_set, tested, before, after, comparison = _improving_run(stack)
    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="accepted",
    ))

    with pytest.raises(ContractError) as caught:
        stack.application.get_outcome(result.refs[0].entity_id, "c" * 32)

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND


# --- the 04 read-only helper ------------------------------------------------


def test_summaries_for_excludes_open_revisions(tmp_path):
    stack = _stack(tmp_path)
    open_artifact, open_binding = open_session_artifact(
        stack.repository, stack.application.files, stack.application.performance,
        stack.installation, stack.context_id, stack.device_id,
        csv_bytes([10, 20, 30, 40]), "open",
    )
    crafted_summary(
        stack.repository, artifact=open_artifact, binding_refs=[open_binding],
    )

    assert stack.application.performance.summaries_for([open_binding], USER) == []


def test_summaries_for_is_invisible_to_another_user(tmp_path):
    stack = _stack(tmp_path)
    base = _applied(stack, 101)
    binding, _, _ = _run(stack, base, "owned", [20, 20, 20, 20])
    assert len(stack.application.performance.summaries_for([binding], USER)) == 1
    assert stack.application.performance.summaries_for([binding], "c" * 32) == []


# --- repeated samples are rounds, not summary rows --------------------------


def test_three_analyses_of_one_artifact_are_one_round(tmp_path):
    stack = _stack(tmp_path)
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    tested = _applied(stack, 102, parent=base, change_set=change_set,
                      origin="change_set")
    before_binding, before_summary, before_artifact = _run(
        stack, base, "dup-before", [20, 20, 20, 20],
    )
    for index in range(2):
        _analyze_again(stack, before_artifact, before_binding, f"dup-before-{index}")
    protocol = _protocol(stack, "protocol-dup")
    stack.clock.advance(60)
    after_binding, after_summary, after_artifact = _run(
        stack, tested, "dup-after", [10, 10, 10, 10],
    )
    for index in range(2):
        _analyze_again(stack, after_artifact, after_binding, f"dup-after-{index}")
    comparison = _comparison(
        stack, before_summary, after_summary, protocol, "cmp-dup",
    )

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[before_binding], after=[after_binding],
        comparison=comparison, acceptance="accepted",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "inconclusive"
    assert any("rounds" in note for note in outcome.limitations)


def test_an_older_analyzer_round_does_not_count_towards_three(tmp_path):
    stack = _stack(tmp_path)
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    tested = _applied(stack, 102, parent=base, change_set=change_set,
                      origin="change_set")
    before = [
        _run(stack, base, f"v-before-{index}", [20, 20, 20, 20])
        for index in range(3)
    ]
    protocol = _protocol(stack, "protocol-version")
    stack.clock.advance(60)
    after = [
        _run(stack, tested, f"v-after-{index}", [10, 10, 10, 10])
        for index in range(2)
    ]
    # A third round was analysed only under the older rule set: it is a real
    # sample but not one of the current-version valid rounds.
    stale = after[0][1].model_dump(mode="json")
    stale["id"] = uid(890)
    stale["artifact_id"] = uid(891)
    stale["analyzer_version"] = "analyzer/0"
    store_raw(stack.repository, "PerformanceSummary", stale)
    comparison = _comparison(stack, before[0][1], after[0][1], protocol, "cmp-v")

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="accepted",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "inconclusive"
    assert any("rounds" in note for note in outcome.limitations)


def test_an_unbound_binding_is_not_a_valid_round(tmp_path):
    stack = _stack(tmp_path)
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    tested = _applied(stack, 102, parent=base, change_set=change_set,
                      origin="change_set")
    before = [
        _run(stack, base, f"u-before-{index}", [20, 20, 20, 20])
        for index in range(3)
    ]
    protocol = _protocol(stack, "protocol-unbound")
    stack.clock.advance(60)
    after = [
        _run(stack, tested, f"u-after-{index}", [10, 10, 10, 10])
        for index in range(2)
    ]
    loose = _run(stack, None, "u-loose", [10, 10, 10, 10], state="unverified")
    comparison = _comparison(stack, before[0][1], after[0][1], protocol, "cmp-unbound")

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before],
        after=[item[0] for item in after] + [loose[0]],
        comparison=comparison, acceptance="accepted",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "inconclusive"
    assert any("rounds" in note for note in outcome.limitations)


def test_a_recommendation_adoption_above_the_change_set_downgrades(tmp_path):
    stack = _stack(tmp_path)
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    changed = _applied(stack, 106, parent=base, change_set=change_set,
                       origin="change_set")
    tested = _applied(stack, 102, parent=changed, origin="recommendation")
    before = [
        _run(stack, base, f"r-before-{index}", [20, 20, 20, 20])
        for index in range(3)
    ]
    protocol = _protocol(stack, "protocol-recommendation")
    stack.clock.advance(60)
    after = [
        _run(stack, tested, f"r-after-{index}", [10, 10, 10, 10])
        for index in range(3)
    ]
    comparison = _comparison(stack, before[0][1], after[0][1], protocol, "cmp-rec")

    result = _outcome(stack, _outcome_payload(
        change_set_id=change_set, base=base, tested=tested,
        before=[item[0] for item in before], after=[item[0] for item in after],
        comparison=comparison, acceptance="accepted",
    ))

    outcome = stack.application.get_outcome(result.refs[0].entity_id, USER)
    assert outcome.effect_assessment.value == "inconclusive"
    assert any("outside this ChangeSet" in note for note in outcome.limitations)


# --- round fixtures ---------------------------------------------------------


def _flat_run(stack: Stack):
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    tested = _applied(stack, 102, parent=base, change_set=change_set,
                      origin="change_set")
    before = [_run(stack, base, f"f-before-{i}", [20, 20, 20, 20]) for i in range(3)]
    protocol = _protocol(stack, "protocol-flat")
    stack.clock.advance(60)
    after = [_run(stack, tested, f"f-after-{i}", [20, 20, 20, 20]) for i in range(3)]
    comparison = _comparison(stack, before[0][1], after[0][1], protocol, "cmp-flat")
    return base, change_set, tested, before, after, comparison


def _mixed_run(stack: Stack):
    base = _applied(stack, 101)
    change_set = _change_set(stack, base)
    tested = _applied(stack, 102, parent=base, change_set=change_set,
                      origin="change_set")
    before = [_run(stack, base, f"m-before-{i}", [20, 20, 20, 20]) for i in range(3)]
    protocol = _protocol(stack, "protocol-mixed")
    stack.clock.advance(60)
    after = [
        _run(stack, tested, f"m-after-{i}", [10, 10, 10, 40]) for i in range(3)
    ]
    comparison = _comparison(stack, before[0][1], after[0][1], protocol, "cmp-mixed-run")
    return base, change_set, tested, before, after, comparison
