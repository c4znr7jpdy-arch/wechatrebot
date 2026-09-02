from __future__ import annotations

import argparse
import json
from pathlib import Path

from .bootstrap import run_bootstrap
from .diagnostics import collect_diagnostics
from .desktop import run_desktop
from .install_paths import INSTALL_MARKER, prepare_install_layout, validate_install_path
from .json_io import write_json_atomic
from .launcher import ServiceLaunchError, run_services_foreground
from .payload import PayloadError, install_payload, verify_payload
from .plugin_center import PluginCenterError, run_plugin_center
from .runtime_config import write_runtime_configs
from .self_test import manager_self_test
from .wechat_path import discover_wechat_executables, validate_wechat_executable


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wechatrebot-manager")
    subparsers = parser.add_subparsers(dest="command")
    gui = subparsers.add_parser("gui", help="打开安装向导或机器人管理器")
    gui.add_argument("--payload", type=Path)
    gui.add_argument("--install-root", type=Path)
    gui.add_argument("--allow-incomplete-payload", action="store_true")

    self_test = subparsers.add_parser("self-test", help=argparse.SUPPRESS)
    self_test.add_argument("--output", type=Path, required=True)
    bootstrap = subparsers.add_parser("bootstrap", help="运行 Windows 首次配置向导")
    bootstrap.add_argument("--payload", type=Path, help="待校验并展开的商业载荷目录")
    bootstrap.add_argument(
        "--allow-incomplete-payload",
        action="store_true",
        help="仅开发时允许安装未完成载荷",
    )

    install = subparsers.add_parser("check-install-dir", help="检查安装目录")
    install.add_argument("path")

    subparsers.add_parser("discover-wechat", help="发现本机微信主程序")

    wechat = subparsers.add_parser("check-wechat", help="检查微信主程序")
    wechat.add_argument("path")

    verify = subparsers.add_parser("verify-payload", help="验证商业载荷哈希和边界")
    verify.add_argument("payload", type=Path)
    verify.add_argument("--allow-incomplete", action="store_true")

    install_payload_parser = subparsers.add_parser(
        "install-payload", help="验证并展开商业载荷"
    )
    install_payload_parser.add_argument("payload", type=Path)
    install_payload_parser.add_argument("install_root", type=Path)
    install_payload_parser.add_argument("--allow-incomplete", action="store_true")

    render = subparsers.add_parser("render-config", help="生成微信连接与控制台配置")
    render.add_argument("install_root", type=Path)
    render.add_argument("wechat_exe", type=Path)
    render.add_argument("--wechat-version")

    diagnose = subparsers.add_parser("diagnose", help="生成脱敏安装诊断")
    diagnose.add_argument("install_root", type=Path)

    start = subparsers.add_parser("start", help="按顺序启动回复引擎和微信连接")
    start.add_argument("install_root", type=Path)
    plugin_center = subparsers.add_parser("plugin-center", help=argparse.SUPPRESS)
    plugin_center.add_argument("install_root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command is None:
        try:
            return run_desktop()
        except (FileNotFoundError, OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "gui":
        try:
            return run_desktop(
                payload_root=args.payload,
                install_root=args.install_root,
                allow_incomplete_payload=args.allow_incomplete_payload,
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "self-test":
        manager_self_test(args.output)
        return 0
    if args.command == "bootstrap":
        return run_bootstrap(
            args.payload,
            allow_incomplete_payload=args.allow_incomplete_payload,
        )
    if args.command == "check-install-dir":
        result = validate_install_path(Path(args.path))
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "path": str(result.path),
                    "errors": result.errors,
                    "warnings": result.warnings,
                    "available_bytes": result.available_bytes,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result.ok else 1
    if args.command == "discover-wechat":
        items = discover_wechat_executables()
        print(
            json.dumps(
                [
                    {
                        "path": str(item.path),
                        "version": item.version,
                        "supported": item.supported,
                        "errors": item.errors,
                    }
                    for item in items
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command in {"verify-payload", "install-payload"}:
        try:
            if args.command == "verify-payload":
                verified = verify_payload(
                    args.payload, allow_incomplete=args.allow_incomplete
                )
            else:
                validation = validate_install_path(args.install_root)
                if not validation.ok:
                    raise PayloadError("；".join(validation.errors))
                prepare_install_layout(validation.path)
                write_json_atomic(
                    validation.path / INSTALL_MARKER,
                    {"schema_version": 1, "state": "installing"},
                )
                verified = install_payload(
                    args.payload,
                    validation.path,
                    allow_incomplete=args.allow_incomplete,
                )
                write_json_atomic(
                    validation.path / INSTALL_MARKER,
                    {"schema_version": 1, "state": "payload-installed"},
                )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "file_count": len(verified.files),
                        "source_manifest_status": verified.manifest.get(
                            "source_manifest_status"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        except (OSError, PayloadError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "render-config":
        write_runtime_configs(
            args.install_root,
            args.wechat_exe,
            wechat_version=args.wechat_version,
        )
        print(json.dumps({"ok": True}, ensure_ascii=False))
        return 0
    if args.command == "diagnose":
        report = collect_diagnostics(args.install_root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if args.command == "start":
        try:
            return run_services_foreground(args.install_root)
        except (OSError, ServiceLaunchError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
    if args.command == "plugin-center":
        try:
            return run_plugin_center(args.install_root)
        except (OSError, ValueError, PluginCenterError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1

    item = validate_wechat_executable(args.path)
    print(
        json.dumps(
            {
                "path": str(item.path),
                "version": item.version,
                "supported": item.supported,
                "errors": item.errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if item.supported else 1


if __name__ == "__main__":
    raise SystemExit(main())
