"""REC-T03: publication, impact consumption and the derived review state.

Every fixture is offline: a real verified game, a real context/device and a
package published by the real importer.  The only "provider" is an in-process
stub that answers ``{"candidates": [...]}``; no network, image or game runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import json

import pytest

from game_app.application import Application
from game_app.contracts.common import MutationContext, ResourceResult
from game_app.contracts.enums import (
    CheckStatus,
    GameIdentityStatus,
    RecommendationReadiness,
    ReviewState,
    TaskState,
    TaskType,
)
from game_app.contracts.primitives import utc_now
from game_app.errors import ContractError, ErrorCode
from game_app.knowledge import KnowledgeService
from game_app.recommendation import (
    DECLARED_SCOPE,
    EXECUTABLE_FACTS,
    RecommendationService,
    ReviewService,
    assess_candidates,
    build_content,
    content_hash,
    parse_candidate_reply,
    reduce_readiness,
)
from game_app.tasks import DEFAULT_ROUTES
from tests.product.support.recommendation import (
    SCOPE,
    OTHER,
    build_package,
    confirmed_setting,
    count_objects,
    dataset,
    external_calls,
    package_records,
    record,
    setup,
    source,
    store_applied,
    store_context,
    store_device,
    store_game,
    target_evidence,
    uid,
)


# --- fixtures ---------------------------------------------------------------


def _context(key: str = "recommend-1", *, scope: str = SCOPE) -> MutationContext:
    return MutationContext(
        user_scope_id=scope, idempotency_key=key, request_id=uid(991),
    )


def _ready_records(**overrides):
    """An applicable configuration with a sourced target."""
    records = dataset(extra_evidence=[target_evidence()], **overrides)
    for item in records:
        if item["record_type"] == "configurations":
            item["applicability"] = "applicable"
    return records


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


class CandidateProvider:
    """In-process stub: one ``candidates`` reply, no I/O."""

    def __init__(self, *, reply=None, mode: str = "ok", on_send=None) -> None:
        self.reply = {"candidates": []} if reply is None else reply
        self.mode = mode
        self.on_send = on_send
        self.sends = 0
        self.requests: list[dict] = []

    def send(self, request):
        self.sends += 1
        self.requests.append(dict(request))
        if self.on_send is not None:
            self.on_send(request)
        if self.mode == "unknown":
            raise TimeoutError("simulated unknown outcome after in_flight")
        if self.mode == "reject":
            from game_app.tasks import ProviderRejected

            raise ProviderRejected("simulated provider rejection")
        return self.reply


def _pool_reply(repository, package):
    records = package_records(repository, package)
    return {"candidates": [
        {
            "record_id": record.id,
            "match_class": "direct",
            "reason": "the configuration matches the device",
        }
        for record in records
        if record.record_type == "configurations"
    ]}


@dataclass
class World:
    repository: object
    files: object
    game: object
    context: object
    device: object
    package: object
    applied_id: str | None
    application: Application
    provider: CandidateProvider | None
    task_input: object
    knowledge: object


def _world(
    tmp_path: Path,
    *,
    reply="pool",
    mode: str = "ok",
    applied: bool = True,
    records=None,
    provider: bool = True,
    game_status: GameIdentityStatus = GameIdentityStatus.verified,
):
    repository, files = setup(tmp_path)
    game = store_game(
        repository, status=game_status,
        user=SCOPE if game_status is not GameIdentityStatus.verified else None,
    )
    context = store_context(repository, files, game)
    device = store_device(repository, files)
    prepared = _ready_records() if records is None else records
    package = build_package(repository, files, game, context, records=prepared)
    applied_id = None
    if applied:
        applied_id = store_applied(
            repository, uid_value=920,
            settings=[confirmed_setting(
                repository, files, package, "set-0001", "Low",
            )],
        )
    stub = None
    if provider:
        payload = _pool_reply(repository, package) if reply == "pool" else reply
        stub = CandidateProvider(reply=payload, mode=mode)
    application = Application(
        repository,
        file_root=str(tmp_path / "managed"),
        provider=stub,
    )
    return World(
        repository=repository,
        files=files,
        game=game,
        context=context,
        device=device,
        package=package,
        applied_id=applied_id,
        application=application,
        provider=stub,
        task_input=_input(context, device, package, applied_id=applied_id),
        knowledge=KnowledgeService(repository, files),
    )


def _run(world: World, *, key: str = "recommend-1"):
    accepted = world.application.recommend(world.task_input, _context(key))
    outcome = world.application.run_once()
    return accepted, outcome, world.application.get_task(accepted.task_id)


def _versions(repository) -> list:
    from game_app.contracts.recommendation import RecommendationVersion

    with repository.unit_of_work() as uow:
        return [
            RecommendationVersion.model_validate_json(row.payload_bytes)
            for row in uow.iter_objects("RecommendationVersion")
        ]


def _events(repository) -> list:
    from game_app.contracts.recommendation import RecommendationReviewEvent

    with repository.unit_of_work() as uow:
        return [
            RecommendationReviewEvent.model_validate_json(row.payload_bytes)
            for row in uow.iter_objects("RecommendationReviewEvent")
        ]


def _consumptions(repository) -> list[dict]:
    with repository.unit_of_work() as uow:
        return [dict(row) for row in uow.query(
            "SELECT * FROM impact_consumptions ORDER BY notice_id"
        )]


# --- the task entry ---------------------------------------------------------


def test_recommend_registers_a_recommendation_task_with_the_canonical_route(tmp_path):
    world = _world(tmp_path)
    accepted = world.application.recommend(world.task_input, _context())

    assert DEFAULT_ROUTES["recommendation"] == "/api/recommendations"
    task = world.application.get_task(accepted.task_id)
    assert task.task.type is TaskType.recommendation
    assert task.task.state is TaskState.queued
    # Registration writes nothing else: no version exists before a run.
    assert count_objects(world.repository, "RecommendationVersion") == 0


def test_the_model_request_carries_no_private_input(tmp_path):
    world = _world(tmp_path)
    _run(world)

    request = world.provider.requests[0]["payload"]
    assert set(request) == {
        "task", "declared_scope", "device", "game", "candidates",
    }
    assert set(request["device"]) <= set(EXECUTABLE_FACTS)
    serialised = json.dumps(request)
    # No adopted value, preset, applied id, preference, summary, package id or
    # device id is ever sent; only the public candidate ids and hardware facts.
    assert world.applied_id not in serialised
    assert "Low" not in serialised
    assert "Synthetic Preset" not in serialised
    assert world.package.id not in serialised
    assert world.device.id not in serialised
    assert request["candidates"][0]["local_record_id"] == "cfg-0001"


def test_a_normal_run_publishes_one_version_one_target_and_one_manifest(tmp_path):
    world = _world(tmp_path)
    accepted, outcome, task = _run(world)

    assert outcome is not None and outcome.state is TaskState.completed
    assert task.task.state is TaskState.completed
    assert task.task.domain_result is not None
    assert task.task.domain_result.domain is TaskType.recommendation
    assert count_objects(world.repository, "RecommendationVersion") == 1
    assert count_objects(world.repository, "TargetProposal") == 1
    assert count_objects(world.repository, "RecommendationReviewEvent") == 0

    version = _versions(world.repository)[0]
    assert version.created_by_task_id == accepted.task_id
    assert version.declared_scope == DECLARED_SCOPE
    assert version.candidate_manifest_file_id in {
        ref.entity_id for ref in task.task.result_refs
        if ref.entity_type == "ManagedFile"
    }
    assert world.repository.find_object(
        "ManagedFile", version.candidate_manifest_file_id
    ) is not None
    assert version.actions and version.target_proposal is not None
    assert world.provider.sends == 1


def test_the_embedded_target_id_equals_the_stored_proposal_and_repeats(tmp_path):
    world = _world(tmp_path)
    _run(world)
    first = _versions(world.repository)[0]
    stored = world.repository.find_object("TargetProposal", first.target_proposal.id)

    assert stored is not None
    assert stored.object_id == first.target_proposal.id

    # The same content computed for another task yields the same target id.
    knowledge = KnowledgeService(world.repository, world.files)
    task_input = _input(
        world.context, world.device, world.package, applied_id=world.applied_id,
    )
    assessments = assess_candidates(
        task_input, repository=world.repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    content = build_content(
        task_input, assessments, repository=world.repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    assert content.target_proposal is not None
    assert content.target_proposal.id == first.target_proposal.id


def test_an_unreadable_reply_fails_with_provider_failed_and_no_version(tmp_path):
    world = _world(tmp_path, reply={"unexpected": []})
    _accepted, outcome, task = _run(world)

    assert outcome is not None and outcome.state is TaskState.failed
    assert task.task.last_error_code == ErrorCode.PROVIDER_FAILED.value
    assert count_objects(world.repository, "RecommendationVersion") == 0
    assert count_objects(world.repository, "TargetProposal") == 0
    assert world.provider.sends == 1


def test_an_unknown_call_leaves_the_task_partial_and_never_resends(tmp_path):
    world = _world(tmp_path, mode="unknown")
    _accepted, outcome, task = _run(world)

    assert outcome is not None and outcome.state is TaskState.partial
    assert task.task.last_error_code == ErrorCode.CALL_OUTCOME_UNKNOWN.value
    assert count_objects(world.repository, "RecommendationVersion") == 0
    assert world.provider.sends == 1
    with world.repository.unit_of_work() as uow:
        rows = uow.query("SELECT call_state FROM external_calls ORDER BY rowid")
    assert [row["call_state"] for row in rows] == ["outcome_unknown"]


def test_a_readable_reply_with_no_legal_candidate_publishes_a_blocked_version(tmp_path):
    records = dataset(extra_evidence=[target_evidence()])
    for item in records:
        if item["record_type"] == "configurations":
            # Not accepted by the author, so it never enters the pool.
            item["record_status"] = "rejected"
    world = _world(tmp_path, records=records, reply={"candidates": []})
    _accepted, outcome, task = _run(world)

    assert outcome is not None and outcome.state is TaskState.completed
    assert task.task.domain_result.code == RecommendationReadiness.blocked.value
    version = _versions(world.repository)[0]
    assert version.readiness is RecommendationReadiness.blocked
    assert version.actions == []


def test_a_candidate_reference_outside_the_package_is_kept_as_a_diagnostic(tmp_path):
    world = _world(tmp_path, reply={"candidates": [
        {"record_id": uid(777), "match_class": "direct", "reason": "made up"},
    ]})
    _accepted, outcome, task = _run(world)

    assert outcome is not None and outcome.state is TaskState.completed
    version = _versions(world.repository)[0]
    # Nothing the reply invented is published, and the reply stays readable.
    assert version.readiness is not RecommendationReadiness.actionable
    offence = [
        check for check in version.checks
        if check.check_id == "reference.model_candidates"
    ]
    assert offence and offence[0].status is CheckStatus.fail
    assert "package" in (offence[0].note or "")
    assert version.actions
    assert all(
        action.action.value != "set_value" for action in version.actions
    )


def test_an_unverified_game_revision_never_reaches_actionable(tmp_path):
    world = _world(tmp_path)
    # The package was published while the identity was verified; the context
    # now names a later, merely provisional revision of the same series.
    from game_app.contracts.knowledge import Game
    from tests.product.support.recommendation import NOW, check

    provisional = Game(
        id=uid(612), schema_version="product-contract/0.4", created_at=NOW,
        created_by_task_id=None, series_id=world.game.series_id, version=2,
        canonical_name="Synthetic Game", aliases=[], platform_scope=["steam"],
        identity_status=GameIdentityStatus.provisional,
        identity_checks=[check()], declaring_user_scope_id=SCOPE,
    )
    with world.repository.unit_of_work() as uow:
        uow.insert_versioned("Game", provisional)
    context = store_context(
        world.repository, world.files, provisional,
        uid_value=613, installation=614,
    )
    task_input = _input(
        context, world.device, world.package, applied_id=world.applied_id,
    )
    world.application.recommend(task_input, _context())
    outcome = world.application.run_once()

    assert outcome is not None and outcome.state is TaskState.completed
    version = _versions(world.repository)[0]
    # The gate's verdict is a critical check and must reach the version, not
    # just the candidate manifest.
    identity = [
        check for check in version.checks if check.check_id == "identity.game"
    ]
    assert len(identity) == 1
    assert identity[0].status is CheckStatus.unknown
    assert version.readiness is not RecommendationReadiness.actionable


def test_an_unreadable_constraint_is_kept_in_the_published_checks(tmp_path):
    records = _ready_records()
    records.append(record("cns-0001", "constraints", {
        "id": "cns-0001",
        "kind": "numeric_threshold",
        "setting_id": "set-0001",
        "when": {"vram_gb": 8},
        "requires": {"dlss": True},
        "affected_values": ["Ultra"],
        "verified_scope": True,
    }))
    world = _world(tmp_path, records=records)
    _accepted, outcome, _task = _run(world)

    assert outcome is not None and outcome.state is TaskState.completed
    version = _versions(world.repository)[0]
    constraint = [
        check for check in version.checks if check.check_id == "value.constraint"
    ]
    assert len(constraint) == 1
    # A predicate that cannot be executed is unknown, never silently dropped.
    assert constraint[0].status is CheckStatus.unknown
    # Its evidence names the package that holds the record, not an import
    # snapshot, so a consumer can resolve it.
    assert constraint[0].evidence_refs[0].record.package_id == world.package.id
    assert version.readiness is not RecommendationReadiness.actionable


def test_without_a_provider_the_run_fails_and_writes_no_external_call(tmp_path):
    world = _world(tmp_path, provider=False)
    _accepted, outcome, task = _run(world)

    assert outcome is not None and outcome.state is TaskState.failed
    assert task.task.last_error_code == ErrorCode.PROVIDER_NOT_CONFIGURED.value
    assert task.task.stop_reason.kind == "execution"
    assert task.task.stop_reason.code == "provider_error"
    assert external_calls(world.repository) == []
    assert count_objects(world.repository, "RecommendationVersion") == 0


# --- versions and supersession ----------------------------------------------


def test_publish_is_its_own_authorization_boundary(tmp_path):
    world = _world(tmp_path)
    knowledge = KnowledgeService(world.repository, world.files)
    service = RecommendationService(world.repository, world.files, knowledge)
    task_input = _input(
        world.context, world.device, world.package, applied_id=world.applied_id,
    )
    content = build_content(
        task_input,
        assess_candidates(
            task_input, repository=world.repository, knowledge=knowledge,
            user_scope_id=SCOPE,
        ),
        repository=world.repository, knowledge=knowledge, user_scope_id=SCOPE,
    )

    with pytest.raises(ContractError) as caught:
        service.publish_recommendation(
            task_input, content, user_scope_id=OTHER, task_id=uid(5009),
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert count_objects(world.repository, "RecommendationVersion") == 0
    assert count_objects(world.repository, "TargetProposal") == 0


def test_the_same_task_retry_returns_the_same_version_and_a_new_task_makes_another(tmp_path):
    world = _world(tmp_path)
    knowledge = KnowledgeService(world.repository, world.files)
    service = RecommendationService(
        world.repository, world.files, knowledge, clock=utc_now,
    )
    task_input = _input(
        world.context, world.device, world.package, applied_id=world.applied_id,
    )
    assessments = assess_candidates(
        task_input, repository=world.repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    content = build_content(
        task_input, assessments, repository=world.repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    first = service.publish_recommendation(
        task_input, content, assessments=assessments,
        user_scope_id=SCOPE, task_id=uid(5001),
    )
    retry = service.publish_recommendation(
        task_input, content, assessments=assessments,
        user_scope_id=SCOPE, task_id=uid(5001),
    )
    assert retry.version.id == first.version.id
    assert retry.created is False
    assert count_objects(world.repository, "RecommendationVersion") == 1

    other = service.publish_recommendation(
        task_input, content, assessments=assessments,
        user_scope_id=SCOPE, task_id=uid(5002),
    )
    assert other.version.id != first.version.id
    assert other.version.parent_id == first.version.id
    assert other.version.version == 2
    assert count_objects(world.repository, "RecommendationVersion") == 2


def test_a_new_version_supersedes_the_old_one_and_leaves_it_unchanged(tmp_path):
    world = _world(tmp_path)
    _run(world, key="recommend-1")
    first = _versions(world.repository)[0]
    before = world.repository.get_object(
        "RecommendationVersion", first.id,
    ).payload_bytes

    _run(world, key="recommend-2")
    versions = _versions(world.repository)
    first_again = next(item for item in versions if item.id == first.id)
    second = next(item for item in versions if item.id != first.id)

    assert first_again.parent_id is None
    assert second.parent_id == first.id
    events = [event for event in _events(world.repository)
              if event.recommendation_id == first.id]
    assert len(events) == 1
    assert events[0].previous_state is ReviewState.current
    assert events[0].next_state is ReviewState.superseded
    assert world.repository.get_object(
        "RecommendationVersion", first.id,
    ).payload_bytes == before
    review = ReviewService(world.repository, world.knowledge)
    assert review.derived_state(first.id) is ReviewState.superseded


def test_publishing_never_moves_the_current_applied_pointer(tmp_path):
    world = _world(tmp_path)
    before = world.repository.get_object(
        "AppliedConfiguration", world.applied_id,
    ).payload_bytes
    _run(world)

    assert world.repository.get_object(
        "AppliedConfiguration", world.applied_id,
    ).payload_bytes == before
    assert count_objects(world.repository, "CurrentAppliedEvent") == 0
    assert count_objects(world.repository, "PlaySession") == 0


def test_a_new_recommendation_has_no_review_event_and_reads_as_current(tmp_path):
    world = _world(tmp_path)
    _run(world)
    version = _versions(world.repository)[0]
    review = ReviewService(world.repository, world.knowledge)

    assert _events(world.repository) == []
    assert review.derived_state(version.id) is ReviewState.current


# --- impact consumption ------------------------------------------------------


@dataclass
class Lineage:
    repository: object
    files: object
    game: object
    context: object
    device: object
    old: object
    new: object
    second_new: object
    unrelated: object


def _records_in(scope: str, revision: str):
    """The same dataset, captured from another source scope."""
    records = _ready_records()
    for item in records:
        item["id"] = f"{scope}:{item['local_id']}"
        item["reference_scope"] = scope
        item["source_id"] = scope
        item["revision_id"] = revision
    return records


def _lineage(tmp_path: Path) -> Lineage:
    """Packages of one series; only ``src-0001`` changes between old and new."""
    (tmp_path).mkdir(parents=True, exist_ok=True)
    repository, files = setup(tmp_path)
    game = store_game(repository)
    context = store_context(repository, files, game)
    device = store_device(repository, files)
    sources = lambda first: [  # noqa: E731 - one literal per run
        source("src-0001", "rev-0001", content_hash=first),
        source("src-0002", "rev-0002", content_hash="c" * 64),
    ]
    old = build_package(
        repository, files, game, context,
        key="pkg-old", run_id="run-1", records=_ready_records(),
        sources=sources("a" * 64),
    )
    new = build_package(
        repository, files, game, context,
        key="pkg-new", run_id="run-2", records=_ready_records(), prior=old.id,
        sources=sources("b" * 64),
    )
    second_new = build_package(
        repository, files, game, context,
        key="pkg-new-2", run_id="run-3", records=_ready_records(), prior=old.id,
        sources=sources("d" * 64),
    )
    unrelated = build_package(
        repository, files, game, context,
        key="pkg-unrelated", run_id="run-9",
        records=_records_in("src-0002", "rev-0002"),
        sources=sources("a" * 64),
    )
    return Lineage(
        repository=repository, files=files, game=game, context=context,
        device=device, old=old, new=new, second_new=second_new,
        unrelated=unrelated,
    )


def _notice(repository, files, old, new, *, key="notice-1", request=9000):
    return KnowledgeService(repository, files).notify_impacts(
        old.id, new.id, user_scope_id=SCOPE,
        idempotency_key=key, request_id=uid(request),
    )


def _app_for(lineage: Lineage, tmp_path: Path, package) -> Application:
    """An application whose stub answers with this package's own candidates."""
    return Application(
        lineage.repository,
        file_root=str(tmp_path / "managed"),
        provider=CandidateProvider(reply=_pool_reply(lineage.repository, package)),
    )


