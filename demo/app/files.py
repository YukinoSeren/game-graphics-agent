"""Managed demo file storage. Files are addressed by ID, never by path."""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

from .contracts import DemoError, JsonObject

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_MEDIA_TYPES = {
    "text/csv",
    "text/plain",
    "application/json",
    "application/octet-stream",
}
_UNSAFE_NAME = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")

# A staged file is only a candidate. It becomes downloadable only after the
# command transaction registers it.
STAGING_DIR = ".staging"


def validate_filename(filename: str) -> str:
    if not isinstance(filename, str):
        raise DemoError("invalid_filename", "文件名必须是文本。")
    label = filename.strip()
    if not label:
        return "upload"
    if len(label) > 120:
        raise DemoError("invalid_filename", "文件名过长。")
    if label in (".", "..") or ".." in label or _UNSAFE_NAME.search(label):
        raise DemoError(
            "invalid_filename",
            "文件名不能包含路径分隔符或控制字符。",
            recovery_actions=["rename_file"],
        )
    if Path(label).is_absolute():
        raise DemoError("invalid_filename", "文件名不能是绝对路径。")
    return label


class ManagedFiles:
    def __init__(self, runtime_root: Path, *, db_path: Path | None = None):
        self.runtime_root = Path(runtime_root)
        self.uploads_root = self.runtime_root / "uploads"
        self.staging_root = self.uploads_root / STAGING_DIR
        self.uploads_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else None

    def stage(self, data: bytes, media_type: str) -> JsonObject:
        if not isinstance(data, (bytes, bytearray)):
            raise DemoError("invalid_file", "上传内容必须是二进制数据。")
        data = bytes(data)
        if len(data) > MAX_UPLOAD_BYTES:
            raise DemoError(
                "file_too_large",
                "单个文件不能超过 5 MiB。",
                status=413,
                recovery_actions=["choose_smaller_file"],
            )
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise DemoError(
                "unsupported_media_type",
                "目前只接受 CSV、JSON 或纯文本文件。",
                status=400,
                recovery_actions=["choose_supported_file"],
            )
        staged_path = self.staging_root / f"{uuid.uuid4().hex}.part"
        staged_path.write_bytes(data)
        return {
            "staged_path": str(staged_path),
            "content_hash": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "media_type": media_type,
        }

    def discard(self, staged: JsonObject) -> None:
        try:
            Path(staged["staged_path"]).unlink()
        except OSError:
            pass

    def register(self, tx, staged: JsonObject, filename: str) -> JsonObject:
        label = validate_filename(filename)
        staged_path = Path(staged["staged_path"])
        if not staged_path.is_file():
            raise DemoError("staged_file_missing", "暂存文件不存在，请重新上传。", status=404)
        digest = hashlib.sha256(staged_path.read_bytes()).hexdigest()
        if digest != staged["content_hash"]:
            raise DemoError("staged_file_changed", "暂存文件内容已变化，请重新上传。", status=409)
        file_id = f"demo_file_{uuid.uuid4().hex}"
        relative = Path("uploads") / file_id
        target = self.runtime_root / relative
        shutil.move(str(staged_path), str(target))
        record = tx.insert(
            "File",
            {
                "filename_label": label,
                "media_type": staged["media_type"],
                "content_hash": digest,
                "size": staged["size"],
            },
            origin="uploaded_demo_file",
            entity_id=file_id,
        )
        tx.add_file_row(
            file_id=file_id,
            rel_path=str(relative).replace("\\", "/"),
            content_hash=digest,
            media_type=staged["media_type"],
            size=staged["size"],
        )
        return record

    def read(self, file_id: str, *, expected_hash: str | None = None) -> bytes:
        if self.db_path is None:
            raise DemoError("files_unavailable", "文件存储尚未就绪。", status=500)
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT rel_path, content_hash FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise DemoError("file_not_registered", "文件不存在或尚未保存。", status=404)
        path = (self.runtime_root / row["rel_path"]).resolve()
        try:
            path.relative_to(self.uploads_root.resolve())
        except ValueError as exc:
            raise DemoError("file_not_registered", "文件路径无效。", status=404) from exc
        if not path.is_file():
            raise DemoError(
                "artifact_missing",
                "原始记录不可用，请重新导入或恢复备份。",
                status=404,
                recovery_actions=["reimport_file", "restore_backup"],
            )
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["content_hash"]:
            raise DemoError(
                "artifact_hash_mismatch",
                "原始文件内容与保存记录不一致。",
                status=409,
                recovery_actions=["reimport_file"],
            )
        if expected_hash is not None and expected_hash != row["content_hash"]:
            raise DemoError(
                "artifact_hash_mismatch",
                "引用的原件哈希与文件记录不一致。",
                status=409,
                recovery_actions=["reload_sample"],
            )
        return data

    def path_for(self, file_id: str) -> Path:
        if self.db_path is None:
            raise DemoError("files_unavailable", "文件存储尚未就绪。", status=500)
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            row = conn.execute(
                "SELECT rel_path FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise DemoError("file_not_registered", "文件不存在。", status=404)
        return self.runtime_root / row[0]

    def unlink_managed(self, file_id: str) -> None:
        """Remove a just-moved file whose registration is being rolled back."""
        if not isinstance(file_id, str) or not file_id:
            return
        path = (self.uploads_root / file_id).resolve()
        try:
            path.relative_to(self.uploads_root.resolve())
        except ValueError:
            return
        try:
            path.unlink()
        except OSError:
            pass

    def delete_registered(self, file_ids: list[str]) -> None:
        """Remove demo-managed files. Used only by demo reset/restore."""
        if self.db_path is None:
            return
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            for file_id in file_ids:
                row = conn.execute(
                    "SELECT rel_path FROM files WHERE id = ?", (file_id,)
                ).fetchone()
                if row is None:
                    continue
                path = (self.runtime_root / row[0]).resolve()
                try:
                    path.relative_to(self.uploads_root.resolve())
                except ValueError:
                    continue
                try:
                    path.unlink()
                except OSError:
                    pass
                conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        finally:
            conn.close()
