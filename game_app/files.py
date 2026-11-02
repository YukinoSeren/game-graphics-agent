"""Managed files: staged writes, atomic publication, recovery and backup.

Publishing is deliberately two-phase: files are written and hashed first; then a
short database transaction re-verifies them and records ManagedFile objects,
domain references and the original response.  A crash in between leaves a
complete but unconsumable file that a retry may reuse — never a published
resource with missing bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID, uuid5

from game_app.contracts.enums import FileOperationPhase, PublicationStatus
from game_app.contracts.primitives import (
    Hash,
    ID,
    NonNegativeInt,
    StrictModel,
    Timestamp,
    new_id,
    utc_now,
)
from game_app.errors import ContractError, ErrorCode, FieldError
from game_app.repository import ProductRepository, UnitOfWork, canonical_json

_NAMESPACE = UUID("1f6a5d0c-8b1c-4c11-9a5e-6b9c0c2d7f43")


class FileEntry(StrictModel):
    relative_name: str
    byte_length: NonNegativeInt
    sha256: Hash
    media_type: str


class FileManifest(StrictModel):
    """Ordered file entries.  A ``prepared`` operation starts empty; every
    published manifest is non-empty, enforced when files are staged."""

    entries: list[FileEntry]


class ManagedFile(StrictModel):
    id: ID
    schema_version: str
    created_at: Timestamp
    manifest: FileEntry
    relative_path: str
    file_status: PublicationStatus
    # The persistent user whose operation published the file.  Null marks an
    # internal artifact (task/provider output) that no user entry may consume.
    user_scope_id: ID | None = None


@dataclass
class PublishHooks:
    """Test-only injection points; production callers pass none."""

    before_write: Callable[[Path], None] | None = None
    after_rename: Callable[[Path], None] | None = None
    before_commit: Callable[[], None] | None = None
    after_commit: Callable[[], None] | None = None


@dataclass(frozen=True)
class PublishResult:
    operation_id: str
    file_ids: list[str]
    refs: list[dict[str, Any]]
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class FileOperationRecord:
    operation_id: str
    kind: str
    user_scope_id: str
    method: str
    canonical_route: str
    key: str
    request_schema_version: str
    request_hash: str
    owner_ref: str | None
    file_ids: list[str]
    manifest: FileManifest
    phase: str
    last_error_code: str | None
    created_at: str
    updated_at: str


class FileService:
    def __init__(self, repository: ProductRepository, root: str | Path) -> None:
        self.repository = repository
        self.root = Path(root)
        self.staging_dir = self.root / "staging"
        self.objects_dir = self.root / "objects"

    # -- publish -----------------------------------------------------------
    def publish(
        self,
        *,
        kind: str,
        files: Mapping[str, bytes],
        user_scope_id: str,
        method: str,
        canonical_route: str,
        key: str,
        request_schema_version: str,
        request_hash: str,
        writer: Callable[[UnitOfWork, FileOperationRecord, list[ManagedFile]], Sequence[Any]] | None = None,
        hooks: PublishHooks | None = None,
        owner_ref: str | None = None,
    ) -> PublishResult:
        hooks = hooks or PublishHooks()
        material = _materialise(files)
        replay = self._replay(user_scope_id, method, canonical_route, key, request_schema_version, request_hash)
        if replay is not None:
            return replay

        operation = self._get_or_create_operation(
            kind=kind,
            user_scope_id=user_scope_id,
            method=method,
            canonical_route=canonical_route,
            key=key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            owner_ref=owner_ref,
        )
        if operation.phase != FileOperationPhase.committed.value:
            operation = self._stage(operation, material)
            self._write_finals(operation, material, hooks)
        managed = self._managed_files(operation)

        if hooks.before_commit is not None:
            hooks.before_commit()

        with self.repository.unit_of_work() as uow:
            # A concurrent submission of the same key may have committed while
            # this one was staging: replay its response instead of re-running.
            winner = self._replay_committed(
                uow, user_scope_id, method, canonical_route, key,
                request_schema_version, request_hash,
            )
            if winner is not None:
                return winner
            # Re-verify inside the publishing transaction: a file that changed
            # between staging and commit must not become consumable.
            for entry, file_id in zip(operation.manifest.entries, operation.file_ids):
                final = self.objects_dir / file_id
                _verify_file(final, entry)
            refs: list[Any] = []
            if writer is not None:
                refs = list(writer(uow, operation, managed))
            now = utc_now()
            for managed_file in managed:
                uow.insert_object_bytes(
                    "ManagedFile",
                    schema_version="product-contract/0.4",
                    raw=canonical_json(managed_file.model_dump(mode="json")),
                    object_id=managed_file.id,
                    created_at=now,
                )
            uow.execute(
                "UPDATE file_operations SET phase=?, updated_at=? WHERE operation_id=?",
                (FileOperationPhase.committed.value, now, operation.operation_id),
            )
            body = {
                "operation_id": operation.operation_id,
                "file_ids": list(operation.file_ids),
                "refs": [ref.model_dump(mode="json") for ref in refs],
            }
            uow.insert_idempotency(
                user_scope_id=user_scope_id,
                method=method,
                canonical_route=canonical_route,
                key=key,
                request_schema_version=request_schema_version,
                request_hash=request_hash,
                response_status=201,
                response_body=canonical_json(body),
                result_refs=[ref.model_dump(mode="json") for ref in refs],
            )

        if hooks.after_commit is not None:
            hooks.after_commit()
        return PublishResult(
            operation_id=operation.operation_id,
            file_ids=list(operation.file_ids),
            refs=[ref.model_dump(mode="json") for ref in refs],
            status_code=201,
            body=body,
        )

    # -- reads --------------------------------------------------------------
    def read_managed_file(self, file_id: str) -> bytes:
        stored = self.repository.get_object("ManagedFile", file_id)
        payload = stored.payload
        if payload.get("file_status") != PublicationStatus.published.value:
            raise ContractError(
                ErrorCode.ARTIFACT_UNAVAILABLE,
                "this file is not currently consumable",
            )
        path = self.root / payload["relative_path"]
        entry = FileEntry.model_validate(payload["manifest"])
        try:
            _verify_file(path, entry)
            return path.read_bytes()
        except ContractError:
            raise
        except OSError as error:
            raise ContractError(
                ErrorCode.ARTIFACT_UNAVAILABLE, "the stored file cannot be read"
            ) from error

    def get_operation(self, operation_id: str) -> FileOperationRecord:
        with self.repository.unit_of_work() as uow:
            rows = uow.query("SELECT * FROM file_operations WHERE operation_id=?", (operation_id,))
        if not rows:
            raise ContractError(ErrorCode.RESOURCE_NOT_FOUND, "no such file operation")
        return _operation_from_row(rows[0])

    def get_operation_by_key(
        self, user_scope_id: str, method: str, canonical_route: str, key: str
    ) -> FileOperationRecord | None:
        with self.repository.unit_of_work() as uow:
            rows = uow.query(
                "SELECT * FROM file_operations WHERE user_scope_id=? AND method=?"
                " AND canonical_route=? AND key=?",
                (user_scope_id, method, canonical_route, key),
            )
        return None if not rows else _operation_from_row(rows[0])

    # -- abandoned work ------------------------------------------------------
    def abandon(self, operation_id: str, *, reason: str) -> None:
        operation = self.get_operation(operation_id)
        if operation.phase == FileOperationPhase.committed.value:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "a committed operation cannot be abandoned",
            )
        shutil.rmtree(self.staging_dir / operation_id, ignore_errors=True)
        for file_id in operation.file_ids:
            path = self.objects_dir / file_id
            if path.is_file():
                path.unlink()
        with self.repository.unit_of_work() as uow:
            uow.execute(
                "UPDATE file_operations SET phase=?, last_error_code=?, updated_at=?"
                " WHERE operation_id=?",
                (FileOperationPhase.abandoned.value, reason, utc_now(), operation_id),
            )

    # -- artifacts produced by other modules --------------------------------
    def store_artifact(
        self,
        data: bytes,
        *,
        relative_name: str,
        stable_key: str,
        user_scope_id: str | None = None,
    ) -> str:
        """Store an immutable artifact (e.g. a provider response original).

        The file id is derived from ``stable_key`` so a retry after a crash
        reuses the same published file and never duplicates the object.
        """
        _materialise({relative_name: data})
        file_id = uuid5(_NAMESPACE, stable_key).hex
        entry = FileEntry(
            relative_name=relative_name,
            byte_length=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            media_type=_media_type(relative_name),
        )
        existing = self.repository.find_object("ManagedFile", file_id)
        if existing is not None:
            _verify_file(self.objects_dir / file_id, entry)
            return file_id

        self.objects_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        final = self.objects_dir / file_id
        if final.is_file():
            _verify_file(final, entry)
        else:
            temporary = self.objects_dir / f".{file_id}.{new_id()}.tmp"
            try:
                _write_private(temporary, data)
                os.replace(temporary, final)
            except OSError as error:
                temporary.unlink(missing_ok=True)
                raise ContractError(
                    ErrorCode.STORAGE_COMMIT_FAILED,
                    "the artifact could not be written",
                ) from error

        managed = ManagedFile(
            id=file_id,
            schema_version="product-contract/0.4",
            created_at=utc_now(),
            manifest=entry,
            relative_path=f"objects/{file_id}",
            file_status=PublicationStatus.published,
            user_scope_id=user_scope_id,
        )
        with self.repository.unit_of_work() as uow:
            uow.insert_object_bytes(
                "ManagedFile",
                schema_version="product-contract/0.4",
                raw=canonical_json(managed.model_dump(mode="json")),
                object_id=file_id,
                created_at=managed.created_at,
            )
        return file_id

    # -- backup --------------------------------------------------------------
    def backup(self, destination: str | Path) -> Path:
        destination = Path(destination)
        if destination.exists() and any(destination.iterdir()):
            raise ContractError(
                ErrorCode.STORAGE_COMMIT_FAILED,
                "the backup destination already contains files",
            )
        destination.mkdir(parents=True, exist_ok=True)
        database_copy = destination / "product.sqlite3"
        try:
            source = self.repository.connect()
            with sqlite3.connect(database_copy) as target:
                source.backup(target)
            with sqlite3.connect(database_copy) as check:
                if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ContractError(
                        ErrorCode.STORAGE_COMMIT_FAILED, "the database backup failed verification"
                    )
            entries = [_entry_for(database_copy, "product.sqlite3", "application/vnd.sqlite3")]
            if self.objects_dir.is_dir():
                shutil.copytree(self.objects_dir, destination / "objects", dirs_exist_ok=True)
                for path in sorted(self.objects_dir.rglob("*")):
                    if path.is_file():
                        relative = path.relative_to(self.root).as_posix()
                        entries.append(_entry_for(
                            destination / relative, relative, _media_type(relative)
                        ))
            # The manifest alone cannot prove completeness: every file the
            # backed-up database publishes must exist and still match its hash.
            entries.extend(self._verify_referenced_files(database_copy, destination))
        except OSError as error:
            raise ContractError(
                ErrorCode.STORAGE_COMMIT_FAILED, "the backup could not be written"
            ) from error
        (destination / "backup-manifest.json").write_text(
            json.dumps({"entries": [entry.model_dump(mode="json") for entry in entries]},
                       ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return destination

    # -- internals ------------------------------------------------------------
    def _replay_committed(
        self,
        uow: UnitOfWork,
        user_scope_id: str,
        method: str,
        canonical_route: str,
        key: str,
        request_schema_version: str,
        request_hash: str,
    ) -> PublishResult | None:
        record = uow.find_idempotency(user_scope_id, method, canonical_route, key)
        if record is None:
            return None
        if record.lifecycle.value == "tombstoned":
            raise ContractError(
                ErrorCode.RESOURCE_GONE,
                "this file submission was cleaned up; start a new logical submission",
            )
        if (
            record.request_schema_version != request_schema_version
            or record.request_hash != request_hash
        ):
            raise ContractError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "the same key was already used for different file content or contract version",
                field_errors=[FieldError(
                    path="idempotency_key", code="conflict", message="different request hash",
                )],
            )
        body = json.loads(record.response_body)
        return PublishResult(
            operation_id=body["operation_id"],
            file_ids=list(body["file_ids"]),
            refs=list(body["refs"]),
            status_code=record.response_status,
            body=body,
        )

    def _verify_referenced_files(
        self, database_copy: Path, destination: Path
    ) -> list[FileEntry]:
        verified: list[FileEntry] = []
        with sqlite3.connect(database_copy) as conn:
            rows = conn.execute(
                "SELECT id, payload FROM objects WHERE entity_type='ManagedFile'"
            ).fetchall()
        for file_id, payload in rows:
            data = json.loads(payload)
            if data.get("file_status") != PublicationStatus.published.value:
                continue
            entry = FileEntry.model_validate(data["manifest"])
            copied = destination / data["relative_path"]
            try:
                _verify_file(copied, entry)
            except ContractError as error:
                raise ContractError(
                    ErrorCode.STORAGE_COMMIT_FAILED,
                    f"backup is incomplete: published file {file_id} is missing or changed",
                ) from error
            verified.append(_entry_for(copied, data["relative_path"], entry.media_type))
        return verified

    # -- internals ------------------------------------------------------------
    def _replay(
        self,
        user_scope_id: str,
        method: str,
        canonical_route: str,
        key: str,
        request_schema_version: str,
        request_hash: str,
    ) -> PublishResult | None:
        record = self.repository.find_idempotency(user_scope_id, method, canonical_route, key)
        if record is None:
            return None
        if record.lifecycle.value == "tombstoned":
            raise ContractError(
                ErrorCode.RESOURCE_GONE,
                "this file submission was cleaned up; start a new logical submission",
            )
        if record.request_schema_version != request_schema_version or record.request_hash != request_hash:
            raise ContractError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "the same key was already used for different file content or contract version",
                field_errors=[FieldError(
                    path="idempotency_key", code="conflict", message="different request hash",
                )],
            )
        body = json.loads(record.response_body)
        return PublishResult(
            operation_id=body["operation_id"],
            file_ids=list(body["file_ids"]),
            refs=list(body["refs"]),
            status_code=record.response_status,
            body=body,
        )

    def _get_or_create_operation(
        self,
        *,
        kind: str,
        user_scope_id: str,
        method: str,
        canonical_route: str,
        key: str,
        request_schema_version: str,
        request_hash: str,
        owner_ref: str | None,
    ) -> FileOperationRecord:
        existing = self.get_operation_by_key(user_scope_id, method, canonical_route, key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ContractError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "the same key was already used for different file content",
                )
            return existing
        operation_id = new_id()
        now = utc_now()
        try:
            with self.repository.unit_of_work() as uow:
                uow.execute(
                    "INSERT INTO file_operations(operation_id, kind, user_scope_id, method,"
                    " canonical_route, key, request_schema_version, request_hash, owner_ref,"
                    " file_ids, manifest, phase, last_error_code, created_at, updated_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, NULL, ?, ?)",
                    (
                        operation_id,
                        kind,
                        user_scope_id,
                        method,
                        canonical_route,
                        key,
                        request_schema_version,
                        request_hash,
                        owner_ref,
                        canonical_json({"entries": []}).decode(),
                        FileOperationPhase.prepared.value,
                        now,
                        now,
                    ),
                )
        except ContractError:
            raise
        except sqlite3.IntegrityError:
            concurrent = self.get_operation_by_key(user_scope_id, method, canonical_route, key)
            if concurrent is None:
                raise
            return concurrent
        return self.get_operation(operation_id)

    def _stage(
        self, operation: FileOperationRecord, material: Mapping[str, bytes]
    ) -> FileOperationRecord:
        manifest = operation.manifest
        if manifest.entries:
            stored = {entry.relative_name: entry for entry in manifest.entries}
            if set(stored) != set(material):
                raise ContractError(
                    ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                    "the retried submission has a different file set",
                )
            for name, data in material.items():
                if hashlib.sha256(data).hexdigest() != stored[name].sha256:
                    raise ContractError(
                        ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                        "the retried submission changed a frozen file",
                    )
            return operation

        staging = self.staging_dir / operation.operation_id
        entries: list[FileEntry] = []
        for name in sorted(material):
            data = material[name]
            path = staging / name
            try:
                _write_private(path, data)
            except OSError as error:
                raise ContractError(
                    ErrorCode.STORAGE_COMMIT_FAILED,
                    "staged files could not be written; nothing was published",
                ) from error
            entries.append(FileEntry(
                relative_name=name,
                byte_length=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                media_type=_media_type(name),
            ))
        manifest = FileManifest(entries=entries)
        file_ids = [
            uuid5(_NAMESPACE, f"{operation.operation_id}:{entry.relative_name}").hex
            for entry in manifest.entries
        ]
        with self.repository.unit_of_work() as uow:
            uow.execute(
                "UPDATE file_operations SET manifest=?, file_ids=?, phase=?, updated_at=?"
                " WHERE operation_id=?",
                (
                    canonical_json(manifest.model_dump(mode="json")).decode(),
                    canonical_json(file_ids).decode(),
                    FileOperationPhase.files_ready.value,
                    utc_now(),
                    operation.operation_id,
                ),
            )
        return self.get_operation(operation.operation_id)

    def _write_finals(
        self,
        operation: FileOperationRecord,
        material: Mapping[str, bytes],
        hooks: PublishHooks,
    ) -> None:
        self.objects_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for entry, file_id in zip(operation.manifest.entries, operation.file_ids):
            final = self.objects_dir / file_id
            if final.is_file():
                if hashlib.sha256(final.read_bytes()).hexdigest() == entry.sha256:
                    continue
                raise ContractError(
                    ErrorCode.ARTIFACT_UNAVAILABLE,
                    "a different file already occupies this managed path",
                )
            data = material.get(entry.relative_name)
            if data is None:
                data = (self.staging_dir / operation.operation_id / entry.relative_name).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry.sha256:
                raise ContractError(
                    ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                    "staged bytes no longer match the manifest",
                )
            # Each writer uses its own temporary name: two same-key submissions
            # may race here, and a shared name lets one delete the other's file.
            temporary = self.objects_dir / f".{file_id}.{new_id()}.tmp"
            try:
                if hooks.before_write is not None:
                    hooks.before_write(final)
                _write_private(temporary, data)
                os.replace(temporary, final)
            except OSError as error:
                temporary.unlink(missing_ok=True)
                # A concurrent submission of the same key may have renamed
                # identical bytes into place first; on Windows the second
                # os.replace then fails with a sharing violation.  Identical
                # content is not a conflict — verify and accept it.
                try:
                    already_identical = (
                        final.is_file()
                        and hashlib.sha256(final.read_bytes()).hexdigest() == entry.sha256
                    )
                except OSError:
                    already_identical = False
                if already_identical:
                    if hooks.after_rename is not None:
                        hooks.after_rename(final)
                    continue
                raise ContractError(
                    ErrorCode.STORAGE_COMMIT_FAILED,
                    "the managed file could not be written; nothing was published",
                ) from error
            if hooks.after_rename is not None:
                hooks.after_rename(final)

    def _managed_files(self, operation: FileOperationRecord) -> list[ManagedFile]:
        now = utc_now()
        return [
            ManagedFile(
                id=file_id,
                schema_version="product-contract/0.4",
                created_at=now,
                manifest=entry,
                relative_path=f"objects/{file_id}",
                file_status=PublicationStatus.published,
                user_scope_id=operation.user_scope_id,
            )
            for entry, file_id in zip(operation.manifest.entries, operation.file_ids)
        ]


def _materialise(files: Mapping[str, bytes]) -> dict[str, bytes]:
    if not files:
        raise ContractError(ErrorCode.INPUT_VALIDATION_FAILED, "at least one file is required")
    material: dict[str, bytes] = {}
    for name, data in files.items():
        if not _is_safe_relative_name(name):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "managed file names must be relative POSIX names inside the managed root",
                field_errors=[FieldError(path="relative_name", code="type", message="unsafe name")],
            )
        if not isinstance(data, (bytes, bytearray)):
            raise ContractError(ErrorCode.INPUT_VALIDATION_FAILED, "file content must be bytes")
        material[name] = bytes(data)
    return material


def _is_safe_relative_name(name: str) -> bool:
    """Reject anything that could resolve outside the managed root.

    ``Path("C:x.json")`` is not absolute on Windows but joins to a drive-relative
    path, so a drive prefix or any colon is refused explicitly rather than
    relying on ``is_absolute``.  Empty, ``.`` and ``..`` segments, backslashes
    and non-normalised names are refused too.
    """
    if not isinstance(name, str) or not name or ":" in name or "\\" in name:
        return False
    pure = Path(name)
    if pure.is_absolute() or pure.drive or pure.as_posix() != name:
        return False
    parts = name.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _write_private(path: Path, data: bytes) -> None:
    """Write bytes with owner-only permissions where the OS honours them."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _media_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return {
        ".json": "application/json",
        ".csv": "text/csv",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".sqlite3": "application/vnd.sqlite3",
    }.get(suffix, "application/octet-stream")


