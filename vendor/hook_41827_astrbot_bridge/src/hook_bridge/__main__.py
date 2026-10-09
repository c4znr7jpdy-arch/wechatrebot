from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
import sys

from .app import BridgeApplication
from .config import Settings, SettingsError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hook-astrbot-bridge",
        description="Bridge HOOK 4.1.8.27 to AstrBot over OneBot V11.",
    )
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate offline files and settings without starting processes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.load(args.config)
    except SettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    errors = settings.preflight(require_wechat=not args.check)
    if errors:
        for error in errors:
            print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    if args.check:
        print("Offline check passed: configuration and HOOK binaries are valid.")
        if settings.wechat_exe_path is None:
            print(
                "Warning: wechat_exe_path is not configured; real startup remains disabled."
            )
        elif not settings.wechat_exe_path.is_file():
            print(
                f"Warning: wechat_exe_path does not exist: {settings.wechat_exe_path}"
            )
        return 0

    try:
        return asyncio.run(BridgeApplication(settings).run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

