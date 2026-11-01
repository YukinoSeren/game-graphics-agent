"""ENG-T01: strict contract primitives and the versioned product repository.

These tests are the behavioral contract for the first offline slice.  They use
only temporary SQLite databases and synthetic entities; no real game, device or
provider is involved.
"""

import json
import sqlite3
import threading

import pytest
from pydantic import ValidationError

from game_app.contracts.primitives import (
    ID,
    ObjectRef,
    Revision,
    StrictModel,
    VersionHeader,
)
from game_app.errors import ContractError, ErrorCode
from game_app.repository import ProductRepository, canonical_json


class SyntheticEntity(StrictModel):
    """Test-only versioned entity; real entities are owned by their modules."""

    id: ID
    series_id: ID
    version: Revision
    name: str


def _header(uid: str, series: str, version: int, name: str = "synthetic") -> SyntheticEntity:
    return SyntheticEntity(
        id=uid,
        series_id=series,
        version=version,
        name=name,
    )


def _uid(value: int) -> str:
    return f"{value:032x}"


def _repo(tmp_path) -> ProductRepository:
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    return repo


def test_revision_rejects_bool():
    with pytest.raises(ValidationError) as caught:
        VersionHeader.model_validate({
            "series_id": "00000000000000000000000000000001",
            "version": True,
        })
    assert any(error["loc"] == ("version",) for error in caught.value.errors())


def test_id_rejects_bad_uuid_and_unknown_fields():
    with pytest.raises(ValidationError):
        VersionHeader.model_validate({"series_id": "not-a-uuid", "version": 1})
    with pytest.raises(ValidationError):
        VersionHeader.model_validate({
            "series_id": "00000000000000000000000000000001",
            "version": 1,
            "extra": 1,
        })
    # Floats and numeric strings are not revisions either.
    for bad in (1.0, "1"):
        with pytest.raises(ValidationError):
            VersionHeader.model_validate({
                "series_id": "00000000000000000000000000000001",
                "version": bad,
            })


def test_scalar_rejects_nan_and_infinity():
    from game_app.contracts.common import FieldValue

    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            FieldValue[int].model_validate({
                "value_state": "known",
                "value": bad,
                "acquisition_status": "succeeded",
            })


def test_same_version_duplicate_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    first = _header(_uid(1), _uid(100), 1)
    second = _header(_uid(2), _uid(100), 1)
    with repo.unit_of_work() as uow:
        uow.insert_versioned("Game", first)
    with pytest.raises(ContractError) as caught:
        with repo.unit_of_work() as uow:
            uow.insert_versioned("Game", second)
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert any(error.path == "version" for error in caught.value.field_errors)


def test_cross_type_reference_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    game = _header(_uid(1), _uid(100), 1)
    child = _header(_uid(2), _uid(200), 1)
    with repo.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
        uow.insert_versioned("GameContext", child)
        uow.insert_reference(
            child.id,
            "game",
            ObjectRef(entity_type="Game", entity_id=game.id),
        )
    # A reference that claims the target is another type must be refused even
    # though the ID exists; otherwise a wrong entity could be joined silently.
    with pytest.raises(ContractError) as caught:
        with repo.unit_of_work() as uow:
            uow.insert_reference(
                child.id,
                "selected_device",
                ObjectRef(entity_type="DeviceSnapshot", entity_id=game.id),
            )
    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    with pytest.raises(ContractError):
        with repo.unit_of_work() as uow:
            uow.insert_reference(
                child.id,
                "missing",
                ObjectRef(entity_type="Game", entity_id=_uid(999)),
            )


def test_legacy_bytes_and_idempotency_response_are_re_read_verbatim(tmp_path):
    repo = _repo(tmp_path)
    legacy_id = _uid(7)
    # Deliberately non-canonical JSON: key order and spacing must survive.
    raw = b'{"schema_version": "product-contract/0.1",  "id": "' + legacy_id.encode() + b'", "z": 1, "a": 2}'
    with repo.unit_of_work() as uow:
        uow.insert_object_bytes(
            "AppliedConfiguration",
            schema_version="product-contract/0.1",
            raw=raw,
            object_id=legacy_id,
            created_at="2026-01-01T00:00:00+00:00",
        )
        uow.insert_idempotency(
            user_scope_id=_uid(1),
            method="POST",
            canonical_route="/api/applied-configurations",
            key="legacy-key",
            request_schema_version="product-contract/0.1",
            request_hash="a" * 64,
            response_status=201,
            response_body=b'{"refs": [{"entity_id": "' + legacy_id.encode() + b'"}]}',
        )

    stored = repo.get_object("AppliedConfiguration", legacy_id)
    assert stored.payload_bytes == raw
    assert stored.schema_version == "product-contract/0.1"

    record = repo.find_idempotency(
        _uid(1), "POST", "/api/applied-configurations", "legacy-key"
    )
    assert record is not None
    assert record.response_body == b'{"refs": [{"entity_id": "' + legacy_id.encode() + b'"}]}'
    assert record.request_schema_version == "product-contract/0.1"


def test_unit_of_work_exception_leaves_no_partial_facts(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(RuntimeError):
        with repo.unit_of_work() as uow:
            uow.insert_versioned("Game", _header(_uid(1), _uid(100), 1))
            raise RuntimeError("injected failure")

    assert repo.find_object("Game", _uid(1)) is None
    with pytest.raises(ContractError):
        repo.get_object("Game", _uid(1))


def test_each_connection_enables_foreign_keys(tmp_path):
    repo = _repo(tmp_path)
    conn = repo.connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO object_references(owner_id, relation, target_type, target_id, locator)"
                " VALUES (?, ?, ?, ?, NULL)",
                (_uid(1), "broken", "Game", _uid(2)),
            )
    finally:
        conn.close()


