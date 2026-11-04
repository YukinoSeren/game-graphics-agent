"""Independent SQLite storage for the temporary demo.

Six tables, one write lock, scoped entities, and replayable commands. This
is not the formal project's database design.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .contracts import (
    Clock,
    Command,
    DemoError,
    DomainResult,
    JsonObject,
    RealClock,
    Reply,
    Scope,
    is_positive_int,
)
from .scenarios import ScenarioCatalog

ORIGINS = ("fixture", "simulated", "user_declared", "uploaded_demo_file", "demo_seed")
RESERVED_ENTITY_KEYS = {
    "id",
    "kind",
    "scenario_id",
    "workspace_epoch",
    "created_at",
    "demo_only",
    "data_origin",
    "revision",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspaces (
    scenario_id TEXT PRIMARY KEY,
    workspace_epoch INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    context_id TEXT,
    current_applied_id TEXT,
    mode TEXT NOT NULL,
    script TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    scenario_id TEXT NOT NULL,
    workspace_epoch INTEGER NOT NULL,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    data_origin TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scenario_id, workspace_epoch, id)
);
CREATE TABLE IF NOT EXISTS tasks (
    scenario_id TEXT NOT NULL,
    workspace_epoch INTEGER NOT NULL,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    phase_index INTEGER NOT NULL,
    parent_id TEXT,
    payload TEXT NOT NULL,
    result TEXT NOT NULL,
    next_run_at REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scenario_id, workspace_epoch, id)
);
CREATE TABLE IF NOT EXISTS events (
    scenario_id TEXT NOT NULL,
    workspace_epoch INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scenario_id, workspace_epoch, seq)
);
CREATE TABLE IF NOT EXISTS mutations (
    scenario_id TEXT NOT NULL,
    workspace_epoch INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    action TEXT NOT NULL,
    status INTEGER NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scenario_id, workspace_epoch, request_id)
);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    rel_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _loads(text: str) -> Any:
    return json.loads(text)


def canonical_hash(command: Command) -> str:
    material = {
        "action": command.action,
        "scenario_id": command.scenario_id,
        "workspace_epoch": command.workspace_epoch,
        "expected_revision": command.expected_revision,
        "payload": command.payload,
    }
    try:
        encoded = _dumps(material).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DemoError("invalid_payload", "请求包含无法序列化的数值或结构。") from exc
    return hashlib.sha256(encoded).hexdigest()


def validate_command(command: Command) -> None:
    if not isinstance(command.request_id, str) or not command.request_id.strip():
        raise DemoError("invalid_request_id", "request_id 不能为空。")
    if len(command.request_id) > 128:
        raise DemoError("invalid_request_id", "request_id 过长。")
    if not isinstance(command.action, str) or not command.action.strip():
        raise DemoError("invalid_action", "action 不能为空。")
    if not is_positive_int(command.workspace_epoch):
        raise DemoError("invalid_epoch", "workspace_epoch 必须是正整数。")
    if not is_positive_int(command.expected_revision):
        raise DemoError("invalid_revision", "expected_revision 必须是正整数。")
    if not isinstance(command.payload, dict):
        raise DemoError("invalid_payload", "payload 必须是 JSON 对象。")


class Transaction:
    """Scoped write handle passed to a single command handler."""

    def __init__(self, store: "DemoStore", conn: sqlite3.Connection, scope: Scope):
        self._store = store
        self._conn = conn
        self.scope = scope

    @property
    def catalog(self) -> ScenarioCatalog:
        return self._store.catalog

    @property
    def clock(self) -> Clock:
        return self._store.clock

    def workspace(self) -> JsonObject:
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE scenario_id = ?", (self.scope.scenario_id,)
        ).fetchone()
        if row is None:
            raise DemoError("unknown_scenario", "演示案例不存在。", status=404)
        return _workspace_from_row(row)

    def rescope(self, workspace_epoch: int) -> "Transaction":
        return Transaction(self._store, self._conn, Scope(self.scope.scenario_id, workspace_epoch))

    @contextmanager
    def savepoint(self):
        """Rollback one task publisher while preserving its failure status."""
        self._conn.execute("SAVEPOINT task_publisher")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK TO task_publisher")
            self._conn.execute("RELEASE task_publisher")
            raise
        else:
            self._conn.execute("RELEASE task_publisher")

    def insert(
        self,
        kind: str,
        payload: JsonObject,
        *,
        origin: str,
        entity_id: str | None = None,
        revision: int = 1,
    ) -> JsonObject:
        if not isinstance(kind, str) or not kind:
            raise DemoError("invalid_entity", "实体类型不能为空。")
        if origin not in ORIGINS:
            raise DemoError("invalid_origin", "不支持的数据来源标记。")
        if not isinstance(payload, dict):
            raise DemoError("invalid_entity", "实体内容必须是 JSON 对象。")
        if not is_positive_int(revision):
            raise DemoError("invalid_entity", "实体修订必须是正整数。")
        clash = RESERVED_ENTITY_KEYS.intersection(payload)
        if clash:
            raise DemoError(
                "reserved_field",
                f"实体内容不能覆盖保留字段：{', '.join(sorted(clash))}。",
            )
        entity_id = entity_id or f"demo_{kind.lower()}_{uuid.uuid4().hex}"
        created_at = self.clock.now_iso()
        record = {
            "id": entity_id,
            "kind": kind,
            "scenario_id": self.scope.scenario_id,
            "workspace_epoch": self.scope.workspace_epoch,
            "created_at": created_at,
            "demo_only": True,
            "data_origin": origin,
            "revision": revision,
        }
        record.update(payload)
        self._conn.execute(
            "INSERT INTO entities "
            "(scenario_id, workspace_epoch, kind, id, revision, data_origin, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.scope.scenario_id,
                self.scope.workspace_epoch,
                kind,
                entity_id,
                revision,
                origin,
                _dumps(record),
                created_at,
            ),
        )
        return record

    def get(self, entity_id: str, *, kind: str | None = None) -> JsonObject:
        if not isinstance(entity_id, str) or not entity_id:
            raise DemoError("invalid_reference", "实体引用不能为空。")
        row = self._conn.execute(
            "SELECT kind, payload FROM entities "
            "WHERE scenario_id = ? AND workspace_epoch = ? AND id = ?",
            (self.scope.scenario_id, self.scope.workspace_epoch, entity_id),
        ).fetchone()
        if row is None:
            raise DemoError("not_found", "演示对象不存在或不属于当前案例。", status=404)
        if kind is not None and row["kind"] != kind:
            raise DemoError(
                "wrong_type",
                f"演示对象类型不是 {kind}。",
                status=404,
            )
        return _loads(row["payload"])

    def all_entities(self) -> list[JsonObject]:
        rows = self._conn.execute(
            "SELECT kind, id, revision, data_origin, payload FROM entities "
            "WHERE scenario_id = ? AND workspace_epoch = ? ORDER BY rowid",
            (self.scope.scenario_id, self.scope.workspace_epoch),
        ).fetchall()
        return [
            {
                "kind": row["kind"],
                "id": row["id"],
                "revision": row["revision"],
                "data_origin": row["data_origin"],
                "payload": _loads(row["payload"]),
            }
            for row in rows
        ]

    def active_tasks(self) -> list[JsonObject]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE scenario_id = ? AND workspace_epoch = ? "
            "AND status IN ('queued', 'running') ORDER BY rowid",
            (self.scope.scenario_id, self.scope.workspace_epoch),
        ).fetchall()
        return [_task_from_row(row) for row in rows]

    def update_entity(
        self, entity_id: str, changes: JsonObject, *, revision: int
    ) -> JsonObject:
        if not is_positive_int(revision):
            raise DemoError("invalid_entity", "实体修订必须是正整数。")
        clash = RESERVED_ENTITY_KEYS.intersection(changes)
        if clash:
            raise DemoError("reserved_field", "实体更新不能覆盖保留字段。")
        record = self.get(entity_id)
        record.update(changes)
        record["revision"] = revision
        self._conn.execute(
            "UPDATE entities SET revision = ?, payload = ? "
            "WHERE scenario_id = ? AND workspace_epoch = ? AND id = ?",
            (
                revision,
                _dumps(record),
                self.scope.scenario_id,
                self.scope.workspace_epoch,
                entity_id,
            ),
        )
        return record

    def find(self, kind: str, **equals: Any) -> list[JsonObject]:
        rows = self._conn.execute(
            "SELECT payload FROM entities "
            "WHERE scenario_id = ? AND workspace_epoch = ? AND kind = ? ORDER BY rowid",
            (self.scope.scenario_id, self.scope.workspace_epoch, kind),
        ).fetchall()
        found = []
        for row in rows:
            payload = _loads(row["payload"])
            if all(payload.get(key) == value for key, value in equals.items()):
                found.append(payload)
        return found

    def update_workspace(self, **fields: Any) -> JsonObject:
        allowed = {"context_id", "current_applied_id", "mode", "script"}
        unknown = set(fields) - allowed
        if unknown:
            raise DemoError("invalid_workspace_field", "工作区字段不受支持。")
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            self._conn.execute(
                f"UPDATE workspaces SET {assignments}, updated_at = ? WHERE scenario_id = ?",
                (*fields.values(), self.clock.now_iso(), self.scope.scenario_id),
            )
        return self.workspace()

    def insert_task(
        self,
        kind: str,
        *,
        phases: list[JsonObject],
        input_data: JsonObject,
        parent_id: str | None = None,
        result: JsonObject | None = None,
    ) -> JsonObject:
        task_id = f"demo_task_{uuid.uuid4().hex}"
        now = self.clock.now_iso()
        payload = {"phases": phases, "phase_index": 0, "input": input_data}
        first_phase = phases[0]["key"] if phases else "done"
        self._conn.execute(
            "INSERT INTO tasks (scenario_id, workspace_epoch, id, kind, status, phase, "
            "phase_index, parent_id, payload, result, next_run_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.scope.scenario_id,
                self.scope.workspace_epoch,
                task_id,
                kind,
                "queued",
                first_phase,
                0,
                parent_id,
                _dumps(payload),
                _dumps(result or {}),
                self.clock.monotonic(),
                now,
            ),
        )
        self.emit(
            "task.queued",
            {"task_id": task_id, "kind": kind, "parent_id": parent_id},
        )
        task = self.task_row(task_id)
        if kind == "feedback.review":
            from .feedback import sync_analysis_status
            sync_analysis_status(self, task, "queued")
        return task

    def task_row(self, task_id: str) -> JsonObject:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE scenario_id = ? AND workspace_epoch = ? AND id = ?",
            (self.scope.scenario_id, self.scope.workspace_epoch, task_id),
        ).fetchone()
        if row is None:
            raise DemoError("not_found", "演示任务不存在或不属于当前案例。", status=404)
        return _task_from_row(row)

    def workspace_for(self, scenario_id: str) -> JsonObject:
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE scenario_id = ?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise DemoError("unknown_scenario", "演示案例不存在。", status=404)
        return _workspace_from_row(row)

    def reset_workspace(self, scenario_id: str, catalog: ScenarioCatalog) -> JsonObject:
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE scenario_id = ?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise DemoError("unknown_scenario", "演示案例不存在。", status=404)
        old_epoch = row["workspace_epoch"]
        new_epoch = old_epoch + 1
        scenario = catalog.get(scenario_id)
        now = self.clock.now_iso()
        context = self._store._initial_context(scenario, now)
        context["workspace_epoch"] = new_epoch
        file_rows = self._conn.execute(
            "SELECT id FROM entities WHERE scenario_id = ? AND workspace_epoch = ? "
            "AND kind = 'File'",
            (scenario_id, old_epoch),
        ).fetchall()
        self._conn.execute(
            "INSERT INTO entities (scenario_id, workspace_epoch, kind, id, revision, "
            "data_origin, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scenario_id,
                new_epoch,
                "ContextSnapshot",
                context["id"],
                1,
                "fixture",
                _dumps(context),
                now,
            ),
        )
        self._conn.execute(
            "UPDATE workspaces SET workspace_epoch = ?, revision = 1, context_id = ?, "
            "current_applied_id = NULL, updated_at = ? WHERE scenario_id = ?",
            (new_epoch, context["id"], now, scenario_id),
        )
        self._conn.execute(
            "UPDATE tasks SET status = 'interrupted', updated_at = ? "
            "WHERE scenario_id = ? AND workspace_epoch = ? "
            "AND status IN ('queued', 'running', 'waiting_input')",
            (now, scenario_id, old_epoch),
        )
        return {
            "scenario_id": scenario_id,
            "old_epoch": old_epoch,
            "new_epoch": new_epoch,
            "file_ids": [item["id"] for item in file_rows],
        }

    def advance_task(
        self,
        task_id: str,
        expected_phase: int,
        publish: Callable[["Transaction", JsonObject], JsonObject | None],
    ) -> bool:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE scenario_id = ? AND workspace_epoch = ? AND id = ?",
            (self.scope.scenario_id, self.scope.workspace_epoch, task_id),
        ).fetchone()
        if row is None or row["status"] not in ("queued", "running"):
            return False
        if row["phase_index"] != expected_phase:
            return False
        task = _task_from_row(row)
        update = publish(self, task) or {}
        if update.get("defer_s", 0) > 0:
            # A persisted presentation deadline must survive fast/manual
            # advancement without marking the final phase as complete.
            self._conn.execute(
                "UPDATE tasks SET status = 'running', next_run_at = ?, updated_at = ? WHERE id = ?",
                (self.clock.monotonic() + float(update["defer_s"]), self.clock.now_iso(), task_id),
            )
            return True
        payload = _loads(row["payload"])
        phases = payload.get("phases", [])
        index = int(payload.get("phase_index", 0))
        failed_phase = update.get("status") == "failed"
        if index < len(phases):
            phases[index]["status"] = "failed" if failed_phase else "done"
        next_index = index if failed_phase else index + 1
        status = update.get("status")
        if status is None:
            status = "completed" if next_index >= len(phases) else "running"
        phase_key = phases[next_index]["key"] if next_index < len(phases) else "done"
        next_run_at = self.clock.monotonic() + float(update.get("delay_s") or 0.0)
        result = _loads(row["result"])
        result.update(update.get("result") or {})
        payload["phase_index"] = next_index
        self._conn.execute(
            "UPDATE tasks SET status = ?, phase = ?, phase_index = ?, payload = ?, "
            "result = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
            (
                status,
                phase_key,
                next_index,
                _dumps(payload),
                _dumps(result),
                next_run_at,
                self.clock.now_iso(),
                task_id,
            ),
        )
        self.emit(
            "task.phase",
            {
                "task_id": task_id,
                "phase": phases[index]["key"] if index < len(phases) else "done",
                "status": status,
                "detail": update.get("detail", {}),
            },
        )
        if status in ("completed", "partial", "failed", "superseded", "waiting_input"):
            self.emit(
                "task.terminal" if status != "waiting_input" else "task.waiting_input",
                {"task_id": task_id, "status": status},
            )
        return True

    def set_task_status(self, task_id: str, status: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM tasks WHERE scenario_id = ? AND workspace_epoch = ? AND id = ?",
            (self.scope.scenario_id, self.scope.workspace_epoch, task_id),
        ).fetchone()
        if row is None:
            raise DemoError("not_found", "演示任务不存在或不属于当前案例。", status=404)
        self._conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, self.clock.now_iso(), task_id),
        )
        self.emit("task.status", {"task_id": task_id, "status": status})
        task = self.task_row(task_id)
        if task["kind"] == "feedback.review":
            from .feedback import sync_analysis_status
            sync_analysis_status(self, task, status)

    def add_file_row(
        self,
        *,
        file_id: str,
        rel_path: str,
        content_hash: str,
        media_type: str,
        size: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO files (id, rel_path, content_hash, media_type, size, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, rel_path, content_hash, media_type, size, self.clock.now_iso()),
        )

    def file_row(self, file_id: str) -> JsonObject:
        row = self._conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            raise DemoError("file_not_registered", "演示文件不存在。", status=404)
        return dict(row)

    def file_bytes(self, file_id: str, *, expected_hash: str | None = None) -> bytes:
        row = self.file_row(file_id)
        path = self._store.runtime_root / row["rel_path"]
        if not path.is_file():
            raise DemoError(
                "artifact_missing",
                "演示原件不可用，请重新加载样例或恢复备份。",
                status=404,
                recovery_actions=["reload_sample", "restore_backup"],
            )
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != row["content_hash"]:
            raise DemoError(
                "artifact_hash_mismatch",
                "演示原件内容与登记哈希不一致。",
                status=409,
                recovery_actions=["reload_sample"],
            )
        if expected_hash is not None and expected_hash != row["content_hash"]:
            raise DemoError(
                "artifact_hash_mismatch",
                "引用的原件哈希与文件记录不一致。",
                status=409,
                recovery_actions=["reload_sample"],
            )
        return data

    def latest_seq(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS last FROM events "
            "WHERE scenario_id = ? AND workspace_epoch = ?",
            (self.scope.scenario_id, self.scope.workspace_epoch),
        ).fetchone()
        return int(row["last"])

    def emit(self, event: str, payload: JsonObject | None = None) -> int:
        if not isinstance(event, str) or not event:
            raise DemoError("invalid_event", "事件名称不能为空。")
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS last FROM events "
            "WHERE scenario_id = ? AND workspace_epoch = ?",
            (self.scope.scenario_id, self.scope.workspace_epoch),
        ).fetchone()
        seq = int(row["last"]) + 1
        self._conn.execute(
            "INSERT INTO events (scenario_id, workspace_epoch, seq, event, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.scope.scenario_id,
                self.scope.workspace_epoch,
                seq,
                event,
                _dumps(payload or {}),
                self.clock.now_iso(),
            ),
        )
        return seq


def _task_from_row(row: sqlite3.Row) -> JsonObject:
    payload = _loads(row["payload"])
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "phase": row["phase"],
        "phase_index": row["phase_index"],
        "phases": payload.get("phases", []),
        "input": payload.get("input", {}),
        "parent_id": row["parent_id"],
        "result": _loads(row["result"]),
        "scenario_id": row["scenario_id"],
        "workspace_epoch": row["workspace_epoch"],
        "updated_at": row["updated_at"],
        "demo_only": True,
    }


def _workspace_from_row(row: sqlite3.Row) -> JsonObject:
    return {
        "scenario_id": row["scenario_id"],
        "workspace_epoch": row["workspace_epoch"],
        "revision": row["revision"],
        "context_id": row["context_id"],
        "current_applied_id": row["current_applied_id"],
        "mode": row["mode"],
        "script": row["script"],
        "updated_at": row["updated_at"],
    }


class DemoStore:
    def __init__(self, runtime_root: Path, *, clock: Clock | None = None):
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.runtime_root / "demo.sqlite3"
        self.clock: Clock = clock or RealClock()
        self.catalog: ScenarioCatalog | None = None
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self, catalog: ScenarioCatalog) -> None:
        self.catalog = catalog
        with self._lock, self._connection() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute("BEGIN IMMEDIATE")
            try:
                # A restart cannot continue an in-flight simulation: the worker
                # is gone. Preserve published stages and mark the rest for an
                # explicit "continue demo" task.
                conn.execute(
                    "UPDATE tasks SET status = 'interrupted' "
                    "WHERE status IN ('queued', 'running')"
                )
                for row in conn.execute("SELECT * FROM tasks WHERE kind = 'feedback.review' AND status = 'interrupted'").fetchall():
                    tx = Transaction(self, conn, Scope(row["scenario_id"], row["workspace_epoch"]))
                    from .feedback import sync_analysis_status
                    sync_analysis_status(tx, _task_from_row(row), "interrupted")
                for scenario in catalog.list():
                    scenario_id = scenario["scenario_id"]
                    row = conn.execute(
                        "SELECT 1 FROM workspaces WHERE scenario_id = ?", (scenario_id,)
                    ).fetchone()
                    if row is not None:
                        continue
                    now = self.clock.now_iso()
                    context = self._initial_context(scenario, now)
                    conn.execute(
                        "INSERT INTO entities "
                        "(scenario_id, workspace_epoch, kind, id, revision, data_origin, "
                        "payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            scenario_id,
                            1,
                            "ContextSnapshot",
                            context["id"],
                            1,
                            "fixture",
                            _dumps(context),
                            now,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO workspaces "
                        "(scenario_id, workspace_epoch, revision, context_id, "
                        "current_applied_id, mode, script, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (scenario_id, 1, 1, context["id"], None, "narrated", "none", now),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _initial_context(self, scenario: JsonObject, now: str) -> JsonObject:
        source = scenario["context"]
        payload = {
            key: value
            for key, value in source.items()
            if key not in ("schema", "demo_only", "scenario_id")
        }
        return {
            "id": f"demo_contextsnapshot_{uuid.uuid4().hex}",
            "kind": "ContextSnapshot",
            "scenario_id": scenario["scenario_id"],
            "workspace_epoch": 1,
            "created_at": now,
            "demo_only": True,
            "data_origin": "fixture",
            "revision": 1,
            **payload,
        }

    def workspace(self, scenario_id: str) -> JsonObject:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE scenario_id = ?", (scenario_id,)
            ).fetchone()
        if row is None:
            raise DemoError("unknown_scenario", "演示案例不存在。", status=404)
        return _workspace_from_row(row)

    def entity(self, scope: Scope, entity_id: str, *, kind: str | None = None) -> JsonObject:
        with self._connection() as conn:
            tx = Transaction(self, conn, scope)
            return tx.get(entity_id, kind=kind)

    def find(self, scope: Scope, kind: str, **equals: Any) -> list[JsonObject]:
        with self._connection() as conn:
            return Transaction(self, conn, scope).find(kind, **equals)

    def task(self, scope: Scope, task_id: str) -> JsonObject:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE scenario_id = ? AND workspace_epoch = ? AND id = ?",
                (scope.scenario_id, scope.workspace_epoch, task_id),
            ).fetchone()
        if row is None:
            raise DemoError("not_found", "演示任务不存在或不属于当前案例。", status=404)
        return _task_from_row(row)

    def tasks(self, scope: Scope) -> list[JsonObject]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE scenario_id = ? AND workspace_epoch = ? "
                "ORDER BY rowid",
                (scope.scenario_id, scope.workspace_epoch),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def history(self, scope: Scope, *, after: int = 0, limit: int = 20) -> JsonObject:
        if not is_positive_int(limit) or limit > 200:
            raise DemoError("invalid_limit", "分页大小必须在 1 到 200 之间。")
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise DemoError("invalid_cursor", "分页游标必须是非负整数。")
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT seq, event, payload, created_at FROM events "
                "WHERE scenario_id = ? AND workspace_epoch = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (scope.scenario_id, scope.workspace_epoch, after, limit),
            ).fetchall()
        events = [
            {
                "seq": row["seq"],
                "event": row["event"],
                "payload": _loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        next_cursor = events[-1]["seq"] if events else after
        return {"events": events, "next_cursor": next_cursor}

    def next_due_task(self, now: float) -> tuple[Scope, JsonObject] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT t.* FROM tasks t JOIN workspaces w ON w.scenario_id = t.scenario_id "
                "WHERE t.workspace_epoch = w.workspace_epoch "
                "AND t.status IN ('queued', 'running') "
                "AND t.next_run_at <= ? AND w.mode != 'step' "
                "ORDER BY t.next_run_at, t.rowid LIMIT 1",
                (now,),
            ).fetchone()
        if row is None:
            return None
        return Scope(row["scenario_id"], row["workspace_epoch"]), _task_from_row(row)

    def advance_task(
        self,
        scope: Scope,
        task_id: str,
        expected_phase: int,
        publish: Callable[[Transaction, JsonObject], JsonObject | None],
    ) -> bool:
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                workspace = conn.execute(
                    "SELECT * FROM workspaces WHERE scenario_id = ?", (scope.scenario_id,)
                ).fetchone()
                if workspace is None or workspace["workspace_epoch"] != scope.workspace_epoch:
                    conn.execute("ROLLBACK")
                    return False
                tx = Transaction(self, conn, scope)
                advanced = tx.advance_task(task_id, expected_phase, publish)
                conn.execute("COMMIT" if advanced else "ROLLBACK")
                return advanced
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mutate(
        self, command: Command, handler: Callable[[Transaction], DomainResult]
    ) -> Reply:
        validate_command(command)
        if self.catalog is None:
            raise DemoError("not_initialized", "演示存储尚未初始化。", status=500)
        try:
            self.catalog.get(command.scenario_id)
        except Exception as exc:
            raise DemoError("unknown_scenario", "演示案例不存在。", status=404) from exc
        request_hash = canonical_hash(command)
        scope = Scope(command.scenario_id, command.workspace_epoch)
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                workspace = Transaction(self, conn, scope).workspace()
                if workspace["workspace_epoch"] != command.workspace_epoch:
                    raise DemoError(
                        "stale_epoch",
                        "该工作区已重置，旧页面请求不再生效。",
                        status=409,
                        recovery_actions=["refresh"],
                    )
                replay = conn.execute(
                    "SELECT request_hash, status, response FROM mutations "
                    "WHERE scenario_id = ? AND workspace_epoch = ? AND request_id = ?",
                    (command.scenario_id, command.workspace_epoch, command.request_id),
                ).fetchone()
                if replay is not None:
                    if replay["request_hash"] != request_hash:
                        raise DemoError(
                            "request_id_conflict",
                            "同一操作键已用于不同内容，请刷新后重新确认。",
                            status=409,
                            recovery_actions=["refresh", "new_request_id"],
                        )
                    conn.execute("COMMIT")
                    return Reply(replay["status"], _loads(replay["response"]))
                if workspace["revision"] != command.expected_revision:
                    raise DemoError(
                        "revision_conflict",
                        "工作区已发生变化，请刷新后重新确认。",
                        status=409,
                        field_errors=[
                            {
                                "field": "expected_revision",
                                "expected": workspace["revision"],
                                "received": command.expected_revision,
                            }
                        ],
                        recovery_actions=["refresh"],
                    )
                tx = Transaction(self, conn, scope)
                result = handler(tx)
                current = tx.workspace()
                if current["workspace_epoch"] != command.workspace_epoch:
                    # A demo reset intentionally starts a fresh epoch from this
                    # command; do not bump the new epoch's revision again.
                    new_epoch = current["workspace_epoch"]
                    new_revision = current["revision"]
                else:
                    new_epoch = command.workspace_epoch
                    new_revision = workspace["revision"] + 1
                    conn.execute(
                        "UPDATE workspaces SET revision = ?, updated_at = ? "
                        "WHERE scenario_id = ?",
                        (new_revision, self.clock.now_iso(), command.scenario_id),
                    )
                body = {
                    "demo_only": True,
                    "request_id": command.request_id,
                    "scenario_id": command.scenario_id,
                    "workspace_epoch": new_epoch,
                    "workspace_revision": new_revision,
                    "refs": result.refs,
                    "task_id": result.task_id,
                    "data": result.data,
                }
                conn.execute(
                    "INSERT INTO mutations "
                    "(scenario_id, workspace_epoch, request_id, request_hash, action, "
                    "status, response, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        command.scenario_id,
                        command.workspace_epoch,
                        command.request_id,
                        request_hash,
                        command.action,
                        result.status,
                        _dumps(body),
                        self.clock.now_iso(),
                    ),
                )
                conn.execute("COMMIT")
                return Reply(result.status, body)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def bump_epoch(self, scenario_id: str) -> JsonObject:
        """Reset primitive: a fresh epoch with a fresh initial context."""
        if self.catalog is None:
            raise DemoError("not_initialized", "演示存储尚未初始化。", status=500)
        scenario = self.catalog.get(scenario_id)
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM workspaces WHERE scenario_id = ?", (scenario_id,)
                ).fetchone()
                if row is None:
                    raise DemoError("unknown_scenario", "演示案例不存在。", status=404)
                epoch = row["workspace_epoch"] + 1
                now = self.clock.now_iso()
                context = self._initial_context(scenario, now)
                context["workspace_epoch"] = epoch
                conn.execute(
                    "INSERT INTO entities "
                    "(scenario_id, workspace_epoch, kind, id, revision, data_origin, "
                    "payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        scenario_id,
                        epoch,
                        "ContextSnapshot",
                        context["id"],
                        1,
                        "fixture",
                        _dumps(context),
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE workspaces SET workspace_epoch = ?, revision = 1, context_id = ?, "
                    "current_applied_id = NULL, updated_at = ? WHERE scenario_id = ?",
                    (epoch, context["id"], now, scenario_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.workspace(scenario_id)
