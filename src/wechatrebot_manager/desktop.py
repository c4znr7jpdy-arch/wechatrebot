from __future__ import annotations

import json
from pathlib import Path
import sys

from .control_center import run_control_center
from .install_paths import INSTALL_MARKER
from .location import load_install_location
from .setup_wizard import run_setup_wizard
from .single_instance import SingleInstanceLock


def _is_payload_root(path: Path) -> bool:
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
        candidates.append(Path(bundle_root) / "payload")
    executable_path = Path(executable or sys.executable).expanduser().resolve()
    candidates.extend(
        (
            executable_path.parent / "payload",
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


def run_desktop(
    *,
    payload_root: str | Path | None = None,
    install_root: str | Path | None = None,
    allow_incomplete_payload: bool = False,
) -> int:
    installed = (
        Path(install_root).expanduser().resolve()
        if install_root is not None
        else install_root_from_executable() or load_install_location()
    )
    instance = SingleInstanceLock(installed or "setup")
    if not instance.acquire():
        if sys.platform == "win32":
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("微信机器人", "管理器已经在运行。", parent=root)
            root.destroy()
        return 3
    try:
        if installed is not None:
            return run_control_center(installed)
        payload = find_bundled_payload(payload_root)
        if payload is None:
            if payload_root is not None:
                raise FileNotFoundError(f"商业载荷无效：{Path(payload_root).resolve()}")
            raise FileNotFoundError("未找到与安装程序配套的 payload 目录。")
        return run_setup_wizard(
            payload,
            allow_incomplete_payload=allow_incomplete_payload,
        )
    finally:
        instance.release()


__all__ = [
    "find_bundled_payload",
    "install_root_from_executable",
    "run_desktop",
]
