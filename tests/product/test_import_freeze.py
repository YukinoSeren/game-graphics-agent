"""IMP-T01: freezing a stopped collector run and recognising its format.

The reader never rewrites the source directory, never guesses from file names
alone, and refuses a group whose bytes changed while it was being copied.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from game_app.contracts.tasks import KnowledgeImportInput, TaskInputAdapter
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService
from game_app.importer import (
    COLLECTOR_RUN_FORMAT_ID,
    FormatInspection,
    FormatRegistry,
    FreezeHooks,
    FrozenInputReader,
)
from game_app.repository import ProductRepository
from game_app.tasks import DEFAULT_ROUTES

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "product"
SMOKE_RUN = FIXTURES / "collector-run-smoke" / "v1"
REQUIRED = ("run.json", "sources.json", "records.json", "gaps.json", "extractions.json")
SCOPE = "a" * 32


def _copy_run(tmp_path: Path) -> Path:
    """A run directory as the collector leaves it: members only, no fixture manifest."""
    target = tmp_path / "run"
    shutil.copytree(SMOKE_RUN, target)
    (target / "manifest.json").unlink()
    return target


def _reader(tmp_path: Path) -> tuple[FrozenInputReader, ProductRepository]:
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    service = FileService(repository, tmp_path / "managed")
    return FrozenInputReader(service), repository


def _freeze(
    reader: FrozenInputReader,
    directory: Path,
    *,
    key: str = "freeze-1",
    hooks: FreezeHooks | None = None,
):
    return reader.freeze(
        directory,
        user_scope_id=SCOPE,
        idempotency_key=key,
        hooks=hooks,
    )


def _tree_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def _read_json(directory: Path, name: str):
    return json.loads((directory / name).read_text(encoding="utf-8"))


def _write_json(directory: Path, name: str, value) -> None:
    (directory / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _objects(repository: ProductRepository, entity_type: str) -> list:
    with repository.unit_of_work() as uow:
        return uow.iter_objects(entity_type)


# --- format recognition ------------------------------------------------------


def test_standard_group_is_recognised(tmp_path):
    reader, _ = _reader(tmp_path)
    inspection = reader.inspect_format(_copy_run(tmp_path))
    assert inspection.format_id == COLLECTOR_RUN_FORMAT_ID
    assert inspection.required_files == list(REQUIRED)
    assert inspection.missing_files == []


def test_smoke_fixture_matches_its_recorded_manifest(fixture_loader):
    manifest = fixture_loader.load_fixture("collector-run-smoke", 1)
    assert {entry.relative_name for entry in manifest.files} == set(REQUIRED)


def test_missing_records_file_is_rejected_without_publishing(tmp_path):
    run = _copy_run(tmp_path)
    (run / "records.json").unlink()
    reader, repository = _reader(tmp_path)

    inspection = reader.inspect_format(run)
    assert inspection.format_id is None
    assert inspection.missing_files == ["records.json"]

    with pytest.raises(ContractError) as caught:
        _freeze(reader, run)
    assert caught.value.code is ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT
    # "fails before publishing": no managed file, no import snapshot was created.
    assert _objects(repository, "ManagedFile") == []
    assert _objects(repository, "ImportSnapshot") == []


def test_top_level_array_is_not_recognised(tmp_path):
    run = _copy_run(tmp_path)
    (run / "run.json").write_text("[]\n", encoding="utf-8")
    reader, _ = _reader(tmp_path)

    inspection = reader.inspect_format(run)
    assert inspection.format_id is None
    assert inspection.limitations
    assert any("run.json" in item for item in inspection.limitations)


def test_unknown_contract_version_is_not_recognised(tmp_path):
    run = _copy_run(tmp_path)
    run_state = _read_json(run, "run.json")
    run_state["contract_version"] = "9.9"
    _write_json(run, "run.json", run_state)
    reader, _ = _reader(tmp_path)

    inspection = reader.inspect_format(run)
    assert inspection.format_id is None
    assert any("contract_version" in item for item in inspection.limitations)


def test_cross_file_reference_that_cannot_be_located_blocks_the_freeze(tmp_path):
    run = _copy_run(tmp_path)
    records = _read_json(run, "records.json")
    records[0]["source_id"] = "src-9999"
    _write_json(run, "records.json", records)
    reader, repository = _reader(tmp_path)

    inspection = reader.inspect_format(run)
    assert inspection.format_id == COLLECTOR_RUN_FORMAT_ID
    assert any("src-9999" in item for item in inspection.limitations)

    with pytest.raises(ContractError) as caught:
        _freeze(reader, run)
    assert caught.value.code is ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT
    assert _objects(repository, "ManagedFile") == []


def test_record_without_a_source_reference_is_not_recognised(tmp_path):
    run = _copy_run(tmp_path)
    records = _read_json(run, "records.json")
    records[0].pop("source_id")
    _write_json(run, "records.json", records)
    reader, _ = _reader(tmp_path)

    inspection = reader.inspect_format(run)
    assert inspection.format_id is None
    assert any("source_id" in item for item in inspection.limitations)


def test_inspection_model_has_only_the_local_read_only_fields():
    inspection = FormatInspection(
        format_id=None, required_files=[], missing_files=[], limitations=[]
    )
    assert set(inspection.model_dump()) == {
        "format_id", "required_files", "missing_files", "limitations"
    }


def test_wrongly_typed_run_field_is_not_recognised(tmp_path):
    run = _copy_run(tmp_path)
    run_state = _read_json(run, "run.json")
    run_state["stop_reason"] = {"code": "coverage_reached"}
    _write_json(run, "run.json", run_state)
    reader, _ = _reader(tmp_path)

    inspection = reader.inspect_format(run)
    assert inspection.format_id is None
    assert any("stop_reason" in item for item in inspection.limitations)


# --- stable copy and fingerprint ---------------------------------------------


def test_freeze_leaves_the_source_directory_byte_identical(tmp_path):
    run = _copy_run(tmp_path)
    before = _tree_hashes(run)
    reader, _ = _reader(tmp_path)

    frozen = _freeze(reader, run)

    assert frozen.format_id == COLLECTOR_RUN_FORMAT_ID
    assert _tree_hashes(run) == before


def test_freeze_copies_only_the_declared_group_members(tmp_path):
    run = _copy_run(tmp_path)
    (run / "notes.txt").write_text("not part of a collector run\n", encoding="utf-8")
    before = _tree_hashes(run)
    reader, _ = _reader(tmp_path)

    frozen = _freeze(reader, run)

    copied = {entry.relative_name for entry in frozen.manifest.entries}
    assert copied == set(REQUIRED)
    assert "notes.txt" not in copied
    assert _tree_hashes(run) == before


def test_changed_file_during_the_copy_window_is_rejected(tmp_path):
    run = _copy_run(tmp_path)
    reader, repository = _reader(tmp_path)
    original = (run / "sources.json").read_bytes()

    def rewrite_sources():
        (run / "sources.json").write_bytes(original.replace(b"src-0001", b"src-0002", 1))

    with pytest.raises(ContractError) as caught:
        _freeze(reader, run, hooks=FreezeHooks(after_copy=rewrite_sources))
    assert caught.value.code is ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT
    assert _objects(repository, "ManagedFile") == []
    with repository.unit_of_work() as uow:
        assert uow.find_idempotency(
            SCOPE, "POST", "/api/knowledge-imports", "freeze-1"
        ) is None


def test_same_mtime_with_different_bytes_is_still_a_change(tmp_path):
    run = _copy_run(tmp_path)
    reader, repository = _reader(tmp_path)
    path = run / "sources.json"
    original = path.read_bytes()
    stat = path.stat()

    def rewrite_with_same_timestamp():
        # Same length, same mtime: only the hash can tell these apart.
        path.write_bytes(original.replace(b"src-0001", b"src-0002", 1))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert path.stat().st_mtime_ns == stat.st_mtime_ns
        assert len(path.read_bytes()) == len(original)

    with pytest.raises(ContractError) as caught:
        _freeze(reader, run, hooks=FreezeHooks(after_copy=rewrite_with_same_timestamp))
    assert caught.value.code is ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT
    assert _objects(repository, "ManagedFile") == []


def test_manifest_fingerprint_is_name_length_and_hash(tmp_path):
    reader, _ = _reader(tmp_path)
    frozen = _freeze(reader, _copy_run(tmp_path))

    entries = frozen.manifest.entries
    assert [entry.relative_name for entry in entries] == sorted(REQUIRED)
    for entry in entries:
        data = (SMOKE_RUN / entry.relative_name).read_bytes()
        assert entry.byte_length == len(data)
        assert entry.sha256 == hashlib.sha256(data).hexdigest()


def test_derived_report_is_archived_as_markdown(tmp_path):
    run = _copy_run(tmp_path)
    (run / "report.md").write_text("# synthetic report\n", encoding="utf-8")
    reader, _ = _reader(tmp_path)

    frozen = _freeze(reader, run)

    entry = next(
        item for item in frozen.manifest.entries if item.relative_name == "report.md"
    )
    assert entry.media_type == "text/markdown"


def test_freeze_is_idempotent_for_the_same_key_and_bytes(tmp_path):
    run = _copy_run(tmp_path)
    reader, _ = _reader(tmp_path)

    first = _freeze(reader, run, key="same-key")
    second = _freeze(reader, run, key="same-key")

    assert second.operation_id == first.operation_id
    assert second.file_ids == first.file_ids


# --- derived candidates ------------------------------------------------------


def test_candidates_subset_of_rebuilt_set_is_accepted(tmp_path):
    run = _copy_run(tmp_path)
    records = _read_json(run, "records.json")
    derived = [row for row in records if row["record_type"] == "configurations"]
    _write_json(run, "candidates.json", derived)
    reader, _ = _reader(tmp_path)

    frozen = _freeze(reader, run)

    assert frozen.format_id == COLLECTOR_RUN_FORMAT_ID
    assert [entry.relative_name for entry in frozen.manifest.entries] == sorted(
        [*REQUIRED, "candidates.json"]
    )


def test_candidates_with_unknown_local_id_are_rejected(tmp_path):
    run = _copy_run(tmp_path)
    records = _read_json(run, "records.json")
    derived = [row for row in records if row["record_type"] == "configurations"]
    spliced = [dict(derived[0], local_id="cfg-9999")]
    _write_json(run, "candidates.json", spliced)
    reader, repository = _reader(tmp_path)

    with pytest.raises(ContractError) as caught:
        _freeze(reader, run)
    assert caught.value.code is ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT
    assert _objects(repository, "ManagedFile") == []
    assert reader.files.get_operation_by_key(
        SCOPE, "POST", "/api/knowledge-imports", "freeze-1"
    ) is None


def test_missing_candidates_file_is_only_a_limitation(tmp_path):
    reader, _ = _reader(tmp_path)
    inspection = reader.inspect_format(_copy_run(tmp_path))
    assert inspection.format_id == COLLECTOR_RUN_FORMAT_ID
    assert any("candidates.json" in item for item in inspection.limitations)


def test_candidate_rebuild_ignores_excluded_and_superseded_records(tmp_path):
    run = _copy_run(tmp_path)
    records = _read_json(run, "records.json")
    kept = dict(records[0])
    excluded = dict(records[0], local_id="cfg-0002", recommendation_status="excluded_branch")
    superseded = dict(records[0], local_id="cfg-0003", superseded_by="src-0001:cfg-0001")
    _write_json(run, "records.json", [kept, excluded, superseded, records[1]])
    # The on-disk derived list names a record the exact rebuild drops.
    _write_json(run, "candidates.json", [excluded])
    reader, _ = _reader(tmp_path)

    with pytest.raises(ContractError) as caught:
        _freeze(reader, run)
    assert caught.value.code is ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT


# --- prototype facts survive -------------------------------------------------


def test_interrupted_run_is_frozen_without_fabricating_execution(tmp_path):
    run = _copy_run(tmp_path)
    run_state = _read_json(run, "run.json")
    run_state.update(interrupted=True, status="partial_scope", stop_reason="budget_exhausted")
    run_state.pop("finished_at")
    _write_json(run, "run.json", run_state)
    reader, repository = _reader(tmp_path)

    frozen = _freeze(reader, run)

    assert frozen.format_id == COLLECTOR_RUN_FORMAT_ID
    joined = " ".join(frozen.inspection.limitations)
    assert "interrupted" in joined
    assert "partial_scope" in joined
    assert "budget_exhausted" in joined
    # The absence of finished_at is reported as a limitation, not invented.
    assert "finished_at" in joined
    assert _objects(repository, "TaskRun") == []
    assert _objects(repository, "ImportSnapshot") == []
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0


def test_frozen_bytes_keep_the_original_interrupted_fact(tmp_path):
    run = _copy_run(tmp_path)
    run_state = _read_json(run, "run.json")
    run_state.update(interrupted=True, status="partial_scope", stop_reason="source_unavailable")
    _write_json(run, "run.json", run_state)
    reader, _ = _reader(tmp_path)

    frozen = _freeze(reader, run)

    stored = json.loads(
        reader.files.read_managed_file(frozen.file_ids["run.json"])
    )
    assert stored["interrupted"] is True
    assert stored["status"] == "partial_scope"
    assert stored["stop_reason"] == "source_unavailable"


def test_diagnostic_sqlite_is_preserved_without_writes(tmp_path):
    run = _copy_run(tmp_path)
    database = run / "knowledge.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("INSERT INTO runs VALUES ('smoke-run-0001', '{}')")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    reader, _ = _reader(tmp_path)

    frozen = _freeze(reader, run)

    assert "knowledge.sqlite3" in {entry.relative_name for entry in frozen.manifest.entries}
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


# --- contract supplement -----------------------------------------------------


def _knowledge_import_payload(**overrides):
    payload = {
        "kind": "knowledge_import",
        "file_ids": ["0" * 31 + "1", "0" * 31 + "2"],
        "game_context_id": "b" * 32,
        "mapping_version": "collector-import/1",
        "identity_mapping_ids": ["c" * 32],
    }
    payload.update(overrides)
    return payload


def test_knowledge_import_input_is_part_of_the_task_union():
    typed = TaskInputAdapter.validate_python(_knowledge_import_payload())
    assert isinstance(typed, KnowledgeImportInput)
    assert DEFAULT_ROUTES["knowledge_import"] == "/api/knowledge-imports"


def test_knowledge_import_input_requires_at_least_one_file():
    with pytest.raises(ValidationError):
        TaskInputAdapter.validate_python(_knowledge_import_payload(file_ids=[]))
    with pytest.raises(ValidationError):
        TaskInputAdapter.validate_python(_knowledge_import_payload(kind="knowledge"))


def test_knowledge_import_input_does_not_accept_a_local_path():
    with pytest.raises(ValidationError):
        TaskInputAdapter.validate_python(
            _knowledge_import_payload(file_ids=["C:/runs/latest/run.json"])
        )