def _publish_from(
    lineage: Lineage,
    tmp_path: Path,
    package,
    *,
    uid_value: int = 921,
    key: str = "recommend-1",
):
    applied_id = store_applied(
        lineage.repository, uid_value=uid_value,
        settings=[confirmed_setting(
            lineage.repository, lineage.files, package, "set-0001", "Low",
        )],
    )
    application = _app_for(lineage, tmp_path, package)
    task_input = _input(
        lineage.context, lineage.device, package, applied_id=applied_id,
    )
    application.recommend(task_input, _context(key))
    outcome = application.run_once()
    assert outcome.state is TaskState.completed
    return application, task_input


def _newest(repository):
    return max(_versions(repository), key=lambda item: item.version)


def test_consuming_a_real_impact_notice_marks_the_recommendation_needs_review(tmp_path):
    lineage = _lineage(tmp_path)
    application, _task_input = _publish_from(lineage, tmp_path, lineage.old)
    version = _newest(lineage.repository)
    notice = _notice(lineage.repository, lineage.files, lineage.old, lineage.new)
    assert notice is not None
    before = lineage.repository.get_object(
        "RecommendationVersion", version.id,
    ).payload_bytes

    result = application.consume_impact(notice.id, _context())

    assert isinstance(result, ResourceResult)
    assert result.refs[0].entity_type == "ImpactNotice"
    assert {ref.entity_type for ref in result.refs} >= {
        "ImpactNotice", "RecommendationReviewEvent", "RecommendationVersion",
    }
    events = _events(lineage.repository)
    assert len(events) == 1
    assert events[0].recommendation_id == version.id
    assert events[0].previous_state is ReviewState.current
    assert events[0].next_state is ReviewState.needs_review
    assert events[0].new_package_id == lineage.new.id
    # The content and the current-adoption pointer are untouched.
    assert lineage.repository.get_object(
        "RecommendationVersion", version.id,
    ).payload_bytes == before
    assert count_objects(lineage.repository, "CurrentAppliedEvent") == 0
    assert _consumptions(lineage.repository)[0]["outcome"] == "needs_review"