def _verify_file(path: Path, entry: FileEntry) -> None:
    try:
        data = path.read_bytes()
    except FileNotFoundError as error:
        raise ContractError(
            ErrorCode.ARTIFACT_UNAVAILABLE,
            f"managed file {entry.relative_name} is missing",
        ) from error
    except OSError as error:
        raise ContractError(
            ErrorCode.ARTIFACT_UNAVAILABLE, "the managed file cannot be read"
        ) from error
    if len(data) != entry.byte_length or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise ContractError(
            ErrorCode.ARTIFACT_UNAVAILABLE,
            f"managed file {entry.relative_name} no longer matches its manifest",
        )


def _entry_for(path: Path, relative_name: str, media_type: str) -> FileEntry:
    data = path.read_bytes()
    return FileEntry(
        relative_name=relative_name,
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        media_type=media_type,
    )


def _operation_from_row(row: Any) -> FileOperationRecord:
    return FileOperationRecord(
        operation_id=row["operation_id"],
        kind=row["kind"],
        user_scope_id=row["user_scope_id"],
        method=row["method"],
        canonical_route=row["canonical_route"],
        key=row["key"],
        request_schema_version=row["request_schema_version"],
        request_hash=row["request_hash"],
        owner_ref=row["owner_ref"],
        file_ids=list(json.loads(row["file_ids"] or "[]")),
        manifest=FileManifest.model_validate_json(row["manifest"]),
        phase=row["phase"],
        last_error_code=row["last_error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = [
    "FileEntry",
    "FileManifest",
    "ManagedFile",
    "FileOperationRecord",
    "FileService",
    "PublishHooks",
    "PublishResult",
]
