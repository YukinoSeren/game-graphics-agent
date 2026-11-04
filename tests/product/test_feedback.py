"""FBK-T01: the original words, explicit preferences and target acceptance.

Every fixture is offline: a synthetic installation, session and provider stub.
The tests assert that the player's text is saved before the model is called,
that a reading is a new linked record rather than an edit, and that a stale
preference or acceptance conflicts instead of overwriting.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from game_app.application import Application
from game_app.contracts.common import EvidenceRef, MutationContext
from game_app.contracts.enums import (
    AcquisitionMethod,
    FrameStreamKind,
    InterpretationStatus,
    PreferenceExplicitness,
    PreferenceLifecycle,
    PreferenceScope,
    ValueState,
)
from game_app.contracts.feedback import (
    Feedback,
    FeedbackInput,
    Preference,
    PreferenceDeclarationInput,
    TargetAcceptanceInput,
)
from game_app.contracts.performance import (
    CreateSessionRequest,
    NumericValueV01,
)
from game_app.contracts.primitives import utc_now
from game_app.contracts.recommendation import TargetProposal
from game_app.errors import ContractError, ErrorCode
from game_app.repository import canonical_json
from tests.product.support.configuration import USER, context_key, setup, store_raw, uid
from tests.product.test_sessions import FakeClock

SCHEMA = "product-contract/0.4"
OTHER = "c" * 32


def test_a_direct_feedback_registration_repeats_the_ownership_checks(tmp_path):
    world = _world(tmp_path)
    typed = _input(world.session_id, world.segment_id)
    # The original words already exist (saved by the owner's registration).
    from game_app.feedback import _original_feedback_id

    store_raw(world.repository, "Feedback", {
        "id": _original_feedback_id(typed),
        "schema_version": SCHEMA,
        "created_at": world.clock(),
        "created_by_task_id": None,
        "session_id": world.session_id,
        "segment_id": world.segment_id,
        "raw_text": typed.raw_text,
        "selections": dict(typed.selections),
        "symptoms": [],
        "interpretation_status": "unparsed",
    })
    # A task then arrives through the generic entry claiming another scope: it
    # must not read the original's words or write a derived interpretation.
    accepted = world.application.register_task(
        typed, MutationContext(
            user_scope_id=OTHER, idempotency_key="foreign", request_id=uid(997),
        ),
    )
    world.application.run_once()
    view = world.application.get_task(accepted.task_id)

    assert view.task.state.value == "failed"
    assert view.error.code is ErrorCode.RESOURCE_NOT_FOUND
    derived = [
        item for item in _objects(world.repository, "Feedback")
        if item["interpretation_status"] != "unparsed"
    ]
    assert derived == []


# --- fixtures ---------------------------------------------------------------


class FeedbackProvider:
    """In-process stub: one interpretation reply, no I/O."""

    def __init__(self, *, reply=None, mode: str = "ok", on_send=None) -> None:
        self.reply = {"symptoms": []} if reply is None else reply
        self.mode = mode
        self.on_send = on_send
        self.sends = 0
        self.requests: list[dict] = []

    def send(self, request):
        self.sends += 1
        self.requests.append(dict(request))
        if self.on_send is not None:
            self.on_send(request)
        if self.mode == "reject":
            from game_app.tasks import ProviderRejected

            raise ProviderRejected("simulated provider rejection")
        return self.reply


class World:
    def __init__(self, repository, application, provider, session_id, segment_id,
                 installation, context_id, device_id, clock):
        self.repository = repository
        self.application = application
        self.provider = provider
        self.session_id = session_id
        self.segment_id = segment_id
        self.installation = installation
        self.context_id = context_id
        self.device_id = device_id
        self.clock = clock


def _world(tmp_path, *, reply=None, mode="ok", provider=True, on_send=None) -> World:
    repository, files, installation, context_id, device_id = setup(tmp_path)
    clock = FakeClock()
    stub = None
    if provider:
        stub = FeedbackProvider(reply=reply, mode=mode, on_send=on_send)
    application = Application(
        repository, file_root=str(tmp_path / "managed"), provider=stub, clock=clock,
    )
    session_id, segment_id = _session(
        application, installation, context_id, device_id,
    )
    return World(
        repository, application, stub, session_id, segment_id,
        installation, context_id, device_id, clock,
    )


def _session(application, installation, context_id, device_id, *,
             recommendation_id=None, key="session-1"):
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
        "recommendation_id": recommendation_id,
    }))
    result = application.create_session(request, context_key(key))
    return (
        next(ref.entity_id for ref in result.refs if ref.entity_type == "PlaySession"),
        next(ref.entity_id for ref in result.refs if ref.entity_type == "SessionSegment"),
    )


def _input(session_id, segment_id=None, **overrides) -> FeedbackInput:
    payload = {
        "kind": "feedback",
        "session_id": session_id,
        "segment_id": segment_id,
        "raw_text": "the frames stutter when the scene loads",
        "selections": {"feeling": "want_improvement"},
    }
    payload.update(overrides)
    return FeedbackInput.model_validate_json(canonical_json(payload))


def _run(world: World, task_input=None, *, key="feedback-1", scope=USER):
    task_input = _input(world.session_id, world.segment_id) if task_input is None else task_input
    context = MutationContext(
        user_scope_id=scope, idempotency_key=key, request_id=uid(999),
    )
    accepted = world.application.record_feedback(task_input, context)
    outcome = world.application.run_once()
    return accepted, outcome, world.application.get_task(accepted.task_id)


def _objects(repository, entity_type):
    with repository.unit_of_work() as uow:
        return [json.loads(row["payload"]) for row in uow.query(
            "SELECT payload FROM objects WHERE entity_type=? ORDER BY created_at, rowid",
            (entity_type,),
        )]


def _subdir(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def _feedback(world, feedback_id) -> Feedback:
    return world.application.get_feedback(feedback_id, USER)


def _derived(world):
    derived = [item for item in _objects(world.repository, "Feedback")
               if item["interpretation_status"] != "unparsed"]
    assert len(derived) == 1
    return derived[0]


def _inferred_reply():
    return {"symptoms": [{
        "kind": "stutter",
        "severity": "moderate",
        "range": "scene load",
        "interpretation": "inferred",
    }]}


# --- FBK-V01: the original is a fact, the reading is appended -------------


def test_the_original_is_saved_before_the_model_is_called(tmp_path):
    seen: list[list[dict]] = []
    world = _world(tmp_path, reply=_inferred_reply())
    world.provider.on_send = lambda request: seen.append(
        _objects(world.repository, "Feedback")
    )
    accepted, outcome, view = _run(world)
    assert seen, "the provider was never called"
    assert [item["interpretation_status"] for item in seen[0]] == ["unparsed"]
    originals = [item for item in seen[0]]
    assert originals[0]["created_by_task_id"] == accepted.task_id
    assert originals[0]["raw_text"] == "the frames stutter when the scene loads"
    assert originals[0]["selections"] == {"feeling": "want_improvement"}
    assert view.task.state.value == "completed"


def test_an_empty_feedback_is_refused_before_the_key_is_consumed(tmp_path):
    world = _world(tmp_path)
    with pytest.raises(ContractError) as error:
        world.application.record_feedback(
            {
                "kind": "feedback",
                "session_id": world.session_id,
                "segment_id": None,
                "raw_text": "   ",
                "selections": {},
            },
            context_key("empty-1"),
        )
    assert error.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert world.repository.find_idempotency(
        USER, "POST", "/api/feedback", "empty-1",
    ) is None


def test_a_session_from_another_user_is_invisible(tmp_path):
    world = _world(tmp_path)
    with pytest.raises(ContractError) as error:
        world.application.record_feedback(
            _input(world.session_id, world.segment_id),
            MutationContext(
                user_scope_id=OTHER, idempotency_key="other-1",
                request_id=uid(998),
            ),
        )
    assert error.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_a_segment_from_another_session_is_refused(tmp_path):
    world = _world(tmp_path)
    other_session, other_segment = _session(
        world.application, world.installation, world.context_id, world.device_id,
        key="session-2",
    )
    with pytest.raises(ContractError) as error:
        world.application.record_feedback(
            _input(world.session_id, other_segment), context_key("segment-1"),
        )
    assert error.value.code is ErrorCode.REFERENCE_INVALID


def test_a_provider_failure_keeps_the_original_unparsed(tmp_path):
    world = _world(tmp_path, mode="reject")
    accepted, outcome, view = _run(world)
    assert view.task.state.value == "partial"
    assert view.task.domain_result.code == "unparsed"
    assert view.task.last_error_code == ErrorCode.PROVIDER_FAILED.value
    original = _feedback(world, view.task.result_refs[0].entity_id)
    assert original.interpretation_status is InterpretationStatus.unparsed
    assert original.raw_text == "the frames stutter when the scene loads"
    assert _objects(world.repository, "FeedbackInterpretationLink") == []
    assert world.provider.sends == 1


def test_an_unreadable_reply_is_not_guessed(tmp_path):
    world = _world(tmp_path, reply={"mood": "fine"})
    accepted, outcome, view = _run(world)
    assert view.task.state.value == "partial"
    assert view.task.domain_result.code == "unparsed"
    assert _objects(world.repository, "Feedback")[0]["symptoms"] == []
    assert _objects(world.repository, "FeedbackInterpretationLink") == []


def test_a_provider_not_configured_keeps_the_words_unparsed(tmp_path):
    world = _world(tmp_path, provider=False)
    accepted, outcome, view = _run(world)
    assert view.task.state.value == "partial"
    assert view.task.domain_result.code == "unparsed"
    assert view.task.last_error_code == ErrorCode.PROVIDER_NOT_CONFIGURED.value
    assert _feedback(world, view.task.result_refs[0].entity_id).raw_text


def test_a_successful_parse_links_a_new_feedback_and_keeps_the_original(tmp_path):
    world = _world(tmp_path, reply=_inferred_reply())
    accepted, outcome, view = _run(world)
    assert view.task.state.value == "completed"
    assert view.task.domain_result.code == "inferred"
    original = [item for item in _objects(world.repository, "Feedback")
                if item["interpretation_status"] == "unparsed"][0]
    derived = _derived(world)
    link = _objects(world.repository, "FeedbackInterpretationLink")[0]
    assert link["source_feedback_id"] == original["id"]
    assert link["derived_feedback_id"] == derived["id"]
    assert link["task_id"] == accepted.task_id
    assert derived["raw_text"] == original["raw_text"]
    assert derived["symptoms"][0]["kind"] == "stutter"
    # The request carries the player's words, never the session identity.
    payload = world.provider.requests[0]["payload"]
    assert payload == {
        "raw_text": "the frames stutter when the scene loads",
        "selections": {"feeling": "want_improvement"},
        "segment_present": True,
    }


def test_an_unknown_severity_stays_unknown_and_a_known_one_is_a_reading(tmp_path):
    world = _world(tmp_path, reply=_inferred_reply())
    _run(world)
    derived = Feedback.model_validate_json(canonical_json(_derived(world)))
    symptom = derived.symptoms[0]
    assert symptom.severity.value_state is ValueState.known
    assert symptom.severity.value == "moderate"
    assert symptom.severity.provenance[0].method is AcquisitionMethod.agent_derived
    assert symptom.severity.provenance[0].evidence_refs[0].entity_type == "Feedback"
    assert symptom.range.value_state is ValueState.known

    world2 = _world(_subdir(tmp_path, "second"), reply={"symptoms": [{
        "kind": "blur", "interpretation": "uncertain",
    }]})
    _run(world2)
    symptom2 = Feedback.model_validate_json(canonical_json(_derived(world2))).symptoms[0]
    assert symptom2.severity.value_state is ValueState.unknown
    assert symptom2.severity.value is None
    assert symptom2.range.value_state is ValueState.unknown
    assert symptom2.severity.provenance == []


def test_a_second_parse_appends_another_interpretation(tmp_path):
    world = _world(tmp_path, reply=_inferred_reply())
    first, _, _ = _run(world, key="feedback-1")
    second, _, _ = _run(world, key="feedback-2")
    links = _objects(world.repository, "FeedbackInterpretationLink")
    assert len(links) == 2
    assert {link["task_id"] for link in links} == {
        first.task_id, second.task_id,
    }
    originals = [item for item in _objects(world.repository, "Feedback")
                 if item["interpretation_status"] == "unparsed"]
    assert len(originals) == 1
    assert originals[0]["id"] == links[0]["source_feedback_id"]


def test_the_same_key_replays_the_same_task(tmp_path):
    world = _world(tmp_path, reply=_inferred_reply())
    first, _, _ = _run(world, key="feedback-1")
    again = world.application.record_feedback(
        _input(world.session_id, world.segment_id),
        context_key("feedback-1"),
    )
    assert again.task_id == first.task_id
    assert world.provider.sends == 1


# --- FBK-V02: explicit preference versions ---------------------------------


def _preference_payload(world, **overrides):
    payload = {
        "series_id": None,
        "expected_current_id": None,
        "feedback_id": world.feedback_id,
        "scope": "global",
        "scope_id": None,
        "content": "prefer image quality over frame rate",
        "lifecycle": "active",
    }
    payload.update(overrides)
    return PreferenceDeclarationInput.model_validate_json(canonical_json(payload))


def _feedback_world(tmp_path, **kwargs) -> World:
    world = _world(tmp_path, reply=_inferred_reply(), **kwargs)
    _, _, view = _run(world)
    world.feedback_id = view.task.result_refs[0].entity_id
    return world


def _preferences(repository):
    return sorted(
        (Preference.model_validate_json(canonical_json(item))
         for item in _objects(repository, "Preference")),
        key=lambda item: item.version,
    )


def test_a_new_explicit_preference_starts_version_one(tmp_path):
    world = _feedback_world(tmp_path)
    result = world.application.declare_preference(
        _preference_payload(world), context_key("pref-1"),
    )
    assert {ref.entity_type for ref in result.refs} == {
        "Preference", "PreferenceDeclaration",
    }
    stored = _preferences(world.repository)
    assert len(stored) == 1
    assert stored[0].version == 1
    assert stored[0].parent_id is None
    assert stored[0].explicitness is PreferenceExplicitness.explicit
    assert stored[0].scope is PreferenceScope.global_
    assert stored[0].scope_id is None
    declaration = _objects(world.repository, "PreferenceDeclaration")[0]
    assert declaration["source_feedback_id"] == world.feedback_id
    assert declaration["input"]["content"] == "prefer image quality over frame rate"


def test_a_global_preference_cannot_carry_a_scope_id():
    with pytest.raises(ValidationError):
        PreferenceDeclarationInput.model_validate({
            "series_id": None,
            "expected_current_id": None,
            "feedback_id": uid(1),
            "scope": "global",
            "scope_id": uid(2),
            "content": "x",
            "lifecycle": "active",
        })


def test_a_game_preference_requires_an_existing_game(tmp_path):
    world = _feedback_world(tmp_path)
    with pytest.raises(ContractError) as error:
        world.application.declare_preference(
            _preference_payload(world, scope="game", scope_id=uid(4242)),
            context_key("pref-game"),
        )
    assert error.value.code is ErrorCode.REFERENCE_INVALID
    assert _preferences(world.repository) == []


def test_another_users_feedback_cannot_be_declared(tmp_path):
    world = _feedback_world(tmp_path)
    with pytest.raises(ContractError) as error:
        world.application.declare_preference(
            _preference_payload(world),
            MutationContext(
                user_scope_id=OTHER, idempotency_key="pref-other",
                request_id=uid(997),
            ),
        )
    assert error.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_a_correction_with_a_stale_current_id_conflicts(tmp_path):
    world = _feedback_world(tmp_path)
    first = world.application.declare_preference(
        _preference_payload(world), context_key("pref-1"),
    )
    current = first.refs[0].entity_id
    corrected = world.application.declare_preference(
        _preference_payload(
            world, series_id=first.refs[0].entity_id, expected_current_id=current,
            content="prefer frame rate",
        ),
        context_key("pref-2"),
    )
    assert corrected.refs[0].entity_id != current
    with pytest.raises(ContractError) as error:
        world.application.declare_preference(
            _preference_payload(
                world, series_id=first.refs[0].entity_id,
                expected_current_id=current, content="stale correction",
            ),
            context_key("pref-3"),
        )
    assert error.value.code is ErrorCode.PREFERENCE_VERSION_CONFLICT
    stored = _preferences(world.repository)
    assert [item.version for item in stored] == [1, 2]
    assert stored[1].content == "prefer frame rate"


def test_a_withdrawal_appends_a_version_and_keeps_the_old_one(tmp_path):
    world = _feedback_world(tmp_path)
    first = world.application.declare_preference(
        _preference_payload(world), context_key("pref-1"),
    )
    series = first.refs[0].entity_id
    second = world.application.declare_preference(
        _preference_payload(
            world, series_id=series, expected_current_id=series,
            lifecycle="withdrawn",
        ),
        context_key("pref-2"),
    )
    again = world.application.declare_preference(
        _preference_payload(
            world, series_id=series, expected_current_id=series,
            lifecycle="withdrawn",
        ),
        context_key("pref-2"),
    )
    assert [ref.entity_id for ref in again.refs] == [
        ref.entity_id for ref in second.refs
    ]
    stored = _preferences(world.repository)
    assert [item.lifecycle for item in stored] == [
        PreferenceLifecycle.active, PreferenceLifecycle.withdrawn,
    ]
    assert stored[0].id == series
    assert stored[1].parent_id == series
    assert stored[0].lifecycle is PreferenceLifecycle.active


def test_a_new_preference_cannot_start_withdrawn():
    with pytest.raises(ValidationError):
        PreferenceDeclarationInput.model_validate({
            "series_id": None,
            "expected_current_id": None,
            "feedback_id": uid(1),
            "scope": "global",
            "scope_id": None,
            "content": "x",
            "lifecycle": "withdrawn",
        })


# --- FBK-V02: target acceptance --------------------------------------------


def _recommendation_stub(repository, *, task_id, proposal_id, key="rec-1"):
    recommendation_id = uid(700 + len(_objects(repository, "RecommendationVersion")))
    store_raw(repository, "RecommendationVersion", {
        "id": recommendation_id,
        "created_by_task_id": task_id,
        "target_proposal": {"id": proposal_id},
    })
    return recommendation_id


def _proposal(repository, *, task_id, feedback_id):
    proposal = TargetProposal(
        id=uid(800 + len(_objects(repository, "TargetProposal"))),
        schema_version=SCHEMA,
        created_at=utc_now(),
        created_by_task_id=task_id,
        value=NumericValueV01(
            number_kind="exact", value=60, lower=None, upper=None,
            unknown_reason=None,
        ),
        basis_refs=[EvidenceRef(
            kind="local", entity_type="Feedback", entity_id=feedback_id,
        )],
        frame_stream_kind=FrameStreamKind.application_present,
        note="the session's target",
    )
    store_raw(repository, "TargetProposal", proposal.model_dump(mode="json"))
    return proposal.id


def _acceptance_world(tmp_path):
    world = _feedback_world(tmp_path)
    task_id = _objects(world.repository, "Feedback")[0]["created_by_task_id"]
    proposal_id = _proposal(
        world.repository, task_id=task_id, feedback_id=world.feedback_id,
    )
    recommendation_id = _recommendation_stub(
        world.repository, task_id=task_id, proposal_id=proposal_id,
    )
    session_id, segment_id = _session(
        world.application, world.installation, world.context_id, world.device_id,
        recommendation_id=recommendation_id, key="acceptance-session",
    )
    world.session_id = session_id
    world.segment_id = segment_id
    world.proposal_id = proposal_id
    world.feedback_id = None
    _, _, view = _run(world, key="acceptance-feedback")
    world.feedback_id = view.task.result_refs[0].entity_id
    return world


def _acceptance(world, **overrides) -> TargetAcceptanceInput:
    payload = {
        "proposal_id": world.proposal_id,
        "session_id": world.session_id,
        "accepted": True,
        "feedback_id": world.feedback_id,
        "supersedes_id": None,
    }
    payload.update(overrides)
    return TargetAcceptanceInput.model_validate_json(canonical_json(payload))


def test_acceptance_requires_the_session_to_name_the_recommendation(tmp_path):
    world = _feedback_world(tmp_path)
    task_id = _objects(world.repository, "Feedback")[0]["created_by_task_id"]
    proposal_id = _proposal(
        world.repository, task_id=task_id, feedback_id=world.feedback_id,
    )
    with pytest.raises(ContractError) as error:
        world.application.record_target_acceptance(
            TargetAcceptanceInput(
                proposal_id=proposal_id,
                session_id=world.session_id,
                accepted=True,
                feedback_id=world.feedback_id,
                supersedes_id=None,
            ),
            context_key("accept-1"),
        )
    assert error.value.code is ErrorCode.REFERENCE_INVALID


def test_acceptance_is_recorded_and_the_projection_advances(tmp_path):
    world = _acceptance_world(tmp_path)
    result = world.application.record_target_acceptance(
        _acceptance(world), context_key("accept-1"),
    )
    assert [ref.entity_type for ref in result.refs] == ["TargetAcceptance"]
    stored = _objects(world.repository, "TargetAcceptance")
    assert len(stored) == 1
    assert stored[0]["accepted"] is True
    with world.repository.unit_of_work() as uow:
        row = uow.execute(
            "SELECT * FROM target_acceptance_current WHERE session_id=? AND proposal_id=?",
            (world.session_id, world.proposal_id),
        ).fetchone()
    assert row["acceptance_id"] == result.refs[0].entity_id
    assert row["revision"] == 1


def test_a_stale_supersedes_conflicts_instead_of_overwriting(tmp_path):
    world = _acceptance_world(tmp_path)
    first = world.application.record_target_acceptance(
        _acceptance(world), context_key("accept-1"),
    )
    with pytest.raises(ContractError) as error:
        world.application.record_target_acceptance(
            _acceptance(world, accepted=False, supersedes_id=uid(999)),
            context_key("accept-2"),
        )
    assert error.value.code is ErrorCode.TARGET_ACCEPTANCE_CONFLICT
    assert len(_objects(world.repository, "TargetAcceptance")) == 1
    with world.repository.unit_of_work() as uow:
        row = uow.execute(
            "SELECT * FROM target_acceptance_current WHERE session_id=? AND proposal_id=?",
            (world.session_id, world.proposal_id),
        ).fetchone()
    assert row["acceptance_id"] == first.refs[0].entity_id


def test_a_superseding_acceptance_keeps_the_previous_record(tmp_path):
    world = _acceptance_world(tmp_path)
    first = world.application.record_target_acceptance(
        _acceptance(world), context_key("accept-1"),
    )
    second = world.application.record_target_acceptance(
        _acceptance(
            world, accepted=False, supersedes_id=first.refs[0].entity_id,
        ),
        context_key("accept-2"),
    )
    stored = _objects(world.repository, "TargetAcceptance")
    assert len(stored) == 2
    assert stored[0]["accepted"] is True
    assert stored[1]["accepted"] is False
    assert stored[1]["supersedes_id"] == first.refs[0].entity_id
    with world.repository.unit_of_work() as uow:
        row = uow.execute(
            "SELECT * FROM target_acceptance_current WHERE session_id=? AND proposal_id=?",
            (world.session_id, world.proposal_id),
        ).fetchone()
        events = uow.query(
            "SELECT * FROM target_acceptance_events ORDER BY pointer_revision",
        )
    assert row["acceptance_id"] == second.refs[0].entity_id
    assert row["revision"] == 2
    assert len(events) == 2


def test_acceptance_without_a_prior_record_rejects_supersedes(tmp_path):
    world = _acceptance_world(tmp_path)
    with pytest.raises(ContractError) as error:
        world.application.record_target_acceptance(
            _acceptance(world, supersedes_id=uid(777)),
            context_key("accept-1"),
        )
    assert error.value.code is ErrorCode.TARGET_ACCEPTANCE_CONFLICT
    assert _objects(world.repository, "TargetAcceptance") == []
