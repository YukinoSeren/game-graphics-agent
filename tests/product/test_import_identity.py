"""2D-C: a player-declared import identity mapping, scoped to its declarer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from game_app.contracts.common import CheckResult, MutationContext
from game_app.contracts.enums import (
    CheckStatus,
    GameIdentityStatus,
    PublicationStatus,
)
from game_app.contracts.knowledge import (
    Game,
    ImportIdentityMappingRequest,
)
from game_app.contracts.primitives import utc_now
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileEntry, FileManifest, FileService
from game_app.importer import (
    COLLECTOR_RUN_FORMAT_ID,
    COLLECTOR_REQUIRED_FILES,
    FormatInspection,
    FrozenGroup,
    ImportPublisher,
)
from game_app.knowledge import IdentityService
from game_app.repository import ProductRepository, canonical_json

USER = "a" * 32
OTHER = "b" * 32


def _uid(value: int) -> str:
    return f"{value:032x}"


def _ctx(key: str = "identity-key-1", user: str = USER) -> MutationContext:
    return MutationContext(
        user_scope_id=user, idempotency_key=key, request_id=_uid(990),
    )


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _service(tmp_path: Path) -> tuple[FileService, ProductRepository]:
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    return FileService(repository, tmp_path / "managed"), repository


def _store_game(
    repository: ProductRepository,
    *,
    serial: int,
    status: str = "provisional",
    owner: str | None = USER,
    platform_scope: list[str] | None = None,
) -> Game:
    game_id = _uid(serial)
    series_id = _uid(serial + 1)
    checks = []
    if status == "verified":
        checks = [CheckResult(
            check_id="identity.verified",
            definition_version="test",
            status=CheckStatus.pass_,
            input_refs=[],
            evidence_refs=[],
            checked_at=utc_now(),
            method="test stub",
            note="synthetic verified identity",
        )]
    game = Game(
        id=game_id,
        schema_version="product-contract/0.4",
        created_at=utc_now(),
        created_by_task_id=None,
        series_id=series_id,
        version=1,
        canonical_name="Synthetic Game",
        aliases=[],
        platform_scope=list(platform_scope or ([] if status != "verified" else ["Windows PC"])),
        identity_status=GameIdentityStatus(status),
        identity_checks=checks,
        declaring_user_scope_id=owner,
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
    return game


def _declare(
    service: IdentityService,
    game: Game,
    *,
    key: str = "identity-key-1",
    user: str = USER,
    run_id: str = "run-1",
    legacy_game_id: str = "0123456789abcdef",
):
    return service.declare_import_identity(
        ImportIdentityMappingRequest(
            run_id=run_id,
            legacy_game_id=legacy_game_id,
            game_revision_id=game.id,
        ),
        _ctx(key, user),
    )


def _run_files(*, run_id="run-1") -> dict[str, bytes]:
    return {
        "run.json": _json({
            "id": run_id,
            "contract_version": "0.1",
            "started_at": "2026-11-01T00:00:00+00:00",
            "finished_at": "2026-11-01T00:05:00+00:00",
            "input": {"game_name": "Synthetic Game", "mode": "discover",
                      "platform_scope": "Windows PC", "seed_urls": []},
            "game_id": "0123456789abcdef",
            "status": "completed_scope",
            "stop_reason": "coverage_reached",
            "interrupted": False,
        }),
        "sources.json": _json([{
            "id": "src-0001", "revision_id": "rev-0001",
            "url": "https://example.invalid/guide",
            "retrieved_at": "2026-11-01T00:01:00+00:00",
            "role": "official_version_compatibility",
            "status": "extracted", "lineage_status": "unknown",
        }]),
        "records.json": _json([{
            "id": "src-0001:cfg-0001",
            "local_id": "cfg-0001",
            "record_type": "configurations",
            "record_status": "accepted",
            "recommendation_status": None,
            "applicability": "unknown",
            "reference_scope": "src-0001",
            "source_id": "src-0001",
            "revision_id": "rev-0001",
            "game_id": "0123456789abcdef",
            "payload": {"name": "P", "settings": []},
        }]),
        "gaps.json": _json([]),
        "extractions.json": _json([]),
    }


def _group(files: FileService, members: dict[str, bytes], *, name: str) -> FrozenGroup:
    file_ids = {
        member: files.store_artifact(
            data, relative_name=member, stable_key=f"identity:{name}:{member}"
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


def _stored_context(repository: ProductRepository) -> str:
    context_id = _uid(700)
    with repository.unit_of_work() as uow:
        uow.insert_object_bytes(
            "GameContext",
            schema_version="product-contract/0.4",
            raw=canonical_json({
                "id": context_id,
                "schema_version": "product-contract/0.4",
                "created_at": utc_now(),
                "created_by_task_id": None,
                "series_id": context_id,
                "version": 1,
                "game_id": _uid(701),
                "game_revision_id": _uid(702),
                "installation_id": None,
                "platform": {
                    "value_state": "unknown", "value": None,
                    "unknown_reason": "not_provided", "candidates": [],
                    "provenance": [], "acquisition_status": "not_attempted",
                    "attempts": [], "applicability_note": None, "resolution": None,
                },
                "build_identifiers": [],
                "process_candidates": [],
                "selected_process": None,
                "adapter_id": None,
                "adapter_version": None,
                "capabilities": {
                    "can_identify": _not_run("adapter.identify"),
                    "can_read_config": _not_run("adapter.read_config"),
                    "can_bind_capture": _not_run("adapter.bind_capture"),
                },
                "checks": [],
            }),
            object_id=context_id,
            series_id=context_id,
            version=1,
        )
    return context_id


def _not_run(check_id: str) -> dict:
    return {
        "check_id": check_id, "definition_version": "test", "status": "not_run",
        "input_refs": [], "evidence_refs": [], "checked_at": None,
        "method": "test stub", "note": None,
    }


def _publish(
    publisher: ImportPublisher,
    group: FrozenGroup,
    *,
    key: str,
    context_id: str,
    mappings=(),
    user: str = USER,
):
    return publisher.publish(
        frozen=group,
        game_context_id=context_id,
        mapping_version="collector-import/1",
        identity_mapping_ids=list(mappings),
        user_scope_id=user,
        idempotency_key=key,
    )


def _count(repository: ProductRepository, entity_type: str) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def test_a_declared_mapping_makes_an_import_publishable(tmp_path):
    files, repository = _service(tmp_path)
    game = _store_game(repository, serial=100)
    identity = IdentityService(repository, files)
    mapping_id = _declare(identity, game).refs[0].entity_id
    context_id = _stored_context(repository)
    group = _group(files, _run_files(), name="published")

    result = _publish(
        ImportPublisher(repository, files), group,
        key="imp-1", context_id=context_id, mappings=[mapping_id],
    )

    assert result.status is PublicationStatus.published
    assert result.code == "published"
    assert result.game_id == game.series_id
    stored = repository.get_object("IdentityMapping", mapping_id).payload
    assert stored["declared_by_user_scope_id"] == USER
    assert stored["legacy_namespace"] == "collector_run:run-1"
    assert stored["legacy_value"] == "0123456789abcdef"
    assert stored["checks"][0]["check_id"] == "identity.declared"
    assert stored["checks"][0]["status"] == "pass"


def test_a_mapping_cannot_be_redirected_to_another_game(tmp_path):
    files, repository = _service(tmp_path)
    first_game = _store_game(repository, serial=110)
    other_game = _store_game(repository, serial=120)
    identity = IdentityService(repository, files)
    first = _declare(identity, first_game, key="map-a")

    with pytest.raises(ContractError) as caught:
        _declare(identity, other_game, key="map-b")

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert any(
        error.path == "legacy_game_id" and error.code == "conflict"
        for error in caught.value.field_errors
    )
    stored = repository.get_object("IdentityMapping", first.refs[0].entity_id).payload
    assert stored["target_game_id"] == first_game.series_id
    assert _count(repository, "IdentityMapping") == 1


def test_repeating_a_declaration_reuses_the_mapping_and_the_snapshot(tmp_path):
    files, repository = _service(tmp_path)
    game = _store_game(repository, serial=130)
    identity = IdentityService(repository, files)
    first = _declare(identity, game, key="map-1")
    replay = _declare(identity, game, key="map-1")
    other_key = _declare(identity, game, key="map-2")

    assert replay.refs[0].entity_id == first.refs[0].entity_id
    assert other_key.refs[0].entity_id == first.refs[0].entity_id
    assert _count(repository, "IdentityMapping") == 1

    context_id = _stored_context(repository)
    group = _group(files, _run_files(), name="same-mapping")
    publisher = ImportPublisher(repository, files)
    left = _publish(
        publisher, group, key="imp-a", context_id=context_id,
        mappings=[first.refs[0].entity_id],
    )
    right = _publish(
        publisher, group, key="imp-b", context_id=context_id,
        mappings=[other_key.refs[0].entity_id],
    )
    assert left.snapshot_id == right.snapshot_id
    assert _count(repository, "ImportSnapshot") == 1


def test_an_import_without_a_mapping_stays_retained_only(tmp_path):
    files, repository = _service(tmp_path)
    _store_game(repository, serial=140)
    context_id = _stored_context(repository)
    group = _group(files, _run_files(), name="no-mapping")

    result = _publish(
        ImportPublisher(repository, files), group,
        key="imp-1", context_id=context_id,
    )

    assert result.code == "retained_only"
    assert result.game_id is None


def test_another_users_declared_mapping_is_not_usable(tmp_path):
    files, repository = _service(tmp_path)
    game = _store_game(repository, serial=150)
    identity = IdentityService(repository, files)
    mapping_id = _declare(identity, game, user=USER).refs[0].entity_id
    context_id = _stored_context(repository)
    group = _group(files, _run_files(), name="foreign-mapping")

    with pytest.raises(ContractError) as caught:
        _publish(
            ImportPublisher(repository, files), group,
            key="imp-other", context_id=context_id,
            mappings=[mapping_id], user=OTHER,
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _count(repository, "ImportSnapshot") == 0


def test_a_legacy_unverified_game_cannot_be_declared(tmp_path):
    files, repository = _service(tmp_path)
    game = _store_game(
        repository, serial=160, status="legacy_unverified", owner=None,
    )
    identity = IdentityService(repository, files)

    with pytest.raises(ContractError) as caught:
        _declare(identity, game)

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _count(repository, "IdentityMapping") == 0


def test_another_users_provisional_game_cannot_be_declared(tmp_path):
    files, repository = _service(tmp_path)
    game = _store_game(repository, serial=170, owner=OTHER)
    identity = IdentityService(repository, files)

    with pytest.raises(ContractError) as caught:
        _declare(identity, game, user=USER)

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _count(repository, "IdentityMapping") == 0


def test_a_verified_game_is_publicly_declarable(tmp_path):
    files, repository = _service(tmp_path)
    game = _store_game(repository, serial=180, status="verified", owner=None)
    identity = IdentityService(repository, files)

    result = _declare(identity, game, user=USER)

    assert result.refs[0].entity_id
    stored = repository.get_object("IdentityMapping", result.refs[0].entity_id).payload
    assert stored["target_game_id"] == game.series_id
    assert stored["declared_by_user_scope_id"] == USER


def test_the_same_key_with_different_content_is_an_idempotency_conflict(tmp_path):
    files, repository = _service(tmp_path)
    game = _store_game(repository, serial=190)
    other = _store_game(repository, serial=200)
    identity = IdentityService(repository, files)
    _declare(identity, game, key="same")

    with pytest.raises(ContractError) as caught:
        _declare(identity, other, key="same")

    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert _count(repository, "IdentityMapping") == 1
