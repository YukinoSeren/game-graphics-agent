"""IMP-T02: capture-scoped record allocation, reference resolution and isolation.

The mapper reads a frozen group through the managed-file service only.  It never
merges records across capture scopes, never lets a later duplicate win, and
never raises or lowers the original record status.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from game_app.contracts.enums import LegacyRecordState
from game_app.contracts.knowledge import (
    BoundarySemantics,
    ImportedRecord,
    RecordExtension,
    Source,
    SourceRevision,
)
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileEntry, FileManifest, FileService
from game_app.importer import (
    COLLECTOR_RUN_FORMAT_ID,
    COLLECTOR_REQUIRED_FILES,
    FormatInspection,
    FrozenGroup,
    FrozenInputReader,
    ImportValidator,
    ScopedReferenceMapper,
)
from game_app.repository import ProductRepository, canonical_json

SCOPE = "a" * 32
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "product"
SMOKE_RUN = FIXTURES / "collector-run-smoke" / "v1"


def _uid(value: int) -> str:
    return f"{value:032x}"


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _source(source_id="src-0001", revision_id="rev-0001", url="https://example.invalid/guide"):
    return {
        "id": source_id,
        "revision_id": revision_id,
        "url": url,
        "retrieved_at": "2026-11-01T00:01:00+00:00",
        "role": "official_version_compatibility",
        "status": "extracted",
        "lineage_status": "unknown",
    }


def _record(
    local_id: str,
    record_type: str,
    payload,
    *,
    scope: str = "src-0001",
    source_id: str = "src-0001",
    revision_id: str = "rev-0001",
    game_id: str = "0123456789abcdef",
    **extra,
):
    row = {
        "id": f"{scope}:{local_id}",
        "local_id": local_id,
        "record_type": record_type,
        "record_status": "accepted",
        "recommendation_status": None,
        "applicability": "unknown",
        "reference_scope": scope,
        "source_id": source_id,
        "revision_id": revision_id,
        "game_id": game_id,
        "payload": payload,
    }
    row.update(extra)
    return row


def _run_files(
    *,
    run_id="run-1",
    game_id="0123456789abcdef",
    sources=None,
    records=None,
    extractions=None,
):
    return {
        "run.json": _json({
            "id": run_id,
            "contract_version": "0.1",
            "started_at": "2026-11-01T00:00:00+00:00",
            "finished_at": "2026-11-01T00:05:00+00:00",
            "input": {"game_name": "Synthetic Game", "mode": "discover",
                      "platform_scope": "Windows PC", "seed_urls": []},
            "game_id": game_id,
            "status": "completed_scope",
            "stop_reason": "coverage_reached",
            "interrupted": False,
        }),
        "sources.json": _json([_source()] if sources is None else sources),
        "records.json": _json([] if records is None else records),
        "gaps.json": _json([]),
        "extractions.json": _json([] if extractions is None else extractions),
    }


def _service(tmp_path: Path) -> tuple[FileService, ProductRepository]:
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    return FileService(repository, tmp_path / "managed"), repository


def _freeze(files: FileService, tmp_path: Path, name: str, members: dict[str, bytes]) -> FrozenGroup:
    directory = tmp_path / f"run-{name}"
    directory.mkdir()
    for relative_name, data in members.items():
        (directory / relative_name).write_bytes(data)
    return FrozenInputReader(files).freeze(
        directory, user_scope_id=SCOPE, idempotency_key=f"freeze-{name}"
    )


def _group(files: FileService, members: dict[str, bytes], *, name: str) -> FrozenGroup:
    """A frozen group as it looks after IMP-T01, without re-running its probe.

    Each member is a real managed artifact, so the mapper reads real bytes.
    """
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
        operation_id=_uid(hash(name) & 0xFFFF),
        file_ids=file_ids,
        manifest=FileManifest(entries=entries),
        inspection=FormatInspection(
            format_id=COLLECTOR_RUN_FORMAT_ID,
            required_files=list(COLLECTOR_REQUIRED_FILES),
            missing_files=[],
            limitations=[],
        ),
    )


def _map(files: FileService, group: FrozenGroup, *, import_id: str, mappings=()):
    return ScopedReferenceMapper(files).map(
        group, import_snapshot_id=import_id, identity_mappings=list(mappings)
    )


def _accepted(result) -> list[ImportedRecord]:
    return [
        record for record in result.records
        if record.id in set(result.validation.accepted_record_ids)
    ]


def _diagnostic(result) -> list[ImportedRecord]:
    return [
        record for record in result.records
        if record.id in set(result.validation.diagnostic_record_ids)
    ]


def _check(record: ImportedRecord, check_id: str):
    return next(check for check in record.checks if check.check_id == check_id)


# --- scope and identity ------------------------------------------------------


def test_same_local_id_in_two_runs_and_sources_stays_two_records(tmp_path):
    files, _ = _service(tmp_path)
    payload = {"name": "Synthetic preset", "settings": []}
    first = _freeze(files, tmp_path, "a", _run_files(
        run_id="run-a",
        records=[_record("cfg-0001", "configurations", payload)],
        extractions=[],
    ))
    second = _freeze(files, tmp_path, "b", _run_files(
        run_id="run-b",
        records=[_record("cfg-0001", "configurations", {**payload, "name": "Other"})],
    ))

    left = _map(files, first, import_id=_uid(1))
    right = _map(files, second, import_id=_uid(2))

    left_record = _accepted(left)[0]
    right_record = _accepted(right)[0]
    assert left_record.record_snapshot_id != right_record.record_snapshot_id
    assert left_record.local_record_id == right_record.local_record_id == "cfg-0001"
    assert left_record.import_snapshot_id != right_record.import_snapshot_id
    assert left_record.payload != right_record.payload
    assert left_record.payload_hash != right_record.payload_hash


def test_record_identity_is_its_entity_header(tmp_path):
    files, _ = _service(tmp_path)
    group = _freeze(files, tmp_path, "same", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))

    result = _map(files, group, import_id=_uid(1))

    record = _accepted(result)[0]
    assert record.record_snapshot_id == record.id


def test_scope_and_legacy_id_are_copied_verbatim(tmp_path):
    files, _ = _service(tmp_path)
    group = _freeze(files, tmp_path, "verbatim", _run_files(
        sources=[_source("scope:odd", "rev-0001")],
        records=[_record(
            "cfg:with:colons", "configurations", {"name": "P", "settings": []},
            scope="scope:odd", source_id="scope:odd",
        )],
    ))

    result = _map(files, group, import_id=_uid(1))

    record = _accepted(result)[0]
    assert record.capture_scope_id == "scope:odd"
    assert record.local_record_id == "cfg:with:colons"
    assert record.legacy_record_id == "scope:odd:cfg:with:colons"
    # No colon splitting: the scope is never derived from the legacy id.
    assert record.capture_scope_id != "scope"


# --- duplicate keys ----------------------------------------------------------


def test_duplicate_key_is_diagnostic_and_never_last_write_wins(tmp_path):
    files, _ = _service(tmp_path)
    members = _run_files(records=[
        _record("cfg-0001", "configurations", {"name": "first", "settings": []}),
        _record("cfg-0001", "configurations", {"name": "second", "settings": []}),
    ])
    group = _group(files, members, name="duplicate")

    result = _map(files, group, import_id=_uid(1))

    assert len(result.records) == 2
    assert _accepted(result) == []
    assert {record.id for record in _diagnostic(result)} == {
        record.id for record in result.records
    }
    assert {record.payload["name"] for record in result.records} == {"first", "second"}
    assert all(
        _check(record, "import.reference").status.value == "fail"
        or any("duplicate" in (check.note or "") for check in record.checks)
        for record in result.records
    )


def test_four_part_key_is_unique_for_usable_records(tmp_path):
    files, repository = _service(tmp_path)
    group = _freeze(files, tmp_path, "keys", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))
    result = _map(files, group, import_id=_uid(1))
    record = _accepted(result)[0]

    with repository.unit_of_work() as uow:
        uow.claim_imported_record_key(
            record_snapshot_id=record.id,
            import_snapshot_id=record.import_snapshot_id,
            capture_scope_id=record.capture_scope_id,
            record_type=record.record_type,
            local_record_id=record.local_record_id,
            usable=True,
        )
    with pytest.raises(ContractError) as caught:
        with repository.unit_of_work() as uow:
            uow.claim_imported_record_key(
                record_snapshot_id=_uid(999),
                import_snapshot_id=record.import_snapshot_id,
                capture_scope_id=record.capture_scope_id,
                record_type=record.record_type,
                local_record_id=record.local_record_id,
                usable=True,
            )
    assert caught.value.code is ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT


# --- reference resolution and isolation --------------------------------------


def test_bad_setting_foreign_key_isolates_that_record_and_keeps_the_rest(tmp_path):
    files, _ = _service(tmp_path)
    records = [
        _record("set-0001", "settings", {
            "id": "set-0001", "raw_name": "Synthetic setting",
            "canonical_name": None, "aliases": [],
            "proof": {"block_id": "b1", "quote": "q", "scope": "page"},
        }),
        _record("cfg-0001", "configurations", {
            "id": "cfg-0001", "name": "Broken", "kind": "resolved_configuration",
            "assignments": [{"setting_id": "set-9999", "value": {"value_kind": "fixed", "value": 1},
                             "evidence_ids": ["evi-0001"]}],
            "preset_reference": None, "environment_ids": [], "source_capture_complete": True,
            "runtime_complete": False,
            "proof": {"block_id": "b1", "quote": "q", "scope": "page"},
        }),
        _record("evi-0001", "evidence", {
            "id": "evi-0001", "kind": "setting_description", "origin": "source_statement",
            "statement": "synthetic", "setting_ids": [], "input_ids": [],
        }),
    ]
    group = _freeze(files, tmp_path, "badref", _run_files(records=records, extractions=[]))

    result = _map(files, group, import_id=_uid(1))

    accepted = {record.local_record_id for record in _accepted(result)}
    diagnostic = {record.local_record_id for record in _diagnostic(result)}
    assert accepted == {"set-0001", "evi-0001"}
    assert diagnostic == {"cfg-0001"}
    broken = next(record for record in _diagnostic(result) if record.local_record_id == "cfg-0001")
    assert "set-9999" in _check(broken, "import.reference").note


def test_isolation_propagates_to_dependents_only(tmp_path):
    files, _ = _service(tmp_path)
    setting = {
        "id": "set-0001", "raw_name": "Synthetic setting",
        "canonical_name": None, "aliases": [],
        "proof": {"block_id": "b1", "quote": "q", "scope": "page"},
    }
    records = [
        _record("set-0001", "settings", setting),
        # A duplicate makes both copies diagnostic.
        _record("set-0001", "settings", dict(setting, raw_name="Duplicate")),
        _record("cfg-0001", "configurations", {
            "id": "cfg-0001", "name": "Depends on duplicate", "kind": "resolved_configuration",
            "assignments": [{"setting_id": "set-0001",
                             "value": {"value_kind": "fixed", "value": 1},
                             "evidence_ids": ["evi-0001"]}],
            "preset_reference": None, "environment_ids": [],
            "source_capture_complete": False, "runtime_complete": False,
            "proof": {"block_id": "b1", "quote": "q", "scope": "page"},
        }, id="src-0001:cfg-0001"),
        _record("evi-0001", "evidence", {
            "id": "evi-0001", "kind": "setting_description", "origin": "source_statement",
            "statement": "synthetic", "setting_ids": [], "input_ids": [],
        }),
        _record("evi-0002", "evidence", {
            "id": "evi-0002", "kind": "setting_description", "origin": "source_statement",
            "statement": "independent", "setting_ids": [], "input_ids": [],
        }),
    ]
    group = _group(files, _run_files(records=records), name="propagate")

    result = _map(files, group, import_id=_uid(1))

    accepted = {record.local_record_id for record in _accepted(result)}
    diagnostic = {record.local_record_id for record in _diagnostic(result)}
    assert accepted == {"evi-0001", "evi-0002"}
    assert diagnostic == {"set-0001", "cfg-0001"}


def test_references_allocate_before_resolution_regardless_of_row_order(tmp_path):
    files, _ = _service(tmp_path)
    configuration = _record("cfg-0001", "configurations", {
        "id": "cfg-0001", "name": "Uses a later setting", "kind": "resolved_configuration",
        "assignments": [{"setting_id": "set-0001", "value": {"value_kind": "fixed", "value": 1},
                         "evidence_ids": ["evi-0001"]}],
        "preset_reference": None, "environment_ids": [], "source_capture_complete": True,
        "runtime_complete": False,
        "proof": {"block_id": "b1", "quote": "q", "scope": "page"},
    })
    setting = _record("set-0001", "settings", {
        "id": "set-0001", "raw_name": "Later", "canonical_name": None, "aliases": [],
        "proof": {"block_id": "b1", "quote": "q", "scope": "page"},
    })
    evidence = _record("evi-0001", "evidence", {
        "id": "evi-0001", "kind": "setting_description", "origin": "source_statement",
        "statement": "synthetic", "setting_ids": ["set-0001"], "input_ids": [],
    })
    group = _freeze(files, tmp_path, "order", _run_files(
        records=[configuration, setting, evidence], extractions=[],
    ))

    result = _map(files, group, import_id=_uid(1))

    assert {record.local_record_id for record in _accepted(result)} == {
        "cfg-0001", "set-0001", "evi-0001",
    }
    assert _diagnostic(result) == []


def test_superseded_target_missing_stays_unknown_and_is_not_filled_from_another_run(
    tmp_path,
):
    files, _ = _service(tmp_path)
    other = _freeze(files, tmp_path, "other", _run_files(
        run_id="run-other",
        records=[_record("cfg-0001", "configurations", {"name": "Other run", "settings": []})],
    ))
    group = _freeze(files, tmp_path, "missing-superseded", _run_files(
        records=[_record(
            "cfg-0002", "configurations", {"name": "Superseded", "settings": []},
            superseded_by="src-0001:cfg-0001",
        )],
    ))

    result = _map(files, group, import_id=_uid(1))
    other_result = _map(files, other, import_id=_uid(2))

    record = next(item for item in result.records if item.local_record_id == "cfg-0002")
    check = _check(record, "import.superseded_by")
    assert check.status.value == "unknown"
    assert "src-0001:cfg-0001" in (check.note or "")
    # The pointer is never resolved against another run's identically named row.
    assert result.validation.accepted_record_ids == [record.id]
    assert other_result.records[0].id != record.id
    assert record.upstream_envelope["superseded_by"] == "src-0001:cfg-0001"


# --- prototype facts survive -------------------------------------------------


def test_quarantined_stays_quarantined_and_accepted_is_not_upgraded(tmp_path):
    files, _ = _service(tmp_path)
    group = _freeze(files, tmp_path, "status", _run_files(
        records=[
            _record("set-0001", "settings", {
                "id": "set-0001", "raw_name": "Accepted", "canonical_name": None,
                "aliases": [], "proof": {"block_id": "b1", "quote": "q", "scope": "page"},
            }),
            _record("set-0002", "settings", {
                "id": "set-0002", "raw_name": "Quarantined", "canonical_name": None,
                "aliases": [], "proof": {"block_id": "b1", "quote": "q", "scope": "page"},
            }, record_status="quarantined"),
        ],
        extractions=[],
    ))

    result = _map(files, group, import_id=_uid(1))

    accepted = next(item for item in result.records if item.local_record_id == "set-0001")
    quarantined = next(item for item in result.records if item.local_record_id == "set-0002")
    assert accepted.upstream_status is LegacyRecordState.accepted
    assert quarantined.upstream_status is LegacyRecordState.quarantined
    assert quarantined.id in result.validation.diagnostic_record_ids
    # Import success neither performs nor claims a new applicability decision.
    assert _check(accepted, "import.applicability").status.value == "not_run"
    assert _check(accepted, "import.identity").status.value == "not_run"
    with _service(tmp_path)[1].unit_of_work() as uow:
        objects = uow.iter_objects("KnowledgePackage")
    assert objects == []


def test_legacy_extraction_without_a_returned_model_is_null_and_explained(tmp_path):
    files, _ = _service(tmp_path)
    group = _freeze(files, tmp_path, "model", _run_files(
        records=[],
        extractions=[{
            "source_id": "src-0001",
            "status": "ok",
            "model": "synthetic-model",
            "started_at": "2026-11-01T00:02:00+00:00",
            "raw": "{}",
            "parsed": {},
        }],
    ))

    result = _map(files, group, import_id=_uid(1))

    assert len(result.extractions) == 1
    extraction = result.extractions[0]
    assert extraction.returned_model is None
    assert extraction.model_requested == "synthetic-model"
    assert any("actual returned model" in note for note in extraction.limitations)
    with _service(tmp_path)[1].unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0
        assert uow.iter_objects("TaskRun") == []


def test_payload_hash_and_manifest_byte_hash_are_different_and_recomputable(tmp_path):
    files, _ = _service(tmp_path)
    payload = {"name": "Synthetic preset", "settings": [{"k": 1}]}
    members = _run_files(
        records=[_record("cfg-0001", "configurations", payload)],
    )
    group = _freeze(files, tmp_path, "hash", members)

    result = _map(files, group, import_id=_uid(1))

    record = _accepted(result)[0]
    assert record.payload_hash == hashlib.sha256(canonical_json(payload)).hexdigest()
    assert record.payload_hash != hashlib.sha256(members["records.json"]).hexdigest()
    entry = next(item for item in group.manifest.entries if item.relative_name == "records.json")
    assert entry.sha256 == hashlib.sha256(members["records.json"]).hexdigest()
    assert files.read_managed_file(group.file_ids["records.json"]) == members["records.json"]


def test_upstream_envelope_keeps_every_original_field(tmp_path):
    files, _ = _service(tmp_path)
    record = _record(
        "cfg-0001", "configurations", {"name": "P", "settings": []},
        issues=["synthetic_issue"], extraction_status="partial", custom_note="keep me",
        superseded_by=None,
    )
    group = _freeze(files, tmp_path, "envelope", _run_files(records=[record], extractions=[]))

    result = _map(files, group, import_id=_uid(1))

    stored = _accepted(result)[0]
    assert stored.upstream_envelope == record
    assert stored.payload == record["payload"]
    assert "custom_note" not in stored.payload


# --- sources -----------------------------------------------------------------


def test_source_and_revision_are_mapped_from_the_frozen_sources_file(tmp_path):
    files, _ = _service(tmp_path)
    group = _freeze(files, tmp_path, "sources", _run_files(
        sources=[_source(url="HTTPS://Example.INVALID/Guide?p=1#frag")],
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))

    result = _map(files, group, import_id=_uid(1))

    assert len(result.sources) == 1
    source = result.sources[0]
    assert isinstance(source, Source)
    assert source.original_url == "HTTPS://Example.INVALID/Guide?p=1#frag"
    assert source.canonical_url == "https://example.invalid/Guide?p=1"
    revision = result.source_revisions[0]
    assert isinstance(revision, SourceRevision)
    assert revision.source_id == source.id
    assert revision.capture_scope == "src-0001"
    assert revision.upstream_revision_id == "rev-0001"
    assert revision.document_file_id is None
    assert revision.content_hash is None
    record = _accepted(result)[0]
    assert record.source_revision_id == revision.id


def test_two_sources_with_the_same_url_are_not_merged(tmp_path):
    files, _ = _service(tmp_path)
    group = _freeze(files, tmp_path, "two-sources", _run_files(
        sources=[_source("src-0001", "rev-0001"), _source("src-0002", "rev-0002")],
        records=[
            _record("cfg-0001", "configurations", {"name": "A", "settings": []},
                    scope="src-0001", source_id="src-0001", revision_id="rev-0001"),
            _record("cfg-0001", "configurations", {"name": "B", "settings": []},
                    scope="src-0002", source_id="src-0002", revision_id="rev-0002"),
        ],
    ))

    result = _map(files, group, import_id=_uid(1))

    assert {source.canonical_url for source in result.sources} == {
        "https://example.invalid/guide"
    }
    assert len(result.sources) == 2
    assert {revision.capture_scope for revision in result.source_revisions} == {
        "src-0001", "src-0002",
    }
    assert len(_accepted(result)) == 2


# --- boundaries --------------------------------------------------------------


def test_interval_evidence_records_unknown_strictness(tmp_path):
    files, _ = _service(tmp_path)
    group = _freeze(files, tmp_path, "bounds", _run_files(
        records=[_record("evi-0001", "evidence", {
            "id": "evi-0001", "kind": "measurement", "origin": "source_author_measurement",
            "statement": "roughly 55-60 fps",
            "metric": "average_fps", "unit": "fps",
            "numeric": {"number_kind": "interval", "lower": 55, "upper": 60},
            "input_ids": [], "setting_ids": [],
        })],
        extractions=[],
    ))

    result = _map(files, group, import_id=_uid(1))

    record = _accepted(result)[0]
    extension = record.extension
    assert isinstance(extension, RecordExtension)
    bounds = extension.boundary_semantics
    assert isinstance(bounds, BoundarySemantics)
    assert bounds.lower_inclusive is None
    assert bounds.upper_inclusive is None
    assert bounds.status.value == "unknown"
    # The original numeric shape is untouched.
    assert record.payload["numeric"] == {
        "number_kind": "interval", "lower": 55, "upper": 60,
    }


def test_identity_mapping_must_name_the_import_namespace(tmp_path):
    files, _ = _service(tmp_path)
    group = _freeze(files, tmp_path, "identity", _run_files(
        records=[], extractions=[],
    ))

    result = _map(files, group, import_id=_uid(1))

    assert result.game_id is None
    assert any("game" in item for item in result.limitations)


def test_validator_and_mapper_agree_on_the_validation_shape(tmp_path):
    files, _ = _service(tmp_path)
    group = _freeze(files, tmp_path, "shape", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
    ))

    mapped = ScopedReferenceMapper(files).map(group, import_snapshot_id=_uid(1))
    validated = ImportValidator(ScopedReferenceMapper(files)).validate(
        group, import_snapshot_id=_uid(1)
    )

    assert set(validated.model_dump()) == {
        "accepted_record_ids", "diagnostic_record_ids", "checks"
    }
    assert mapped.validation.accepted_record_ids == validated.accepted_record_ids
    assert mapped.validation.diagnostic_record_ids == validated.diagnostic_record_ids


def test_missing_required_member_is_an_inconsistent_group(tmp_path):
    files, _ = _service(tmp_path)
    members = _run_files(records=[])
    members.pop("records.json")
    group = _group(files, members, name="missing")

    with pytest.raises(ContractError) as caught:
        _map(files, group, import_id=_uid(1))
    assert caught.value.code is ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT


def test_record_group_vocabulary_matches_the_collector():
    from game_app.importer import COLLECTOR_RECORD_GROUPS
    from game_collector.schema import RECORD_GROUPS

    assert COLLECTOR_RECORD_GROUPS == tuple(RECORD_GROUPS)


def test_mapping_writes_no_product_fact(tmp_path):
    from game_app.importer import COLLECTOR_REQUIRED_FILES  # noqa: F401

    files, repository = _service(tmp_path)
    group = _freeze(files, tmp_path, "readonly", _run_files(
        records=[_record("cfg-0001", "configurations", {"name": "P", "settings": []})],
        extractions=[{"source_id": "src-0001", "status": "ok", "model": "m"}],
    ))
    before = {
        entity: len(_objects(repository, entity))
        for entity in ("ImportedRecord", "ImportSnapshot", "TaskRun", "ExternalCallRecord",
                       "Source", "SourceRevision", "ImportedExtraction")
    }

    _map(files, group, import_id=_uid(1))

    after = {
        entity: len(_objects(repository, entity))
        for entity in before
    }
    assert after == before
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0


def _objects(repository, entity_type: str) -> list:
    with repository.unit_of_work() as uow:
        return uow.iter_objects(entity_type)


# --- 2D-C: inline source document facts --------------------------------------


def _managed_file_count(repository) -> int:
    return len(_objects(repository, "ManagedFile"))


def _source_with_document(content_hash: str) -> dict:
    row = _source()
    row["document"] = {
        "content_hash": content_hash,
        "truncated": True,
        "retrieval_method": "http_fetch",
    }
    return row


def test_inline_document_facts_reach_the_source_revision(tmp_path):
    files, repository = _service(tmp_path)
    content_hash = "c" * 64
    group = _freeze(files, tmp_path, "document", _run_files(
        sources=[_source_with_document(content_hash)],
        records=[],
    ))
    before = _managed_file_count(repository)

    result = _map(files, group, import_id=_uid(1))

    revision = result.source_revisions[0]
    assert revision.content_hash == content_hash
    assert revision.truncated is True
    assert revision.capture_method == "http_fetch"
    # The inline document stays inside the frozen sources.json original; it is
    # never registered as a second managed file.
    assert revision.document_file_id is None
    check = next(item for item in revision.checks if item.check_id == "source.document")
    assert check.status.value == "not_run"
    assert "frozen" in (check.note or "")
    assert _managed_file_count(repository) == before


def test_a_source_without_a_document_keeps_the_not_run_check(tmp_path):
    files, _ = _service(tmp_path)
    group = _freeze(files, tmp_path, "no-document", _run_files(
        sources=[_source()],
        records=[],
    ))

    result = _map(files, group, import_id=_uid(1))

    revision = result.source_revisions[0]
    assert revision.content_hash is None
    assert revision.truncated is False
    assert revision.capture_method == "collector_run_metadata"
    check = next(item for item in revision.checks if item.check_id == "source.document")
    assert check.status.value == "not_run"