def test_unknown_location_names_are_not_merged_and_located_slot_is_unique(tmp_path):
    repo = _repo(tmp_path)
    install = _uid(10)
    first = repo.register_local_setting(
        installation_id=install,
        namespace="player",
        raw_name="Quality",
        menu_path=[],
        local_setting_id=_uid(1000),
        series_id=_uid(2000),
    )
    second = repo.register_local_setting(
        installation_id=install,
        namespace="player",
        raw_name="Quality",
        menu_path=[],
        local_setting_id=_uid(1001),
        series_id=_uid(2001),
    )
    assert first.local_setting_id != second.local_setting_id
    assert len(repo.list_local_setting_heads(install)) == 2
    assert repo.get_local_setting_slot(install, "player", "Quality", []) is None

    # auto resolution at an unknown menu position is a validation error, never
    # a silent pick of the first same-named entry.
    with pytest.raises(ContractError) as caught:
        repo.resolve_local_setting(install, "player", "Quality", [], identity_mode="auto")
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED

    located = repo.register_local_setting(
        installation_id=install,
        namespace="player",
        raw_name="Quality",
        menu_path=["Graphics", "Quality"],
        local_setting_id=_uid(1002),
        series_id=_uid(2002),
    )
    assert repo.get_local_setting_slot(
        install, "player", "Quality", ["Graphics", "Quality"]
    ) == located.series_id
    with pytest.raises(ContractError) as caught:
        repo.register_local_setting(
            installation_id=install,
            namespace="player",
            raw_name="Quality",
            menu_path=["Graphics", "Quality"],
            local_setting_id=_uid(1003),
            series_id=_uid(2003),
        )
    assert caught.value.code is ErrorCode.LOCAL_SETTING_CONFLICT


def test_legacy_duplicate_located_headers_go_to_ambiguity_bucket(tmp_path):
    repo = _repo(tmp_path)
    install = _uid(10)
    repo.import_legacy_local_settings([
        {
            "local_setting_id": _uid(1000),
            "installation_id": install,
            "namespace": "player",
            "raw_name": "Quality",
            "menu_path": ["Graphics"],
            "series_id": _uid(2000),
        },
        {
            "local_setting_id": _uid(1001),
            "installation_id": install,
            "namespace": "player",
            "raw_name": "Quality",
            "menu_path": ["Graphics"],
            "series_id": _uid(2001),
        },
    ])
    # Both series survive; the slot is withheld and automatic selection refuses.
    assert len(repo.list_local_setting_heads(install)) == 2
    assert repo.get_local_setting_slot(install, "player", "Quality", ["Graphics"]) is None
    assert repo.list_local_setting_ambiguities(install)
    with pytest.raises(ContractError) as caught:
        repo.resolve_local_setting(
            install, "player", "Quality", ["Graphics"], identity_mode="auto"
        )
    assert caught.value.code is ErrorCode.LOCAL_SETTING_CONFLICT


def test_information_amendment_allows_one_successor_and_keeps_root(tmp_path):
    repo = _repo(tmp_path)
    source = _uid(300)
    root = _uid(301)
    repo.record_information_amendment(
        successor_id=_uid(400),
        source_request_id=source,
        root_request_id=root,
        root_version=3,
    )
    assert repo.get_information_amendment_root(_uid(400)) == (root, 3)
    with pytest.raises(ContractError) as caught:
        repo.record_information_amendment(
            successor_id=_uid(401),
            source_request_id=source,
            root_request_id=root,
            root_version=3,
        )
    assert caught.value.code is ErrorCode.INFORMATION_CONTEXT_CHANGED


def test_game_declaration_slot_encodes_unknown_platform_explicitly(tmp_path):
    repo = _repo(tmp_path)
    user = _uid(50)
    series = _uid(500)
    assert repo.claim_game_declaration_slot(user, "My Game", None, series) == series
    # Case/space normalisation decides the same slot, and a null platform is a
    # real key component rather than a SQL NULL that could duplicate.
    assert repo.claim_game_declaration_slot(user, "  my game ", None, series) == series
    other_platform = repo.claim_game_declaration_slot(user, "My Game", "steam", _uid(501))
    assert other_platform != series
    raw_key = repo.game_declaration_slot_platform_key(user, "My Game", None)
    assert raw_key is not None


def test_canonical_json_is_stable_and_rejects_nan():
    assert canonical_json({"b": 1, "a": [2]}) == b'{"a":[2],"b":1}'
    with pytest.raises(ValueError):
        canonical_json({"a": float("nan")})


def test_snapshots_require_expected_revision_and_keep_history(tmp_path):
    repo = _repo(tmp_path)
    entity = _uid(1)
    with repo.unit_of_work() as uow:
        assert uow.append_snapshot("InformationRequest", entity, {"state": "open"}) == 1
    with pytest.raises(ContractError):
        with repo.unit_of_work() as uow:
            uow.append_snapshot(
                "InformationRequest", entity, {"state": "resolved"}, expected_revision=3
            )
    with repo.unit_of_work() as uow:
        assert uow.append_snapshot(
            "InformationRequest", entity, {"state": "resolved"}, expected_revision=1
        ) == 2
    assert repo.get_snapshot("InformationRequest", entity, 1) == {"state": "open"}
    assert repo.get_snapshot("InformationRequest", entity, 2) == {"state": "resolved"}
    assert repo.get_head("InformationRequest", entity) == 2
