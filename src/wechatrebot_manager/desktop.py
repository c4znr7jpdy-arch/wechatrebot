from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import zipfile

from .app_shell import run_application
from .install_paths import INSTALL_MARKER
from .location import load_install_location
from .payload import PAYLOAD_ARCHIVE, PAYLOAD_MANIFEST
from .setup_wizard import run_setup_wizard
from .single_instance import SingleInstanceLock


def _is_payload_root(path: Path) -> bool:
    if path.is_file():
        try:
            with zipfile.ZipFile(path) as archive:
                payload = json.loads(archive.read(PAYLOAD_MANIFEST).decode("utf-8"))
        except (KeyError, OSError, UnicodeDecodeError, zipfile.BadZipFile, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and payload.get("schema_version") == 1
    try:
        payload = json.loads(
            (path / "release-manifest.json").read_text(encoding="utf-8-sig")
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("schema_version") == 1


def find_bundled_payload(
    explicit: str | Path | None = None,
    *,
    executable: str | Path | None = None,
) -> Path | None:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit).expanduser())
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / PAYLOAD_ARCHIVE)
        candidates.append(Path(bundle_root) / "payload")
    executable_path = Path(executable or sys.executable).expanduser().resolve()
    candidates.extend(
        (
            executable_path.parent / PAYLOAD_ARCHIVE,
            executable_path.parent / "payload",
            Path.cwd() / PAYLOAD_ARCHIVE,
            Path.cwd() / "payload",
        )
    )
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        if _is_payload_root(resolved):
            return resolved
    return None


def install_root_from_executable(executable: str | Path | None = None) -> Path | None:
    executable_path = Path(executable or sys.executable).expanduser().resolve()
    candidates = (executable_path.parent, executable_path.parent.parent)
    for candidate in candidates:
        try:
            marker = json.loads(
                (candidate / INSTALL_MARKER).read_text(encoding="utf-8-sig")
            )
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if marker.get("state") in {"ready", "configured"}:
            return candidate
    return None


def _show_startup_notice(message: str, *, close_after_ms: int = 0) -> None:
    if sys.platform != "win32":
        return
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        if close_after_ms:
            from tkinter import ttk

            root.title("微信机器人")
            root.resizable(False, False)
            root.protocol("WM_DELETE_WINDOW", root.quit)
            content = ttk.Frame(root, padding=20)
            content.pack(fill="both", expand=True)
            ttk.Label(content, text=message, wraplength=360).pack(pady=(0, 16))
            ttk.Button(content, text="确定", command=root.quit).pack(anchor="e")
            root.after(close_after_ms, root.quit)
            root.deiconify()
            root.mainloop()
        else:
            messagebox.showinfo("微信机器人", message, parent=root)
    finally:
        root.destroy()


def run_desktop(
    *,
    payload_root: str | Path | None = None,
    install_root: str | Path | None = None,
) -> int:
    relaunch_root = os.environ.pop("WECHATREBOT_UPDATE_RELAUNCH_ROOT", "")
    installed = (
        Path(install_root).expanduser().resolve()
        if install_root is not None
        else install_root_from_executable() or load_install_location()
    )
    if installed is not None:
        installed = installed.resolve()
        if relaunch_root != str(installed):
            helper = SingleInstanceLock(f"update-helper:{installed}")
            if not helper.acquire():
                # Do not claim the manager mutex while its updater is still
                # replacing files and will shortly launch the confirmed build.
                _show_startup_notice(
                    "更新正在安装，完成后会自动打开管理器，请稍候。",
                    close_after_ms=3000,
                )
                return 4
            helper.release()
    instance = SingleInstanceLock(installed or "setup")
    if not instance.acquire():
        _show_startup_notice("管理器已经在运行。")
        return 3
    try:
        if installed is not None:
            return run_application(installed)
        payload = find_bundled_payload(payload_root)
        if payload is None:
            if payload_root is not None:
                raise FileNotFoundError(f"商业载荷无效：{Path(payload_root).resolve()}")
            raise FileNotFoundError("未找到与安装程序配套的 payload.wrb。")
        return run_setup_wizard(payload)
    finally:
        instance.release()


__all__ = [
    "find_bundled_payload",
    "install_root_from_executable",
    "run_desktop",
]
