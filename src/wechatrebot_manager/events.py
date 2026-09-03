from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import threading
from typing import Mapping

from .customer_logs import sanitize_log_text


EVENT_TYPES = frozenset(
    {
        "APP_START",
        "CHECK_WECHAT",
        "WECHAT_READY",
        "CORE_START",
        "CORE_READY",
        "CONNECTION_START",
        "CONNECTION_CONNECTED",
        "FUNCTION_LOADED",
        "WECHAT_CLOSED",
        "CONNECTION_STOP",
        "CORE_STOP",
        "APP_ERROR",
    }
)
_SENSITIVE_KEY = re.compile(r"(?i)(api.?key|token|authorization|secret|password)")


def _safe_value(value: object) -> object:
    if isinstance(value, str):
        return sanitize_log_text(value)[:500]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:20]]
    if isinstance(value, Mapping):
        return {
            str(key): ("***" if _SENSITIVE_KEY.search(str(key)) else _safe_value(item))
            for key, item in list(value.items())[:30]
        }
    return sanitize_log_text(str(value))[:500]


@dataclass(frozen=True, slots=True)
class EventRecord:
    time: str
    event: str
    status: str
    code: str = ""
    message: str = ""
    details: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "time": self.time,
            "event": self.event,
            "status": self.status,
        }
        if self.code:
            payload["code"] = self.code
        if self.message:
            payload["message"] = sanitize_log_text(self.message)[:500]
        if self.details:
            payload["details"] = _safe_value(self.details)
        return payload


class EventStore:
    def __init__(self, install_root: str | Path) -> None:
        root = Path(install_root).expanduser().resolve()
        self.path = root / "logs" / "lifecycle-events.jsonl"
        self._lock = threading.Lock()

    def emit(
        self,
        event: str,
        status: str,
        *,
        code: str = "",
        message: str = "",
        details: Mapping[str, object] | None = None,
    ) -> EventRecord:
        if event not in EVENT_TYPES:
            raise ValueError(f"未知事件类型：{event}")
        record = EventRecord(
            time=datetime.now().astimezone().isoformat(timespec="seconds"),
            event=event,
            status=status,
            code=str(code),
            message=message,
            details=details,
        )
        line = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        return record

    def recent(self, limit: int = 100) -> tuple[dict[str, object], ...]:
        if limit <= 0:
            return ()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return ()
        records: list[dict[str, object]] = []
        for line in lines[-limit:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return tuple(records)


__all__ = ["EVENT_TYPES", "EventRecord", "EventStore"]
