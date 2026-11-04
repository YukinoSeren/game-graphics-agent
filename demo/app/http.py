"""Local HTTP surface for the temporary demo (loopback only)."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .contracts import DemoError, Scope
from .files import ALLOWED_MEDIA_TYPES, MAX_UPLOAD_BYTES

API_PREFIX = "/api/demo/v1"
MAX_JSON_BYTES = 1024 * 1024
MAX_MULTIPART_OVERHEAD = 64 * 1024
SERVICE_VERSION = "demo/1.3"
API_VERSION = "demo-api/1"
UI_CONTRACT = "conversation/2"
_HEADER_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\]')


def _header_text(value: Any, fallback: str) -> str:
    """Keep a payload value safe inside a quoted response header."""
    text = _HEADER_UNSAFE.sub("_", str(value if value is not None else "")).strip()
    return text[:120] or fallback

PAGES = [
    {"id": "conversation", "label": "与助手对话"},
    {"id": "library", "label": "我的游戏"},
    {"id": "history", "label": "历史记录"},
]
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _reject_constant(value: str) -> None:
    raise DemoError("invalid_json", "请求包含非有限数值。", status=400)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


class DemoRequestHandler(BaseHTTPRequestHandler):
    service = None
    static_root: Path
    server_version = "demo-server/1.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Demo-Only", "true")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = _json_bytes({**payload, "demo_only": True})
        self._send(status, body, "application/json; charset=utf-8")

    def _send_error(self, error: DemoError) -> None:
        self._send_json(error.status, error.to_body())

    def _read_body(self, limit: int) -> bytes:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise DemoError("length_required", "请求必须携带 Content-Length。", status=411)
        try:
            length = int(length_header)
        except ValueError as exc:
            raise DemoError("invalid_length", "Content-Length 无效。") from exc
        if length < 0:
            raise DemoError("invalid_length", "Content-Length 无效。")
        if length > limit:
            self._drain(min(length, 8 * 1024 * 1024))
            self._body_consumed = True
            raise DemoError("request_too_large", "请求超过大小上限。", status=413)
        body = self.rfile.read(length)
        self._body_consumed = True
        return body

    def _drain_remaining(self) -> None:
        if getattr(self, "_body_consumed", False):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            self._drain(min(length, 8 * 1024 * 1024))
        self._body_consumed = True

    def _drain(self, size: int) -> None:
        """Consume a bounded oversized body so the client can read the 413."""
        remaining = size
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _same_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin is None:
            return
        host = self.headers.get("Host")
        expected = {f"http://{host}", f"https://{host}"} if host else set()
        if origin not in expected:
            raise DemoError("cross_origin", "服务只接受同源请求。", status=403)

    def _scope(self, query: dict[str, list[str]]) -> Scope:
        scenario_id = (query.get("scenario_id") or [""])[0]
        epoch_text = (query.get("workspace_epoch") or [""])[0]
        if not scenario_id:
            raise DemoError("missing_field", "缺少 scenario_id。", field_errors=[{"field": "scenario_id", "error": "required"}])
        try:
            epoch = int(epoch_text)
        except ValueError as exc:
            raise DemoError("invalid_epoch", "workspace_epoch 必须是整数。") from exc
        return Scope(scenario_id, epoch)

    # -- GET ----------------------------------------------------------
    def do_GET(self) -> None:
        try:
            split = urlsplit(self.path)
            path = unquote(split.path)
            query = parse_qs(split.query)
            if path.startswith(API_PREFIX):
                self._api_get(path[len(API_PREFIX):], query)
            else:
                self._static_get(path)
        except DemoError as error:
            self._send_error(error)
        except Exception:
            self._send_error(DemoError("internal_error", "暂时无法完成读取。", status=500))

    def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
        service = self.service
        if path in ("", "/health"):
            self._send_json(200, {
                "service": SERVICE_VERSION,
                "api": API_VERSION,
                "ui_contract": UI_CONTRACT,
                "capabilities": service.registered_actions(),
                "status": "ok",
                "fixtures_ready": True,
            })
            return
        if path == "/bootstrap":
            scenarios = []
            for scenario in service.catalog.list():
                scenarios.append({"scenario_id": scenario["scenario_id"]})
            self._send_json(200, {
                "api": API_VERSION,
                "service": SERVICE_VERSION,
                "ui_contract": UI_CONTRACT,
                "capabilities": service.registered_actions(),
                "scenarios": scenarios,
                "pages": PAGES,
            })
            return
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[0] == "workspaces":
            workspace = service.workspace(parts[1])
            self._send_json(200, {"workspace": workspace})
            return
        if len(parts) == 2 and parts[0] == "catalogs":
            service.require_active_scenario(parts[1])
            scenario = service.catalog.get(parts[1])
            public = {key: value for key, value in scenario.items() if not key.startswith("_")}
            self._send_json(200, {"catalog": public})
            return
        if len(parts) == 1 and parts[0] == "entities":
            scope = self._scope(query)
            kind = (query.get("kind") or [None])[0]
            if not kind:
                raise DemoError("invalid_kind", "列举实体必须指定 kind。")
            self._send_json(200, {"entities": service.find(scope, kind)})
            return
        if len(parts) == 2 and parts[0] in ("entities", "tasks"):
            scope = self._scope(query)
            if parts[0] == "entities":
                kind = (query.get("kind") or [None])[0]
                entity = service.entity(scope, parts[1], kind=kind)
                self._send_json(200, {"entity": entity})
            else:
                self._send_json(200, {"task": service.task(scope, parts[1])})
            return
        if path == "/history":
            scope = self._scope(query)
            after = int((query.get("after") or ["0"])[0])
            limit = int((query.get("limit") or ["20"])[0])
            self._send_json(200, service.history(scope, after=after, limit=limit))
            return
        if path == "/recent-play-logs":
            from .play_logs import list_recent
            scope = self._scope(query)
            workspace = service.workspace(scope.scenario_id)
            if workspace["workspace_epoch"] != scope.workspace_epoch:
                raise DemoError("stale_workspace", "记录已更新，请刷新后重试。", status=409)
            self._send_json(200, {"logs": list_recent(service)})
            return
        if len(parts) == 2 and parts[0] == "files":
            scope = self._scope(query)
            record = service.entity(scope, parts[1], kind="File")
            data = service.files.read(parts[1], expected_hash=record.get("content_hash"))
            media_type = record.get("media_type")
            if media_type not in ALLOWED_MEDIA_TYPES:
                media_type = "application/octet-stream"
            filename = _header_text(record.get("filename_label"), "demo-file")
            disposition = f'attachment; filename="{filename}"'
            self._send(200, data, media_type, {"Content-Disposition": disposition})
            return
        raise DemoError("not_found", "请求的内容不存在。", status=404)

    def _static_get(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        static_root = self.static_root.resolve()
        candidate = (static_root / relative).resolve()
        if not candidate.is_relative_to(static_root):
            raise DemoError("not_found", "资源不存在。", status=404)
        if not candidate.is_file():
            raise DemoError("not_found", "资源不存在。", status=404)
        if candidate.suffix.lower() not in STATIC_TYPES:
            raise DemoError("not_found", "资源类型不受支持。", status=404)
        self._send(200, candidate.read_bytes(), STATIC_TYPES[candidate.suffix.lower()])

    # -- POST ---------------------------------------------------------
    def do_POST(self) -> None:
        self._body_consumed = False
        try:
            self._same_origin()
            split = urlsplit(self.path)
            path = split.path
            if path == f"{API_PREFIX}/commands":
                self._post_command()
            elif path == f"{API_PREFIX}/uploads":
                self._post_upload()
            else:
                raise DemoError("not_found", "请求的内容不存在。", status=404)
        except DemoError as error:
            self._drain_remaining()
            self._send_error(error)
        except Exception:
            self._drain_remaining()
            self._send_error(DemoError("internal_error", "暂时无法完成该操作。", status=500))

    def _post_command(self) -> None:
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/json" not in content_type:
            raise DemoError("unsupported_media_type", "命令请求必须是 application/json。")
        raw = self._read_body(MAX_JSON_BYTES)
        try:
            document = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DemoError("invalid_json", "请求体不是有效 JSON。") from exc
        if not isinstance(document, dict):
            raise DemoError("invalid_json", "请求体必须是 JSON 对象。")
        from .contracts import Command

        required = ("request_id", "scenario_id", "workspace_epoch", "expected_revision", "action", "payload")
        missing = [key for key in required if key not in document]
        if missing:
            raise DemoError(
                "missing_field",
                f"缺少字段：{', '.join(missing)}。",
                field_errors=[{"field": key, "error": "required"} for key in missing],
            )
        command = Command(
            request_id=document["request_id"],
            scenario_id=document["scenario_id"],
            workspace_epoch=document['workspace_epoch'],
            expected_revision=document["expected_revision"],
            action=document["action"],
            payload=document["payload"],
        )
        reply = self.service.command(command)
        self._send_json(reply.status, reply.body)

    def _post_upload(self) -> None:
        content_type = self.headers.get("Content-Type") or ""
        if not content_type.lower().startswith("multipart/form-data"):
            raise DemoError("unsupported_media_type", "上传必须使用 multipart/form-data。")
        raw = self._read_body(MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD)
        message = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + raw
        )
        if not message.is_multipart():
            raise DemoError("invalid_upload", "上传内容缺少 multipart 边界。")
        command_field = None
        file_field = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "command" and command_field is None:
                command_field = part.get_payload(decode=True) or b""
            elif name == "file" and file_field is None:
                file_field = part
            elif name in ("command", "file"):
                raise DemoError("invalid_upload", "上传只能包含一个 command 和一个 file。")
        if command_field is None or file_field is None:
            raise DemoError("invalid_upload", "上传必须包含 command 与 file 字段。")
        try:
            document = json.loads(command_field.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DemoError("invalid_command", "上传命令不是有效 JSON。") from exc
        if not isinstance(document, dict):
            raise DemoError("invalid_command", "上传命令必须是 JSON 对象。")
        from .contracts import Command

        required = ("request_id", "scenario_id", "workspace_epoch", "expected_revision", "action", "payload")
        if any(key not in document for key in required):
            raise DemoError("missing_field", "上传命令字段不完整。")
        data = file_field.get_payload(decode=True) or b""
        filename = file_field.get_filename() or "upload"
        media_type = file_field.get_content_type() or "application/octet-stream"
        command = Command(
            request_id=document["request_id"],
            scenario_id=document["scenario_id"],
            workspace_epoch=document['workspace_epoch'],
            expected_revision=document["expected_revision"],
            action=document["action"],
            payload=document["payload"],
        )
        reply = self.service.upload(command, data=data, media_type=media_type, filename=filename)
        self._send_json(reply.status, reply.body)


def create_http_server(service, *, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    static_root = Path(service.catalog.root).parent / "static"
    handler = type(
        "BoundDemoRequestHandler",
        (DemoRequestHandler,),
        {"service": service, "static_root": static_root},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
