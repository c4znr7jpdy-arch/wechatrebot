"""消息缓冲 — 被动感知 (Chat Awareness)

SQLite 持久化 + 内存 LRU 缓存。
群聊消息写入本地数据库，重启后历史仍可查。
"""
from __future__ import annotations

import os
import time
import sqlite3
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, List, Optional


DB_PATH = os.getenv(
    "CHAT_HISTORY_DB",
    str(Path(__file__).parent.parent / "data" / "chat_history.db"),
)


@dataclass
class BufferedMessage:
    ts: int
    group_id: Optional[str]
    user_id: str
    nickname: str
    content: str
    is_bot: bool = False


def _init_db(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            nickname TEXT NOT NULL,
            content TEXT NOT NULL,
            is_bot INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_group_ts
        ON chat_messages(group_id, ts DESC)
    """)
    conn.commit()
    return conn


class MessageBuffer:
    def __init__(self, per_group_cap: int = 50, total_groups_cap: int = 30) -> None:
        self._per_group_cap = per_group_cap
        self._total_groups_cap = total_groups_cap
        self._buckets: OrderedDict[str, Deque[BufferedMessage]] = OrderedDict()
        self._lock = threading.Lock()
        self._conn = _init_db(DB_PATH)

    def append(self, msg: BufferedMessage) -> None:
        key = msg.group_id or f"@private:{msg.user_id}"

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque(maxlen=self._per_group_cap)
                self._buckets[key] = bucket
                self._evict_if_needed()
            else:
                self._buckets.move_to_end(key)
            bucket.append(msg)

        if msg.group_id:
            try:
                self._conn.execute(
                    "INSERT INTO chat_messages (ts, group_id, user_id, nickname, content, is_bot) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (msg.ts, msg.group_id, msg.user_id, msg.nickname, msg.content, int(msg.is_bot)),
                )
                self._conn.commit()
            except Exception:
                pass

    def get_recent(self, group_id: str, limit: int = 10) -> List[BufferedMessage]:
        with self._lock:
            bucket = self._buckets.get(group_id)
            if bucket:
                self._buckets.move_to_end(group_id)
                items = list(bucket)[-limit:]
                if items:
                    return items

        # 内存没有，从数据库加载
        try:
            rows = self._conn.execute(
                "SELECT ts, group_id, user_id, nickname, content, is_bot "
                "FROM chat_messages WHERE group_id = ? ORDER BY ts DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
            messages = [
                BufferedMessage(
                    ts=r[0], group_id=r[1], user_id=r[2],
                    nickname=r[3], content=r[4], is_bot=bool(r[5]),
                )
                for r in reversed(rows)
            ]
            # 回填内存缓存
            if messages:
                with self._lock:
                    bucket = deque(messages, maxlen=self._per_group_cap)
                    self._buckets[group_id] = bucket
                    self._buckets.move_to_end(group_id)
                    self._evict_if_needed()
            return messages
        except Exception:
            return []

    def get_user_history(
        self,
        user_id: str,
        group_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[BufferedMessage]:
        """查询某用户的历史发言（跨群或指定群）"""
        try:
            if group_id:
                rows = self._conn.execute(
                    "SELECT ts, group_id, user_id, nickname, content, is_bot "
                    "FROM chat_messages WHERE user_id = ? AND group_id = ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (user_id, group_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT ts, group_id, user_id, nickname, content, is_bot "
                    "FROM chat_messages WHERE user_id = ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            return [
                BufferedMessage(
                    ts=r[0], group_id=r[1], user_id=r[2],
                    nickname=r[3], content=r[4], is_bot=bool(r[5]),
                )
                for r in reversed(rows)
            ]
        except Exception:
            return []

    def find_user_by_nickname(self, nickname: str, group_id: Optional[str] = None) -> Optional[str]:
        """通过昵称模糊查找 user_id"""
        try:
            if group_id:
                row = self._conn.execute(
                    "SELECT user_id FROM chat_messages "
                    "WHERE nickname LIKE ? AND group_id = ? "
                    "ORDER BY ts DESC LIMIT 1",
                    (f"%{nickname}%", group_id),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT user_id FROM chat_messages "
                    "WHERE nickname LIKE ? "
                    "ORDER BY ts DESC LIMIT 1",
                    (f"%{nickname}%",),
                ).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def _evict_if_needed(self) -> None:
        while len(self._buckets) > self._total_groups_cap:
            self._buckets.popitem(last=False)


_buffer: Optional[MessageBuffer] = None


def get_buffer() -> MessageBuffer:
    global _buffer
    if _buffer is None:
        _buffer = MessageBuffer()
    return _buffer


def format_perception_context(messages: List[BufferedMessage], max_text_len: int = 200) -> str:
    if not messages:
        return ""
    lines = []
    for m in messages:
        prefix = "[bot] " if m.is_bot else ""
        speaker = m.nickname or m.user_id
        content = m.content[:max_text_len]
        lines.append(f"{prefix}{speaker}: {content}")
    return "<recent_messages>\n" + "\n".join(lines) + "\n</recent_messages>"
