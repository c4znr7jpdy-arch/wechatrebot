from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading

from .customer_logs import sanitize_log_text


_SECRET_QUERY = re.compile(r"(?i)(https://[^\s?#]+)\?[^\s#]+")
_MAX_LOG_BYTES = 2 * 1024 * 1024


class UpdateLogger:
    """Small structured update log stored beside the customer's other logs."""

    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.path = self.install_root / "logs" / "update.log"
        self._lock = threading.Lock()

    def _safe(self, value: object) -> str:
        text = sanitize_log_text(str(value or ""))
        text = _SECRET_QUERY.sub(r"\1?<redacted>", text)
        text = text.replace(str(self.install_root), "<install>")
        return text[:1200]

    def write(
        self,
        event: str,
        *,
        state: str = "",
        version: str = "",
        error_code: str = "",
        message: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        record: dict[str, object] = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": self._safe(event),
        }
        if state:
            record["state"] = self._safe(state)
        if version:
            record["version"] = self._safe(version)
        if error_code:
            record["error_code"] = self._safe(error_code)
        if message:
            record["message"] = self._safe(message)
        if details:
            safe_details = {}
            for key, value in list(details.items())[:20]:
                safe_details[self._safe(key)] = self._safe(value)
            record["details"] = safe_details
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= _MAX_LOG_BYTES:
                    previous = self.path.with_suffix(".log.1")
                    os.replace(self.path, previous)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            except OSError:
                # Logging must never make the client or updater unusable.
                return


__all__ = ["UpdateLogger"]
