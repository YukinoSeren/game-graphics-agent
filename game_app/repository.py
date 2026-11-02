"""SQLite product repository: immutable objects, aggregate history, references.

The repository owns storage only: no network, no domain rules and no file
copies.  Every write goes through an explicit :class:`UnitOfWork` (short
``BEGIN IMMEDIATE`` transaction) so a failure leaves no partial facts.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from pydantic import BaseModel, ValidationError

from game_app.contracts.device import LocalSettingDefinition
from game_app.contracts.enums import (
    ENTITY_TYPES,
    IdempotencyLifecycle,
    StateDimension,
    TaskState,
)
from game_app.contracts.primitives import (
    ObjectRef,
    new_id,
    utc_now,
)
from game_app.errors import (
    ContractError,
    ErrorCode,
    FieldError,
    empty_details,
)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def canonical_json(value: Any) -> bytes:
    """Canonical JSON used for hashes and stored payloads (UTF-8, sorted keys)."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _require_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            f"{name} must be a 32-character lowercase hex ID",
            field_errors=[FieldError(path=name, code="type", message="invalid ID")],
        )
    return value


def _require_type(entity_type: str) -> str:
    if entity_type not in ENTITY_TYPES:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            f"unregistered entity type: {entity_type}",
            field_errors=[FieldError(path="entity_type", code="enum", message="unregistered type")],
        )
    return entity_type


@dataclass(frozen=True)
class StoredObject:
    entity_type: str
    object_id: str
    schema_version: str
    payload_bytes: bytes
    payload: Any
    series_id: str | None
    version: int | None
    created_at: str


@dataclass(frozen=True)
class IdempotencyRow:
    user_scope_id: str
    method: str
    canonical_route: str
    key: str
    request_schema_version: str
    request_hash: str
    task_id: str | None
    result_refs: list[Any]
    response_status: int
    response_body: bytes
    lifecycle: IdempotencyLifecycle
    created_at: str


@dataclass(frozen=True)
class LocalSettingHead:
    local_setting_id: str
    series_id: str
    installation_id: str
    namespace: str
    raw_name: str
    menu_path: tuple[str, ...]
    version: int
    parent_definition_id: str | None
    created_at: str

    @property
    def located(self) -> bool:
        return bool(self.menu_path)


class ProductRepository:
    """One product database.  Connections are per thread and FK-enforced."""

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._local = threading.local()

    # -- connections -----------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            isolation_level=None,
            timeout=self.timeout,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            conn.close()
            raise ContractError(
                ErrorCode.STORAGE_COMMIT_FAILED,
                "SQLite foreign keys could not be enabled",
            )
        self._local.conn = conn
        return conn

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- migrations ------------------------------------------------------
    def migrate(self) -> list[int]:
        conn = self.connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        executed: list[int] = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in applied:
                continue
            script = (
                "BEGIN IMMEDIATE;\n"
                + path.read_text(encoding="utf-8")
                + f"\nINSERT INTO schema_migrations(version, applied_at) VALUES({version}, '{utc_now()}');\n"
                + "COMMIT;"
            )
            try:
                conn.executescript(script)
            except sqlite3.Error as error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise ContractError(
                    ErrorCode.STORAGE_COMMIT_FAILED,
                    f"migration {path.name} failed; the database was rolled back",
                ) from error
            executed.append(version)
        return executed

    # -- transactions ----------------------------------------------------
    @contextmanager
    def unit_of_work(self) -> Iterator["UnitOfWork"]:
        work = UnitOfWork(self, self.connect())
        with work:
            yield work

    # -- thin wrappers used by tests and callers -------------------------
    def get_object(self, entity_type: str, object_id: str) -> StoredObject:
        with self.unit_of_work() as uow:
            return uow.get_object(entity_type, object_id)

    def find_object(self, entity_type: str, object_id: str) -> StoredObject | None:
        with self.unit_of_work() as uow:
            return uow.find_object(entity_type, object_id)

    def get_snapshot(self, entity_type: str, object_id: str, revision: int) -> Any:
        with self.unit_of_work() as uow:
            return uow.get_snapshot(entity_type, object_id, revision)

    def get_head(self, entity_type: str, object_id: str) -> int | None:
        with self.unit_of_work() as uow:
            return uow.get_head(entity_type, object_id)

    def find_idempotency(
        self, user_scope_id: str, method: str, canonical_route: str, key: str
    ) -> IdempotencyRow | None:
        with self.unit_of_work() as uow:
            return uow.find_idempotency(user_scope_id, method, canonical_route, key)

    def list_local_setting_heads(self, installation_id: str) -> list[LocalSettingHead]:
        with self.unit_of_work() as uow:
            return uow.list_local_setting_heads(installation_id)

    def register_local_setting(self, **kwargs: Any) -> LocalSettingHead:
        with self.unit_of_work() as uow:
            return uow.register_local_setting(**kwargs)

    def import_legacy_local_settings(self, rows: Iterable[Mapping[str, Any]]) -> int:
        with self.unit_of_work() as uow:
            return uow.import_legacy_local_settings(rows)

    def get_local_setting_slot(
        self,
        installation_id: str,
        namespace: str,
        raw_name: str,
        menu_path: Sequence[str] | str,
    ) -> str | None:
        with self.unit_of_work() as uow:
            return uow.get_local_setting_slot(installation_id, namespace, raw_name, menu_path)

    def resolve_local_setting(
        self,
        installation_id: str,
        namespace: str,
        raw_name: str,
        menu_path: Sequence[str] | str,
        *,
        identity_mode: str,
    ) -> str:
        with self.unit_of_work() as uow:
            return uow.resolve_local_setting(
                installation_id, namespace, raw_name, menu_path, identity_mode=identity_mode
            )

    def list_local_setting_ambiguities(self, installation_id: str) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            return uow.list_local_setting_ambiguities(installation_id)

    def record_information_amendment(
        self,
        *,
        successor_id: str,
        source_request_id: str,
        root_request_id: str,
        root_version: int,
    ) -> None:
        with self.unit_of_work() as uow:
            uow.record_information_amendment(
                successor_id=successor_id,
                source_request_id=source_request_id,
                root_request_id=root_request_id,
                root_version=root_version,
            )

    def get_information_amendment_root(self, successor_id: str) -> tuple[str, int] | None:
        with self.unit_of_work() as uow:
            return uow.get_information_amendment_root(successor_id)

    def claim_game_declaration_slot(
        self, user_scope_id: str, raw_name: str, platform: str | None, series_id: str
    ) -> str:
        with self.unit_of_work() as uow:
            return uow.claim_game_declaration_slot(user_scope_id, raw_name, platform, series_id)

    def game_declaration_slot_platform_key(
        self, user_scope_id: str, raw_name: str, platform: str | None
    ) -> str | None:
        with self.unit_of_work() as uow:
            return uow.game_declaration_slot_platform_key(user_scope_id, raw_name, platform)