def test_a_recompute_after_needs_review_supersedes_it_without_restoring_it(tmp_path):
    lineage = _lineage(tmp_path)
    application, _task_input = _publish_from(lineage, tmp_path, lineage.old)
    version = _newest(lineage.repository)
    notice = _notice(lineage.repository, lineage.files, lineage.old, lineage.new)
    application.consume_impact(notice.id, _context())
    assert ReviewService(
        lineage.repository, application.knowledge,
    ).derived_state(version.id) is ReviewState.needs_review

    # A new task recomputes the same content: a new version, not a restore.
    _publish_from(
        lineage, tmp_path, lineage.old, uid_value=922, key="recommend-2",
    )

    versions = _versions(lineage.repository)
    assert len(versions) == 2
    newest = _newest(lineage.repository)
    assert newest.parent_id == version.id
    review = ReviewService(lineage.repository, application.knowledge)
    assert review.derived_state(version.id) is ReviewState.superseded
    assert review.derived_state(newest.id) is ReviewState.current


def test_consuming_the_same_notice_twice_returns_the_same_result(tmp_path):
    lineage = _lineage(tmp_path)
    application, _task_input = _publish_from(lineage, tmp_path, lineage.old)
    notice = _notice(lineage.repository, lineage.files, lineage.old, lineage.new)
    first = application.consume_impact(notice.id, _context())
    second = application.consume_impact(notice.id, _context())

    assert second == first
    assert len(_events(lineage.repository)) == 1
    rows = _consumptions(lineage.repository)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "needs_review"


