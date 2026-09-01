from __future__ import annotations

import argparse
import json
from pathlib import Path

from .bootstrap import run_bootstrap
from .install_paths import validate_install_path
from .wechat_path import discover_wechat_executables, validate_wechat_executable


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wechatrebot-manager")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("bootstrap", help="运行 Windows 首次配置向导")

    install = subparsers.add_parser("check-install-dir", help="检查安装目录")
    install.add_argument("path")

    subparsers.add_parser("discover-wechat", help="发现本机微信主程序")

    wechat = subparsers.add_parser("check-wechat", help="检查微信主程序")
    wechat.add_argument("path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "bootstrap":
        return run_bootstrap()
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

