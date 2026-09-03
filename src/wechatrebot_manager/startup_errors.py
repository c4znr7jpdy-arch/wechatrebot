from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sys

from .customer_logs import sanitize_log_text


def startup_error_log_path(local_app_data: str | Path | None = None) -> Path:
    value = local_app_data or os.environ.get("LOCALAPPDATA")
    if value:
        base = Path(value).expanduser().resolve()
    else:
        base = Path.home() / "AppData" / "Local"
    return base / "WeChatReBot" / "logs" / "startup-error.log"


def customer_startup_error_message(exc: BaseException) -> str:
    detail = sanitize_log_text(str(exc)).strip()
    if isinstance(exc, FileNotFoundError) and "payload" in detail.casefold():
        return (
            "启动文件不完整。\n\n"
            "请不要单独复制 WeChatReBot.exe。请使用完整交付目录，并确认 EXE "
            "旁边保留 payload.wrb 和 DISTRIBUTION-MANIFEST.json。"
        )
    if detail:
        return f"程序未能打开。\n\n原因：{detail}\n\n请将启动错误记录发给技术支持。"
    return "程序未能打开。请将启动错误记录发给技术支持。"


def record_startup_error(
    exc: BaseException,
    *,
    local_app_data: str | Path | None = None,
) -> Path | None:
    target = startup_error_log_path(local_app_data)
    line = (
        f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
        f"{type(exc).__name__}: {sanitize_log_text(str(exc)).strip()}\n"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
    except OSError:
        return None
    return target


def report_startup_error(exc: BaseException) -> None:
    log_path = record_startup_error(exc)
    message = customer_startup_error_message(exc)
    if log_path is not None:
        message += f"\n\n错误记录：{log_path}"
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                message,
                "微信机器人 · 无法打开",
                0x00000010 | 0x00000000,
            )
            return
        except (AttributeError, OSError):
            pass
    print(message, file=sys.stderr)


__all__ = [
    "customer_startup_error_message",
    "record_startup_error",
    "report_startup_error",
    "startup_error_log_path",
]
