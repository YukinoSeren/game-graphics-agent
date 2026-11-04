"""Entry point for the offline demo server. Loopback only."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEMO_ROOT = Path(__file__).resolve().parent


def _bootstrap_import_path() -> None:
    if __package__ in (None, ""):
        parent = str(DEMO_ROOT.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)


_bootstrap_import_path()

from demo.app.http import create_http_server  # noqa: E402
from demo.app.service import create_service  # noqa: E402

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
RUNTIME_ROOT = (DEMO_ROOT / "runtime").resolve()


def _resolve_runtime_root(data_dir: str | None) -> Path | None:
    """Only the demo/runtime tree may hold demo runtime artifacts."""
    if data_dir is None:
        return RUNTIME_ROOT
    candidate = Path(data_dir).expanduser().resolve()
    if candidate != RUNTIME_ROOT:
        try:
            candidate.relative_to(RUNTIME_ROOT)
        except ValueError:
            return None
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="临时演示服务（仅本机、离线）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--host", default="127.0.0.1", help="仅允许回环地址")
    parser.add_argument("--data-dir", default=None, help="演示运行数据目录")
    args = parser.parse_args(argv)

    if args.host not in LOOPBACK_HOSTS:
        print("演示服务只允许绑定回环地址（127.0.0.1）。", file=sys.stderr)
        return 2
    if not (1 <= args.port <= 65535):
        print("端口必须在 1 到 65535 之间。", file=sys.stderr)
        return 2

    runtime_root = _resolve_runtime_root(args.data_dir)
    if runtime_root is None:
        print(
            "演示运行目录必须位于 demo/runtime/ 内；拒绝在其他位置写入演示数据。",
            file=sys.stderr,
        )
        return 2
    fixture_root = DEMO_ROOT / "fixtures"
    try:
        service = create_service(runtime_root, fixture_root=fixture_root, start_worker=True)
    except Exception as exc:
        print(f"演示资源加载失败：{exc}", file=sys.stderr)
        return 3
    try:
        server = create_http_server(service, host=args.host, port=args.port)
    except OSError as exc:
        print(
            f"无法监听 {args.host}:{args.port}（{exc}）。"
            "端口可能已被占用，请使用 --port 指定其他端口；不会结束已有进程。",
            file=sys.stderr,
        )
        service.close()
        return 4

    host, port = server.server_address[:2]
    print(f"演示服务已启动：http://{host}:{port}/")
    print("仅用于演示；数据写入 demo/runtime/。按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止演示服务……")
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
