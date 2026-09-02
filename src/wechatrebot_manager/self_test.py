from __future__ import annotations

import json
import platform
from pathlib import Path
import struct
import sys

from . import __version__
from .json_io import write_json_atomic


def manager_self_test(output: str | Path) -> dict[str, object]:
    import tkinter

    report = {
        "schema_version": 1,
        "ok": True,
        "manager_version": __version__,
        "python_version": platform.python_version(),
        "architecture": struct.calcsize("P") * 8,
        "frozen": bool(getattr(sys, "frozen", False)),
        "tk_version": str(tkinter.TkVersion),
    }
    write_json_atomic(Path(output).expanduser().resolve(), report)
    return report
