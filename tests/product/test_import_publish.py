"""IMP-T03: publishing an import snapshot, three-part dedupe and file fault windows.

The publisher writes the snapshot through ``FileService.publish`` so a crash at
either window is observable: an unconsumable orphan after the rename, and the
original response after a lost commit response.  Same input, same identity.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from game_app.contracts.common import MutationContext
from game_app.contracts.enums import PublicationStatus, TaskState
from game_app.contracts.knowledge import IdentityMapping
from game_app.contracts.primitives import utc_now
from game_app.contracts.tasks import KnowledgeImportInput
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileEntry, FileManifest, FileService, PublishHooks
from game_app.importer import (
    COLLECTOR_RUN_FORMAT_ID,
    COLLECTOR_REQUIRED_FILES,
    FormatInspection,
    FrozenGroup,
    FrozenInputReader,
    ImportPublishResult,
    ImportPublisher,
    ImportService,
    frozen_group_from_file_ids,
    import_fingerprint,
    mapping_context_hash,
)
from game_app.repository import ProductRepository, canonical_json
from game_app.tasks import TaskService

SCOPE = "a" * 32
CTX_KEY = "import-key-1"


def _uid(value: int) -> str:
    return f"{value:032x}"


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _source(source_id="src-0001", revision_id="rev-0001"):
    return {
        "id": source_id,
        "revision_id": revision_id,
        "url": f"https://example.invalid/{source_id}",
        "retrieved_at": "2026-11-01T00:01:00+00:00",
        "role": "official_version_compatibility",
        "status": "extracted",
        "lineage_status": "unknown",
    }


def _record(local_id: str, record_type: str, payload, **extra):
    row = {
        "id": f"src-0001:{local_id}",
        "local_id": local_id,
        "record_type": record_type,
        "record_status": "accepted",
        "recommendation_status": None,
        "applicability": "unknown",
        "reference_scope": "src-0001",
        "source_id": "src-0001",
        "revision_id": "rev-0001",
        "game_id": "0123456789abcdef",
        "payload": payload,
    }
    row.update(extra)
    return row


def _run_files(*, run_id="run-1", records=None, extractions=None):
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
        "sources.json": _json([_source()]),
        "records.json": _json([] if records is None else records),
        "gaps.json": _json([]),
        "extractions.json": _json([] if extractions is None else extractions),
    }


def _service(tmp_path: Path):
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    files = FileService(repository, tmp_path / "managed")
    return files, repository


def _freeze(files, tmp_path, name, members):
    directory = tmp_path / f"run-{name}"
    directory.mkdir()
    for relative_name, data in members.items():
        (directory / relative_name).write_bytes(data)
    return FrozenInputReader(files).freeze(
        directory, user_scope_id=SCOPE, idempotency_key=f"freeze-{name}"
    )


def _group(files, members, *, name) -> FrozenGroup:
    file_ids = {
        member: files.store_artifact(
            data, relative_name=f"import/{name}/{member}",
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


def _stored_context(repository: ProductRepository) -> str:
    """A minimal stored context stub; real context construction is 01/04 work."""
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


def _publisher(repository, files) -> ImportPublisher:
    return ImportPublisher(repository, files)


def _publish(publisher, group, *, key, context_id, mappings=(), hooks=None,
             mapping_version="collector-import/1"):
    return publisher.publish(
        frozen=group,
        game_context_id=context_id,
        mapping_version=mapping_version,
        identity_mapping_ids=list(mappings),
        user_scope_id=SCOPE,
        idempotency_key=key,
        hooks=hooks,
    )


def _count(repository, entity_type):
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def _snapshot(repository, snapshot_id):
    stored = repository.get_object("ImportSnapshot", snapshot_id)
    return json.loads(stored.payload_bytes)


# --- dedupe ------------------------------------------------------------------


def test_fingerprint_and_context_hash_are_stable_and_distinct(tmp_path):
    files, repository = _service(tmp_path)
    group = _freeze(files, tmp_path, "fp", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))

    assert import_fingerprint(group.manifest) == import_fingerprint(group.manifest)
    assert import_fingerprint(group.manifest) != mapping_context_hash(
        "collector-import/1", []
    )
    assert mapping_context_hash("collector-import/1", []) == mapping_context_hash(
        "collector-import/1", []
    )
    assert mapping_context_hash("collector-import/1", [_uid(1)]) != mapping_context_hash(
        "collector-import/1", []
    )
    # Entry order never changes the fingerprint.
    reversed_manifest = FileManifest(entries=list(reversed(group.manifest.entries)))
    assert import_fingerprint(reversed_manifest) == import_fingerprint(group.manifest)


def test_same_input_two_submissions_share_one_snapshot_identity(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "race", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))
    publisher = _publisher(repository, files)

    results: list[ImportPublishResult] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def submit(key: str):
        try:
            barrier.wait(timeout=5)
            results.append(_publish(
                publisher, group, key=key, context_id=context_id
            ))
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    threads = [
        threading.Thread(target=submit, args=("k-1",)),
        threading.Thread(target=submit, args=("k-2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    assert len(results) == 2
    assert len({result.snapshot_id for result in results}) == 1
    assert _count(repository, "ImportSnapshot") == 1


def test_only_identity_mappings_change_creates_a_child_snapshot(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "remap", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))
    publisher = _publisher(repository, files)
    mapping_id = _store_identity_mapping(repository)

    first = _publish(publisher, group, key="k-1", context_id=context_id)
    second = _publish(
        publisher, group, key="k-2", context_id=context_id, mappings=[mapping_id]
    )

    assert second.snapshot_id != first.snapshot_id
    assert second.import_fingerprint == first.import_fingerprint
    assert second.mapping_context_hash != first.mapping_context_hash
    assert second.parent_import_snapshot_id == first.snapshot_id
    assert _snapshot(repository, second.snapshot_id)["parent_import_snapshot_id"] == first.snapshot_id
    assert _snapshot(repository, second.snapshot_id)["identity_mapping_ids"] == [mapping_id]


def test_changed_bytes_create_a_new_fingerprint_without_a_parent(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    first_group = _freeze(files, tmp_path, "bytes-a", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "A", "settings": []})],
    ))
    second_group = _freeze(files, tmp_path, "bytes-b", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "B", "settings": []})],
    ))
    publisher = _publisher(repository, files)

    first = _publish(publisher, first_group, key="k-1", context_id=context_id)
    second = _publish(publisher, second_group, key="k-2", context_id=context_id)

    assert second.import_fingerprint != first.import_fingerprint
    assert second.parent_import_snapshot_id is None
    assert _count(repository, "ImportSnapshot") == 2


def _store_identity_mapping(repository) -> str:
    game_id = _uid(710)
    series_id = _uid(711)
    with repository.unit_of_work() as uow:
        uow.insert_object_bytes(
            "Game",
            schema_version="product-contract/0.4",
            raw=canonical_json({
                "id": game_id,
                "schema_version": "product-contract/0.4",
                "created_at": utc_now(),
                "created_by_task_id": None,
                "series_id": series_id,
                "version": 1,
                "canonical_name": "Synthetic Game",
                "aliases": [],
                "platform_scope": [],
                "identity_status": "provisional",
                "identity_checks": [],
                "declaring_user_scope_id": SCOPE,
            }),
            object_id=game_id,
            series_id=series_id,
            version=1,
        )
    mapping = IdentityMapping(
        id=_uid(712),
        schema_version="product-contract/0.4",
        created_at=utc_now(),
        legacy_namespace="collector_run:run-1",
        legacy_value="0123456789abcdef",
        target_game_id=series_id,
        checks=[],
    )
    with repository.unit_of_work() as uow:
        uow.insert_object("IdentityMapping", mapping)
    return mapping.id


# --- file windows ------------------------------------------------------------


def test_rename_then_crash_leaves_an_unconsumable_orphan_and_retry_reuses_it(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "rename", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))
    publisher = _publisher(repository, files)

    def crash(_path):
        raise RuntimeError("simulated process exit after the rename")

    with pytest.raises(RuntimeError):
        _publish(
            publisher, group, key="k-1", context_id=context_id,
            hooks=PublishHooks(after_rename=crash),
        )

    frozen_files = len(group.file_ids)
    assert _count(repository, "ImportSnapshot") == 0
    # The frozen originals are already managed; only a declaration was added,
    # and it was never recorded as a ManagedFile.
    assert _count(repository, "ManagedFile") == frozen_files
    operation = files.get_operation_by_key(
        SCOPE, "POST", "/internal/knowledge-import-publish", "k-1"
    )
    orphan_id = operation.file_ids[0]
    assert (files.objects_dir / orphan_id).is_file()
    with pytest.raises(ContractError) as caught:
        files.read_managed_file(orphan_id)
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND

    retried = _publish(publisher, group, key="k-1", context_id=context_id)

    assert retried.declaration_file_id == orphan_id
    assert _count(repository, "ImportSnapshot") == 1
    assert _count(repository, "ManagedFile") == frozen_files + 1
    assert files.read_managed_file(retried.declaration_file_id)


def test_lost_response_after_commit_replays_the_same_snapshot(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "commit", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))
    publisher = _publisher(repository, files)

    def lose(_result):
        raise RuntimeError("the response was lost after the commit")

    with pytest.raises(RuntimeError):
        _publish(
            publisher, group, key="k-1", context_id=context_id,
            hooks=PublishHooks(after_commit=lambda: lose(None)),
        )

    first = _publish(publisher, group, key="k-1", context_id=context_id)
    second = _publish(publisher, group, key="k-1", context_id=context_id)

    assert first.snapshot_id == second.snapshot_id
    assert first.declaration_file_id == second.declaration_file_id
    assert _count(repository, "ImportSnapshot") == 1


def test_rejected_input_stays_rejected_on_every_retry(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    members = _run_files(records=[])
    members["records.json"] = b"not json at all\n"
    group = _group(files, members, name="rejected")
    publisher = _publisher(repository, files)

    first = _publish(publisher, group, key="k-1", context_id=context_id)
    same_key = _publish(publisher, group, key="k-1", context_id=context_id)
    other_key = _publish(publisher, group, key="k-2", context_id=context_id)

    assert first.status is PublicationStatus.rejected
    assert first.code == "retained_only"
    assert same_key.snapshot_id == first.snapshot_id
    assert other_key.snapshot_id == first.snapshot_id
    assert other_key.code == "retained_only"
    assert _count(repository, "ImportSnapshot") == 1
    stored = _snapshot(repository, first.snapshot_id)
    assert stored["import_status"] == "rejected"
    assert any("records.json" in item for item in stored["limitations"])


# --- results and records -----------------------------------------------------


def test_resolved_identity_publishes_usable_records(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "resolved", _run_files(
        records=[
            _record("cfg-0001", "configurations", {"name": "P", "settings": []}),
            _record("evi-0001", "evidence", {
                "id": "evi-0001", "kind": "setting_description",
                "origin": "source_statement", "statement": "synthetic",
                "setting_ids": [], "input_ids": [],
            }),
        ],
    ))
    mapping_id = _store_identity_mapping(repository)

    result = _publish(
        _publisher(repository, files), group, key="k-1", context_id=context_id,
        mappings=[mapping_id],
    )

    assert result.status is PublicationStatus.published
    assert result.code == "published"
    assert result.game_id == _uid(711)
    assert len(result.accepted_record_ids) == 2
    stored = _snapshot(repository, result.snapshot_id)
    assert stored["identity_mapping_ids"] == [mapping_id]


def test_published_import_reports_usable_records(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "published", _run_files(
        records=[
            _record("cfg-0001", "configurations", {"name": "P", "settings": []}),
            _record("cfg-0002", "configurations", {"name": "Q", "settings": []},
                    superseded_by="src-0001:cfg-9999"),
        ],
        extractions=[{
            "source_id": "src-0001", "status": "ok", "model": "synthetic-model",
            "started_at": "2026-11-01T00:02:00+00:00", "raw": "{}", "parsed": {},
        }],
    ))
    publisher = _publisher(repository, files)

    result = _publish(publisher, group, key="k-1", context_id=context_id)

    assert result.status is PublicationStatus.published
    assert result.code == "retained_only"  # the legacy game id has no mapping
    assert _count(repository, "ImportedRecord") == 2
    assert len(result.accepted_record_ids) == 2
    assert result.diagnostic_record_ids == []
    stored = _snapshot(repository, result.snapshot_id)
    assert stored["legacy_run_id"] == "run-1"
    assert stored["origin"] == "collector_run"
    assert stored["manifest"]["entries"]
    extractions = _count(repository, "ImportedExtraction")
    assert extractions == 1


def test_no_usable_records_is_retained_only_and_originals_stay_readable(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "empty", _run_files(records=[], extractions=[]))
    publisher = _publisher(repository, files)

    result = _publish(publisher, group, key="k-1", context_id=context_id)

    assert result.code == "retained_only"
    assert result.accepted_record_ids == []
    assert result.status is PublicationStatus.published
    for name, file_id in group.file_ids.items():
        assert files.read_managed_file(file_id)


def test_import_records_claim_their_four_part_keys(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "keys", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))

    result = _publish(_publisher(repository, files), group, key="k-1", context_id=context_id)

    with repository.unit_of_work() as uow:
        rows = uow.query(
            "SELECT * FROM imported_record_keys WHERE import_snapshot_id=?",
            (result.snapshot_id,),
        )
    assert len(rows) == 1
    assert rows[0]["usable"] == 1
    assert rows[0]["local_record_id"] == "cfg-0001"
    with repository.unit_of_work() as uow:
        relations = uow.query(
            "SELECT relation, target_type FROM object_references WHERE owner_id=?",
            (result.snapshot_id,),
        )
    assert {"source", "source_revision", "record"} <= {
        row["relation"] for row in relations
    }


def test_context_of_another_user_is_not_importable(tmp_path):
    """A caller must not publish against someone else's installation context."""
    files, repository = _service(tmp_path)
    context_id = _owned_context(repository)
    group = _freeze(files, tmp_path, "foreign-context", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))
    publisher = _publisher(repository, files)

    with pytest.raises(ContractError) as caught:
        publisher.publish(
            frozen=group,
            game_context_id=context_id,
            mapping_version="collector-import/1",
            identity_mapping_ids=[],
            user_scope_id=_uid(4242),
            idempotency_key="k-foreign",
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert _count(repository, "ImportSnapshot") == 0
    # Only the pre-existing frozen originals are managed; no declaration exists.
    assert _count(repository, "ManagedFile") == len(group.file_ids)


def _owned_context(repository: ProductRepository) -> str:
    """A context bound to an installation that belongs to ``SCOPE``."""
    installation_id = _uid(730)
    with repository.unit_of_work() as uow:
        uow.insert_object_bytes(
            "GameInstallation",
            schema_version="product-contract/0.4",
            raw=canonical_json({
                "id": installation_id,
                "schema_version": "product-contract/0.4",
                "created_at": utc_now(),
                "created_by_task_id": None,
                "series_id": installation_id,
                "version": 1,
                "revision": 1,
                "user_scope_id": SCOPE,
                "game_id": _uid(731),
                "origin": "user_provided",
                "platform": {
                    "value_state": "unknown", "value": None,
                    "unknown_reason": "not_provided", "candidates": [],
                    "provenance": [], "acquisition_status": "not_attempted",
                    "attempts": [], "applicability_note": None, "resolution": None,
                },
                "private_path_ref": None,
            }),
            object_id=installation_id,
            series_id=installation_id,
            version=1,
        )
    context_id = _uid(732)
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
                "game_id": _uid(731),
                "game_revision_id": _uid(733),
                "installation_id": installation_id,
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


def test_a_malformed_group_is_rejected_instead_of_raising_a_validation_error(tmp_path):
    """A task-path import must fail as a rejected import, not an internal error."""
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    members = _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    )
    # A blank source URL survives the format probe but no Source may store it.
    sources = json.loads(members["sources.json"])
    sources[0]["url"] = ""
    members["sources.json"] = _json(sources)
    file_ids = [
        files.store_artifact(
            data, relative_name=name, stable_key=f"bad:{name}"
        )
        for name, data in members.items()
    ]
    group = frozen_group_from_file_ids(files, file_ids)

    result = _publish(
        _publisher(repository, files), group, key="k-bad", context_id=context_id
    )

    assert result.status is PublicationStatus.rejected
    assert result.code == "retained_only"
    assert result.accepted_record_ids == []
    assert any("sources.json" in item or "url" in item for item in result.limitations)


