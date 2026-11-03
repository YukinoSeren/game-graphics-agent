"""2D-A: the application layer wires real handlers and refuses to fake success."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from game_app.application import Application
from game_app.contracts.common import MutationContext
from game_app.contracts.enums import TaskState
from game_app.contracts.primitives import utc_now
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileEntry, FileManifest
from game_app.importer import (
    COLLECTOR_RUN_FORMAT_ID,
    COLLECTOR_REQUIRED_FILES,
    FormatInspection,
    FrozenGroup,
)
from game_app.repository import ProductRepository, canonical_json
from game_app.tasks import TaskService
from game_app.device import ProbeReading
from tests.product.support.spies import SpyProvider

USER = "a" * 32


def _uid(value: int) -> str:
    return f"{value:032x}"


def _ctx(key: str = "app-key-1") -> MutationContext:
    return MutationContext(user_scope_id=USER, idempotency_key=key, request_id=_uid(991))


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class StubProbe:
    def __init__(self, values=None):
        self.values = values or {}
        self.calls = []

    def probe(self, field_paths, *, timeout):
        self.calls.append(list(field_paths))
        return [
            ProbeReading(
                field_path=path,
                status="succeeded",
                raw=str(self.values[path]).encode("utf-8"),
                text=str(self.values[path]),
            )
            for path in field_paths
        ]


def _repository(tmp_path: Path) -> ProductRepository:
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    return repository


def _run_files() -> dict[str, bytes]:
    return {
        "run.json": _json({
            "id": "run-1",
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


def _group(files, members: dict[str, bytes]) -> FrozenGroup:
    file_ids = {
        name: files.store_artifact(
            data, relative_name=name, stable_key=f"app-group:{name}"
        )
        for name, data in members.items()
    }
    entries = [
        FileEntry(
            relative_name=name,
            byte_length=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            media_type="application/json",
        )
        for name, data in sorted(members.items())
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


def _count(repository, entity_type) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def test_application_executes_registered_import_and_scan_handlers(tmp_path):
    repository = _repository(tmp_path)
    probe = StubProbe(values={"cpu": "Example CPU"})
    application = Application(
        repository,
        file_root=str(tmp_path / "managed"),
        provider=SpyProvider(),
        probe=probe,
    )

    scan = application.register_task(
        {"kind": "device_scan", "game_context_id": None, "field_paths": ["cpu"]},
        _ctx("scan"),
    )
    assert application.run_once().state is TaskState.completed
    assert _count(repository, "DeviceSnapshot") == 1
    assert scan.task_id

    group = _group(application.files, _run_files())
    context_id = _stored_context(repository)
    accepted = application.register_task(
        {
            "kind": "knowledge_import",
            "file_ids": list(group.file_ids.values()),
            "game_context_id": context_id,
            "mapping_version": "collector-import/1",
            "identity_mapping_ids": [],
        },
        _ctx("import"),
    )
    assert application.run_once().state is TaskState.partial
    assert _count(repository, "ImportSnapshot") == 1
    assert accepted.task_id


def test_an_unregistered_task_type_is_rejected_without_creating_a_task(tmp_path):
    repository = _repository(tmp_path)
    application = Application(
        repository, file_root=str(tmp_path / "managed"), provider=SpyProvider()
    )

    with pytest.raises(ContractError) as caught:
        application.register_task(
            {
                # ``analysis`` is registered by this batch; ``capture`` is not
                # registered until its handler exists, so it keeps the refusal.
                "kind": "capture",
                "artifact_id": _uid(1),
                "bindings": [],
                "analyzer_version": "a",
                "cleaning_protocol_version": "c",
            },
            _ctx("analysis"),
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert [(error.path, error.code) for error in caught.value.field_errors] == [
        ("kind", "enum")
    ]
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0
    assert application.tasks.provider.sends == 0


def test_task_service_without_a_handler_fails_instead_of_completing(tmp_path):
    repository = _repository(tmp_path)
    service = TaskService(repository, provider=SpyProvider())
    accepted = service.register_task(
        {"kind": "knowledge", "game_context_id": _uid(1),
         "budget": {"max_model_calls": 1}},
        _ctx("missing"),
    )

    outcome = service.run_once()

    assert outcome.state is TaskState.failed
    with repository.unit_of_work() as uow:
        row = uow.query("SELECT * FROM tasks WHERE task_id=?", (accepted.task_id,))[0]
    assert row["last_error_code"] == ErrorCode.INTERNAL_ERROR.value
    stop = json.loads(row["stop_reason"])
    assert stop == {"kind": "execution", "code": "validation_failed"}


def test_without_a_probe_device_scan_registration_is_rejected(tmp_path):
    repository = _repository(tmp_path)
    application = Application(
        repository, file_root=str(tmp_path / "managed"), provider=SpyProvider()
    )

    with pytest.raises(ContractError) as caught:
        application.register_task(
            {"kind": "device_scan", "game_context_id": None, "field_paths": ["cpu"]},
            _ctx("no-probe"),
        )

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0