def test_a_notice_with_no_affected_dependency_is_only_recorded(tmp_path):
    lineage = _lineage(tmp_path)
    # This recommendation cites only ``src-0002`` records, which did not change.
    application, _task_input = _publish_from(
        lineage, tmp_path, lineage.unrelated,
    )
    version = _newest(lineage.repository)
    notice = _notice(lineage.repository, lineage.files, lineage.old, lineage.new)

    result = application.consume_impact(notice.id, _context())

    assert [ref.entity_type for ref in result.refs] == ["ImpactNotice"]
    assert _events(lineage.repository) == []
    rows = _consumptions(lineage.repository)
    assert rows[0]["outcome"] == "no_affected_dependency"
    review = ReviewService(lineage.repository, application.knowledge)
    assert review.derived_state(version.id) is ReviewState.current


def test_an_already_needs_review_recommendation_is_not_marked_twice(tmp_path):
    lineage = _lineage(tmp_path)
    application, _task_input = _publish_from(lineage, tmp_path, lineage.old)
    version = _newest(lineage.repository)
    first = _notice(
        lineage.repository, lineage.files, lineage.old, lineage.new,
    )
    application.consume_impact(first.id, _context())

    # A second notice over the same old package, with a different new one.
    second = _notice(
        lineage.repository, lineage.files, lineage.old, lineage.second_new,
        key="notice-2", request=9001,
    )
    assert second is not None and second.id != first.id
    result = application.consume_impact(second.id, _context())

    assert [ref.entity_type for ref in result.refs] == ["ImpactNotice"]
    assert len(_events(lineage.repository)) == 1
    outcomes = sorted(row["outcome"] for row in _consumptions(lineage.repository))
    assert outcomes == ["already_needs_review", "needs_review"]
    review = ReviewService(lineage.repository, application.knowledge)
    assert review.derived_state(version.id) is ReviewState.needs_review