def test_published_snapshot_can_read_back_its_frozen_files(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "readback", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))

    result = _publish(_publisher(repository, files), group, key="k-1", context_id=context_id)

    with repository.unit_of_work() as uow:
        refs = uow.query(
            "SELECT target_id FROM object_references WHERE owner_id=?"
            " AND target_type='ManagedFile'",
            (result.snapshot_id,),
        )
    referenced = {row["target_id"] for row in refs}
    assert set(group.file_ids.values()) <= referenced
    for file_id in group.file_ids.values():
        assert files.read_managed_file(file_id)


def test_registered_import_replays_before_its_references_are_revalidated(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "replay-order", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))
    publisher = _publisher(repository, files)
    first = _publish(publisher, group, key="k-1", context_id=context_id)

    # The registered three-part identity replays even when the request now
    # names a context that no longer resolves: a retry returns the original
    # import instead of failing on a reference checked only for new imports.
    replay = publisher.publish(
        frozen=group,
        game_context_id=_uid(999),
        mapping_version="collector-import/1",
        identity_mapping_ids=[],
        user_scope_id=SCOPE,
        idempotency_key="k-1",
    )

    assert replay.snapshot_id == first.snapshot_id


def test_format_limitations_reach_the_published_snapshot(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "limits", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))
    assert any("candidates.json" in item for item in group.inspection.limitations)

    result = _publish(
        _publisher(repository, files), group, key="k-1", context_id=context_id
    )

    assert any("candidates.json" in item for item in result.limitations)


