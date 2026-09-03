from __future__ import annotations

import json
from pathlib import Path
import sys

from .app_shell import run_application
from .install_paths import INSTALL_MARKER
from .location import load_install_location
from .payload import sha256_file
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


def is_development_preview(executable: str | Path | None = None) -> bool:
    """Accept an incomplete payload only inside a deliberately assembled audit package."""
    executable_path = Path(executable or sys.executable).expanduser().resolve()
    manifest_path = executable_path.parent / "DISTRIBUTION-MANIFEST.json"
    payload_manifest = executable_path.parent / "payload" / "release-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        files = manifest["files"]
    except (FileNotFoundError, KeyError, OSError, TypeError, json.JSONDecodeError):
        return False
    if not (
        isinstance(manifest, dict)
        and manifest.get("schema_version") == 1
        and manifest.get("entrypoint") == executable_path.name
        and manifest.get("development_preview") is True
        and manifest.get("payload_status") != "ready"
        and isinstance(files, list)
    ):
        return False
    expected = next(
        (
            item.get("sha256")
            for item in files
            if isinstance(item, dict)
            and item.get("path") == "payload/release-manifest.json"
        ),
        None,
    )
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    try:
        return sha256_file(payload_manifest).casefold() == expected.casefold()
    except OSError:
        return False


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
            return run_application(installed)
        payload = find_bundled_payload(payload_root)
        if payload is None:
            if payload_root is not None:
                raise FileNotFoundError(f"商业载荷无效：{Path(payload_root).resolve()}")
            raise FileNotFoundError("未找到与安装程序配套的 payload 目录。")
        return run_setup_wizard(
            payload,
            allow_incomplete_payload=(
                allow_incomplete_payload
                or (payload_root is None and is_development_preview())
            ),
        )
    finally:
        instance.release()


__all__ = [
    "find_bundled_payload",
    "install_root_from_executable",
    "is_development_preview",
    "run_desktop",
]