def test_a_superseded_recommendation_is_not_rewritten(tmp_path):
    lineage = _lineage(tmp_path)
    application, _task_input = _publish_from(lineage, tmp_path, lineage.old)
    first = _newest(lineage.repository)
    # A newer version cites only the unchanged source, and supersedes the first.
    _publish_from(
        lineage, tmp_path, lineage.unrelated, uid_value=922, key="recommend-2",
    )
    review = ReviewService(lineage.repository, application.knowledge)
    assert review.derived_state(first.id) is ReviewState.superseded
    events_before = len(_events(lineage.repository))
    notice = _notice(lineage.repository, lineage.files, lineage.old, lineage.new)

    application.consume_impact(notice.id, _context())

    assert _consumptions(lineage.repository)[0]["outcome"] == "superseded_only"
    assert len(_events(lineage.repository)) == events_before
    assert review.derived_state(first.id) is ReviewState.superseded


def test_another_players_notice_or_recommendation_is_not_visible(tmp_path):
    lineage = _lineage(tmp_path)
    application, _task_input = _publish_from(lineage, tmp_path, lineage.old)
    version = _newest(lineage.repository)
    notice = _notice(lineage.repository, lineage.files, lineage.old, lineage.new)

    with pytest.raises(ContractError) as caught:
        application.get_recommendation(version.id, OTHER)
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    with pytest.raises(ContractError) as caught:
        application.review.consume_impact(notice.id, user_scope_id=OTHER)
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _events(lineage.repository) == []
    assert _consumptions(lineage.repository) == []


