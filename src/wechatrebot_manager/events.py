from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4
from typing import Mapping

from .customer_logs import (
    ACTIVITY_RETENTION_SECONDS, ERROR_REPEAT_WINDOW_SECONDS,
    log_timestamp, sanitize_log_text, read_log_tail,
)
from .json_io import write_text_atomic


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
_STORE_LOCK = threading.RLock()
_MAX_LOG_BYTES = 2 * 1024 * 1024


def _safe_value(value: object) -> object:
    if isinstance(value, str):
        return sanitize_log_text(value)[:12000]
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
    event_id: str = ""
    count: int = 1
    first_time: str = ""
    occurrences: tuple[tuple[int, int], ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "time": self.time,
            "event": self.event,
            "status": self.status,
            "event_id": self.event_id,
        }
        if self.code:
            payload["code"] = self.code
        if self.message:
            payload["message"] = sanitize_log_text(self.message)[:4000]
        if self.details:
            payload["details"] = _safe_value(self.details)
        if self.count > 1:
            payload.update(count=self.count, first_time=self.first_time)
            if self.occurrences:
                payload["occurrences"] = self.occurrences
        return payload


class EventStore:
    def __init__(self, install_root: str | Path) -> None:
        root = Path(install_root).expanduser().resolve()
        self.path = root / "logs" / "lifecycle-events.jsonl"
        self._lock = _STORE_LOCK
        self._last_pruned = 0.0

    @staticmethod
    def _records(lines: Iterable[str]) -> Iterator[dict]:
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                yield item

    @classmethod
    def _file_records(cls, path: Path) -> Iterator[dict]:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                yield from cls._records(handle)
        except FileNotFoundError:
            return

    @classmethod
    def _read(cls, path: Path) -> list[dict]:
        try:
            text, _ = read_log_tail(path, max_bytes=_MAX_LOG_BYTES, max_lines=20000)
        except FileNotFoundError:
            return []
        return list(cls._records(text.splitlines()))

    @staticmethod
    def _write(path: Path, records: Iterable[dict]) -> None:
        write_text_atomic(path, (
            json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            for item in records
        ))

    @staticmethod
    def _retained(item: dict, cutoff: float) -> dict | None:
        timestamp = log_timestamp(item.get("time")) or 0
        if timestamp < cutoff:
            return None
        occurrences = item.get("occurrences")
        if not isinstance(occurrences, list):
            return item
        kept = sorted(entry for entry in occurrences if isinstance(entry, list) and len(entry) == 2
                      and isinstance(entry[0], int) and isinstance(entry[1], int)
                      and cutoff <= entry[0] <= timestamp and entry[1] > 0)
        if not kept:
            return None
        return {**item, "occurrences": kept, "count": sum(entry[1] for entry in kept),
                "first_time": datetime.fromtimestamp(kept[0][0]).astimezone().isoformat(timespec="seconds")}

    def prune(self) -> None:
        """Remove expired manager records on reads and writes, including rotation."""
        with self._lock:
            now = time.time()
            if now - self._last_pruned < 60:
                return
            cutoff = now - ACTIVITY_RETENTION_SECONDS
            for path in (self.path, self.path.with_suffix(".jsonl.1")):
                if not path.exists():
                    continue
                # A rotated file can exceed the size limit by its final record.
                # Never replace a complete file with a truncated display tail.
                if any(self._retained(item, cutoff) != item for item in self._file_records(path)):
                    self._write(path, (
                        current for item in self._file_records(path)
                        if (current := self._retained(item, cutoff)) is not None
                    ))
            self._last_pruned = now

    def _merge_error(self, record: EventRecord) -> EventRecord | None:
        if record.event != "APP_ERROR":
            return None
        # emit rotates before reaching this point, bounding this complete read.
        records = list(self._file_records(self.path))
        current = record.to_dict()
        timestamp = log_timestamp(record.time)
        for index in range(len(records) - 1, -1, -1):
            item = records[index]
            previous = log_timestamp(item.get("time"))
            if previous is None or not 0 <= timestamp - previous <= ERROR_REPEAT_WINDOW_SECONDS:
                continue
            if not all(item.get(key) == current.get(key) for key in ("event", "status", "code", "message", "details")):
                continue
            item = self._retained(item, timestamp - ACTIVITY_RETENTION_SECONDS)
            if item is None:
                continue
            first_time = str(item.get("first_time") or item.get("time") or record.time)
            if timestamp - (log_timestamp(first_time) or 0) > ERROR_REPEAT_WINDOW_SECONDS:
                # Store short bounded summaries; the feed joins continuous
                # summaries, and per-second counts expire exactly at 48 hours.
                continue
            count = item.get("count", 1)
            occurrences = item.get("occurrences")
            if not isinstance(occurrences, list) or not occurrences:
                occurrences = [[int(previous), count if isinstance(count, int) and count > 0 else 1]]
            occurrences = [list(entry) for entry in occurrences]
            if occurrences[-1][0] == int(timestamp):
                occurrences[-1][1] += 1
            else:
                occurrences.append([int(timestamp), 1])
            merged = replace(record, event_id=str(item.get("event_id") or record.event_id),
                             count=sum(entry[1] for entry in occurrences), first_time=first_time,
                             occurrences=tuple(tuple(entry) for entry in occurrences))
            records.pop(index)
            records.append(merged.to_dict())
            self._write(self.path, records)
            return merged
        return None

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
            event_id=uuid4().hex,
        )
        line = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            if self.path.exists() and self.path.stat().st_size >= _MAX_LOG_BYTES:
                self.path.replace(self.path.with_suffix(".jsonl.1"))
            self.prune()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            merged = self._merge_error(record)
            if merged is not None:
                return merged
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        return record

    def recent(self, limit: int = 100) -> tuple[dict[str, object], ...]:
        if limit <= 0:
            return ()
        try:
            with self._lock:
                self.prune()
                records = self._read(self.path)
        except (FileNotFoundError, OSError):
            return ()
        cutoff = time.time() - ACTIVITY_RETENTION_SECONDS
        return tuple(current for item in records[-limit:]
                     if (current := self._retained(item, cutoff)) is not None)


__all__ = ["EVENT_TYPES", "EventRecord", "EventStore"]
