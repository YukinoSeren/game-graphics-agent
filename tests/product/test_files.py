"""ENG-T03: managed files, publish/commit windows, recovery and backup.

All fault injection targets temporary directories and databases; nothing here
touches a player library or a real provider.
"""

import hashlib
import json
import sqlite3

import pytest

from game_app.contracts.common import MutationContext
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService, PublishHooks
from game_app.repository import ProductRepository, canonical_json


class SimulatedCrash(RuntimeError):
    """Stands in for the process exiting at an injected boundary."""


def _uid(value: int) -> str:
    return f"{value:032x}"


def _ctx(key: str = "file-key") -> MutationContext:
    return MutationContext(user_scope_id=_uid(90), idempotency_key=key, request_id=_uid(91))


def _publish(service: FileService, key: str = "file-key", *, hooks=None, writer=None, files=None):
    files = files or {"inputs/one.json": b'{"value": 1}\n'}
    request_hash = hashlib.sha256(canonical_json({
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()
    })).hexdigest()
    return service.publish(
        kind="import",
        files=files,
        user_scope_id=_uid(90),
        method="POST",
        canonical_route="/api/knowledge-imports",
        key=key,
        request_schema_version="product-contract/0.4",
        request_hash=request_hash,
        writer=writer,
        hooks=hooks,
    )


def _objects_dir(service: FileService):
    return service.root / "objects"


