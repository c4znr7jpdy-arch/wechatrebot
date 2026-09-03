from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


_GROUP_ID = re.compile(r"([^:]+@chatroom)")


def _plain_conversation_id(session_id: str, group_id: str) -> tuple[str, str]:
    group_match = _GROUP_ID.search(group_id) or _GROUP_ID.search(session_id)
    if group_match:
        return "group", group_match.group(1)
    value = session_id.rsplit(":", 1)[-1].strip() or session_id
    return "private", value


def _display_name(
    kind: str,
    conversation_id: str,
    sender_name: str,
    group_name: str = "",
) -> str:
    if kind == "group":
        if group_name.strip():
            return group_name.strip()
        compact = conversation_id.removesuffix("@chatroom")
        return f"微信群 · {compact[-8:]}"
    return sender_name.strip() or conversation_id


def _identity_metadata(value: object) -> dict[str, str]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: str(payload.get(key) or "").strip()
        for key in ("group_name", "group_nickname", "wechat_name")
    }


class ChatHistoryService:
    """Product-owned history index imported incrementally from the memory module."""

    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.path = self.install_root / "data" / "chat-history.db"
        self.source = (
            self.install_root
            / "app"
            / "astrbot"
            / "data"
            / "plugin_data"
            / "astrbot_plugin_livingmemory"
            / "conversations.db"
        )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS import_sources (
                    source_key TEXT PRIMARY KEY,
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    conversation_type TEXT NOT NULL,
                    role TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    group_nickname TEXT NOT NULL DEFAULT '',
                    wechat_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    UNIQUE(source_key, source_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_history_conversation
                    ON messages(conversation_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_history_timestamp
                    ON messages(timestamp DESC);
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            for column in ("group_name", "group_nickname", "wechat_name"):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE messages ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )

    def sync(self) -> int:
        if not self.source.is_file():
            return 0
        source_key = str(self.source.resolve()).casefold()
        with self._connect() as target:
            row = target.execute(
                "SELECT last_message_id FROM import_sources WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            last_id = int(row[0]) if row else 0
            source_uri = f"file:{self.source.resolve().as_posix()}?mode=ro"
            try:
                source = sqlite3.connect(source_uri, uri=True, timeout=2)
                source.row_factory = sqlite3.Row
                rows = source.execute(
                    """
                    SELECT id, session_id, role, content, sender_id,
                           COALESCE(sender_name, '') AS sender_name,
                           COALESCE(group_id, '') AS group_id, timestamp,
                           COALESCE(metadata, '{}') AS metadata
                    FROM messages
                    WHERE id > ?
                    ORDER BY id
                    """,
                    (last_id,),
                ).fetchall()
            except sqlite3.Error:
                return 0
            finally:
                if "source" in locals():
                    source.close()
            highest = last_id
            for item in rows:
                identity = _identity_metadata(item["metadata"])
                kind, conversation_id = _plain_conversation_id(
                    str(item["session_id"]), str(item["group_id"])
                )
                target.execute(
                    """
                    INSERT OR IGNORE INTO messages (
                        source_key, source_message_id, session_id,
                        conversation_id, conversation_type, role, sender_id,
                        sender_name, group_id, group_name, group_nickname,
                        wechat_name, content, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_key,
                        int(item["id"]),
                        str(item["session_id"]),
                        conversation_id,
                        kind,
                        str(item["role"]),
                        str(item["sender_id"]),
                        str(item["sender_name"]),
                        str(item["group_id"]),
                        identity.get("group_name", ""),
                        identity.get("group_nickname", ""),
                        identity.get("wechat_name", ""),
                        str(item["content"]),
                        float(item["timestamp"]),
                    ),
                )
                highest = max(highest, int(item["id"]))
            target.execute(
                """
                INSERT INTO import_sources(source_key, last_message_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    last_message_id = excluded.last_message_id,
                    updated_at = excluded.updated_at
                """,
                (source_key, highest, datetime.now().astimezone().isoformat()),
            )
            return len(rows)

    def conversations(
        self,
        *,
        kind: str = "all",
        search: str = "",
        start_time: float | None = None,
        end_time: float | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        self.sync()
        clauses: list[str] = []
        params: list[object] = []
        if kind in {"private", "group"}:
            clauses.append("conversation_type = ?")
            params.append(kind)
        if search.strip():
            pattern = f"%{search.strip()}%"
            clauses.append(
                "(content LIKE ? OR sender_name LIKE ? OR sender_id LIKE ? OR conversation_id LIKE ? OR group_name LIKE ? OR group_nickname LIKE ? OR wechat_name LIKE ?)"
            )
            params.extend((pattern,) * 7)
        if start_time is not None:
            clauses.append("timestamp >= ?")
            params.append(float(start_time))
        if end_time is not None:
            clauses.append("timestamp <= ?")
            params.append(float(end_time))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT conversation_id, conversation_type, COUNT(*) AS message_count,
                   MAX(timestamp) AS last_timestamp,
                   MAX(CASE WHEN role = 'user' THEN sender_name ELSE '' END) AS sender_name,
                   MAX(group_name) AS group_name
            FROM messages
            {where}
            GROUP BY conversation_id, conversation_type
            ORDER BY last_timestamp DESC
            LIMIT ?
        """
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            totals = connection.execute(
                """
                SELECT COUNT(*) AS messages,
                       COUNT(DISTINCT conversation_id) AS conversations,
                       SUM(CASE WHEN conversation_type = 'private' THEN 1 ELSE 0 END) AS private_messages,
                       SUM(CASE WHEN conversation_type = 'group' THEN 1 ELSE 0 END) AS group_messages
                FROM messages
                """
            ).fetchone()
        return {
            "items": [
                {
                    "conversation_id": row["conversation_id"],
                    "type": row["conversation_type"],
                    "display_name": _display_name(
                        row["conversation_type"],
                        row["conversation_id"],
                        row["sender_name"] or "",
                        row["group_name"] or "",
                    ),
                    "message_count": row["message_count"],
                    "last_timestamp": row["last_timestamp"],
                }
                for row in rows
            ],
            "summary": {
                "messages": int(totals["messages"] or 0),
                "conversations": int(totals["conversations"] or 0),
                "private_messages": int(totals["private_messages"] or 0),
                "group_messages": int(totals["group_messages"] or 0),
            },
        }

    def messages(
        self,
        conversation_id: str,
        *,
        before: float | None = None,
        limit: int = 200,
    ) -> tuple[dict[str, Any], ...]:
        self.sync()
        clauses = ["conversation_id = ?"]
        params: list[object] = [conversation_id]
        if before is not None:
            clauses.append("timestamp < ?")
            params.append(float(before))
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, conversation_type, role, sender_id, sender_name,
                       group_id, group_name, group_nickname, wechat_name,
                       content, timestamp
                FROM messages
                WHERE {' AND '.join(clauses)}
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return tuple(
            {
                "id": row["id"],
                "type": row["conversation_type"],
                "role": row["role"],
                "sender_id": row["sender_id"],
                "sender_name": row["sender_name"] or (
                    "机器人" if row["role"] == "assistant" else row["sender_id"]
                ),
                "group_id": _plain_conversation_id("", row["group_id"])[1]
                if row["group_id"]
                else "",
                "group_name": row["group_name"],
                "group_nickname": row["group_nickname"],
                "wechat_name": row["wechat_name"],
                "content": row["content"],
                "timestamp": row["timestamp"],
            }
            for row in reversed(rows)
        )

    def delete_conversation(self, conversation_id: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
            )
            return max(0, cursor.rowcount)

    def delete_before(self, timestamp: float) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM messages WHERE timestamp < ?", (float(timestamp),)
            )
            return max(0, cursor.rowcount)

    def export(self, target: str | Path, *, format: str = "csv") -> Path:
        output = Path(target).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT conversation_type, conversation_id, role, sender_id,
                       sender_name, group_name, group_nickname, wechat_name,
                       content, timestamp
                FROM messages ORDER BY timestamp
                """
            ).fetchall()
        if format.casefold() == "json":
            output.write_text(
                json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
        else:
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    (
                        "聊天类型", "聊天对象", "群名称", "角色", "发送人微信号",
                        "发送人微信昵称", "发送人群昵称", "内容", "时间",
                    )
                )
                for row in rows:
                    writer.writerow(
                        (
                            "群聊" if row["conversation_type"] == "group" else "个人聊天",
                            row["conversation_id"],
                            row["group_name"],
                            "机器人" if row["role"] == "assistant" else "用户",
                            row["sender_id"],
                            row["wechat_name"] or row["sender_name"],
                            row["group_nickname"],
                            row["content"],
                            datetime.fromtimestamp(
                                row["timestamp"], tz=timezone.utc
                            ).astimezone().isoformat(timespec="seconds"),
                        )
                    )
        return output


__all__ = ["ChatHistoryService"]
