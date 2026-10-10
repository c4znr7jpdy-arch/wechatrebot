from __future__ import annotations

import json
from html.parser import HTMLParser
import importlib.util
import importlib.resources
import platform
from pathlib import Path
import struct
import sys
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .app_shell import _asset_root
from .error_catalog import load_error_catalog
from .json_io import write_json_atomic


def _timezone_data_ready() -> bool:
    try:
        # Require the packaged resource, not a developer machine's cached zone
        # or system database. Windows has no standard IANA timezone database.
        resource = importlib.resources.files("tzdata.zoneinfo.Asia") / "Shanghai"
        return resource.is_file() and ZoneInfo.no_cache("Asia/Shanghai").key == "Asia/Shanghai"
    except (ImportError, OSError, ValueError, ZoneInfoNotFoundError):
        return False


class _EntryAssets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.paths.append(values["src"])
        elif tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.paths.append(values["href"])


def _app_assets_ready(root: Path) -> bool:
    try:
        root = root.resolve()
        parser = _EntryAssets()
        parser.feed((root / "index.html").read_text("utf-8"))
        if not parser.paths:
            return False
        for value in parser.paths:
            parsed = urlsplit(value)
            path = (root / unquote(parsed.path).lstrip("/")).resolve()
            if parsed.scheme or parsed.netloc or not path.is_relative_to(root) or not path.is_file():
                return False
        return True
    except (OSError, ValueError):
        return False


def manager_self_test(output: str | Path) -> dict[str, object]:
    import tkinter

    asset_root = _asset_root()
    app_assets = _app_assets_ready(asset_root)
    webview_ready = importlib.util.find_spec("webview.platforms.edgechromium") is not None
    try:
        error_codes_ready = load_error_catalog().get("19000").code == "19000"
    except (OSError, ValueError, json.JSONDecodeError):
        error_codes_ready = False
    timezone_data_ready = _timezone_data_ready()
    report = {
        "schema_version": 1,
        "ok": app_assets and webview_ready and error_codes_ready and timezone_data_ready,
        "manager_version": __version__,
        "python_version": platform.python_version(),
        "architecture": struct.calcsize("P") * 8,
        "frozen": bool(getattr(sys, "frozen", False)),
        "tk_version": str(tkinter.TkVersion),
        "app_assets": app_assets,
        "webview2_backend": webview_ready,
        "error_codes": error_codes_ready,
        "timezone_data": timezone_data_ready,
    }
    write_json_atomic(Path(output).expanduser().resolve(), report)
    return report
