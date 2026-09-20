from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .bootstrap import run_bootstrap
from .diagnostics import collect_diagnostics
from .desktop import run_desktop
from .install_paths import INSTALL_MARKER, prepare_install_layout, validate_install_path
from .installed_entries import is_start_entrypoint, launch_installed_manager
from .json_io import write_json_atomic
from .launcher import ServiceLaunchError, run_services_foreground
from .payload import (
    PayloadError,
    install_payload,
    payload_relative_paths,
    verify_payload,
)
from .plugin_center import PluginCenterError, run_plugin_center
from .runtime_config import write_runtime_configs
from .self_test import manager_self_test
from .startup_errors import report_startup_error
from .updates import apply_update_plan
from .uninstaller import (
    apply_uninstall_plan,
    is_uninstall_entrypoint,
    run_uninstaller,
)
from .wechat_path import discover_wechat_executables, validate_wechat_executable


def _print_json(payload: object, *, indent: int | None = None) -> None:
    """Write JSON without crashing on the Windows GBK console."""
    stream = sys.stdout
    if stream is None:
        return
    rendered = json.dumps(payload, ensure_ascii=False, indent=indent)
    try:
        stream.write(rendered + "\n")
    except UnicodeEncodeError:
        stream.write(json.dumps(payload, ensure_ascii=True, indent=indent) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wechatrebot-manager")
    subparsers = parser.add_subparsers(dest="command")
    gui = subparsers.add_parser("gui", help="打开安装向导或机器人管理器")
    gui.add_argument("--payload", type=Path)
    gui.add_argument("--install-root", type=Path)

    self_test = subparsers.add_parser("self-test", help=argparse.SUPPRESS)
    self_test.add_argument("--output", type=Path, required=True)
    bootstrap = subparsers.add_parser("bootstrap", help="运行 Windows 首次配置向导")
    bootstrap.add_argument("--payload", type=Path, help="待校验并展开的商业载荷目录")

    install = subparsers.add_parser("check-install-dir", help="检查安装目录")
    install.add_argument("path")

    subparsers.add_parser("discover-wechat", help="发现本机微信主程序")

    wechat = subparsers.add_parser("check-wechat", help="检查微信主程序")
    wechat.add_argument("path")

    verify = subparsers.add_parser("verify-payload", help="验证商业载荷哈希和边界")
    verify.add_argument("payload", type=Path)

    install_payload_parser = subparsers.add_parser(
        "install-payload", help="验证并展开商业载荷"
    )
    install_payload_parser.add_argument("payload", type=Path)
    install_payload_parser.add_argument("install_root", type=Path)

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
    plugin_center.add_argument("feature_id", nargs="?")
    apply_update = subparsers.add_parser("apply-update", help=argparse.SUPPRESS)
    apply_update.add_argument("--plan", type=Path, required=True)
    uninstall = subparsers.add_parser("uninstall", help="彻底卸载软件并保留聊天记录")
    uninstall.add_argument("--install-root", type=Path, required=True)
    apply_uninstall = subparsers.add_parser(
        "apply-uninstall", help=argparse.SUPPRESS
    )
    apply_uninstall.add_argument("--plan", type=Path, required=True)
    native_install = subparsers.add_parser(
        "install-from-native-setup", help=argparse.SUPPRESS
    )
    native_install.add_argument("--payload", type=Path, required=True)
    native_install.add_argument("--install-root", type=Path, required=True)
    native_install.add_argument("--status-file", type=Path)
    native_uninstall = subparsers.add_parser(
        "prepare-native-uninstall", help=argparse.SUPPRESS
    )
    native_uninstall.add_argument("--install-root", type=Path, required=True)
    native_uninstall.add_argument("--status-file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values and is_uninstall_entrypoint():
        # Uninstall also supports failed/unfinished installs. Its own validator
        # reports invalid markers in a visible dialog instead of a hidden crash.
        return run_uninstaller(Path(sys.executable).resolve().parent)
    if not values and is_start_entrypoint():
        try:
            from .desktop import install_root_from_executable

            root = install_root_from_executable()
            if root is None:
                raise FileNotFoundError("未找到机器人安装目录，请重新安装软件。")
            launch_installed_manager(root)
            return 0
        except Exception as exc:
            report_startup_error(exc)
            return 1
    args = _parser().parse_args(values)
    if args.command is None:
        try:
            return run_desktop()
        except Exception as exc:
            report_startup_error(exc)
            return 1
    if args.command == "gui":
        try:
            return run_desktop(
                payload_root=args.payload,
                install_root=args.install_root,
            )
        except Exception as exc:
            report_startup_error(exc)
            return 1
    if args.command == "self-test":
        manager_self_test(args.output)
        return 0
    if args.command == "apply-update":
        return apply_update_plan(args.plan)
    if args.command == "uninstall":
        return run_uninstaller(args.install_root)
    if args.command == "apply-uninstall":
        return apply_uninstall_plan(args.plan)
    if args.command in {"install-from-native-setup", "prepare-native-uninstall"}:
        from .native_setup import install_from_native_setup, prepare_native_uninstall

        try:
            if args.command == "install-from-native-setup":
                result = install_from_native_setup(args.payload, args.install_root)
            else:
                result = prepare_native_uninstall(args.install_root)
            exit_code = 0
        except (OSError, RuntimeError, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
            exit_code = 1
        _print_json(result, indent=2)
        if args.status_file is not None:
            try:
                write_json_atomic(args.status_file, result)
            except OSError:
                pass
        return exit_code
    if args.command == "bootstrap":
        return run_bootstrap(args.payload)
    if args.command == "check-install-dir":
        result = validate_install_path(Path(args.path))
        _print_json(
            {
                "ok": result.ok,
                "path": str(result.path),
                "errors": result.errors,
                "warnings": result.warnings,
                "available_bytes": result.available_bytes,
            },
            indent=2,
        )
        return 0 if result.ok else 1
    if args.command == "discover-wechat":
        items = discover_wechat_executables()
        _print_json(
            [
                {
                    "path": str(item.path),
                    "version": item.version,
                    "supported": item.supported,
                    "errors": item.errors,
                }
                for item in items
            ],
            indent=2,
        )
        return 0
    if args.command in {"verify-payload", "install-payload"}:
        try:
            if args.command == "verify-payload":
                verified = verify_payload(args.payload)
            else:
                relative_paths = payload_relative_paths(args.payload)
                validation = validate_install_path(
                    args.install_root,
                    payload_relative_paths=relative_paths,
                )
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
                )
                write_json_atomic(
                    validation.path / INSTALL_MARKER,
                    {"schema_version": 1, "state": "payload-installed"},
                )
            _print_json(
                {
                    "ok": True,
                    "file_count": len(verified.files),
                },
                indent=2,
            )
            return 0
        except (OSError, PayloadError) as exc:
            _print_json({"ok": False, "error": str(exc)})
            return 1
    if args.command == "render-config":
        write_runtime_configs(
            args.install_root,
            args.wechat_exe,
            wechat_version=args.wechat_version,
        )
        _print_json({"ok": True})
        return 0
    if args.command == "diagnose":
        report = collect_diagnostics(args.install_root)
        _print_json(report, indent=2)
        return 0 if report["ok"] else 1
    if args.command == "start":
        try:
            return run_services_foreground(args.install_root)
        except (OSError, ServiceLaunchError, ValueError) as exc:
            _print_json({"ok": False, "error": str(exc)})
            return 1
    if args.command == "plugin-center":
        try:
            return run_plugin_center(args.install_root, args.feature_id)
        except (OSError, ValueError, PluginCenterError) as exc:
            _print_json({"ok": False, "error": str(exc)})
            return 1

    item = validate_wechat_executable(args.path)
    _print_json(
        {
            "path": str(item.path),
            "version": item.version,
            "supported": item.supported,
            "errors": item.errors,
        },
        indent=2,
    )
    return 0 if item.supported else 1


if __name__ == "__main__":
    raise SystemExit(main())