def test_a_notice_never_marks_a_recommendation_for_another_game(tmp_path):
    lineage = _lineage(tmp_path)
    application, _task_input = _publish_from(lineage, tmp_path, lineage.old)
    # A second game captured from the same source ids: identical identity
    # triples, a different game.  A notice about the first game must not touch it.
    other_game = store_game(
        lineage.repository, uid_value=610, series=611, name="Other Game",
    )
    other_context = store_context(
        lineage.repository, lineage.files, other_game,
        uid_value=710, installation=711,
    )
    other_package = build_package(
        lineage.repository, lineage.files, other_game, other_context,
        key="pkg-other-game", run_id="run-77", records=_ready_records(),
        sources=[
            source("src-0001", "rev-0001", content_hash="a" * 64),
            source("src-0002", "rev-0002", content_hash="c" * 64),
        ],
    )
    other_app = Application(
        lineage.repository, file_root=str(tmp_path / "managed"),
        provider=CandidateProvider(
            reply=_pool_reply(lineage.repository, other_package)
        ),
    )
    other_applied = store_applied(
        lineage.repository, uid_value=930,
        game_id=other_game.series_id,
        installation_id=uid(711),
        game_context_id=other_context.id,
        settings=[confirmed_setting(
            lineage.repository, lineage.files, other_package, "set-0001", "Low",
        )],
    )
    other_app.recommend(
        _input(
            other_context, lineage.device, other_package, applied_id=other_applied,
        ),
        _context("recommend-other-game"),
    )
    assert other_app.run_once().state is TaskState.completed
    other_version = next(
        item for item in _versions(lineage.repository)
        if item.input.package_id == other_package.id
    )
    notice = _notice(lineage.repository, lineage.files, lineage.old, lineage.new)

    application.consume_impact(notice.id, _context())

    review = ReviewService(lineage.repository, application.knowledge)
    assert review.derived_state(other_version.id) is ReviewState.current
    marked = [event for event in _events(lineage.repository)
              if event.recommendation_id == other_version.id]
    assert marked == []


