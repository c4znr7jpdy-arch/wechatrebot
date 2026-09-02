from __future__ import annotations

import json
import os
from pathlib import Path

from .install_paths import INSTALL_MARKER
from .json_io import write_json_atomic


def locator_path(local_app_data: str | Path | None = None) -> Path:
    value = local_app_data or os.environ.get("LOCALAPPDATA")
    if value:
        base = Path(value).expanduser().resolve()
    else:
        base = Path.home() / "AppData" / "Local"
    return base / "WeChatReBot" / "install-location.json"


def save_install_location(
    install_root: str | Path,
    *,
    local_app_data: str | Path | None = None,
) -> Path:
    root = Path(install_root).expanduser().resolve()
    target = locator_path(local_app_data)
    write_json_atomic(
        target,
        {"schema_version": 1, "install_root": str(root)},
    )
    return target


def load_install_location(
    *,
    local_app_data: str | Path | None = None,
) -> Path | None:
    target = locator_path(local_app_data)
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
        root = Path(str(payload["install_root"])).expanduser().resolve()
        marker = json.loads((root / INSTALL_MARKER).read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, KeyError, OSError, TypeError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != 1 or marker.get("state") not in {
        "ready",
        "configured",
    }:
        return None
    return root
