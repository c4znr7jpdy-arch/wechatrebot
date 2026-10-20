"""Trusted manager skin catalog and installation-scoped preferences."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import sys

from .customer_config import _object, save_ui_preference


_SKIN_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
_STYLESHEET_SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


def _catalog_root() -> Path:
    candidates = [Path(__file__).with_name("app_webui")]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.insert(0, Path(bundle_root) / "wechatrebot_manager" / "app_webui")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    raise FileNotFoundError("主界面资源缺失，请重新安装软件。")


def load_skin_catalog(asset_root: Path | None = None) -> list[dict[str, str]]:
    root = (asset_root if asset_root is not None else _catalog_root()).resolve()
    value = json.loads((root / "skins.json").read_text(encoding="utf-8-sig"))
    if not isinstance(value, list) or not value:
        raise ValueError("界面皮肤目录格式无效。")
    catalog: list[dict[str, str]] = []
    identifiers: set[str] = set()
    stylesheets: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or any(
            not isinstance(item.get(key), str) for key in ("id", "name", "description", "stylesheet")
        ):
            raise ValueError("界面皮肤目录格式无效。")
        identifier, stylesheet = item["id"], item["stylesheet"]
        if not _SKIN_ID.fullmatch(identifier) or identifier in identifiers or not item["name"].strip():
            raise ValueError("界面皮肤标识无效或重复。")
        if identifier == "default":
            if stylesheet:
                raise ValueError("默认皮肤不得覆盖基础样式。")
        elif (
            not stylesheet
            or any(not _STYLESHEET_SEGMENT.fullmatch(part) for part in stylesheet.split("/"))
            or PurePosixPath(stylesheet).suffix != ".css"
            or not (root / stylesheet).resolve().is_relative_to(root)
            or stylesheet.casefold() in stylesheets
        ):
            raise ValueError("界面皮肤样式路径无效或重复。")
        identifiers.add(identifier)
        if stylesheet:
            stylesheets.add(stylesheet.casefold())
        catalog.append({key: item[key] for key in ("id", "name", "description", "stylesheet")})
    if "default" not in identifiers:
        raise ValueError("界面皮肤目录缺少默认皮肤。")
    return catalog


def load_ui_skin(root: str | Path, *, asset_root: Path | None = None) -> str:
    preferences = _object(Path(root) / "data/manager/ui-preferences.json")
    skin = preferences.get("skin")
    return skin if any(item["id"] == skin for item in load_skin_catalog(asset_root)) else ""


def save_ui_skin(root: str | Path, skin: str, *, asset_root: Path | None = None) -> None:
    if not any(item["id"] == skin for item in load_skin_catalog(asset_root)):
        raise ValueError("请选择可用的界面皮肤。")
    save_ui_preference(root, "skin", skin)
