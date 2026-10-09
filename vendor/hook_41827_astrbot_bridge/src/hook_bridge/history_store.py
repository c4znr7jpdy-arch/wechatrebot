from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Sequence
import uuid

from .models import NormalizedEvent, OutboundCommand


def _event_timestamp(value: int | float | None) -> float:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        return time.time()
    if timestamp <= 0:
        return time.time()
    # Some callbacks expose milliseconds while the history UI stores seconds.
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return timestamp


def _command_content(commands: Sequence[OutboundCommand]) -> str:
    parts: list[str] = []
    labels = {
        "image": "[图片]",
        "file": "[文件]",
        "voice": "[语音]",
        "video": "[视频]",
        "emotion": "[表情]",
        "app_xml": "[卡片消息]",
    }
    for command in commands:
        if command.kind in {"text", "at_text"}:
            parts.append(command.text)
        elif command.kind == "link_card":
            details = " · ".join(
                item for item in (command.title, command.description, command.url) if item
            )
            parts.append(f"[链接卡片]{details}")
        else:
            parts.append(labels.get(command.kind, f"[{command.kind}]"))
    return "\n".join(item for item in parts if item).strip()


class MessageHistoryStore:
    """Durable bridge-level journal independent of optional AstrBot plugins."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    dedup_key TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_bridge_history_session
                    ON messages(session_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_bridge_history_timestamp
                    ON messages(timestamp DESC);
                """
            )

    def record_inbound(self, event: NormalizedEvent) -> bool:
        is_group = event.conversation_id.endswith("@chatroom")
        message_id = event.message_id.strip()
        dedup_key = (
            f"in:{event.account_id}:{message_id}"
            if message_id
            else f"in:{uuid.uuid4().hex}"
        )
        metadata = json.dumps(
            {
                "group_name": event.group_name,
                "group_nickname": event.group_nickname,
                "wechat_name": event.wechat_name,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._insert(
            session_id=(
                f"hook_bridge:GroupMessage:{event.conversation_id}"
                if is_group
                else f"hook_bridge:FriendMessage:{event.conversation_id}"
            ),
            role="user",
            content=event.content,
            sender_id=event.sender_id,
            sender_name=(
                event.group_nickname or event.wechat_name or event.sender_name
            ),
            group_id=event.conversation_id if is_group else "",
            timestamp=_event_timestamp(event.create_time),
            metadata=metadata,
            dedup_key=dedup_key,
        )

    def record_outbound(
        self,
        target: str,
        is_group: bool,
        commands: Sequence[OutboundCommand],
        *,
        message_id: str = "",
        create_time: int | float | None = None,
        sender_id: str = "",
        sender_name: str = "",
    ) -> bool:
        dedup_key = (
            f"out:{sender_id}:{target}:{message_id}"
            if message_id
            else f"out:{uuid.uuid4().hex}"
        )
        metadata = json.dumps(
            {
                "group_name": "",
                "group_nickname": "",
                "wechat_name": sender_name,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._insert(
            session_id=(
                f"hook_bridge:GroupMessage:{target}"
                if is_group
                else f"hook_bridge:FriendMessage:{target}"
            ),
            role="assistant",
            content=_command_content(commands),
            sender_id=sender_id,
            sender_name=sender_name,
            group_id=target if is_group else "",
            timestamp=_event_timestamp(create_time),
            metadata=metadata,
            dedup_key=dedup_key,
        )

    def _insert(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        sender_id: str,
        sender_name: str,
        group_id: str,
        timestamp: float,
        metadata: str,
        dedup_key: str,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO messages (
                    session_id, role, content, sender_id, sender_name,
                    group_id, platform, timestamp, metadata, dedup_key
                ) VALUES (?, ?, ?, ?, ?, ?, 'hook_bridge', ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    sender_id,
                    sender_name,
                    group_id,
                    timestamp,
                    metadata,
                    dedup_key,
                ),
            )
            return cursor.rowcount > 0


__all__ = ["MessageHistoryStore"]
