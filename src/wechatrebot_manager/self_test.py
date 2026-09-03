from __future__ import annotations

import json
import importlib.util
import platform
from pathlib import Path
import struct
import sys

from . import __version__
from .app_shell import _asset_root
from .error_catalog import load_error_catalog
from .json_io import write_json_atomic


def manager_self_test(output: str | Path) -> dict[str, object]:
    import tkinter

    asset_root = _asset_root()
    app_assets = all(
        (asset_root / filename).is_file()
        for filename in ("index.html", "app.css", "app.js")
    )
    webview_ready = importlib.util.find_spec("webview.platforms.edgechromium") is not None
    try:
        error_codes_ready = load_error_catalog().get("19000").code == "19000"
    except (OSError, ValueError, json.JSONDecodeError):
        error_codes_ready = False
    report = {
        "schema_version": 1,
        "ok": app_assets and webview_ready and error_codes_ready,
        "manager_version": __version__,
        "python_version": platform.python_version(),
        "architecture": struct.calcsize("P") * 8,
        "frozen": bool(getattr(sys, "frozen", False)),
        "tk_version": str(tkinter.TkVersion),
        "app_assets": app_assets,
        "webview2_backend": webview_ready,
        "error_codes": error_codes_ready,
    }
    write_json_atomic(Path(output).expanduser().resolve(), report)
    return report