def test_a_damaged_member_does_not_block_consumption(tmp_path):
    lineage = _lineage(tmp_path)
    application, _task_input = _publish_from(lineage, tmp_path, lineage.old)
    notice = _notice(lineage.repository, lineage.files, lineage.old, lineage.new)
    with lineage.repository.unit_of_work() as uow:
        refs = uow.query(
            "SELECT target_id FROM object_references WHERE owner_id=?"
            " AND target_type='ManagedFile' ORDER BY target_id LIMIT 1",
            (lineage.new.import_snapshot_ids[0],),
        )
    victim = lineage.repository.get_object("ManagedFile", refs[0]["target_id"])
    (lineage.files.root / victim.payload["relative_path"]).unlink()

    with pytest.raises(ContractError) as caught:
        application.knowledge.get_package(lineage.new.id, user_scope_id=SCOPE)
    assert caught.value.code is ErrorCode.ARTIFACT_UNAVAILABLE

    result = application.consume_impact(notice.id, _context())
    assert result.refs[0].entity_type == "ImpactNotice"
    assert len(_events(lineage.repository)) == 1


def test_consuming_an_impact_calls_no_provider(tmp_path):
    lineage = _lineage(tmp_path)
    application, _task_input = _publish_from(lineage, tmp_path, lineage.old)
    notice = _notice(lineage.repository, lineage.files, lineage.old, lineage.new)
    provider = application.tasks.provider
    sends = provider.sends
    before = len(external_calls(lineage.repository))

    application.consume_impact(notice.id, _context())

    assert provider.sends == sends
    assert len(external_calls(lineage.repository)) == before


