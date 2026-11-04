"""Offline fixtures for the recommendation slices (REC-T01..03).

Everything here is built through the real product entries: a verified ``Game``,
a real ``GameContext``, a real ``DeviceSnapshot`` and a package published by the
real importer/``KnowledgeService.build_package``.  No provider, network, image
or real game is involved.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from game_app.contracts.common import (
    CheckResult,
    EvidenceRef,
    FieldValue,
    Provenance,
)
from game_app.contracts.device import (
    AdapterCapabilities,
    BuildIdentifier,
    DeviceSnapshot,
    DisplayContext,
    GameContext,
    GameInstallation,
    GpuSnapshot,
    Resolution,
)
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    GameIdentityStatus,
    ValueState,
)
from game_app.contracts.knowledge import (
    Game,
    IdentityMapping,
)
from game_app.files import FileEntry, FileManifest, FileService
from game_app.importer import (
    COLLECTOR_RUN_FORMAT_ID,
    COLLECTOR_REQUIRED_FILES,
    FormatInspection,
    FrozenGroup,
    ImportPublisher,
)
from game_app.knowledge import KNOWLEDGE_TASK_SCOPE, KnowledgeService
from game_app.repository import ProductRepository, canonical_json

SCOPE = "a" * 32
OTHER = "b" * 32
NOW = "2026-11-03T00:00:00+00:00"
GAME_LEGACY_ID = "0123456789abcdef"


def uid(value: int) -> str:
    return f"{value:032x}"


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def check(
    check_id: str = "identity.evidence",
    status: CheckStatus = CheckStatus.pass_,
    *,
    method: str = "synthetic test check",
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version="recommendation-checks/0.4",
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else NOW,
        method=method,
    )


def not_run(check_id: str) -> CheckResult:
    return check(check_id, CheckStatus.not_run, method="test stub")


def setup(tmp_path: Path):
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    files = FileService(repository, tmp_path / "managed")
    return repository, files


def artifact_evidence(files: FileService, key: str) -> EvidenceRef:
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


def known(value, files: FileService, key: str):
    return FieldValue[type(value)](
        value_state=ValueState.known,
        value=value,
        acquisition_status=AcquisitionStatus.succeeded,
        provenance=[Provenance(
            method=AcquisitionMethod.user_provided,
            observed_at=None,
            recorded_at=NOW,
            evidence_refs=[artifact_evidence(files, key)],
        )],
    )


def unknown(reason: str = "not_provided"):
    return FieldValue[object](
        value_state=ValueState.unknown,
        value=None,
        unknown_reason=reason,
        acquisition_status=AcquisitionStatus.not_attempted,
    )


# --- game / context / device -------------------------------------------------


def store_game(
    repository: ProductRepository,
    *,
    uid_value: int = 100,
    series: int = 101,
    status: GameIdentityStatus = GameIdentityStatus.verified,
    platforms: tuple[str, ...] = ("steam",),
    user: str | None = None,
    name: str = "Synthetic Game",
) -> Game:
    game = Game(
        id=uid(uid_value),
        schema_version="product-contract/0.4",
        created_at=NOW,
        created_by_task_id=None,
        series_id=uid(series),
        version=1,
        canonical_name=name,
        aliases=[],
        platform_scope=list(platforms),
        identity_status=status,
        identity_checks=[check()],
        declaring_user_scope_id=user,
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
    return game


def store_installation(
    repository: ProductRepository, *, uid_value: int, owner: str = SCOPE,
    game_id: str = uid(101),
) -> str:
    installation_id = uid(uid_value)
    with repository.unit_of_work() as uow:
        uow.insert_versioned("GameInstallation", GameInstallation(
            id=installation_id,
            schema_version="product-contract/0.4",
            created_at=NOW,
            created_by_task_id=None,
            series_id=installation_id,
            version=1,
            revision=1,
            user_scope_id=owner,
            game_id=game_id,
            origin="user_provided",
            platform=unknown(),
            private_path_ref=None,
        ))
    return installation_id


def store_context(
    repository: ProductRepository,
    files: FileService,
    game: Game,
    *,
    uid_value: int = 300,
    owner: str = SCOPE,
    installation: int = 301,
    platform: str | None = "steam",
    version: str | None = None,
) -> GameContext:
    installation_id = store_installation(
        repository, uid_value=installation, owner=owner, game_id=game.series_id,
    )
    if platform is None:
        platform_value = unknown()
    else:
        platform_value = known(platform, files, f"context-{uid_value}")
    builds = []
    if version is not None:
        builds = [BuildIdentifier(
            scheme="test",
            raw_value=version,
            platform=platform or "unknown",
            provenance=[Provenance(
                method=AcquisitionMethod.user_provided,
                observed_at=None,
                recorded_at=NOW,
                evidence_refs=[artifact_evidence(files, f"build-{uid_value}")],
            )],
        )]
    context = GameContext(
        id=uid(uid_value),
        schema_version="product-contract/0.4",
        created_at=NOW,
        created_by_task_id=None,
        series_id=uid(uid_value),
        version=1,
        game_id=game.series_id,
        game_revision_id=game.id,
        installation_id=installation_id,
        platform=platform_value,
        build_identifiers=builds,
        process_candidates=[],
        selected_process=None,
        adapter_id=None,
        adapter_version=None,
        capabilities=AdapterCapabilities(
            can_identify=not_run("adapter.identify"),
            can_read_config=not_run("adapter.read_config"),
            can_bind_capture=not_run("adapter.bind_capture"),
        ),
        checks=[],
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("GameContext", context)
    return context


def store_device(
    repository: ProductRepository,
    files: FileService,
    *,
    uid_value: int = 400,
    owner: str = SCOPE,
    gpu: str | None = "Synthetic GPU",
    vram_bytes: int | None = 8_000_000_000,
    form: str | None = "desktop",
    resolution: tuple[int, int] | None = (1920, 1080),
    device_series: int | None = None,
) -> DeviceSnapshot:
    gpu_id = uid(uid_value + 1)
    gpus = []
    selected = None
    if gpu is not None:
        gpus = [GpuSnapshot(
            id=gpu_id,
            probe_index=0,
            raw_name=known(gpu, files, f"gpu-{uid_value}"),
            vram_bytes=(
                known(vram_bytes, files, f"vram-{uid_value}")
                if vram_bytes is not None else unknown()
            ),
            driver_version=unknown(),
        )]
        selected = gpu_id
    display_resolution = (
        known(Resolution(width=resolution[0], height=resolution[1]),
              files, f"resolution-{uid_value}")
        if resolution is not None else unknown()
    )
    device = DeviceSnapshot(
        id=uid(uid_value),
        schema_version="product-contract/0.4",
        created_at=NOW,
        created_by_task_id=None,
        series_id=uid(uid_value),
        version=1,
        device_series_id=uid(device_series or uid_value + 2),
        user_scope_id=owner,
        captured_at=NOW,
        gpus=gpus,
        selected_gpu_id=selected,
        cpu=known("Synthetic CPU", files, f"cpu-{uid_value}"),
        memory_bytes=known(32768, files, f"memory-{uid_value}"),
        device_form=known(form, files, f"form-{uid_value}") if form else unknown(),
        os_version=known("Synthetic OS", files, f"os-{uid_value}"),
        display=DisplayContext(
            desktop_resolution=display_resolution,
            desktop_resolution_axes={},
            refresh_hz=unknown(),
        ),
        power_context={},
        checks=[],
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("DeviceSnapshot", device)
    return device


# --- package ----------------------------------------------------------------


def source(scope_id: str = "src-0001", revision_id: str = "rev-0001",
           url: str | None = None, content_hash: str | None = None) -> dict:
    row = {
        "id": scope_id,
        "revision_id": revision_id,
        "url": url or f"https://example.invalid/{scope_id}",
        "retrieved_at": "2026-11-01T00:01:00+00:00",
        "role": "official_version_compatibility",
        "status": "extracted",
        "lineage_status": "unknown",
    }
    if content_hash is not None:
        # The frozen format carries the hash inside the inline document; a
        # top-level key would be ignored and the revision would read as
        # unchanged, which silently defeats every impact notice.
        row["document"] = {
            "content_hash": content_hash,
            "truncated": False,
            "retrieval_method": "html",
        }
    return row


def record(
    local_id: str,
    record_type: str,
    payload,
    *,
    status: str = "accepted",
    recommendation_status: str | None = None,
    applicability: str = "unknown",
    **extra,
):
    row = {
        "id": f"src-0001:{local_id}",
        "local_id": local_id,
        "record_type": record_type,
        "record_status": status,
        "recommendation_status": (
            recommendation_status if recommendation_status is not None
            else ("candidate" if record_type == "configurations" else None)
        ),
        "applicability": applicability,
        "reference_scope": "src-0001",
        "source_id": "src-0001",
        "revision_id": "rev-0001",
        "game_id": GAME_LEGACY_ID,
        "payload": payload,
    }
    row.update(extra)
    return row


def assignment(setting_id: str, value, evidence_ids: list[str] | None = None) -> dict:
    if isinstance(value, dict):
        setting_value = value
    else:
        setting_value = {"value_kind": "fixed", "value": value}
    return {
        "setting_id": setting_id,
        "value": setting_value,
        "evidence_ids": evidence_ids or ["evi-0001"],
    }


def dataset(
    *,
    configs: int = 1,
    values: tuple[str, ...] | None = None,
    gpu: str = "Synthetic GPU",
    resolution: str | None = "1920x1080",
    platform: str = "steam",
    version: str | None = "1.0",
    complete: bool = True,
    extra_evidence: list[dict] | None = None,
    extra_records: list[dict] | None = None,
    config_extra: dict | None = None,
    setting_extra: dict | None = None,
) -> list[dict]:
    """A coherent accepted subset: settings, measurement, environment, configs."""
    environment: dict = {"id": "env-0001", "fields": {
        "gpu": {"value": gpu, "proof": {"url": "https://example.invalid/src-0001",
                                        "quote": "synthetic gpu"}},
        "platform": {"value": platform, "proof": {"quote": "synthetic platform"}},
    }}
    if resolution is not None:
        environment["fields"]["resolution"] = {
            "value": resolution, "proof": {"quote": "synthetic resolution"},
        }
    if version is not None:
        environment["fields"]["game_version"] = {
            "value": version, "proof": {"quote": "synthetic version"},
        }
    setting_payload = {
        "id": "set-0001",
        "raw_name": "Synthetic Quality",
        "canonical_name": "Quality",
        "aliases": [],
        "unit": None,
        "proof": {"url": "https://example.invalid/src-0001"},
    }
    if setting_extra:
        setting_payload.update(setting_extra)
    records = [
        record("set-0001", "settings", setting_payload),
        record("evi-0001", "evidence", {
            "id": "evi-0001",
            "kind": "measurement",
            "origin": "source_author_measurement",
            "statement": "synthetic average frame rate",
            "metric": "average_fps",
            "numeric": {"number_kind": "exact", "value": 60},
            "unit": "fps",
            "setting_ids": ["set-0001"],
            "input_ids": [],
            "detected_state": None,
        }),
        record("env-0001", "environments", environment),
    ]
    values = values or tuple(
        "High" if index == 0 else "Ultra" for index in range(configs)
    )
    for index in range(configs):
        payload = {
            "id": f"cfg-{index + 1:04d}",
            "name": f"Preset {index + 1}",
            "kind": "resolved_configuration",
            "assignments": [assignment("set-0001", values[index])],
            "preset_reference": None,
            "environment_ids": ["env-0001"],
            "source_capture_complete": complete,
            "runtime_complete": complete,
        }
        if config_extra:
            payload.update(config_extra)
        records.append(record(
            f"cfg-{index + 1:04d}", "configurations", payload,
        ))
    if extra_evidence:
        records.extend(extra_evidence)
    if extra_records:
        records.extend(extra_records)
    return records


def run_files(records, *, run_id="run-1", status="completed_scope",
              stop_reason="coverage_reached", game_id=GAME_LEGACY_ID,
              sources=None):
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
        "sources.json": _json(sources or [source()]),
        "records.json": _json(records),
        "gaps.json": _json([]),
        "extractions.json": _json([]),
    }


def group(files: FileService, members: dict, *, name: str) -> FrozenGroup:
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


def identity_mapping(
    repository: ProductRepository, game: Game, run_id: str,
    *, legacy_game_id: str = GAME_LEGACY_ID,
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


def publish_import(
    repository: ProductRepository,
    files: FileService,
    game: Game,
    context: GameContext,
    *,
    run_id: str = "run-1",
    records=None,
    status: str = "completed_scope",
    stop_reason: str = "coverage_reached",
    legacy_game_id: str = GAME_LEGACY_ID,
    mapping_game: Game | None = None,
    user_scope_id: str = SCOPE,
    sources=None,
    file_overrides: dict | None = None,
    require_all_files: bool = True,
) -> str:
    members = run_files(
        dataset() if records is None else records,
        run_id=run_id,
        status=status,
        stop_reason=stop_reason,
        game_id=legacy_game_id,
        sources=sources,
    )
    if file_overrides:
        members.update(file_overrides)
    if not require_all_files:
        members = {k: v for k, v in members.items() if k in (file_overrides or members)}
    frozen = group(files, members, name=run_id)
    mapping_id = identity_mapping(
        repository, mapping_game or game, run_id, legacy_game_id=legacy_game_id
    )
    result = ImportPublisher(repository, files).publish(
        frozen=frozen,
        game_context_id=context.id,
        mapping_version="collector-import/1",
        identity_mapping_ids=[mapping_id],
        user_scope_id=user_scope_id,
        idempotency_key=f"import:{run_id}",
    )
    return result.snapshot_id


def service(repository: ProductRepository, files: FileService) -> KnowledgeService:
    return KnowledgeService(repository, files)


def build_package(
    repository: ProductRepository,
    files: FileService,
    game: Game,
    context: GameContext,
    *,
    key: str = "pkg-1",
    run_id: str = "run-1",
    records=None,
    prior: str | None = None,
    user_scope_id: str = SCOPE,
    scope: str = KNOWLEDGE_TASK_SCOPE,
    sources=None,
):
    snapshot_id = publish_import(
        repository, files, game, context,
        run_id=run_id, records=records, user_scope_id=user_scope_id,
        sources=sources,
    )
    result = KnowledgeService(repository, files).build_package(
        [snapshot_id],
        prior,
        scope,
        game_context_id=context.id,
        user_scope_id=user_scope_id,
        idempotency_key=key,
    )
    return result.package


def store_applied(
    repository: ProductRepository,
    *,
    uid_value: int,
    owner: str = SCOPE,
    game_id: str = uid(101),
    installation_id: str | None = None,
    device_snapshot_id: str | None = None,
    game_context_id: str | None = None,
    settings: list | None = None,
    preset_reference: str | None = "Synthetic Preset",
) -> str:
    """An adopted snapshot whose only readers are the recommendation gate."""
    applied_id = uid(uid_value)
    payload = {
        "id": applied_id,
        "schema_version": "product-contract/0.4",
        "created_at": NOW,
        "created_by_task_id": None,
        "series_id": applied_id,
        "version": 1,
        "user_scope_id": owner,
        "installation_id": installation_id or uid(301),
        "game_id": game_id,
        "registration_kind": "current_adoption",
        "device_snapshot_id": device_snapshot_id or uid(400),
        "game_context_id": game_context_id or uid(300),
        "origin": "baseline",
        "recommendation_id": None,
        "parent_applied_id": None,
        "change_set_id": None,
        "restored_from_applied_id": None,
        "settings": settings or [],
        "preset_reference": preset_reference,
        "adopted_at": NOW,
        "activation_state": "unknown",
        "checks": [],
        "declaration_id": uid(900),
    }
    with repository.unit_of_work() as uow:
        uow.insert_object_bytes(
            "AppliedConfiguration",
            schema_version="product-contract/0.4",
            raw=canonical_json(payload),
            object_id=applied_id,
            series_id=applied_id,
            version=1,
        )
    return applied_id


def count_objects(repository: ProductRepository, entity_type: str) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def store_raw(
    repository: ProductRepository,
    entity_type: str,
    payload: dict,
    *,
    owner: str = SCOPE,
) -> str:
    """Insert a JSON projection without re-deriving it from a real entry point."""
    with repository.unit_of_work() as uow:
        uow.insert_object_bytes(
            entity_type,
            schema_version="product-contract/0.4",
            raw=canonical_json(payload),
            object_id=payload["id"],
            series_id=payload.get("series_id", payload["id"]),
            version=payload.get("version", 1),
        )
    return payload["id"]


def external_calls(repository: ProductRepository) -> list[dict]:
    with repository.unit_of_work() as uow:
        return [dict(row) for row in uow.query(
            "SELECT * FROM external_calls ORDER BY rowid"
        )]


def stub_package(    repository: ProductRepository,
    package,
    *,
    publication_status: str = "staging",
    package_id: str | None = None,
):
    """Insert a copy of a package with one changed field (unpublished tests)."""
    payload = package.model_dump(mode="json")
    payload["id"] = package_id or hashlib.sha256(
        f"stub:{package.id}:{publication_status}".encode()
    ).hexdigest()[:32]
    payload["publication_status"] = publication_status
    with repository.unit_of_work() as uow:
        uow.insert_object_bytes(
            "KnowledgePackage",
            schema_version="product-contract/0.4",
            raw=canonical_json(payload),
            object_id=payload["id"],
            series_id=payload["id"],
            # A copy is not a new version of the original series: version stays
            # unset so the (entity_type, series_id, version) index is not hit.
            version=None,
        )
    return payload["id"]


def package_records(repository: ProductRepository, package):
    from game_app.contracts.knowledge import ImportedRecord

    with repository.unit_of_work() as uow:
        return [
            ImportedRecord.model_validate_json(
                uow.get_object("ImportedRecord", item).payload_bytes
            )
            for item in package.record_snapshot_ids
        ]


def member(repository: ProductRepository, package, *, local_id: str):
    from game_app.knowledge import _record_ref

    for item in package_records(repository, package):
        if item.local_record_id == local_id:
            return item, _record_ref(package.id, item)
    raise AssertionError(f"the fixture package has no {local_id}")


def knowledge_setting_ref(repository: ProductRepository, package, local_id: str) -> dict:
    _, ref = member(repository, package, local_id=local_id)
    return {"kind": "knowledge", "record": ref.model_dump(mode="json")}


def confirmed_setting(
    repository: ProductRepository,
    files: FileService,
    package,
    local_id: str,
    value,
    *,
    known_value: bool = True,
    identity: str | None = None,
) -> dict:
    ref = knowledge_setting_ref(repository, package, local_id)
    field_value = FieldValue(
        value_state=ValueState.known if known_value else ValueState.unknown,
        value=value if known_value else None,
        unknown_reason=None if known_value else "not_provided",
        acquisition_status=(
            AcquisitionStatus.succeeded if known_value
            else AcquisitionStatus.not_attempted
        ),
        provenance=(
            [Provenance(
                method=AcquisitionMethod.user_provided,
                observed_at=None,
                recorded_at=NOW,
                evidence_refs=[artifact_evidence(files, f"confirmed-{local_id}")],
            )]
            if known_value else []
        ),
    )
    return {
        "setting_ref": ref,
        "setting_identity_ref": identity,
        "value": field_value.model_dump(mode="json"),
        "unit": None,
        "confirmation_method": "user_provided",
        "activation_state": "unknown",
    }


def target_evidence(
    local_id: str = "evi-0002",
    *,
    numeric: dict | None = None,
    stream: str = "displayed",
    statistic: str = "author 1% low over 60 s, fixed scene",
    statement: str = "author target frame rate",
    **extra,
):
    payload = {
        "id": local_id,
        "kind": "measurement",
        "origin": "source_author_measurement",
        "statement": statement,
        "metric": "average_fps",
        "numeric": numeric or {"number_kind": "exact", "value": 60},
        "unit": "fps",
        "setting_ids": ["set-0001"],
        "input_ids": [],
        "detected_state": None,
        "is_target": True,
        "statistic_definition": statistic,
        "frame_stream_kind": stream,
    }
    payload.update(extra)
    return record(local_id, "evidence", payload)
