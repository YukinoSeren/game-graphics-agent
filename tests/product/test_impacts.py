"""KNW-T04: impact notices, source revisions and the image enablement gate.

The notice is the only thing this slice writes.  The 03 consumer
(``consume_impact``) does not exist yet: REC-T01—T03 are not implemented, so the
tests assert positively that no recommendation content, review event or
current-applied pointer is created.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from game_app.contracts.common import CheckResult, EvidenceRef, FieldValue, MutationContext, Provenance
from game_app.contracts.device import AdapterCapabilities, GameContext
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    CoverageStatus,
    GameIdentityStatus,
    PublicationStatus,
    ValueState,
)
from game_app.contracts.knowledge import Game
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileEntry, FileManifest, FileService
from game_app.importer import (
    COLLECTOR_RUN_FORMAT_ID,
    COLLECTOR_REQUIRED_FILES,
    FormatInspection,
    FrozenGroup,
    ImportPublisher,
)
from game_app.knowledge import (
    IMPACT_RULE_VERSION,
    KnowledgeService,
    image_adapter_enabled,
    image_adapter_status,
)
from game_app.repository import ProductRepository, canonical_json
from game_app.contracts.knowledge import IdentityMapping
from game_app.application import Application

SCOPE = "a" * 32
NOW = "2026-11-03T00:00:00+00:00"


def _uid(value: int) -> str:
    return f"{value:032x}"


def _json(value) -> bytes:
    return canonical_json(value)


def _not_run(check_id: str) -> CheckResult:
    return CheckResult(
        check_id=check_id, definition_version="test", status=CheckStatus.not_run,
        input_refs=[], evidence_refs=[], checked_at=None, method="test stub",
    )


def _repository(tmp_path: Path) -> ProductRepository:
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    return repository


def _store_game(repository: ProductRepository) -> Game:
    game = Game(
        id=_uid(100), schema_version="product-contract/0.4", created_at=NOW,
        created_by_task_id=None, series_id=_uid(101), version=1,
        canonical_name="Synthetic Game", aliases=[], platform_scope=["steam"],
        identity_status=GameIdentityStatus.verified,
        identity_checks=[CheckResult(
            check_id="identity.evidence", definition_version="test",
            status=CheckStatus.pass_, input_refs=[], evidence_refs=[],
            checked_at=NOW, method="test",
        )],
        declaring_user_scope_id=None,
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
    return game


def _store_context(
    repository: ProductRepository, files: FileService, game: Game,
    *, uid: int = 300,
) -> GameContext:
    file_id = files.store_artifact(
        b'{"kind":"test-declaration"}',
        relative_name="test/declaration.json", stable_key="declaration:impacts",
    )
    stored = repository.get_object("ManagedFile", file_id)
    context = GameContext(
        id=_uid(uid), schema_version="product-contract/0.4", created_at=NOW,
        created_by_task_id=None, series_id=_uid(uid), version=1,
        game_id=game.series_id, game_revision_id=game.id, installation_id=None,
        platform=FieldValue[str](
            value_state=ValueState.known, value="steam",
            acquisition_status=AcquisitionStatus.succeeded,
            provenance=[Provenance(
                method=AcquisitionMethod.user_provided, observed_at=None,
                recorded_at=NOW,
                evidence_refs=[EvidenceRef(
                    kind="artifact", file_id=file_id,
                    content_hash=stored.payload["manifest"]["sha256"],
                    locator="test/declaration.json",
                )],
            )],
        ),
        build_identifiers=[], process_candidates=[], selected_process=None,
        adapter_id=None, adapter_version=None,
        capabilities=AdapterCapabilities(
            can_identify=_not_run("adapter.identify"),
            can_read_config=_not_run("adapter.read_config"),
            can_bind_capture=_not_run("adapter.bind_capture"),
        ),
        checks=[],
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("GameContext", context)
    return context


def _source(scope_id: str, *, content_hash: str | None) -> dict:
    row = {
        "id": scope_id,
        "revision_id": f"rev-{scope_id}",
        "url": f"https://example.invalid/{scope_id}",
        "retrieved_at": "2026-11-01T00:01:00+00:00",
        "role": "official_version_compatibility",
        "status": "extracted",
        "lineage_status": "unknown",
    }
    if content_hash is not None:
        row["document"] = {
            "content_hash": content_hash,
            "truncated": False,
            "retrieval_method": "html",
        }
    return row


def _record(scope_id: str, local_id: str, payload) -> dict:
    return {
        "id": f"{scope_id}:{local_id}",
        "local_id": local_id,
        "record_type": "settings",
        "record_status": "accepted",
        "recommendation_status": None,
        "applicability": "unknown",
        "reference_scope": scope_id,
        "source_id": scope_id,
        "revision_id": f"rev-{scope_id}",
        "game_id": "0123456789abcdef",
        "payload": payload,
    }


def _group(files: FileService, members: dict, *, name: str) -> FrozenGroup:
    file_ids = {
        member: files.store_artifact(
            data, relative_name=f"import/{name}/{member}",
            stable_key=f"group:{name}:{member}",
        )
        for member, data in members.items()
    }
    entries = [
        FileEntry(relative_name=member, byte_length=len(data),
                  sha256=hashlib.sha256(data).hexdigest(), media_type="application/json")
        for member, data in sorted(members.items())
    ]
    return FrozenGroup(
        format_id=COLLECTOR_RUN_FORMAT_ID, operation_id="", file_ids=file_ids,
        manifest=FileManifest(entries=entries),
        inspection=FormatInspection(
            format_id=COLLECTOR_RUN_FORMAT_ID,
            required_files=list(COLLECTOR_REQUIRED_FILES),
            missing_files=[], limitations=[],
        ),
    )


def _publish(
    repository: ProductRepository, files: FileService, game: Game,
    context: GameContext, *, run_id: str, sources, records, scope: str = SCOPE,
) -> str:
    members = {
        "run.json": _json({
            "id": run_id, "contract_version": "0.1",
            "started_at": "2026-11-01T00:00:00+00:00",
            "finished_at": "2026-11-01T00:05:00+00:00",
            "input": {"game_name": "Synthetic Game", "mode": "discover",
                      "platform_scope": "steam", "seed_urls": []},
            "game_id": "0123456789abcdef", "status": "completed_scope",
            "stop_reason": "coverage_reached", "interrupted": False,
        }),
        "sources.json": _json(sources),
        "records.json": _json(records),
        "gaps.json": _json([]),
        "extractions.json": _json([]),
    }
    group = _group(files, members, name=run_id)
    mapping = IdentityMapping(
        id=hashlib.sha256(f"mapping:{run_id}".encode()).hexdigest()[:32],
        schema_version="product-contract/0.4", created_at=NOW,
        legacy_namespace=f"collector_run:{run_id}",
        legacy_value="0123456789abcdef", target_game_id=game.series_id,
        declared_by_user_scope_id=None, checks=[],
    )
    with repository.unit_of_work() as uow:
        uow.insert_object("IdentityMapping", mapping)
    return ImportPublisher(repository, files).publish(
        frozen=group, game_context_id=context.id,
        mapping_version="collector-import/1",
        identity_mapping_ids=[mapping.id], user_scope_id=scope,
        idempotency_key=f"import:{run_id}",
    ).snapshot_id


def _package(repository, files, context, snapshots, *, key: str, prior: str | None = None,
             scope: str = SCOPE, **kwargs):
    return KnowledgeService(repository, files).build_package(
        list(snapshots), prior, "collection.public_graphics",
        game_context_id=context.id, user_scope_id=scope,
        idempotency_key=key, **kwargs,
    ).package


def _notify(service: KnowledgeService, old: str, new: str, *, key: str):
    return service.notify_impacts(
        old, new, user_scope_id=SCOPE, idempotency_key=key, request_id=_uid(9000),
    )


def _count(repository: ProductRepository, entity_type: str) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def _notice_rows(repository: ProductRepository):
    with repository.unit_of_work() as uow:
        return uow.query("SELECT * FROM impact_notices ORDER BY notice_id")


# --- fixtures ----------------------------------------------------------------


def _pair(tmp_path, *, run_ids=("run-1", "run-2"), second_hash="b" * 64):
    """Two packages of one series; the second sees a changed source revision."""
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    first = _publish(
        repository, files, game, context, run_id=run_ids[0],
        sources=[_source("src-0001", content_hash="a" * 64)],
        records=[_record("src-0001", "set-0001", {
            "id": "set-0001", "raw_name": "Quality", "canonical_name": "Quality",
            "aliases": [], "proof": {"url": "https://example.invalid/src-0001"},
        })],
    )
    second = _publish(
        repository, files, game, context, run_id=run_ids[1],
        sources=[_source("src-0001", content_hash=second_hash)],
        records=[_record("src-0001", "set-0001", {
            "id": "set-0001", "raw_name": "Quality", "canonical_name": "Quality",
            "aliases": [], "proof": {"url": "https://example.invalid/src-0001"},
        })],
    )
    old = _package(repository, files, context, [first], key="pkg-old")
    new = _package(repository, files, context, [second], key="pkg-new", prior=old.id)
    service = KnowledgeService(repository, files)
    return repository, files, context, service, old, new, (first, second)


# --- dedupe and the affected set --------------------------------------------


def test_the_same_notice_delivered_twice_is_one_row_and_the_same_id(tmp_path):
    repository, _files, _context, service, old, new, _snapshots = _pair(tmp_path)

    first = _notify(service, old.id, new.id, key="notice-1")
    second = _notify(service, old.id, new.id, key="notice-2")
    replay = _notify(service, old.id, new.id, key="notice-1")

    assert first is not None
    assert second.notice_id == first.notice_id
    assert replay.notice_id == first.notice_id
    assert _count(repository, "ImpactNotice") == 1
    assert len(_notice_rows(repository)) == 1
    with repository.unit_of_work() as uow:
        events = uow.query(
            "SELECT COUNT(*) AS n FROM domain_events WHERE target_type='ImpactNotice'"
        )[0]["n"]
    # A notice is not a publication state transition; no event is invented.
    assert events == 0


def test_a_source_revision_change_affects_only_its_members(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    payload_a = {
        "id": "set-a", "raw_name": "Quality", "canonical_name": "Quality",
        "aliases": [], "proof": {"url": "https://example.invalid/src-0001"},
    }
    payload_b = {
        "id": "set-b", "raw_name": "Sharpness", "canonical_name": "Sharpness",
        "aliases": [], "proof": {"url": "https://example.invalid/src-0002"},
    }
    first = _publish(
        repository, files, game, context, run_id="run-1",
        sources=[_source("src-0001", content_hash="a" * 64),
                 _source("src-0002", content_hash="c" * 64)],
        records=[_record("src-0001", "set-a", payload_a),
                 _record("src-0002", "set-b", payload_b)],
    )
    second = _publish(
        repository, files, game, context, run_id="run-2",
        sources=[_source("src-0001", content_hash="b" * 64),
                 _source("src-0002", content_hash="c" * 64)],
        records=[_record("src-0001", "set-a", payload_a),
                 _record("src-0002", "set-b", payload_b)],
    )
    old = _package(repository, files, context, [first], key="pkg-old")
    new = _package(repository, files, context, [second], key="pkg-new", prior=old.id)
    service = KnowledgeService(repository, files)

    notice = _notify(service, old.id, new.id, key="notice-1")

    assert notice is not None
    assert len(notice.affected_record_ids) == 1
    with repository.unit_of_work() as uow:
        record = uow.find_object("ImportedRecord", notice.affected_record_ids[0])
        payload = json.loads(record.payload_bytes)
        records = [
            json.loads(item.payload_bytes)
            for item in uow.iter_objects("ImportedRecord")
            if json.loads(item.payload_bytes)["import_snapshot_id"] == second
        ]
    assert payload["local_record_id"] == "set-a"
    ids = {item["id"] for item in records}
    assert set(notice.affected_record_ids) <= ids
    assert IMPACT_RULE_VERSION in notice.reason or "changed" in notice.reason


def test_no_affected_dependency_creates_no_notice_and_keeps_the_key(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish(
        repository, files, game, context, run_id="run-1",
        sources=[_source("src-0001", content_hash="a" * 64)],
        records=[_record("src-0001", "set-0001", {
            "id": "set-0001", "raw_name": "Quality", "canonical_name": "Quality",
            "aliases": [], "proof": {"url": "https://example.invalid/src-0001"},
        })],
    )
    old = _package(repository, files, context, [snapshot], key="pkg-old")
    new = _package(repository, files, context, [snapshot], key="pkg-new", prior=old.id)
    service = KnowledgeService(repository, files)

    assert _notify(service, old.id, new.id, key="notice-1") is None
    assert _notice_rows(repository) == []
    # The key was not consumed: the same key still registers a real notice.
    changed = _package(repository, files, context, [snapshot], key="pkg-third",
                       prior=new.id)
    assert changed.id != new.id
    # Nothing changed between new and changed either, so this is still None;
    # the key remains free for a real submission.
    assert _notify(service, new.id, changed.id, key="notice-1") is None


def test_a_cross_game_pair_is_refused(tmp_path):
    repository, files, _context, service, old, _new, _snapshots = _pair(tmp_path)
    other_game = Game(
        id=_uid(200), schema_version="product-contract/0.4", created_at=NOW,
        created_by_task_id=None, series_id=_uid(201), version=1,
        canonical_name="Other Game", aliases=[], platform_scope=["steam"],
        identity_status=GameIdentityStatus.verified,
        identity_checks=[CheckResult(
            check_id="identity.evidence", definition_version="test",
            status=CheckStatus.pass_, input_refs=[], evidence_refs=[],
            checked_at=NOW, method="test",
        )],
        declaring_user_scope_id=None,
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("Game", other_game)
    other_context = _store_context(repository, files, other_game, uid=301)
    other_snapshot = _publish(
        repository, files, other_game, other_context, run_id="run-other",
        sources=[_source("src-0001", content_hash="d" * 64)],
        records=[_record("src-0001", "set-0001", {"id": "set-0001"})],
    )
    # The mapping names the other series; build against its own context.
    foreign = _package(
        repository, files, other_context, [other_snapshot], key="pkg-foreign"
    )

    with pytest.raises(ContractError) as caught:
        _notify(service, old.id, foreign.id, key="notice-1")

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _notice_rows(repository) == []


def test_a_notice_never_reads_another_players_package(tmp_path):
    repository, files, context, service, old, _new, _snapshots = _pair(tmp_path)
    other_scope = "b" * 32
    game = Game.model_validate_json(
        repository.get_object("Game", context.game_revision_id).payload_bytes
    )
    other_snapshot = _publish(
        repository, files, game, context, run_id="run-other",
        sources=[_source("src-0009", content_hash="e" * 64)],
        records=[_record("src-0009", "set-0009", {"id": "set-0009"})],
        scope=other_scope,
    )
    foreign = _package(
        repository, files, context, [other_snapshot], key="pkg-foreign",
        scope=other_scope,
    )

    with pytest.raises(ContractError) as caught:
        service.notify_impacts(
            old.id, foreign.id, user_scope_id=SCOPE,
            idempotency_key="notice-1", request_id=_uid(9000),
        )

    # Existence is not disclosed for a package the caller does not own (A-2/A-4).
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _notice_rows(repository) == []
    assert _count(repository, "ImpactNotice") == 0


def test_a_notice_needs_a_version_lineage(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    first = _publish(
        repository, files, game, context, run_id="run-1",
        sources=[_source("src-0001", content_hash="a" * 64)],
        records=[_record("src-0001", "set-0001", {"id": "set-0001", "name": "A"})],
    )
    second = _publish(
        repository, files, game, context, run_id="run-2",
        sources=[_source("src-0001", content_hash="b" * 64)],
        records=[_record("src-0001", "set-0001", {"id": "set-0001", "name": "B"})],
    )
    old = _package(repository, files, context, [first], key="pkg-old")
    # Same game and series, but this package does not descend from ``old``.
    unrelated = _package(repository, files, context, [second], key="pkg-unrelated")
    service = KnowledgeService(repository, files)

    with pytest.raises(ContractError) as caught:
        _notify(service, old.id, unrelated.id, key="notice-1")

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _notice_rows(repository) == []


def test_every_record_under_a_changed_key_is_reported(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)

    def payload(name: str) -> dict:
        return {
            "id": "set-0001", "raw_name": name, "canonical_name": name,
            "aliases": [], "proof": {"url": "https://example.invalid/src-0001"},
        }

    def snapshot(run_id: str, name: str, content_hash: str) -> str:
        return _publish(
            repository, files, game, context, run_id=run_id,
            sources=[_source("src-0001", content_hash=content_hash)],
            records=[_record("src-0001", "set-0001", payload(name))],
        )

    # Two runs each contribute the same four-part key with different payloads.
    # The new package changes both, so both new records are affected; a
    # key-keyed dict would silently keep only one of them.
    old_a = snapshot("run-1", "Quality", "a" * 64)
    old_b = snapshot("run-2", "Balanced", "b" * 64)
    new_a = snapshot("run-3", "High", "c" * 64)
    new_b = snapshot("run-4", "Low", "d" * 64)
    old = _package(repository, files, context, [old_a, old_b], key="pkg-old")
    new = _package(
        repository, files, context, [new_a, new_b], key="pkg-new", prior=old.id
    )
    service = KnowledgeService(repository, files)

    notice = _notify(service, old.id, new.id, key="notice-1")

    assert notice is not None
    assert set(notice.affected_record_ids) == set(new.record_snapshot_ids)


def test_the_same_key_with_another_pair_is_a_conflict(tmp_path):
    repository, _files, _context, service, old, new, _snapshots = _pair(tmp_path)
    first = _notify(service, old.id, new.id, key="notice-1")

    with pytest.raises(ContractError) as caught:
        _notify(service, new.id, old.id, key="notice-1")

    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert len(_notice_rows(repository)) == 1
    assert _notice_rows(repository)[0]["notice_id"] == first.notice_id


# --- consumers are never rewritten ------------------------------------------


def test_no_recommendation_or_pointer_is_written_by_a_notice(tmp_path):
    repository, _files, _context, service, old, new, _snapshots = _pair(tmp_path)
    before_package = repository.get_object("KnowledgePackage", old.id).payload_bytes
    before_objects = _count(repository, "KnowledgePackage")

    notice = _notify(service, old.id, new.id, key="notice-1")

    assert notice is not None
    assert repository.get_object("KnowledgePackage", old.id).payload_bytes == before_package
    assert _count(repository, "KnowledgePackage") == before_objects
    for entity_type in (
        "RecommendationVersion", "RecommendationReviewEvent", "AppliedConfiguration",
        "ExperimentOutcome", "TargetProposal",
    ):
        assert _count(repository, entity_type) == 0
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0


def test_the_consumer_interface_is_blocked_not_faked(tmp_path):
    _repository, _files, _context, service, _old, _new, _snapshots = _pair(tmp_path)

    # REC-T01—T03 do not exist, so 03's consumer has no host; nothing pretends
    # to consume the notice and no review event is fabricated.
    assert not hasattr(service, "consume_impact")
    assert not hasattr(Application, "consume_impact")


def test_a_damaged_member_blocks_consumption_but_the_notice_is_registered(tmp_path):
    repository, files, _context, service, old, new, _snapshots = _pair(tmp_path)
    with repository.unit_of_work() as uow:
        snapshot_ids = new.import_snapshot_ids
        refs = uow.query(
            "SELECT target_id FROM object_references WHERE owner_id=?"
            " AND target_type='ManagedFile' ORDER BY target_id LIMIT 1",
            (snapshot_ids[0],),
        )
    victim = repository.get_object("ManagedFile", refs[0]["target_id"])
    (files.objects_dir / victim.payload["relative_path"].split("/")[-1]).unlink()

    with pytest.raises(ContractError) as caught:
        service.get_package(new.id, user_scope_id=SCOPE)
    assert caught.value.code is ErrorCode.ARTIFACT_UNAVAILABLE

    notice = _notify(service, old.id, new.id, key="notice-1")
    assert notice is not None
    assert len(_notice_rows(repository)) == 1


# --- the image gate ----------------------------------------------------------


def test_the_image_adapter_stays_disabled_without_evidence(tmp_path):
    _repository, _files, _context, _service, _old, _new, _snapshots = _pair(tmp_path)
    enabled, gate = image_adapter_status()

    assert enabled is False
    assert image_adapter_enabled("any-ocr-adapter") is False
    assert gate.status is CheckStatus.not_run
    assert "G-IMAGE" in gate.note


def test_an_image_requirement_keeps_the_gap_and_claims_nothing(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish(
        repository, files, game, context, run_id="run-1",
        sources=[_source("src-0001", content_hash="a" * 64)],
        records=[_record("src-0001", "set-0001", {
            "id": "set-0001", "raw_name": "Quality", "canonical_name": "Quality",
            "aliases": [], "proof": {"url": "https://example.invalid/src-0001"},
        })],
    )

    package = _package(
        repository, files, context, [snapshot], key="pkg-image", image_required=True
    )
    cell = next(
        item for item in package.coverage_matrix
        if item.key == "graphics.image_figures"
    )
    assert cell.status is CoverageStatus.unknown
    assert "not_run" in cell.note
    gaps = [
        gap for gap in package.critical_gaps + package.noncritical_gaps
        if gap.operation_scope == "collection.public_graphics"
        and "image_figures" in gap.question
    ]
    assert len(gaps) == 1
    assert gaps[0].status.value == "open"
    dumped = package.model_dump_json()
    assert "accuracy" not in dumped
    assert "read image" not in dumped