# --- atomicity and the audit -------------------------------------------------


def test_a_failed_publish_leaves_no_version_target_or_event(tmp_path):
    from game_app.files import PublishHooks
    from tests.product.support.spies import CrashInjector

    world = _world(tmp_path)
    _run(world)
    knowledge = world.knowledge
    service = RecommendationService(world.repository, world.files, knowledge)
    task_input = _input(
        world.context, world.device, world.package, applied_id=world.applied_id,
    )
    assessments = assess_candidates(
        task_input, repository=world.repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    content = build_content(
        task_input, assessments, repository=world.repository, knowledge=knowledge,
        user_scope_id=SCOPE,
    )
    versions_before = count_objects(world.repository, "RecommendationVersion")
    targets_before = count_objects(world.repository, "TargetProposal")
    events_before = count_objects(world.repository, "RecommendationReviewEvent")

    injector = CrashInjector("before_commit", guard_root=tmp_path)
    hooks = PublishHooks(before_commit=injector.hook("before_commit"))
    with pytest.raises(Exception):
        service.publish_recommendation(
            task_input, content, assessments=assessments,
            user_scope_id=SCOPE, task_id=uid(5003), hooks=hooks,
        )

    assert count_objects(world.repository, "RecommendationVersion") == versions_before
    assert count_objects(world.repository, "TargetProposal") == targets_before
    assert count_objects(world.repository, "RecommendationReviewEvent") == events_before


def test_every_provider_send_has_one_external_call_row(tmp_path):
    world = _world(tmp_path)
    _run(world)
    calls = external_calls(world.repository)

    assert len(calls) == world.provider.sends == 1
    assert calls[0]["kind"] == "model"
    assert calls[0]["call_state"] == "succeeded"
    with world.repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == len(calls)


# --- reply parsing and readiness helpers -------------------------------------


def test_the_reply_parser_refuses_anything_but_a_candidates_array():
    from game_app.recommendation import RecommendationProtocolError

    assert parse_candidate_reply({"candidates": [{"record_id": uid(1)}, 7]}) == (
        [{"record_id": uid(1)}], 2,
    )
    for reply in ({}, {"candidates": {}}, [], "text"):
        with pytest.raises(RecommendationProtocolError):
            parse_candidate_reply(reply)


def test_the_content_hash_ignores_checked_at_and_the_version_identity(tmp_path):
    world = _world(tmp_path)
    knowledge = KnowledgeService(world.repository, world.files)
    task_input = _input(
        world.context, world.device, world.package, applied_id=world.applied_id,
    )
    first = build_content(
        task_input,
        assess_candidates(
            task_input, repository=world.repository, knowledge=knowledge,
            user_scope_id=SCOPE,
        ),
        repository=world.repository, knowledge=knowledge, user_scope_id=SCOPE,
        clock=lambda: "2026-11-03T01:00:00+00:00",
    )
    second = build_content(
        task_input,
        assess_candidates(
            task_input, repository=world.repository, knowledge=knowledge,
            user_scope_id=SCOPE,
        ),
        repository=world.repository, knowledge=knowledge, user_scope_id=SCOPE,
        clock=lambda: "2026-11-03T09:00:00+00:00",
    )
    assert reduce_readiness(first.checks, DECLARED_SCOPE) is first.readiness
    assert content_hash(first) == content_hash(second)
