"""Demo service assembly: explicit action registry and scoped queries."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from .contracts import (
    Clock,
    Command,
    DemoError,
    DomainResult,
    JsonObject,
    Reply,
    Scope,
)
from .files import ManagedFiles, MAX_UPLOAD_BYTES, validate_filename
from .scenarios import ScenarioCatalog
from .store import DemoStore, validate_command

Validator = Callable[[JsonObject], JsonObject | None]
Handler = Callable[["ActionContext", JsonObject], DomainResult]


class ActionContext:
    """Read-only domain context for one command, bound to one transaction."""

    def __init__(
        self,
        tx,
        command: Command,
        catalog: ScenarioCatalog,
        files: ManagedFiles,
        clock: Clock,
        service: "DemoService | None" = None,
    ):
        self.tx = tx
        self.command = command
        self.catalog = catalog
        self.files = files
        self.clock = clock
        self.service = service

    @property
    def scenario(self) -> JsonObject:
        return self.catalog.get(self.command.scenario_id)

    @property
    def scenario_id(self) -> str:
        return self.command.scenario_id

    def setting(self, setting_id: str) -> JsonObject:
        return self.catalog.setting(self.scenario_id, setting_id)

    def source(self, source_id: str) -> JsonObject:
        return self.catalog.source(self.scenario_id, source_id)

    def recipe(self, label: str) -> JsonObject:
        return self.catalog.recipe(self.scenario_id, label)


def require_fields(
    payload: JsonObject,
    *,
    required: tuple[str, ...],
    allowed: tuple[str, ...] | None = None,
) -> JsonObject:
    if not isinstance(payload, dict):
        raise DemoError("invalid_payload", "payload 必须是 JSON 对象。")
    allowed_fields = set(allowed if allowed is not None else required)
    unknown = set(payload) - allowed_fields
    if unknown:
        raise DemoError(
            "unknown_field",
            f"请求包含未定义的字段：{', '.join(sorted(unknown))}。",
            field_errors=[{"field": key, "error": "unknown"} for key in sorted(unknown)],
        )
    missing = [key for key in required if key not in payload]
    if missing:
        raise DemoError(
            "missing_field",
            f"请求缺少必填字段：{', '.join(missing)}。",
            field_errors=[{"field": key, "error": "required"} for key in missing],
        )
    return payload


class DemoService:
    def __init__(
        self,
        *,
        runtime_root: Path,
        catalog: ScenarioCatalog,
        store: DemoStore,
        files: ManagedFiles,
        clock: Clock,
    ):
        self.runtime_root = Path(runtime_root)
        self.catalog = catalog
        self.store = store
        self.files = files
        self.clock = clock
        self._actions: dict[str, tuple[Validator, Handler]] = {}
        from .tasks import TaskRunner

        self.tasks = TaskRunner(self)

    def register(self, action: str, validate: Validator, handle: Handler) -> None:
        if action in self._actions:
            raise RuntimeError(f"duplicate demo action registration: {action}")
        self._actions[action] = (validate, handle)

    def registered_actions(self) -> list[str]:
        return sorted(self._actions)

    def require_active_scenario(self, scenario_id: str) -> None:
        if scenario_id not in {item["scenario_id"] for item in self.catalog.list()}:
            raise DemoError("unsupported_game", "当前暂不支持这款游戏。", status=404)

    def command(self, command: Command) -> Reply:
        validate_command(command)
        self.require_active_scenario(command.scenario_id)
        entry = self._actions.get(command.action)
        if entry is None:
            raise DemoError(
                "unknown_action",
                "当前暂不支持该操作。",
                field_errors=[{"field": "action", "error": "unknown", "value": command.action}],
            )
        validate, handle = entry
        payload = validate(command.payload) or command.payload
        if not isinstance(payload, dict):
            raise DemoError("invalid_payload", "payload 必须是 JSON 对象。")

        def run(tx) -> DomainResult:
            context = ActionContext(tx, command, self.catalog, self.files, self.clock, self)
            return handle(context, payload)

        reply = self.store.mutate(command, run)
        cleanup = (reply.body.get("data") or {}).pop("_cleanup_file_ids", None)
        if cleanup:
            self.files.delete_registered(list(cleanup))
        return reply

    def upload(
        self,
        command: Command,
        *,
        data: bytes,
        media_type: str,
        filename: str,
    ) -> Reply:
        validate_command(command)
        self.require_active_scenario(command.scenario_id)
        if command.action != "file.upload":
            raise DemoError("invalid_action", "上传入口只接受 file.upload。")
        require_fields(
            command.payload,
            required=("media_type", "filename", "size", "content_hash"),
            allowed=("media_type", "filename", "size", "content_hash"),
        )
        if not isinstance(data, (bytes, bytearray)):
            raise DemoError("invalid_file", "上传内容必须是二进制数据。")
        data = bytes(data)
        if command.payload["media_type"] != media_type:
            raise DemoError("media_type_mismatch", "上传媒体类型与命令声明不一致。")
        validate_filename(filename)
        if command.payload["filename"] != filename:
            raise DemoError("filename_mismatch", "上传文件名与命令声明不一致。")
        if command.payload["size"] != len(data):
            raise DemoError("size_mismatch", "上传字节数与命令声明不一致。")
        if len(data) > MAX_UPLOAD_BYTES:
            raise DemoError("file_too_large", "单个文件不能超过 5 MiB。", status=413)
        digest = hashlib.sha256(data).hexdigest()
        if command.payload["content_hash"] != digest:
            raise DemoError("hash_mismatch", "上传内容哈希与命令声明不一致。")
        staged = self.files.stage(data, media_type)
        used = False

        def run(tx) -> DomainResult:
            nonlocal used
            record = self.files.register(tx, staged, filename)
            used = True
            return DomainResult(201, [{"kind": "File", "id": record["id"]}])

        try:
            reply = self.store.mutate(command, run)
        except Exception:
            self.files.discard(staged)
            raise
        if not used:
            # An idempotent replay returns the original record without running
            # the handler; the staged copy must not be left behind.
            self.files.discard(staged)
        return reply

    def workspace(self, scenario_id: str) -> JsonObject:
        self.require_active_scenario(scenario_id)
        workspace = self.store.workspace(scenario_id)
        workspace["demo_only"] = True
        return workspace

    def entity(self, scope: Scope, entity_id: str, *, kind: str | None = None) -> JsonObject:
        self.require_active_scenario(scope.scenario_id)
        return self.store.entity(scope, entity_id, kind=kind)

    def find(self, scope: Scope, kind: str) -> list[JsonObject]:
        self.require_active_scenario(scope.scenario_id)
        return self.store.find(scope, kind)

    def task(self, scope: Scope, task_id: str) -> JsonObject:
        self.require_active_scenario(scope.scenario_id)
        return self.store.task(scope, task_id)

    def history(self, scope: Scope, *, after: int = 0, limit: int = 20) -> JsonObject:
        self.require_active_scenario(scope.scenario_id)
        return self.store.history(scope, after=after, limit=limit)

    def close(self) -> None:
        self.tasks.stop()


def create_service(
    runtime_root: Path,
    *,
    fixture_root: Path,
    clock: Clock | None = None,
    start_worker: bool = False,
) -> DemoService:
    from .actions import register_builtin_actions
    from .contracts import RealClock

    resolved_clock = clock or RealClock()
    catalog = ScenarioCatalog.load(Path(fixture_root))
    store = DemoStore(Path(runtime_root), clock=resolved_clock)
    store.initialize(catalog)
    files = ManagedFiles(Path(runtime_root), db_path=store.db_path)
    service = DemoService(
        runtime_root=Path(runtime_root),
        catalog=catalog,
        store=store,
        files=files,
        clock=resolved_clock,
    )
    register_builtin_actions(service)
    if start_worker:
        service.tasks.start()
    return service