def test_import_never_calls_a_provider_and_is_not_the_original_run(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "provider", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))

    _publish(_publisher(repository, files), group, key="k-1", context_id=context_id)

    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0
    assert _count(repository, "TaskRun") == 0


# --- task wiring -------------------------------------------------------------


def test_knowledge_import_task_runs_without_a_provider(tmp_path):
    files, repository = _service(tmp_path)
    context_id = _stored_context(repository)
    group = _freeze(files, tmp_path, "task", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))
    tasks = TaskService(repository, files=files)
    tasks.handlers["knowledge_import"] = ImportService(repository, files).handle
    accepted = tasks.register_task(
        KnowledgeImportInput(
            kind="knowledge_import",
            file_ids=list(group.file_ids.values()),
            game_context_id=context_id,
            mapping_version="collector-import/1",
            identity_mapping_ids=[],
        ),
        MutationContext(
            user_scope_id=SCOPE, idempotency_key="task-key-1", request_id=_uid(720),
        ),
    )

    outcome = tasks.run_once()

    assert outcome.task_id == accepted.task_id
    # No identity mapping for the legacy game id: the originals are retained
    # but the import cannot be called published for downstream consumers.
    assert outcome.state is TaskState.partial
    view = tasks.get_task(accepted.task_id)
    assert view.task.domain_result.code == "retained_only"  # no usable mapping
    assert _count(repository, "ImportSnapshot") == 1
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0
