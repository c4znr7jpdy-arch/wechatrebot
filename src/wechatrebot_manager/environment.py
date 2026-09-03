from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Callable, Iterable

from .error_catalog import ErrorDefinition, load_error_catalog
from .runtime_config import write_runtime_configs
from .wechat_path import (
    SUPPORTED_WECHAT_VERSIONS,
    WechatExecutable,
    discover_wechat_executables,
    validate_wechat_executable,
)


WECHAT_DOWNLOAD_URL = "https://weixin.qq.com/"


def configured_wechat_path(install_root: str | Path) -> str:
    path = (
        Path(install_root).expanduser().resolve() / "config" / "manager.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("wechat_exe_path") or "").strip()


def running_wechat_processes() -> tuple[str, ...]:
    if os.name != "nt":
        return ()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            creationflags=flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    names = {"weixin.exe", "wechat.exe"}
    found: set[str] = set()
    for line in result.stdout.splitlines():
        first = line.split(",", 1)[0].strip().strip('"').casefold()
        if first in names:
            found.add(first)
    return tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class WechatEnvironment:
    ready: bool
    code: str
    title: str
    message: str
    solutions: tuple[str, ...]
    path: str = ""
    version: str = ""
    source: str = ""
    process_running: bool = False
    supported_versions: tuple[str, ...] = SUPPORTED_WECHAT_VERSIONS

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "code": self.code,
            "title": self.title,
            "message": self.message,
            "solutions": list(self.solutions),
            "path": self.path,
            "version": self.version,
            "source": self.source,
            "process_running": self.process_running,
            "supported_versions": list(self.supported_versions),
            "download_url": WECHAT_DOWNLOAD_URL,
        }


def _report(
    definition: ErrorDefinition,
    *,
    ready: bool = False,
    item: WechatExecutable | None = None,
    process_running: bool = False,
) -> WechatEnvironment:
    return WechatEnvironment(
        ready=ready,
        code=definition.code,
        title=definition.title,
        message=definition.message,
        solutions=definition.solutions,
        path=str(item.path) if item is not None else "",
        version=str(item.version or "") if item is not None else "",
        source=item.source if item is not None else "",
        process_running=process_running,
    )


def inspect_wechat_environment(
    install_root: str | Path,
    *,
    discoverer: Callable[[], Iterable[WechatExecutable]] = discover_wechat_executables,
    validator: Callable[..., WechatExecutable] = validate_wechat_executable,
    process_probe: Callable[[], tuple[str, ...]] = running_wechat_processes,
) -> WechatEnvironment:
    root = Path(install_root).expanduser().resolve()
    catalog = load_error_catalog(root)
    process_running = bool(process_probe())
    selected = configured_wechat_path(root)
    selected_item: WechatExecutable | None = None
    if selected:
        selected_item = validator(selected, source="configured")
        if selected_item.supported:
            normal = catalog.get("10000")
            return WechatEnvironment(
                ready=True,
                code=normal.code,
                title="微信环境正常",
                message=f"已检测到兼容的微信 {selected_item.version}。",
                solutions=(),
                path=str(selected_item.path),
                version=str(selected_item.version or ""),
                source=selected_item.source,
                process_running=process_running,
            )

    discovered = tuple(discoverer())
    supported = tuple(item for item in discovered if item.supported)
    if supported:
        item = sorted(supported, key=lambda value: str(value.path).casefold())[0]
        normal = catalog.get("10000")
        return WechatEnvironment(
            ready=True,
            code=normal.code,
            title="微信环境正常",
            message=f"已自动检测到兼容的微信 {item.version}。",
            solutions=(),
            path=str(item.path),
            version=str(item.version or ""),
            source=item.source,
            process_running=process_running,
        )

    if selected_item is not None or discovered:
        item = selected_item or discovered[0]
        return _report(
            catalog.get("11002"), item=item, process_running=process_running
        )
    return _report(catalog.get("11001"), process_running=process_running)


def save_wechat_environment(
    install_root: str | Path,
    environment: WechatEnvironment,
) -> None:
    if not environment.ready or not environment.path:
        raise ValueError("微信环境尚未通过检查。")
    write_runtime_configs(
        install_root,
        environment.path,
        wechat_version=environment.version or None,
    )


__all__ = [
    "WECHAT_DOWNLOAD_URL",
    "WechatEnvironment",
    "configured_wechat_path",
    "inspect_wechat_environment",
    "running_wechat_processes",
    "save_wechat_environment",
]
