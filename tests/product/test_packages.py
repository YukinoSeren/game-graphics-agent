"""KNW-T02: coverage reduction, dependency closure and package publication.

Every package here is built from a real frozen ``collector-run/v0.1`` group
through the real importer: the coverage cells are reduced from the published
``ImportedRecord`` rows, not from a hand-made package.  No provider, network or
game is involved.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from game_app.application import Application
from game_app.contracts.common import (
    CheckResult,
    EvidenceRef,
    FieldValue,
    MutationContext,
    Provenance,
)
from game_app.contracts.device import AdapterCapabilities, GameContext
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    CoverageStatus,
    GameIdentityStatus,
    PublicationStatus,
    TaskState,
    ValueState,
)
from game_app.contracts.knowledge import (
    CoverageCell,
    Game,
    IdentityMapping,
    ImportSnapshot,
    KnowledgePackage,
    SnapshotFileEntry,
    SnapshotManifest,
)
from game_app.contracts.primitives import utc_now
from game_app.contracts.tasks import KnowledgeInput
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
    KNOWLEDGE_TASK_SCOPE,
    CoverageEvaluator,
    KnowledgeService,
    image_adapter_status,
)
from game_app.repository import ProductRepository, canonical_json
from game_app.tasks import TaskService
from tests.product.support.spies import SpyProvider

SCOPE = "a" * 32
NOW = "2026-11-03T00:00:00+00:00"


def _uid(value: int) -> str:
    return f"{value:032x}"


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _check(status: CheckStatus = CheckStatus.pass_) -> CheckResult:
    return CheckResult(
        check_id="identity.evidence",
        definition_version="identity-checks/0.4",
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else NOW,
        method="synthetic test check",
    )


def _not_run(check_id: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version="test",
        status=CheckStatus.not_run,
        input_refs=[],
        evidence_refs=[],
        checked_at=None,
        method="test stub",
    )


# --- fixtures ----------------------------------------------------------------


def _repository(tmp_path: Path) -> ProductRepository:
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    return repository


def _store_game(
    repository: ProductRepository,
    *,
    uid: int = 100,
    series: int = 101,
    status: GameIdentityStatus = GameIdentityStatus.verified,
    platforms: tuple[str, ...] = ("steam",),
    user: str | None = None,
) -> Game:
    game = Game(
        id=_uid(uid),
        schema_version="product-contract/0.4",
        created_at=NOW,
        created_by_task_id=None,
        series_id=_uid(series),
        version=1,
        canonical_name="Synthetic Game",
        aliases=[],
        platform_scope=list(platforms),
        identity_status=status,
        identity_checks=[_check()],
        declaring_user_scope_id=user,
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
    return game


def _declaration_evidence(files: FileService, key: str) -> EvidenceRef:
    file_id = files.store_artifact(
        b'{"kind":"test-declaration"}',
        relative_name="test/declaration.json",
        stable_key=f"declaration:{key}",
    )
    stored = files.repository.get_object("ManagedFile", file_id)
    return EvidenceRef(
        kind="artifact",
        file_id=file_id,
        content_hash=stored.payload["manifest"]["sha256"],
        locator="test/declaration.json",
    )


def _store_context(
    repository: ProductRepository,
    files: FileService,
    game: Game,
    *,
    uid: int = 300,
    platform: str | None = "steam",
) -> GameContext:
    if platform is None:
        platform_value = FieldValue[str](
            value_state=ValueState.unknown,
            value=None,
            unknown_reason="not_provided",
            acquisition_status=AcquisitionStatus.not_attempted,
        )
    else:
        platform_value = FieldValue[str](
            value_state=ValueState.known,
            value=platform,
            acquisition_status=AcquisitionStatus.succeeded,
            provenance=[Provenance(
                method=AcquisitionMethod.user_provided,
                observed_at=None,
                recorded_at=NOW,
                evidence_refs=[_declaration_evidence(files, f"context-{uid}")],
            )],
        )
    context = GameContext(
        id=_uid(uid),
        schema_version="product-contract/0.4",
        created_at=NOW,
        created_by_task_id=None,
        series_id=_uid(uid),
        version=1,
        game_id=game.series_id,
        game_revision_id=game.id,
        installation_id=None,
        platform=platform_value,
        build_identifiers=[],
        process_candidates=[],
        selected_process=None,
        adapter_id=None,
        adapter_version=None,
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


def _source(scope_id: str = "src-0001", revision_id: str = "rev-0001") -> dict:
    return {
        "id": scope_id,
        "revision_id": revision_id,
        "url": f"https://example.invalid/{scope_id}",
        "retrieved_at": "2026-11-01T00:01:00+00:00",
        "role": "official_version_compatibility",
        "status": "extracted",
        "lineage_status": "unknown",
    }


def _record(local_id: str, record_type: str, payload, *, status: str = "accepted", **extra):
    row = {
        "id": f"src-0001:{local_id}",
        "local_id": local_id,
        "record_type": record_type,
        "record_status": status,
        "recommendation_status": "candidate" if record_type == "configurations" else None,
        "applicability": "unknown",
        "reference_scope": "src-0001",
        "source_id": "src-0001",
        "revision_id": "rev-0001",
        "game_id": "0123456789abcdef",
        "payload": payload,
    }
    row.update(extra)
    return row


def _dataset(*, configs: int = 1) -> list[dict]:
    """A coherent accepted subset: one setting, one measurement, N configurations."""
    records = [
        _record("set-0001", "settings", {
            "id": "set-0001",
            "raw_name": "Synthetic Quality",
            "canonical_name": "Quality",
            "aliases": [],
            "proof": {"url": "https://example.invalid/src-0001"},
        }),
        _record("evi-0001", "evidence", {
            "id": "evi-0001",
            "kind": "measurement",
            "origin": "source_statement",
            "statement": "synthetic average frame rate",
            "metric": "average_fps",
            "numeric": {"number_kind": "exact", "value": 60},
            "unit": "fps",
            "setting_ids": [],
            "input_ids": [],
            "detected_state": None,
        }),
        _record("env-0001", "environments", {
            "id": "env-0001",
            "fields": {"gpu": {"value": "Synthetic GPU", "proof": {"url": "x"}}},
        }),
    ]
    for index in range(configs):
        records.append(_record(f"cfg-{index + 1:04d}", "configurations", {
            "id": f"cfg-{index + 1:04d}",
            "name": f"Preset {index + 1}",
            "kind": "resolved_configuration",
            "assignments": [{
                "setting_id": "set-0001",
                "value": {"value_kind": "fixed",
                          "value": "High" if index == 0 else "Ultra"},
                "evidence_ids": ["evi-0001"],
            }],
            "preset_reference": None,
            "environment_ids": ["env-0001"],
        }))
    return records


def _run_files(records, *, run_id="run-1", status="completed_scope",
               stop_reason="coverage_reached", game_id="0123456789abcdef"):
    return {
        "run.json": _json({
            "id": run_id,
            "contract_version": "0.1",
            "started_at": "2026-11-01T00:00:00+00:00",
            "finished_at": "2026-11-01T00:05:00+00:00",
            "input": {"game_name": "Synthetic Game", "mode": "discover",
                      "platform_scope": "steam", "seed_urls": []},
            "game_id": game_id,
            "status": status,
            "stop_reason": stop_reason,
            "interrupted": False,
        }),
        "sources.json": _json([_source()]),
        "records.json": _json(records),
        "gaps.json": _json([]),
        "extractions.json": _json([]),
    }


def _group(files: FileService, members: dict, *, name: str) -> FrozenGroup:
    file_ids = {
        member: files.store_artifact(
            data,
            relative_name=f"import/{name}/{member}",
            stable_key=f"group:{name}:{member}",
        )
        for member, data in members.items()
    }
    entries = [
        FileEntry(
            relative_name=member,
            byte_length=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            media_type="application/json",
        )
        for member, data in sorted(members.items())
    ]
    return FrozenGroup(
        format_id=COLLECTOR_RUN_FORMAT_ID,
        operation_id="",
        file_ids=file_ids,
        manifest=FileManifest(entries=entries),
        inspection=FormatInspection(
            format_id=COLLECTOR_RUN_FORMAT_ID,
            required_files=list(COLLECTOR_REQUIRED_FILES),
            missing_files=[],
            limitations=[],
        ),
    )


def _mapping(
    repository: ProductRepository, game: Game, run_id: str,
    *, legacy_game_id: str = "0123456789abcdef",
) -> str:
    mapping = IdentityMapping(
        id=hashlib.sha256(f"mapping:{run_id}".encode()).hexdigest()[:32],
        schema_version="product-contract/0.4",
        created_at=NOW,
        legacy_namespace=f"collector_run:{run_id}",
        legacy_value=legacy_game_id,
        target_game_id=game.series_id,
        declared_by_user_scope_id=None,
        checks=[],
    )
    with repository.unit_of_work() as uow:
        uow.insert_object("IdentityMapping", mapping)
    return mapping.id


def _publish_import(
    repository: ProductRepository,
    files: FileService,
    game: Game,
    context: GameContext,
    *,
    run_id: str = "run-1",
    records=None,
    status: str = "completed_scope",
    stop_reason: str = "coverage_reached",
    legacy_game_id: str = "0123456789abcdef",
    mapping_game: Game | None = None,
    user_scope_id: str = SCOPE,
) -> str:
    group = _group(
        files,
        _run_files(
            _dataset() if records is None else records,
            run_id=run_id,
            status=status,
            stop_reason=stop_reason,
            game_id=legacy_game_id,
        ),
        name=run_id,
    )
    mapping_id = _mapping(
        repository, mapping_game or game, run_id, legacy_game_id=legacy_game_id
    )
    result = ImportPublisher(repository, files).publish(
        frozen=group,
        game_context_id=context.id,
        mapping_version="collector-import/1",
        identity_mapping_ids=[mapping_id],
        user_scope_id=user_scope_id,
        idempotency_key=f"import:{run_id}",
    )
    return result.snapshot_id


def _service(repository: ProductRepository, files: FileService) -> KnowledgeService:
    return KnowledgeService(repository, files)


def _build(service: KnowledgeService, snapshots, context: GameContext, *,
           key: str = "pkg-1", prior: str | None = None, **kwargs):
    return service.build_package(
        list(snapshots),
        prior,
        kwargs.pop("scope", KNOWLEDGE_TASK_SCOPE),
        game_context_id=context.id,
        user_scope_id=SCOPE,
        idempotency_key=key,
        **kwargs,
    )


def _count(repository: ProductRepository, entity_type: str) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def _package_bytes(repository: ProductRepository, package_id: str) -> bytes:
    return repository.get_object("KnowledgePackage", package_id).payload_bytes


# --- coverage ----------------------------------------------------------------


def test_an_absent_basis_is_unknown_and_never_covered(tmp_path):
    """A record subset that does not state a purpose leaves that cell unknown."""
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(
        repository, files, game, context,
        records=[_record("set-0001", "settings", {
            "id": "set-0001", "raw_name": "Synthetic Quality",
            "canonical_name": "Quality", "aliases": [],
            "proof": {"url": "https://example.invalid/src-0001"},
        })],
    )

    built = _build(_service(repository, files), [snapshot], context)
    cells = {cell.key: cell for cell in built.package.coverage_matrix}

    assert cells["graphics.catalog"].status is CoverageStatus.covered
    assert cells["measurement.environment"].status is CoverageStatus.unknown
    assert cells["display.capabilities"].status is CoverageStatus.unknown
    assert cells["version.dependencies"].status is CoverageStatus.unknown
    assert cells["tradeoffs.directions"].status is CoverageStatus.unknown
    # A cell that was never checked is never "covered".
    for cell in built.package.coverage_matrix:
        assert cell.status is not CoverageStatus.covered or cell.evidence_refs
        assert cell.note


def test_a_covered_cell_without_evidence_is_refused_by_the_contract():
    with pytest.raises(ValidationError):
        CoverageCell(
            key="graphics.catalog",
            dimensions={},
            operation_scope=KNOWLEDGE_TASK_SCOPE,
            status=CoverageStatus.covered,
            evidence_refs=[],
            note="no basis",
        )


def test_a_single_configuration_stays_partially_covered(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)

    built = _build(_service(repository, files), [snapshot], context)
    cells = {cell.key: cell for cell in built.package.coverage_matrix}

    assert cells["tradeoffs.directions"].status is CoverageStatus.partially_covered
    assert cells["measurement.environment"].status is CoverageStatus.covered
    assert cells["graphics.catalog"].status is CoverageStatus.covered


def test_a_measurement_without_a_number_does_not_close_the_cell(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    records = _dataset()
    records[1] = _record("evi-0001", "evidence", {
        "id": "evi-0001",
        "kind": "measurement",
        "origin": "source_statement",
        "statement": "frame rate is not stated",
        "metric": "fps_unspecified",
        "numeric": None,
        "unit": None,
        "setting_ids": [],
        "input_ids": [],
        "detected_state": None,
    })
    snapshot = _publish_import(repository, files, game, context, records=records)

    built = _build(_service(repository, files), [snapshot], context)
    cell = next(
        item for item in built.package.coverage_matrix
        if item.key == "measurement.environment"
    )

    assert cell.status is CoverageStatus.partially_covered
    assert "fps_unspecified" in cell.note


def test_missing_image_conditions_leave_only_that_cell_unknown(tmp_path):
    """The image gate blocks one cell; the text cells still publish."""
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)

    built = _build(
        _service(repository, files), [snapshot], context, image_required=True
    )
    cells = {cell.key: cell for cell in built.package.coverage_matrix}

    enabled, gate = image_adapter_status()
    assert enabled is False
    assert gate.status is CheckStatus.not_run
    assert cells["graphics.image_figures"].status is CoverageStatus.unknown
    assert "not_run" in cells["graphics.image_figures"].note
    # The text cells are not judged dead because the image adapter is disabled.
    assert cells["graphics.catalog"].status is CoverageStatus.covered
    assert cells["measurement.environment"].status is CoverageStatus.covered
    assert built.package.publication_status is PublicationStatus.published
    assert "accuracy" not in built.package.model_dump_json()


# --- isolation and dependency closure ---------------------------------------


def test_an_isolated_record_is_diagnostic_only(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    records = _dataset()
    # A configuration whose setting is not in the capture scope is isolated by
    # the importer; it must never become a package member.
    records.append(_record("cfg-9999", "configurations", {
        "id": "cfg-9999",
        "name": "Broken preset",
        "kind": "resolved_configuration",
        "assignments": [{
            "setting_id": "set-9999",
            "value": {"value_kind": "fixed", "value": "Ultra"},
            "evidence_ids": ["evi-0001"],
        }],
        "preset_reference": None,
        "environment_ids": [],
    }))
    snapshot = _publish_import(repository, files, game, context, records=records)

    built = _build(_service(repository, files), [snapshot], context)
    package = built.package

    assert len(package.diagnostic_record_ids) == 1
    isolated = package.diagnostic_record_ids[0]
    assert isolated not in package.record_snapshot_ids
    for group in (package.setting_catalog, package.configuration_templates,
                  package.measurement_evidence, package.constraints):
        assert isolated not in {ref.record_snapshot_id for ref in group}
    diagnostic_gaps = [
        gap for gap in package.critical_gaps + package.noncritical_gaps
        if isolated in gap.diagnostic_record_ids
    ]
    assert len(diagnostic_gaps) == 1


def test_a_member_with_an_unresolved_dependency_is_refused(tmp_path):
    """Fixed choice: a broken member is rejected, not silently kept."""
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)

    with repository.unit_of_work() as uow:
        rows = uow.query(
            "SELECT record_snapshot_id FROM imported_record_keys"
            " WHERE import_snapshot_id=? AND record_type='configurations'",
            (snapshot,),
        )
        stored = uow.find_object("ImportedRecord", rows[0]["record_snapshot_id"])
        payload = json.loads(stored.payload_bytes)
    payload["payload"]["assignments"][0]["setting_id"] = "set-missing"
    with repository.unit_of_work() as uow:
        uow.execute(
            "UPDATE objects SET payload=? WHERE id=?",
            (canonical_json(payload), rows[0]["record_snapshot_id"]),
        )

    with pytest.raises(ContractError) as caught:
        _build(_service(repository, files), [snapshot], context)

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _count(repository, "KnowledgePackage") == 0


# --- identity, scope and inputs ---------------------------------------------


def test_a_cross_game_snapshot_is_refused_with_no_package(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    other = _store_game(repository, uid=110, series=111)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(
        repository, files, other, context, mapping_game=other,
        legacy_game_id="fedcba9876543210",
    )

    with pytest.raises(ContractError) as caught:
        _build(_service(repository, files), [snapshot], context)

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _count(repository, "KnowledgePackage") == 0


def test_an_unverified_identity_publishes_no_matched_package(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(
        repository, status=GameIdentityStatus.provisional, platforms=(), user=SCOPE
    )
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)

    with pytest.raises(ContractError) as caught:
        _build(_service(repository, files), [snapshot], context)

    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    assert _count(repository, "KnowledgePackage") == 0
    # The source facts and their diagnostics are still readable.
    assert _count(repository, "ImportedRecord") > 0


def test_an_unregistered_declared_scope_is_refused(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)

    with pytest.raises(ContractError) as caught:
        _build(_service(repository, files), [snapshot], context, scope="made.up")

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED


def test_a_multi_run_package_has_no_single_execution_fact(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    first = _publish_import(repository, files, game, context, run_id="run-1")
    second = _publish_import(repository, files, game, context, run_id="run-2")

    built = _build(_service(repository, files), [first, second], context)
    package = built.package

    assert package.collection_result is None
    assert package.stop_reason is None
    assert package.collection_execution_ref is None
    execution_check = next(
        check for check in package.checks if check.check_id == "package.execution_fact"
    )
    assert execution_check.status is CheckStatus.not_run


def test_a_fully_covered_text_scope_still_records_the_image_gap(tmp_path):
    """The task never claims completion while figures were not read."""
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    records = _dataset(configs=2)
    records.append(_record("evi-0002", "evidence", {
        "id": "evi-0002", "kind": "observation", "origin": "source_statement",
        "statement": "synthetic observation", "metric": None, "numeric": None,
        "unit": None, "setting_ids": [], "input_ids": [],
        "detected_state": "detected",
    }))
    records.append(_record("ver-0001", "version_impacts", {
        "id": "ver-0001", "evidence_id": "evi-0001", "setting_id": "set-0001",
        "affected_value": "High", "platform": "steam", "version_raw": "1.0",
        "effective_date": None,
    }))
    snapshot = _publish_import(repository, files, game, context, records=records)
    service = _service(repository, files)

    task = TaskService(repository, provider=_NoDiscoveryProvider(), files=files)
    task.handlers["knowledge"] = service.handle
    task_input, mutation = _knowledge_input(context)
    accepted = task.register_task(task_input, mutation)
    outcome = task.run_once()

    assert outcome.state is TaskState.partial
    view = task.get_task(accepted.task_id)
    assert view.task.domain_result.code == "partial_scope"
    with repository.unit_of_work() as uow:
        package = json.loads(uow.latest_objects("KnowledgePackage")[0].payload_bytes)
    cells = {cell["key"]: cell for cell in package["coverage_matrix"]}
    assert cells["graphics.image_figures"]["status"] == "unknown"
    assert "not_run" in cells["graphics.image_figures"]["note"]
    assert package["critical_gaps"], "the unread image condition stays a gap"


# --- ownership ---------------------------------------------------------------


def test_another_players_snapshot_is_never_packaged(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    foreign = _publish_import(
        repository, files, game, context, run_id="run-foreign",
        user_scope_id=_uid(4242),
    )

    with pytest.raises(ContractError) as caught:
        _build(_service(repository, files), [foreign], context)

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _count(repository, "KnowledgePackage") == 0


def test_a_published_package_belongs_to_the_player_who_built_it(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)
    built = _build(_service(repository, files), [snapshot], context)

    with pytest.raises(ContractError) as caught:
        _service(repository, files).get_package(
            built.package.id, user_scope_id=_uid(4242)
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _service(repository, files).get_package(
        built.package.id, user_scope_id=SCOPE
    ).id == built.package.id


def test_a_retained_only_snapshot_cannot_be_a_package_member(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    # Every record is isolated: the configuration names a setting that is not
    # in the group, so the snapshot has no usable member at all.
    snapshot = _publish_import(
        repository, files, game, context,
        records=[_record("cfg-9999", "configurations", {
            "id": "cfg-9999", "name": "Broken", "kind": "resolved_configuration",
            "assignments": [{
                "setting_id": "set-9999",
                "value": {"value_kind": "fixed", "value": "Ultra"},
                "evidence_ids": ["evi-9999"],
            }],
            "preset_reference": None, "environment_ids": [],
        })],
    )

    with pytest.raises(ContractError) as caught:
        _build(_service(repository, files), [snapshot], context)

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _count(repository, "KnowledgePackage") == 0


# --- replay before new-submission guards -------------------------------------


def test_a_registered_key_replays_after_the_world_changed(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)
    service = _service(repository, files)
    first = _build(service, [snapshot], context, key="pkg-1")

    # The snapshot is no longer usable for a *new* submission.
    with repository.unit_of_work() as uow:
        uow.execute(
            "UPDATE import_snapshots SET import_status='staging'"
            " WHERE import_snapshot_id=?",
            (snapshot,),
        )
    replay = _build(service, [snapshot], context, key="pkg-1")

    assert replay.package.id == first.package.id
    assert replay.declaration_file_id == first.declaration_file_id
    assert _count(repository, "KnowledgePackage") == 1


def test_a_registered_key_with_other_content_conflicts_before_validation(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)
    other = _publish_import(repository, files, game, context, run_id="run-2")
    service = _service(repository, files)
    _build(service, [snapshot], context, key="pkg-1")
    with repository.unit_of_work() as uow:
        uow.execute(
            "UPDATE import_snapshots SET import_status='staging'"
            " WHERE import_snapshot_id=?",
            (other,),
        )

    with pytest.raises(ContractError) as caught:
        _build(service, [other], context, key="pkg-1")

    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_collect_knowledge_replays_before_its_prechecks(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    application = Application(repository, file_root=str(tmp_path / "managed"))
    task_input, mutation = _knowledge_input(context)

    accepted = application.collect_knowledge(task_input, mutation)
    # The world changes: the platform is no longer known.
    with repository.unit_of_work() as uow:
        stored = uow.find_object("GameContext", context.id)
        payload = json.loads(stored.payload_bytes)
    payload["platform"] = {
        "value_state": "unknown", "value": None, "unknown_reason": "not_provided",
        "candidates": [], "provenance": [], "acquisition_status": "not_attempted",
        "attempts": [], "applicability_note": None, "resolution": None,
    }
    with repository.unit_of_work() as uow:
        uow.execute(
            "UPDATE objects SET payload=? WHERE id=?",
            (canonical_json(payload), context.id),
        )

    replay = application.collect_knowledge(task_input, mutation)

    assert replay.task_id == accepted.task_id


def test_collect_knowledge_conflicts_before_its_prechecks(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    application = Application(repository, file_root=str(tmp_path / "managed"))
    task_input, mutation = _knowledge_input(context)
    application.collect_knowledge(task_input, mutation)

    zero_budget, same_mutation = _knowledge_input(
        context, key="task-1",
        max_urls=0, max_searches=0, max_read_calls=0, max_model_calls=0,
    )
    with pytest.raises(ContractError) as caught:
        application.collect_knowledge(zero_budget, same_mutation)

    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


# --- coverage honesty --------------------------------------------------------


def test_version_dependencies_need_the_platform_too(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    records = _dataset()
    records.append(_record("ver-0001", "version_impacts", {
        "id": "ver-0001", "evidence_id": "evi-0001", "setting_id": "set-0001",
        "affected_value": None, "platform": None, "version_raw": "1.0",
        "effective_date": None,
    }))
    snapshot = _publish_import(repository, files, game, context, records=records)

    built = _build(_service(repository, files), [snapshot], context)
    cell = next(
        item for item in built.package.coverage_matrix
        if item.key == "version.dependencies"
    )

    assert cell.status is CoverageStatus.partially_covered


def test_a_console_only_exclusion_does_not_close_display_capabilities(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    records = _dataset()
    records.append(_record("con-0001", "constraints", {
        "id": "con-0001", "evidence_id": "evi-0001", "kind": "exclusion",
        "setting_id": "set-0001", "affected_values": [],
        "when": {"platform": "console"}, "requires": {},
        "verified_scope": True, "scope_note": "console versions only",
    }))
    snapshot = _publish_import(repository, files, game, context, records=records)

    built = _build(_service(repository, files), [snapshot], context)
    cell = next(
        item for item in built.package.coverage_matrix
        if item.key == "display.capabilities"
    )

    # The exclusion is about consoles, not this Steam package.
    assert cell.status is not CoverageStatus.not_applicable


def test_private_objects_never_enter_a_package(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)
    summary_id = _uid(8801)
    with repository.unit_of_work() as uow:
        uow.insert_object_bytes(
            "PerformanceSummary",
            schema_version="product-contract/0.4",
            raw=canonical_json({"id": summary_id, "private": True}),
            object_id=summary_id,
        )

    built = _build(_service(repository, files), [snapshot], context)

    assert summary_id not in built.package.model_dump_json()
    assert all(
        ref.record_snapshot_id != summary_id
        for ref in (
            built.package.setting_catalog
            + built.package.configuration_templates
            + built.package.measurement_evidence
            + built.package.constraints
        )
    )


# --- versions and idempotency -----------------------------------------------


def test_a_second_version_leaves_the_first_package_byte_identical(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)
    service = _service(repository, files)

    first = _build(service, [snapshot], context, key="pkg-1")
    before = _package_bytes(repository, first.package.id)
    second = _build(
        service, [snapshot], context, key="pkg-2", prior=first.package.id
    )

    assert second.package.version == first.package.version + 1
    assert second.package.series_id == first.package.series_id
    assert second.package.parent_package_id == first.package.id
    assert _package_bytes(repository, first.package.id) == before
    assert service.get_package(first.package.id, user_scope_id=SCOPE).version == 1


def test_the_same_key_replays_the_original_package(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)
    service = _service(repository, files)

    first = _build(service, [snapshot], context, key="pkg-1")
    replay = _build(service, [snapshot], context, key="pkg-1")

    assert replay.package.id == first.package.id
    assert replay.declaration_file_id == first.declaration_file_id
    assert _count(repository, "KnowledgePackage") == 1


def test_the_same_key_with_other_content_conflicts(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    first = _publish_import(repository, files, game, context, run_id="run-1")
    second = _publish_import(repository, files, game, context, run_id="run-2")
    service = _service(repository, files)

    _build(service, [first], context, key="pkg-1")
    with pytest.raises(ContractError) as caught:
        _build(service, [second], context, key="pkg-1")

    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert _count(repository, "KnowledgePackage") == 1


def test_a_pure_conflict_does_not_consume_the_key(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)
    service = _service(repository, files)

    staging = _uid(9101)
    with repository.unit_of_work() as uow:
        uow.insert_object_bytes(
            "ImportSnapshot",
            schema_version="product-contract/0.4",
            raw=canonical_json({
                "id": staging,
                "schema_version": "product-contract/0.4",
                "created_at": NOW,
                "created_by_task_id": None,
                "origin": "collector_run",
                "legacy_run_id": "run-x",
                "manifest": SnapshotManifest(entries=[SnapshotFileEntry(
                    relative_name="run.json", byte_length=1,
                    sha256="0" * 64, media_type="application/json",
                )]).model_dump(mode="json"),
                "import_fingerprint": "0" * 64,
                "import_status": "staging",
                "mapping_version": "collector-import/1",
                "mapping_context_hash": "1" * 64,
                "parent_import_snapshot_id": None,
                "identity_mapping_ids": [],
                "limitations": [],
            }),
            object_id=staging,
        )
        uow.execute(
            "INSERT INTO import_snapshots(import_snapshot_id, import_fingerprint,"
            " mapping_version, mapping_context_hash, import_status,"
            " declaration_file_id, parent_import_snapshot_id, mapped_game_id,"
            " created_by_task_id, created_at)"
            " VALUES(?, ?, ?, ?, 'staging', ?, NULL, ?, NULL, ?)",
            (staging, "0" * 64, "collector-import/1", "1" * 64, _uid(9102),
             game.series_id, NOW),
        )

    with pytest.raises(ContractError):
        _build(service, [staging], context, key="pkg-1")

    built = _build(service, [snapshot], context, key="pkg-1")

    assert built.package.id
    assert _count(repository, "KnowledgePackage") == 1


def test_building_a_package_makes_no_provider_call(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)

    _build(_service(repository, files), [snapshot], context)

    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0


def test_a_prior_package_from_another_scope_is_refused(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)
    service = _service(repository, files)

    first = _build(service, [snapshot], context, key="pkg-1")
    with pytest.raises(ContractError) as caught:
        _build(
            service, [snapshot], context, key="pkg-2", prior=first.package.id,
            scope="recommendation.configuration",
        )

    assert caught.value.code is ErrorCode.REFERENCE_INVALID


# --- reads -------------------------------------------------------------------


def test_get_package_hides_an_unpublished_package_from_other_players(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)
    built = _build(_service(repository, files), [snapshot], context)
    with repository.unit_of_work() as uow:
        stored = uow.find_object("KnowledgePackage", built.package.id)
        payload = json.loads(stored.payload_bytes)
    payload["publication_status"] = "staging"
    with repository.unit_of_work() as uow:
        uow.execute(
            "UPDATE objects SET payload=? WHERE id=?",
            (canonical_json(payload), built.package.id),
        )

    with pytest.raises(ContractError) as caught:
        _service(repository, files).get_package(
            built.package.id, user_scope_id=_uid(9999)
        )
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    # The declaring player can still read their own unpublished package.
    assert _service(repository, files).get_package(
        built.package.id, user_scope_id=SCOPE
    ).id == built.package.id


def test_get_package_reports_a_missing_original_as_unavailable(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    snapshot = _publish_import(repository, files, game, context)
    built = _build(_service(repository, files), [snapshot], context)

    with repository.unit_of_work() as uow:
        refs = uow.query(
            "SELECT target_id FROM object_references WHERE owner_id=?"
            " AND target_type='ManagedFile'",
            (snapshot,),
        )
    victim = files.repository.get_object("ManagedFile", refs[0]["target_id"])
    (files.objects_dir / victim.payload["relative_path"].split("/")[-1]).unlink()

    with pytest.raises(ContractError) as caught:
        _service(repository, files).get_package(
            built.package.id, user_scope_id=SCOPE
        )
    assert caught.value.code is ErrorCode.ARTIFACT_UNAVAILABLE


# --- the task entry ----------------------------------------------------------


def _knowledge_input(
    context: GameContext, *, key: str = "task-1", prior: str | None = None, **budget
) -> tuple:
    values = {"max_urls": 12, "max_searches": 12, "max_followup_rounds": 2,
              "max_queries_per_gap": 2, "max_model_calls": 16, "max_read_calls": 36}
    values.update(budget)
    return (
        KnowledgeInput(
            kind="knowledge",
            game_context_id=context.id,
            device_snapshot_id=None,
            prior_package_id=prior,
            budget=values,
        ),
        MutationContext(user_scope_id=SCOPE, idempotency_key=key, request_id=_uid(4000)),
    )


class _NoDiscoveryProvider:
    """A search that finds nothing: the task must still package what it has."""

    def __init__(self) -> None:
        self.sends = 0

    def send(self, request):
        self.sends += 1
        return {"urls": []}


def test_the_knowledge_task_builds_a_package_from_published_imports(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    _publish_import(repository, files, game, context)
    provider = _NoDiscoveryProvider()
    service = TaskService(repository, provider=provider, files=files)
    knowledge = _service(repository, files)
    service.handlers["knowledge"] = knowledge.handle
    task_input, mutation = _knowledge_input(context)

    accepted = service.register_task(task_input, mutation)
    outcome = service.run_once()

    assert outcome.task_id == accepted.task_id
    assert outcome.state in (TaskState.completed, TaskState.partial)
    view = service.get_task(accepted.task_id)
    assert view.task.domain_result.code in ("completed_scope", "partial_scope")
    assert _count(repository, "KnowledgePackage") == 1
    # The package builder itself makes no provider call; the two audited
    # searches come from the collection rounds.
    assert knowledge.provider_sends == 0
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 2
    assert provider.sends == 2


def test_the_knowledge_task_without_an_import_is_partial_and_packages_nothing(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    service = TaskService(repository, provider=_NoDiscoveryProvider(), files=files)
    service.handlers["knowledge"] = _service(repository, files).handle
    task_input, mutation = _knowledge_input(context)

    accepted = service.register_task(task_input, mutation)
    outcome = service.run_once()

    assert outcome.state is TaskState.partial
    assert _count(repository, "KnowledgePackage") == 0
    view = service.get_task(accepted.task_id)
    assert view.task.stop_reason.kind == "collection"
    assert view.task.stop_reason.code == "low_information_gain"


def test_a_foreign_prior_package_is_a_pure_conflict_before_registration(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    other_scope = _uid(9999)
    snapshot = _publish_import(
        repository, files, game, context, user_scope_id=other_scope
    )
    foreign = KnowledgeService(repository, files).build_package(
        [snapshot], None, KNOWLEDGE_TASK_SCOPE,
        game_context_id=context.id, user_scope_id=other_scope,
        idempotency_key="pkg-other",
    ).package
    knowledge = _service(repository, files)
    task_input, _mutation = _knowledge_input(context, key="task-1", prior=foreign.id)

    # The prior package is invisible to this caller, so the attempt is a pure
    # conflict and no task key is consumed (error §4.2).
    with pytest.raises(ContractError) as caught:
        knowledge.precheck_collection(task_input, user_scope_id=SCOPE)

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0


def test_a_zero_budget_is_a_pure_conflict_before_registration(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    knowledge = _service(repository, files)
    task_input, _mutation = _knowledge_input(
        context, key="task-1",
        max_urls=0, max_searches=0, max_read_calls=0, max_model_calls=0,
    )

    with pytest.raises(ContractError) as caught:
        knowledge.precheck_collection(task_input, user_scope_id=SCOPE)

    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0
