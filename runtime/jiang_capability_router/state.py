"""按会话保存最近执行成功的机器人能力。"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RecentAction:
    capability_id: str
    display_name: str
    source: str
    status: str
    timestamp: str
    details: dict[str, Any]

    @property
    def timestamp_seconds(self) -> float:
        try:
            return datetime.fromisoformat(self.timestamp).timestamp()
        except (TypeError, ValueError):
            return 0.0


class RecentActionStore:
    def __init__(
        self,
        path: Path,
        ttl_seconds: int = 600,
        max_per_session: int = 5,
    ) -> None:
        self.path = path
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.max_per_session = max(1, int(max_per_session))
        self._lock = threading.RLock()
        self._actions: dict[str, list[RecentAction]] = {}
        self._loaded = False

    def configure(self, ttl_seconds: int, max_per_session: int) -> None:
        with self._lock:
            self.ttl_seconds = max(30, int(ttl_seconds))
            self.max_per_session = max(1, int(max_per_session))
            self._prune_locked()

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError, TypeError):
            return
        sessions = raw.get("sessions", {}) if isinstance(raw, dict) else {}
        if not isinstance(sessions, dict):
            return
        for session, items in sessions.items():
            if not isinstance(session, str) or not isinstance(items, list):
                continue
            parsed: list[RecentAction] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    parsed.append(
                        RecentAction(
                            capability_id=str(item["capability_id"]),
                            display_name=str(item["display_name"]),
                            source=str(item.get("source", "unknown")),
                            status=str(item.get("status", "success")),
                            timestamp=str(item["timestamp"]),
                            details=(
                                item.get("details", {})
                                if isinstance(item.get("details", {}), dict)
                                else {}
                            ),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            if parsed:
                self._actions[session] = parsed[-self.max_per_session :]
        self._prune_locked()

    def _prune_locked(self, now: float | None = None) -> None:
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        minimum = now - self.ttl_seconds
        kept: dict[str, list[RecentAction]] = {}
        for session, actions in self._actions.items():
            valid = [
                action
                for action in actions
                if action.status == "success" and action.timestamp_seconds >= minimum
            ]
            if valid:
                kept[session] = valid[-self.max_per_session :]
        self._actions = kept

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "sessions": {
                session: [asdict(action) for action in actions]
                for session, actions in self._actions.items()
            },
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            "utf-8",
        )
        os.replace(temp_path, self.path)

    def record(
        self,
        session: str,
        capability_id: str,
        display_name: str,
        source: str,
        *,
        status: str = "success",
        details: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> RecentAction | None:
        session = str(session or "").strip()
        capability_id = str(capability_id or "").strip()
        display_name = str(display_name or "").strip()
        if not session or not capability_id or not display_name:
            return None
        dt = timestamp or datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        action = RecentAction(
            capability_id=capability_id,
            display_name=display_name,
            source=str(source or "unknown"),
            status=str(status or "success"),
            timestamp=dt.isoformat(),
            details=dict(details or {}),
        )
        with self._lock:
            self._load_locked()
            actions = self._actions.setdefault(session, [])
            actions.append(action)
            self._prune_locked(now=dt.timestamp())
            if session in self._actions:
                self._actions[session] = self._actions[session][
                    -self.max_per_session :
                ]
            self._save_locked()
        return action

    def list_recent(self, session: str) -> list[RecentAction]:
        with self._lock:
            self._load_locked()
            self._prune_locked()
            return list(self._actions.get(str(session or ""), []))

    def latest(self, session: str) -> RecentAction | None:
        actions = self.list_recent(session)
        return actions[-1] if actions else None

    def clear(self) -> None:
        with self._lock:
            self._actions = {}
            self._loaded = True
            if self.path.exists():
                self.path.unlink()


_DEFAULT_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugin_data"
    / "jiang_capability_router"
    / "recent_actions.json"
)
_STORE = RecentActionStore(_DEFAULT_PATH)


def configure_store(ttl_seconds: int = 600, max_per_session: int = 5) -> None:
    _STORE.configure(ttl_seconds, max_per_session)


def record_action(
    session: str,
    capability_id: str,
    display_name: str,
    source: str,
    *,
    details: dict[str, Any] | None = None,
) -> RecentAction | None:
    return _STORE.record(
        session,
        capability_id,
        display_name,
        source,
        details=details,
    )


def get_recent_actions(session: str) -> list[RecentAction]:
    return _STORE.list_recent(session)


def get_latest_action(session: str) -> RecentAction | None:
    return _STORE.latest(session)