class UnitOfWork:
    """Explicit short write transaction.  Nested units of work are refused."""

    def __init__(self, repository: ProductRepository, connection: sqlite3.Connection) -> None:
        self.repository = repository
        self.connection = connection

    def __enter__(self) -> "UnitOfWork":
        if self.connection.in_transaction:
            raise RuntimeError("nested UnitOfWork is not supported")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            # Lock contention is a storage failure with a stable code; callers
            # retry the same submission, they never start a second one.
            raise ContractError(
                ErrorCode.STORAGE_COMMIT_FAILED,
                "the product database is busy; retry the same submission",
            ) from error
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.connection.in_transaction:
            if exc_type is None:
                try:
                    self.connection.execute("COMMIT")
                except sqlite3.OperationalError as error:
                    self._discard_transaction()
                    raise ContractError(
                        ErrorCode.STORAGE_COMMIT_FAILED,
                        "the product database could not commit; retry the same submission",
                    ) from error
            else:
                self._discard_transaction()
        return False

    def _discard_transaction(self) -> None:
        """Roll back; if that is impossible, drop the connection entirely."""
        try:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
        except sqlite3.OperationalError:
            self.repository.close()

    # -- raw access for module-owned tables ------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, tuple(params)))

    # -- immutable objects -----------------------------------------------
    def insert_versioned(self, entity_type: str, entity: BaseModel) -> ObjectRef:
        _require_type(entity_type)
        if not isinstance(entity, BaseModel):
            raise ContractError(ErrorCode.INPUT_VALIDATION_FAILED, "entity must be a contract model")
        payload = entity.model_dump(mode="json")
        object_id = _require_id(payload.get("id", ""), "id")
        series_id = payload.get("series_id")
        version = payload.get("version")
        if series_id is None or version is None:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a versioned entity requires series_id and version",
            )
        _require_id(series_id, "series_id")
        try:
            self.connection.execute(
                "INSERT INTO objects(id, entity_type, schema_version, payload, series_id, version, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    object_id,
                    entity_type,
                    payload.get("schema_version", "product-contract/0.4"),
                    canonical_json(payload),
                    series_id,
                    version,
                    payload.get("created_at") or utc_now(),
                ),
            )
        except sqlite3.IntegrityError as error:
            message = str(error)
            field = "id" if "objects.id" in message else "version"
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the object or version already exists; existing versions are never overwritten",
                field_errors=[FieldError(path=field, code="conflict", message="duplicate identity")],
            ) from error
        return ObjectRef(entity_type=entity_type, entity_id=object_id)

    def insert_object_bytes(
        self,
        entity_type: str,
        *,
        schema_version: str,
        raw: bytes,
        object_id: str | None = None,
        created_at: str | None = None,
        series_id: str | None = None,
        version: int | None = None,
    ) -> ObjectRef:
        _require_type(entity_type)
        if not isinstance(raw, (bytes, bytearray)):
            raise ContractError(ErrorCode.INPUT_VALIDATION_FAILED, "raw payload must be bytes")
        object_id = _require_id(object_id or new_id(), "id")
        try:
            json.loads(bytes(raw))
        except (ValueError, UnicodeDecodeError) as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "legacy payload is not valid JSON and cannot be projected",
            ) from error
        try:
            self.connection.execute(
                "INSERT INTO objects(id, entity_type, schema_version, payload, series_id, version, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    object_id,
                    entity_type,
                    schema_version,
                    bytes(raw),
                    series_id,
                    version,
                    created_at or utc_now(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the object already exists",
                field_errors=[FieldError(path="id", code="conflict", message="duplicate identity")],
            ) from error
        return ObjectRef(entity_type=entity_type, entity_id=object_id)

    def insert_object(self, entity_type: str, entity: BaseModel) -> ObjectRef:
        """Insert a non-versioned record (local registration, mapping, selection)."""
        _require_type(entity_type)
        if not isinstance(entity, BaseModel):
            raise ContractError(ErrorCode.INPUT_VALIDATION_FAILED, "entity must be a contract model")
        payload = entity.model_dump(mode="json")
        object_id = _require_id(payload.get("id", ""), "id")
        series_id = payload.get("series_id")
        version = payload.get("version")
        if series_id is not None:
            _require_id(series_id, "series_id")
        try:
            self.connection.execute(
                "INSERT INTO objects(id, entity_type, schema_version, payload, series_id, version, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    object_id,
                    entity_type,
                    payload.get("schema_version", "product-contract/0.4"),
                    canonical_json(payload),
                    series_id,
                    version,
                    payload.get("created_at") or utc_now(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the object already exists",
                field_errors=[FieldError(path="id", code="conflict", message="duplicate identity")],
            ) from error
        return ObjectRef(entity_type=entity_type, entity_id=object_id)

    def iter_objects(self, entity_type: str) -> list[StoredObject]:
        rows = self.connection.execute(
            "SELECT * FROM objects WHERE entity_type=? ORDER BY created_at, id", (entity_type,)
        ).fetchall()
        return [self._stored_from_row(row) for row in rows]

    def latest_objects(self, entity_type: str) -> list[StoredObject]:
        """One row per series: the highest version at this read point."""
        rows = self.connection.execute(
            "SELECT o.* FROM objects o JOIN ("
            " SELECT series_id, MAX(version) AS max_version FROM objects"
            " WHERE entity_type=? AND series_id IS NOT NULL GROUP BY series_id"
            ") latest ON o.series_id=latest.series_id AND o.version=latest.max_version"
            " WHERE o.entity_type=? ORDER BY o.created_at, o.id",
            (entity_type, entity_type),
        ).fetchall()
        return [self._stored_from_row(row) for row in rows]

    def latest_for_series(self, entity_type: str, series_id: str) -> StoredObject | None:
        row = self.connection.execute(
            "SELECT * FROM objects WHERE entity_type=? AND series_id=?"
            " ORDER BY version DESC LIMIT 1",
            (entity_type, series_id),
        ).fetchone()
        return None if row is None else self._stored_from_row(row)

    def _stored_from_row(self, row: Any) -> StoredObject:
        return StoredObject(
            entity_type=row["entity_type"],
            object_id=row["id"],
            schema_version=row["schema_version"],
            payload_bytes=bytes(row["payload"]),
            payload=json.loads(bytes(row["payload"])),
            series_id=row["series_id"],
            version=row["version"],
            created_at=row["created_at"],
        )

    def find_object(self, entity_type: str, object_id: str) -> StoredObject | None:
        row = self.connection.execute(
            "SELECT * FROM objects WHERE id = ?", (object_id,)
        ).fetchone()
        if row is None:
            return None
        if row["entity_type"] != entity_type:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced object belongs to another entity type",
                field_errors=[FieldError(
                    path="entity_type", code="reference",
                    message=f"expected {entity_type}",
                )],
            )
        return StoredObject(
            entity_type=row["entity_type"],
            object_id=row["id"],
            schema_version=row["schema_version"],
            payload_bytes=bytes(row["payload"]),
            payload=json.loads(bytes(row["payload"])),
            series_id=row["series_id"],
            version=row["version"],
            created_at=row["created_at"],
        )

    def get_object(self, entity_type: str, object_id: str) -> StoredObject:
        stored = self.find_object(entity_type, object_id)
        if stored is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "the requested object is not available",
                details=empty_details(
                    current_resource_ref=None,
                ),
            )
        return stored

    # -- aggregate snapshots --------------------------------------------
    def get_head(self, entity_type: str, object_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT current_revision FROM aggregate_heads WHERE entity_type=? AND object_id=?",
            (entity_type, object_id),
        ).fetchone()
        return None if row is None else int(row[0])

    def append_snapshot(
        self,
        entity_type: str,
        object_id: str,
        payload: Any,
        *,
        expected_revision: int | None = None,
    ) -> int:
        _require_type(entity_type)
        _require_id(object_id, "object_id")
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        current = self.get_head(entity_type, object_id) or 0
        if expected_revision is not None and expected_revision != current:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the aggregate revision changed; re-read before updating",
                details=empty_details(
                    expected_revision=expected_revision,
                    current_revision=current or None,
                ),
            )
        revision = current + 1
        self.connection.execute(
            "INSERT INTO aggregate_snapshots(entity_type, object_id, revision, payload, created_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (entity_type, object_id, revision, canonical_json(data), utc_now()),
        )
        self.connection.execute(
            "INSERT INTO aggregate_heads(entity_type, object_id, current_revision) VALUES(?, ?, ?)"
            " ON CONFLICT(entity_type, object_id) DO UPDATE SET current_revision=excluded.current_revision",
            (entity_type, object_id, revision),
        )
        return revision

    def get_snapshot(self, entity_type: str, object_id: str, revision: int) -> Any:
        row = self.connection.execute(
            "SELECT payload FROM aggregate_snapshots"
            " WHERE entity_type=? AND object_id=? AND revision=?",
            (entity_type, object_id, revision),
        ).fetchone()
        if row is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the requested revision is not available"
            )
        return json.loads(bytes(row[0]))

    # -- references -------------------------------------------------------
    def insert_reference(
        self,
        owner_id: str,
        relation: str,
        target: ObjectRef,
        locator: str | None = None,
    ) -> None:
        _require_id(owner_id, "owner_id")
        if not relation:
            raise ContractError(ErrorCode.INPUT_VALIDATION_FAILED, "relation must not be empty")
        owner = self.connection.execute("SELECT entity_type FROM objects WHERE id=?", (owner_id,)).fetchone()
        if owner is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referencing object does not exist",
                field_errors=[FieldError(path="owner_id", code="reference", message="unknown owner")],
            )
        target_row = self.connection.execute(
            "SELECT entity_type FROM objects WHERE id=?", (target.entity_id,)
        ).fetchone()
        if target_row is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced object does not exist",
                field_errors=[FieldError(path="entity_id", code="reference", message="unknown target")],
            )
        if target_row["entity_type"] != target.entity_type:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced object belongs to another entity type",
                field_errors=[FieldError(
                    path="entity_type", code="reference",
                    message=f"stored type is {target_row['entity_type']}",
                )],
            )
        try:
            self.connection.execute(
                "INSERT INTO object_references(owner_id, relation, target_type, target_id, locator)"
                " VALUES(?, ?, ?, ?, ?)",
                (owner_id, relation, target.entity_type, target.entity_id, locator),
            )
        except sqlite3.IntegrityError as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the same reference is already registered",
            ) from error

    # -- idempotency ------------------------------------------------------
    def find_idempotency(
        self, user_scope_id: str, method: str, canonical_route: str, key: str
    ) -> IdempotencyRow | None:
        row = self.connection.execute(
            "SELECT * FROM idempotency"
            " WHERE user_scope_id=? AND method=? AND canonical_route=? AND key=?",
            (user_scope_id, method, canonical_route, key),
        ).fetchone()
        if row is None:
            return None
        return IdempotencyRow(
            user_scope_id=row["user_scope_id"],
            method=row["method"],
            canonical_route=row["canonical_route"],
            key=row["key"],
            request_schema_version=row["request_schema_version"],
            request_hash=row["request_hash"],
            task_id=row["task_id"],
            result_refs=json.loads(row["result_refs"] or "[]"),
            response_status=row["response_status"],
            response_body=bytes(row["response_body"]),
            lifecycle=IdempotencyLifecycle(row["lifecycle"]),
            created_at=row["created_at"],
        )

    def insert_idempotency(
        self,
        *,
        user_scope_id: str,
        method: str,
        canonical_route: str,
        key: str,
        request_schema_version: str,
        request_hash: str,
        response_status: int,
        response_body: bytes | Any,
        task_id: str | None = None,
        result_refs: Sequence[Any] = (),
        lifecycle: IdempotencyLifecycle = IdempotencyLifecycle.registered,
    ) -> None:
        _require_id(user_scope_id, "user_scope_id")
        body = response_body if isinstance(response_body, (bytes, bytearray)) else canonical_json(response_body)
        try:
            self.connection.execute(
                "INSERT INTO idempotency(user_scope_id, method, canonical_route, key,"
                " request_schema_version, request_hash, task_id, result_refs, response_status,"
                " response_body, lifecycle, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_scope_id,
                    method,
                    canonical_route,
                    key,
                    request_schema_version,
                    request_hash,
                    task_id,
                    canonical_json(list(result_refs)),
                    response_status,
                    bytes(body),
                    lifecycle.value,
                    utc_now(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ContractError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "the idempotency key is already registered for this submission",
                field_errors=[FieldError(
                    path="idempotency_key", code="conflict", message="key already registered",
                )],
            ) from error

    def tombstone_idempotency(
        self, user_scope_id: str, method: str, canonical_route: str, key: str
    ) -> None:
        cursor = self.connection.execute(
            "UPDATE idempotency SET lifecycle=? WHERE user_scope_id=? AND method=?"
            " AND canonical_route=? AND key=?",
            (IdempotencyLifecycle.tombstoned.value, user_scope_id, method, canonical_route, key),
        )
        if cursor.rowcount == 0:
            raise ContractError(ErrorCode.RESOURCE_NOT_FOUND, "no idempotency record to tombstone")

    # -- local setting directory ------------------------------------------
    @staticmethod
    def _menu_key(menu_path: Sequence[str] | str) -> str:
        if isinstance(menu_path, str):
            parsed = json.loads(menu_path)
        else:
            parsed = list(menu_path)
        if not all(isinstance(part, str) and part for part in parsed):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "menu_path must contain non-empty strings",
                field_errors=[FieldError(path="menu_path", code="type", message="invalid menu path")],
            )
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))

    def _head_from_row(self, row: sqlite3.Row) -> LocalSettingHead:
        return LocalSettingHead(
            local_setting_id=row["local_setting_id"],
            series_id=row["series_id"],
            installation_id=row["installation_id"],
            namespace=row["namespace"],
            raw_name=row["raw_name"],
            menu_path=tuple(json.loads(row["menu_path"])),
            version=row["version"],
            parent_definition_id=row["parent_definition_id"],
            created_at=row["created_at"],
        )

    def register_local_setting(
        self,
        *,
        installation_id: str,
        namespace: str,
        raw_name: str,
        menu_path: Sequence[str] | str,
        local_setting_id: str,
        series_id: str,
        version: int = 1,
        parent_definition_id: str | None = None,
    ) -> LocalSettingHead:
        _require_id(installation_id, "installation_id")
        _require_id(local_setting_id, "local_setting_id")
        _require_id(series_id, "series_id")
        if not namespace or not raw_name:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED, "namespace and raw_name must not be empty"
            )
        menu_key = self._menu_key(menu_path)
        located = menu_key != "[]"

        if located:
            ambiguity = self.connection.execute(
                "SELECT series_ids FROM local_setting_ambiguities"
                " WHERE installation_id=? AND namespace=? AND raw_name=? AND menu_path=?",
                (installation_id, namespace, raw_name, menu_key),
            ).fetchone()
            if ambiguity is not None:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "this legacy location key is ambiguous; choose an existing definition explicitly",
                )
            slot = self.connection.execute(
                "SELECT series_id FROM local_setting_slots"
                " WHERE installation_id=? AND namespace=? AND raw_name=? AND menu_path=?",
                (installation_id, namespace, raw_name, menu_key),
            ).fetchone()
            if slot is not None and slot["series_id"] != series_id:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "another series already owns this located setting slot",
                    details=empty_details(),
                )

        payload = LocalSettingDefinition(
            id=local_setting_id,
            schema_version="product-contract/0.4",
            created_at=utc_now(),
            created_by_task_id=None,
            series_id=series_id,
            version=version,
            parent_definition_id=parent_definition_id,
            installation_id=installation_id,
            namespace=namespace,
            raw_name=raw_name,
            menu_path=json.loads(menu_key),
            evidence_refs=[],
        )
        self.insert_versioned("LocalSettingDefinition", payload)

        if parent_definition_id is not None:
            parent = self.connection.execute(
                "SELECT installation_id, series_id, version FROM local_setting_heads"
                " WHERE local_setting_id=?",
                (parent_definition_id,),
            ).fetchone()
            if parent is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID, "the parent local setting definition is not current"
                )
            if parent["installation_id"] != installation_id:
                # A directory correction stays inside its own installation; a
                # different installation is a separate local record.
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the parent definition belongs to another installation",
                    field_errors=[FieldError(
                        path="supersedes_local_setting_id", code="reference",
                        message="cross-installation correction",
                    )],
                )
            if parent["series_id"] != series_id or version != parent["version"] + 1:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "a correction must be the next version in the same series",
                )
            try:
                self.connection.execute(
                    "INSERT INTO local_setting_successors(parent_id, child_id) VALUES(?, ?)",
                    (parent_definition_id, local_setting_id),
                )
            except sqlite3.IntegrityError as error:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "this definition already has a direct successor",
                ) from error

        old = self.connection.execute(
            "SELECT installation_id, namespace, raw_name, menu_path, located"
            " FROM local_setting_heads WHERE series_id=?",
            (series_id,),
        ).fetchone()
        if old is not None:
            # The slot belongs to the *previous* head's identity; a correction
            # that renames or moves the entry must release exactly that slot.
            self.connection.execute(
                "DELETE FROM local_setting_slots WHERE installation_id=? AND namespace=?"
                " AND raw_name=? AND menu_path=?",
                (old["installation_id"], old["namespace"], old["raw_name"], old["menu_path"]),
            )
        self.connection.execute(
            "INSERT INTO local_setting_heads(series_id, local_setting_id, installation_id, namespace,"
            " raw_name, menu_path, located, version, parent_definition_id, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(series_id) DO UPDATE SET local_setting_id=excluded.local_setting_id,"
            " namespace=excluded.namespace, raw_name=excluded.raw_name, menu_path=excluded.menu_path,"
            " located=excluded.located, version=excluded.version,"
            " parent_definition_id=excluded.parent_definition_id, created_at=excluded.created_at",
            (
                series_id, local_setting_id, installation_id, namespace, raw_name,
                menu_key, int(located), version, parent_definition_id, utc_now(),
            ),
        )
        if located:
            try:
                self.connection.execute(
                    "INSERT INTO local_setting_slots(installation_id, namespace, raw_name, menu_path, series_id)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (installation_id, namespace, raw_name, menu_key, series_id),
                )
            except sqlite3.IntegrityError as error:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "another series already owns this located setting slot",
                ) from error

        row = self.connection.execute(
            "SELECT * FROM local_setting_heads WHERE series_id=?", (series_id,)
        ).fetchone()
        return self._head_from_row(row)

    def import_legacy_local_settings(self, rows: Iterable[Mapping[str, Any]]) -> int:
        grouped: dict[tuple[str, str, str, str], list[str]] = {}
        count = 0
        for row in rows:
            menu_key = self._menu_key(row["menu_path"])
            located = menu_key != "[]"
            self.connection.execute(
                "INSERT INTO local_setting_heads(series_id, local_setting_id, installation_id, namespace,"
                " raw_name, menu_path, located, version, parent_definition_id, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)"
                " ON CONFLICT(series_id) DO UPDATE SET local_setting_id=excluded.local_setting_id,"
                " version=excluded.version, menu_path=excluded.menu_path, located=excluded.located",
                (
                    row["series_id"], row["local_setting_id"], row["installation_id"],
                    row["namespace"], row["raw_name"], menu_key, int(located),
                    int(row.get("version", 1)), utc_now(),
                ),
            )
            count += 1
            if located:
                key = (row["installation_id"], row["namespace"], row["raw_name"], menu_key)
                grouped.setdefault(key, []).append(row["series_id"])
        for (installation_id, namespace, raw_name, menu_key), series in grouped.items():
            unique = sorted(set(series))
            self.connection.execute(
                "DELETE FROM local_setting_slots WHERE installation_id=? AND namespace=?"
                " AND raw_name=? AND menu_path=?",
                (installation_id, namespace, raw_name, menu_key),
            )
            if len(unique) == 1:
                self.connection.execute(
                    "DELETE FROM local_setting_ambiguities WHERE installation_id=? AND namespace=?"
                    " AND raw_name=? AND menu_path=?",
                    (installation_id, namespace, raw_name, menu_key),
                )
                self.connection.execute(
                    "INSERT INTO local_setting_slots(installation_id, namespace, raw_name, menu_path, series_id)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (installation_id, namespace, raw_name, menu_key, unique[0]),
                )
            else:
                # Legacy bytes cannot tell which series the player meant: keep
                # every definition and refuse automatic selection.
                self.connection.execute(
                    "INSERT INTO local_setting_ambiguities(installation_id, namespace, raw_name,"
                    " menu_path, series_ids) VALUES(?, ?, ?, ?, ?)"
                    " ON CONFLICT(installation_id, namespace, raw_name, menu_path)"
                    " DO UPDATE SET series_ids=excluded.series_ids",
                    (installation_id, namespace, raw_name, menu_key, json.dumps(unique)),
                )
        return count

    def list_local_setting_heads(self, installation_id: str) -> list[LocalSettingHead]:
        rows = self.connection.execute(
            "SELECT * FROM local_setting_heads WHERE installation_id=?"
            " ORDER BY created_at, local_setting_id",
            (installation_id,),
        )
        return [self._head_from_row(row) for row in rows]

    def get_local_setting_slot(
        self,
        installation_id: str,
        namespace: str,
        raw_name: str,
        menu_path: Sequence[str] | str,
    ) -> str | None:
        menu_key = self._menu_key(menu_path)
        row = self.connection.execute(
            "SELECT series_id FROM local_setting_slots"
            " WHERE installation_id=? AND namespace=? AND raw_name=? AND menu_path=?",
            (installation_id, namespace, raw_name, menu_key),
        ).fetchone()
        return None if row is None else row["series_id"]

    def resolve_local_setting(
        self,
        installation_id: str,
        namespace: str,
        raw_name: str,
        menu_path: Sequence[str] | str,
        *,
        identity_mode: str,
    ) -> str:
        menu_key = self._menu_key(menu_path)
        if identity_mode == "auto":
            if menu_key == "[]":
                raise ContractError(
                    ErrorCode.INPUT_VALIDATION_FAILED,
                    "automatic matching needs a known menu position; choose a definition explicitly",
                    field_errors=[FieldError(
                        path="menu_path", code="enum",
                        message="auto is not allowed for unknown positions",
                    )],
                )
            ambiguity = self.connection.execute(
                "SELECT series_ids FROM local_setting_ambiguities"
                " WHERE installation_id=? AND namespace=? AND raw_name=? AND menu_path=?",
                (installation_id, namespace, raw_name, menu_key),
            ).fetchone()
            if ambiguity is not None:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "this legacy location key is ambiguous; choose an existing definition explicitly",
                )
        slot = self.get_local_setting_slot(installation_id, namespace, raw_name, menu_key)
        if slot is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "no local setting owns this location"
            )
        return slot

    def list_local_setting_ambiguities(self, installation_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM local_setting_ambiguities WHERE installation_id=?"
            " ORDER BY namespace, raw_name, menu_path",
            (installation_id,),
        )
        return [
            {
                "installation_id": row["installation_id"],
                "namespace": row["namespace"],
                "raw_name": row["raw_name"],
                "menu_path": json.loads(row["menu_path"]),
                "series_ids": json.loads(row["series_ids"]),
            }
            for row in rows
        ]

    # -- amendment links ---------------------------------------------------
    def record_information_amendment(
        self,
        *,
        successor_id: str,
        source_request_id: str,
        root_request_id: str,
        root_version: int,
    ) -> None:
        for value, name in (
            (successor_id, "successor_id"),
            (source_request_id, "source_request_id"),
            (root_request_id, "root_request_id"),
        ):
            _require_id(value, name)
        if not isinstance(root_version, int) or isinstance(root_version, bool) or root_version < 1:
            raise ContractError(ErrorCode.INPUT_VALIDATION_FAILED, "root_version must be >= 1")
        try:
            self.connection.execute(
                "INSERT INTO information_amendment_links(successor_id, source_request_id,"
                " root_request_id, root_version, created_at) VALUES(?, ?, ?, ?, ?)",
                (successor_id, source_request_id, root_request_id, root_version, utc_now()),
            )
        except sqlite3.IntegrityError as error:
            raise ContractError(
                ErrorCode.INFORMATION_CONTEXT_CHANGED,
                "this request already has a successor; continue from the newest handled request",
                details=empty_details(),
            ) from error

    def get_information_amendment_root(self, successor_id: str) -> tuple[str, int] | None:
        row = self.connection.execute(
            "SELECT root_request_id, root_version FROM information_amendment_links"
            " WHERE successor_id=?",
            (successor_id,),
        ).fetchone()
        return None if row is None else (row["root_request_id"], row["root_version"])

    # -- declared game slots ------------------------------------------------
    @staticmethod
    def _normalise_game_name(raw_name: str) -> str:
        """NFC, then strip, then casefold — no other rewriting.

        Internal whitespace is preserved and the platform keeps its declared
        value, because the slot only serves this user's own declarations and
        must not merge names the player typed differently.
        """
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a declared game name must not be empty",
                field_errors=[FieldError(path="raw_name", code="required", message="empty name")],
            )
        return unicodedata.normalize("NFC", raw_name).strip().casefold()

    @staticmethod
    def _platform_key(platform: str | None) -> str:
        if platform is None:
            return "platform:unknown"
        if not platform.strip():
            raise ContractError(ErrorCode.INPUT_VALIDATION_FAILED, "declared platform must not be blank")
        return "platform:" + unicodedata.normalize("NFC", platform)

    def get_or_create_game_declaration_slot(
        self, user_scope_id: str, raw_name: str, platform: str | None, new_series_id: str
    ) -> str:
        """Return the series owning this declaration slot, creating it if free.

        Writers are serialised by ``BEGIN IMMEDIATE``, so the read-then-insert is
        atomic: concurrent different-key selections share one provisional series.
        """
        _require_id(user_scope_id, "user_scope_id")
        _require_id(new_series_id, "new_series_id")
        normalized = self._normalise_game_name(raw_name)
        platform_key = self._platform_key(platform)
        row = self.connection.execute(
            "SELECT series_id FROM game_declaration_slots"
            " WHERE user_scope_id=? AND normalized_raw_name=? AND platform_key=?",
            (user_scope_id, normalized, platform_key),
        ).fetchone()
        if row is not None:
            return row["series_id"]
        self.connection.execute(
            "INSERT INTO game_declaration_slots(user_scope_id, normalized_raw_name, platform_key,"
            " series_id, created_at) VALUES(?, ?, ?, ?, ?)",
            (user_scope_id, normalized, platform_key, new_series_id, utc_now()),
        )
        return new_series_id

    def claim_game_declaration_slot(
        self, user_scope_id: str, raw_name: str, platform: str | None, series_id: str
    ) -> str:
        _require_id(user_scope_id, "user_scope_id")
        _require_id(series_id, "series_id")
        normalized = self._normalise_game_name(raw_name)
        platform_key = self._platform_key(platform)
        row = self.connection.execute(
            "SELECT series_id FROM game_declaration_slots"
            " WHERE user_scope_id=? AND normalized_raw_name=? AND platform_key=?",
            (user_scope_id, normalized, platform_key),
        ).fetchone()
        if row is not None:
            if row["series_id"] != series_id:
                raise ContractError(
                    ErrorCode.INPUT_VALIDATION_FAILED,
                    "this declared name/platform slot already belongs to another game series",
                    field_errors=[FieldError(
                        path="raw_name", code="conflict", message="slot already claimed",
                    )],
                )
            return row["series_id"]
        self.connection.execute(
            "INSERT INTO game_declaration_slots(user_scope_id, normalized_raw_name, platform_key,"
            " series_id, created_at) VALUES(?, ?, ?, ?, ?)",
            (user_scope_id, normalized, platform_key, series_id, utc_now()),
        )
        return series_id

    def game_declaration_slot_platform_key(
        self, user_scope_id: str, raw_name: str, platform: str | None
    ) -> str | None:
        row = self.connection.execute(
            "SELECT platform_key FROM game_declaration_slots"
            " WHERE user_scope_id=? AND normalized_raw_name=? AND platform_key=?",
            (user_scope_id, self._normalise_game_name(raw_name), self._platform_key(platform)),
        ).fetchone()
        return None if row is None else row["platform_key"]

    # -- imported record keys ------------------------------------------------
    def claim_imported_record_key(
        self,
        *,
        record_snapshot_id: str,
        import_snapshot_id: str,
        capture_scope_id: str,
        record_type: str,
        local_record_id: str,
        usable: bool,
    ) -> None:
        """Claim the four-part record key.

        The unique index covers usable records only: an isolated record keeps
        its own identity and envelope, while two usable rows may never share
        ``(snapshot, scope, type, local id)``.  There is no last-write-wins.
        """
        for value, name in (
            (record_snapshot_id, "record_snapshot_id"),
            (import_snapshot_id, "import_snapshot_id"),
        ):
            _require_id(value, name)
        if not isinstance(capture_scope_id, str) or not capture_scope_id:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED, "capture_scope_id must not be empty"
            )
        if not isinstance(record_type, str) or not record_type:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED, "record_type must not be empty"
            )
        if not isinstance(local_record_id, str) or not local_record_id:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED, "local_record_id must not be empty"
            )
        try:
            self.connection.execute(
                "INSERT INTO imported_record_keys(record_snapshot_id, import_snapshot_id,"
                " capture_scope_id, record_type, local_record_id, usable)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (
                    record_snapshot_id,
                    import_snapshot_id,
                    capture_scope_id,
                    record_type,
                    local_record_id,
                    1 if usable else 0,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                "this capture scope already has a usable record with this type and local id",
                field_errors=[FieldError(
                    path="local_record_id", code="conflict",
                    message=f"duplicate {record_type} record {local_record_id!r}",
                )],
            ) from error

    # -- domain events ------------------------------------------------------
    def record_domain_event(
        self,
        *,
        target_type: str,
        target_id: str,
        dimension: str,
        next_state: str,
        cause: str,
        sequence: int = 1,
        previous_state: str | None = None,
        related_refs: Sequence[Any] = (),
        created_at: str | None = None,
    ) -> None:
        """Append one real state event; an object born in a state has no previous.

        Callers must not fabricate an intermediate state.  ``sequence=1`` with
        ``previous_state=None`` is the only shape a creation event may take.
        """
        _require_type(target_type)
        _require_id(target_id, "target_id")
        try:
            StateDimension(dimension)
        except ValueError as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                f"unregistered state dimension: {dimension}",
                field_errors=[FieldError(path="dimension", code="enum", message="unregistered")],
            ) from error
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED, "a domain event sequence is at least 1"
            )
        if not isinstance(next_state, str) or not next_state:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED, "a domain event needs a next state"
            )
        if not isinstance(cause, str) or not cause:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED, "a domain event needs a cause"
            )
        refs = [
            ref.model_dump(mode="json") if isinstance(ref, ObjectRef) else dict(ref)
            for ref in related_refs
        ]
        self.connection.execute(
            "INSERT INTO domain_events(target_type, target_id, dimension, sequence,"
            " previous_state, next_state, cause, related_refs, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                target_type,
                target_id,
                dimension,
                sequence,
                previous_state,
                next_state,
                cause,
                canonical_json(refs).decode("utf-8"),
                created_at or utc_now(),
            ),
        )

    # -- current applied pointer --------------------------------------------
    def current_applied(self, user_scope_id: str, installation_id: str) -> Any | None:
        _require_id(user_scope_id, "user_scope_id")
        _require_id(installation_id, "installation_id")
        return self.connection.execute(
            "SELECT * FROM current_applied WHERE user_scope_id=? AND installation_id=?",
            (user_scope_id, installation_id),
        ).fetchone()

    def advance_current_applied(
        self,
        *,
        user_scope_id: str,
        installation_id: str,
        expected_applied_id: str | None,
        next_applied_id: str,
        now: str,
    ) -> int:
        """Compare-and-set the per-installation pointer and append its event.

        This is the only implementation of the pointer rule: the caller's
        expected id is compared with the row *inside* the write transaction, and
        a mismatch raises before anything is written.
        """
        _require_id(user_scope_id, "user_scope_id")
        _require_id(installation_id, "installation_id")
        _require_id(next_applied_id, "next_applied_id")
        row = self.current_applied(user_scope_id, installation_id)
        previous = None if row is None else row["applied_id"]
        if expected_applied_id != previous:
            raise ContractError(
                ErrorCode.CURRENT_CONFIGURATION_CONFLICT,
                "the current configuration changed; re-read it before adopting",
                field_errors=[FieldError(
                    path="expected_current_applied_id", code="conflict",
                    message="not the current adopted configuration",
                )],
                details=empty_details(
                    current_resource_ref=(
                        None if previous is None
                        else ObjectRef(entity_type="AppliedConfiguration", entity_id=previous)
                    )
                ),
            )
        revision = 1 if row is None else int(row["revision"]) + 1
        self.connection.execute(
            "INSERT INTO current_applied(user_scope_id, installation_id, applied_id,"
            " revision, updated_at) VALUES(?, ?, ?, ?, ?)"
            " ON CONFLICT(user_scope_id, installation_id) DO UPDATE SET"
            " applied_id=excluded.applied_id, revision=excluded.revision,"
            " updated_at=excluded.updated_at",
            (user_scope_id, installation_id, next_applied_id, revision, now),
        )
        self.connection.execute(
            "INSERT INTO current_applied_events(user_scope_id, installation_id,"
            " pointer_revision, previous_applied_id, next_applied_id, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (
                user_scope_id, installation_id, revision, previous, next_applied_id, now,
            ),
        )
        return revision

    # -- metric rule registry ----------------------------------------------
    def record_metric_rule(
        self, *, metric_key: str, registry_version: str, rule_hash: str, definition_id: str
    ) -> None:
        try:
            self.connection.execute(
                "INSERT INTO metric_definition_rules(metric_key, registry_version, rule_hash,"
                " definition_id, created_at) VALUES(?, ?, ?, ?, ?)",
                (metric_key, registry_version, rule_hash, definition_id, utc_now()),
            )
        except sqlite3.IntegrityError as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "this metric rule is already registered",
            ) from error


__all__ = [
    "canonical_json",
    "StoredObject",
    "IdempotencyRow",
    "LocalSettingHead",
    "ProductRepository",
    "UnitOfWork",
]