def test_publish_creates_managed_file_and_committed_operation(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")

    result = _publish(service)
    assert len(result.file_ids) == 1
    assert service.read_managed_file(result.file_ids[0]) == b'{"value": 1}\n'
    operation = service.get_operation(result.operation_id)
    assert operation.phase == "committed"
    stored = repo.get_object("ManagedFile", result.file_ids[0])
    assert stored.payload["file_status"] == "published"
    assert stored.payload["relative_path"] == f"objects/{result.file_ids[0]}"


def test_crash_after_rename_leaves_unconsumable_orphan_and_retry_reuses_it(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")

    def crash(_path):
        raise SimulatedCrash("process exited after rename")

    with pytest.raises(SimulatedCrash):
        _publish(service, hooks=PublishHooks(after_rename=crash))

    operation = service.get_operation_by_key(_uid(90), "POST", "/api/knowledge-imports", "file-key")
    assert operation is not None and operation.phase == "files_ready"
    file_id = operation.file_ids[0]
    final = _objects_dir(service) / file_id
    assert final.is_file()
    # The complete file exists but no consumer may read it yet.
    with pytest.raises(ContractError) as caught:
        service.read_managed_file(file_id)
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND

    retried = _publish(service)
    assert retried.file_ids == [file_id]
    assert service.read_managed_file(file_id) == b'{"value": 1}\n'
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM objects WHERE entity_type='ManagedFile'")[0]["n"] == 1


def test_crash_after_commit_before_response_returns_same_result(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")
    writer_calls: list[str] = []

    def writer(uow, operation, managed_files):
        writer_calls.append(operation.operation_id)
        uow.insert_object_bytes(
            "ImportSnapshot",
            schema_version="product-contract/0.4",
            raw=canonical_json({"id": _uid(5), "state": "validated"}),
            object_id=_uid(5),
        )
        return []

    def crash_after_commit():
        raise SimulatedCrash("response lost after commit")

    with pytest.raises(SimulatedCrash):
        _publish(service, hooks=PublishHooks(after_commit=crash_after_commit), writer=writer)

    operation = service.get_operation_by_key(_uid(90), "POST", "/api/knowledge-imports", "file-key")
    assert operation.phase == "committed"
    retried = _publish(service, writer=writer)
    assert retried.operation_id == operation.operation_id
    assert writer_calls == [operation.operation_id]  # no second domain write
    assert repo.get_object("ImportSnapshot", _uid(5)) is not None


def test_tampered_file_blocks_the_commit(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")

    def tamper(path):
        path.write_bytes(b"not the staged bytes")

    with pytest.raises(ContractError) as caught:
        _publish(service, hooks=PublishHooks(after_rename=tamper))
    assert caught.value.code is ErrorCode.ARTIFACT_UNAVAILABLE
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM objects WHERE entity_type='ManagedFile'")[0]["n"] == 0


def test_missing_file_at_commit_is_unavailable(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")

    def delete_final():
        for path in _objects_dir(service).glob("*"):
            path.unlink()

    with pytest.raises(ContractError) as caught:
        _publish(service, hooks=PublishHooks(before_commit=delete_final))
    assert caught.value.code is ErrorCode.ARTIFACT_UNAVAILABLE


def test_disk_write_and_occupied_destination_fail_without_publishing(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")

    def disk_full(_path):
        raise OSError("simulated disk full")

    with pytest.raises(ContractError) as caught:
        _publish(service, hooks=PublishHooks(before_write=disk_full))
    assert caught.value.code is ErrorCode.STORAGE_COMMIT_FAILED
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM objects WHERE entity_type='ManagedFile'")[0]["n"] == 0

    def occupied(_path):
        raise PermissionError("file is in use")

    with pytest.raises(ContractError) as caught:
        _publish(service, key="file-key-2", hooks=PublishHooks(before_write=occupied))
    assert caught.value.code is ErrorCode.STORAGE_COMMIT_FAILED

    # A clean retry with the same key still publishes exactly once.
    result = _publish(service, key="file-key-2")
    assert service.read_managed_file(result.file_ids[0]) == b'{"value": 1}\n'


def test_same_key_with_different_content_conflicts(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")
    _publish(service)
    with pytest.raises(ContractError) as caught:
        _publish(service, files={"inputs/one.json": b'{"value": 2}\n'})
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_tombstoned_file_key_is_gone(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")
    _publish(service)
    with repo.unit_of_work() as uow:
        uow.tombstone_idempotency(_uid(90), "POST", "/api/knowledge-imports", "file-key")
    with pytest.raises(ContractError) as caught:
        _publish(service)
    assert caught.value.code is ErrorCode.RESOURCE_GONE


def test_abandon_removes_staging_and_keeps_operation_history(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")

    def crash(_path):
        raise SimulatedCrash("stop before commit")

    with pytest.raises(SimulatedCrash):
        _publish(service, hooks=PublishHooks(after_rename=crash))
    operation = service.get_operation_by_key(_uid(90), "POST", "/api/knowledge-imports", "file-key")
    service.abandon(operation.operation_id, reason="test cleanup")
    assert service.get_operation(operation.operation_id).phase == "abandoned"
    assert not (service.root / "staging" / operation.operation_id).exists()


def test_backup_uses_a_consistent_snapshot(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")
    published = _publish(service)

    with repo.unit_of_work() as uow:
        uow.insert_object_bytes(
            "Game",
            schema_version="product-contract/0.4",
            raw=canonical_json({"id": _uid(2), "name": "before backup"}),
            object_id=_uid(2),
        )

    destination = tmp_path / "backups" / "001"
    service.backup(destination)

    backup_db = destination / "product.sqlite3"
    assert backup_db.is_file()
    with sqlite3.connect(backup_db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        ids = {row[0] for row in conn.execute("SELECT id FROM objects")}
    assert _uid(2) in ids
    assert published.file_ids[0] in ids
    # Files and manifest are part of the same snapshot.
    manifest = json.loads((destination / "backup-manifest.json").read_text(encoding="utf-8"))
    names = {entry["relative_name"] for entry in manifest["entries"]}
    assert "product.sqlite3" in names
    assert f"objects/{published.file_ids[0]}" in names
    assert (destination / "objects" / published.file_ids[0]).is_file()

    # Writes after the backup do not appear in the snapshot.
    with repo.unit_of_work() as uow:
        uow.insert_object_bytes(
            "Game",
            schema_version="product-contract/0.4",
            raw=canonical_json({"id": _uid(3), "name": "after backup"}),
            object_id=_uid(3),
        )
    with sqlite3.connect(backup_db) as conn:
        ids_after = {row[0] for row in conn.execute("SELECT id FROM objects")}
    assert _uid(3) not in ids_after


def test_backup_refuses_an_existing_destination(tmp_path):
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    service = FileService(repo, tmp_path / "managed")
    destination = tmp_path / "backups" / "001"
    destination.mkdir(parents=True)
    (destination / "product.sqlite3").write_bytes(b"not a backup")
    with pytest.raises(ContractError) as caught:
        service.backup(destination)
    assert caught.value.code is ErrorCode.STORAGE_COMMIT_FAILED
