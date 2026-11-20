"""Shared reentrant lock for manager configuration mutations."""
from __future__ import annotations

import threading

CONFIG_FILE_LOCK = threading.RLock()

__all__ = ["CONFIG_FILE_LOCK"]
