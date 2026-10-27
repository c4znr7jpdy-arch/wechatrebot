from __future__ import annotations

import base64
import binascii
import csv
from datetime import datetime, timezone
from html import unescape
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
import uuid

from .location import chat_history_path


_GROUP_ID = re.compile(r"([^:]+@chatroom)")
_MESSAGE_XML = re.compile(
    r"^\s*(?:<\?xml[^>]*>\s*)?<(?:msg|appmsg|sysmsg)\b",
    re.IGNORECASE,
)
_EMOJI_XML = re.compile(r"<emoji\b", re.IGNORECASE)
_IMAGE_XML = re.compile(r"<img\b", re.IGNORECASE)
_APP_MESSAGE_XML = re.compile(r"<appmsg\b", re.IGNORECASE)
_LEGACY_LINK_CARD = re.compile(r"^\s*\[链接卡片\]", re.IGNORECASE)
_HTTP_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def _xml_fragment(content: str, tag: str) -> str:
    match = re.search(
        rf"<{tag}\b[^>]*>(.*?)</{tag}>",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else ""


def _xml_text(content: str, tag: str, *, limit: int = 80) -> str:
    value = _xml_fragment(content, tag).strip()
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        value = value[9:-3]
    value = unescape(value)
    if "<" in value or ">" in value:
        return ""
    return " ".join(value.split())[:limit]


def _message_label(content: str, label: str) -> str:
    title = _xml_text(content, "title")
    return f"{label} {title}" if title else label


def _structured_content_summary(content: str) -> str:
    if _EMOJI_XML.search(content):
        return "[表情]"
    if _IMAGE_XML.search(content):
        return "[图片]"
    if re.search(r"<voicemsg\b", content, re.IGNORECASE):
        return "[语音]"
    if re.search(r"<videomsg\b", content, re.IGNORECASE):
        return "[视频]"
    if re.search(r"<location\b", content, re.IGNORECASE):
        return "[位置]"
    if _APP_MESSAGE_XML.search(content):
        app_type = _xml_text(content, "type")
        if app_type == "6":
            return _message_label(content, "[文件]")
        if app_type == "5":
            return _message_label(content, "[链接卡片]")
        if app_type in {"33", "36"}:
            return _message_label(content, "[小程序]")
        if app_type == "19" or re.search(
            r"<recorditem\b", content, re.IGNORECASE
        ):
            return _message_label(content, "[聊天记录]")
        return _message_label(content, "[卡片消息]")
    return "[特殊消息]"


def _quoted_content(refermsg: str) -> str:
    quote_type = _xml_text(refermsg, "type")
    label_by_type = {
        "3": "[图片]",
        "34": "[语音]",
        "42": "[名片]",
        "43": "[视频]",
        "47": "[表情]",
        "48": "[位置]",
        "49": "[卡片消息]",
    }
    if quote_type in label_by_type:
        return label_by_type[quote_type]
    return _xml_text(refermsg, "content", limit=160) or "原消息不可用"


def _message_presentation(
    value: object,
    *,
    message_kind: str = "",
    quote_sender: str = "",
    quote_content: str = "",
) -> dict[str, str]:
    content = str(value or "")
    normalized_kind = str(message_kind or "").strip()
    if normalized_kind == "quote":
        return {
            "content": content.strip() or "回复了一条消息",
            "message_kind": "quote",
            "quote_sender": str(quote_sender or "").strip(),
            "quote_content": str(quote_content or "").strip() or "原消息不可用",
        }
    if normalized_kind == "revoke":
        return {
            "content": content.strip() or "消息已撤回",
            "message_kind": "revoke",
            "quote_sender": "",
            "quote_content": "",
        }
    if not _MESSAGE_XML.search(content):
        if _LEGACY_LINK_CARD.search(content):
            readable = _HTTP_URL.sub("", content)
            readable = re.sub(r"\s*[·|]\s*$", "", readable)
            readable = " ".join(readable.split()).strip() or "[链接卡片]"
            return {
                "content": readable,
                "message_kind": "link_card",
                "quote_sender": "",
                "quote_content": "",
            }
        return {
            "content": content,
            "message_kind": "",
            "quote_sender": "",
            "quote_content": "",
        }
    if re.search(r"<revokemsg\b", content, re.IGNORECASE):
        return {
            "content": _xml_text(content, "replacemsg", limit=160)
            or "消息已撤回",
            "message_kind": "revoke",
            "quote_sender": "",
            "quote_content": "",
        }
    if _APP_MESSAGE_XML.search(content):
        app_type = _xml_text(content, "type")
        if app_type == "57" or re.search(
            r"<refermsg\b", content, re.IGNORECASE
        ):
            refermsg = _xml_fragment(content, "refermsg")
            return {
                "content": _xml_text(content, "title", limit=160)
                or "回复了一条消息",
                "message_kind": "quote",
                "quote_sender": _xml_text(refermsg, "displayname"),
                "quote_content": _quoted_content(refermsg),
            }
    return {
        "content": _structured_content_summary(content),
        "message_kind": "",
        "quote_sender": "",
        "quote_content": "",
    }


def _display_content(
    value: object,
    *,
    message_kind: str = "",
    quote_sender: str = "",
    quote_content: str = "",
) -> str:
    presentation = _message_presentation(
        value,
        message_kind=message_kind,
        quote_sender=quote_sender,
        quote_content=quote_content,
    )
    if presentation["message_kind"] == "quote":
        attribution = "引用"
        if presentation["quote_sender"]:
            attribution += f" {presentation['quote_sender']}"
        return (
            f"{presentation['content']}\n"
            f"{attribution}：{presentation['quote_content']}"
        )
    if presentation["message_kind"] == "revoke":
        return f"[撤回] {presentation['content']}"
    return presentation["content"]


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


def _history_metadata(value: object) -> dict[str, str]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result = {
        key: str(payload.get(key) or "").strip()
        for key in (
            "group_name",
            "group_nickname",
            "wechat_name",
            "wechat_id",
            "avatar",
            "message_kind",
            "quote_sender",
            "quote_content",
        )
    }
    result["media_previews"] = json.dumps(payload.get("media_previews", []))
    return result


def _media_previews(value: object) -> list[str]:
    """Only accept the bridge's bounded inline WebP thumbnails, never paths/URLs."""
    try:
        items = json.loads(str(value or "[]"))
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    result = []
    prefix = "data:image/webp;base64,"
    for item in items[:4]:
        if not isinstance(item, str) or len(item) > 88_000 or not item.startswith(prefix):
            continue
        try:
            raw = base64.b64decode(item[len(prefix):], validate=True)
        except (ValueError, binascii.Error):
            continue
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            result.append(item)
    return result


class ChatHistoryService:
    """Product history index sourced from the bridge with a legacy fallback."""

    def __init__(
        self,
        install_root: str | Path,
        *,
        local_app_data: str | Path | None = None,
    ) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        # The manager passes LOCALAPPDATA for customer installs.  Keeping the
        # legacy default makes the service useful for portable/development
        # callers while the shipped application stores history outside the
        # replaceable installation directory.
        self.path = (
            chat_history_path(local_app_data)
            if local_app_data is not None
            else self.install_root / "data" / "chat-history.db"
        )
        self.legacy_path = self.install_root / "data" / "chat-history.db"
        self.source = (
            self.install_root
            / "app"
            / "astrbot"
            / "data"
            / "plugin_data"
            / "astrbot_plugin_livingmemory"
            / "conversations.db"
        )
        self.bridge_source = (
            self.install_root
            / "app"
            / "bridge"
            / "var"
            / "message-history.db"
        )
        self._migrate_legacy_history()
        self._initialize()

    def _migrate_legacy_history(self) -> None:
        """Copy an installation-local history database to stable user data."""

        if (
            self.path == self.legacy_path
            or self.path.exists()
            or not self.legacy_path.is_file()
        ):
            return
        temporary = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            source = sqlite3.connect(self.legacy_path, timeout=5)
            target = sqlite3.connect(temporary, timeout=5)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            os.replace(temporary, self.path)
        except (OSError, sqlite3.Error):
            # A damaged or locked legacy file must not prevent startup, and a
            # partial target must not block the next migration attempt.
            temporary.unlink(missing_ok=True)

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
                    account_backfill_complete INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    account_id TEXT NOT NULL DEFAULT '',
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
                    wechat_id TEXT NOT NULL DEFAULT '',
                    avatar TEXT NOT NULL DEFAULT '',
                    message_kind TEXT NOT NULL DEFAULT '',
                    quote_sender TEXT NOT NULL DEFAULT '',
                    quote_content TEXT NOT NULL DEFAULT '',
                    media_previews TEXT NOT NULL DEFAULT '[]',
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
            for column in (
                "account_id",
                "group_name",
                "group_nickname",
                "wechat_name",
                "wechat_id",
                "avatar",
                "message_kind",
                "quote_sender",
                "quote_content",
                "media_previews",
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE messages ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            import_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(import_sources)"
                ).fetchall()
            }
            if "account_backfill_complete" not in import_columns:
                connection.execute(
                    "ALTER TABLE import_sources ADD COLUMN "
                    "account_backfill_complete INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_account_conversation "
                "ON messages(account_id, conversation_id, timestamp)"
            )

    def sync(self) -> int:
        source = self.bridge_source if self.bridge_source.is_file() else self.source
        return self._sync_source(source)

    def _sync_source(self, source_path: Path) -> int:
        if not source_path.is_file():
            return 0
        source_key = str(source_path.resolve()).casefold()
        with self._connect() as target:
            row = target.execute(
                "SELECT last_message_id, account_backfill_complete "
                "FROM import_sources WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            last_id = int(row[0]) if row else 0
            backfill_complete = bool(row[1]) if row else False
            source_uri = f"file:{source_path.resolve().as_posix()}?mode=ro"
            try:
                source = sqlite3.connect(source_uri, uri=True, timeout=2)
                source.row_factory = sqlite3.Row
                source_columns = {
                    str(column[1])
                    for column in source.execute(
                        "PRAGMA table_info(messages)"
                    ).fetchall()
                }
                account_expression = (
                    "COALESCE(account_id, '')"
                    if "account_id" in source_columns
                    else "''"
                )
                rows = source.execute(
                    f"""
                    SELECT id, {account_expression} AS account_id,
                           session_id, role, content, sender_id,
                           COALESCE(sender_name, '') AS sender_name,
                           COALESCE(group_id, '') AS group_id, timestamp,
                           COALESCE(metadata, '{{}}') AS metadata
                    FROM messages
                    WHERE id > ?
                    ORDER BY id
                    """,
                    (last_id,),
                ).fetchall()
                if "account_id" in source_columns and not backfill_complete:
                    unassigned = target.execute(
                        "SELECT source_message_id FROM messages "
                        "WHERE source_key = ? AND account_id = ''",
                        (source_key,),
                    ).fetchall()
                    source_ids = [int(item[0]) for item in unassigned]
                    for offset in range(0, len(source_ids), 500):
                        batch = source_ids[offset : offset + 500]
                        placeholders = ",".join("?" for _ in batch)
                        account_rows = source.execute(
                            "SELECT id, COALESCE(account_id, '') AS account_id "
                            f"FROM messages WHERE id IN ({placeholders})",
                            batch,
                        ).fetchall()
                        target.executemany(
                            "UPDATE messages SET account_id = ? "
                            "WHERE source_key = ? AND source_message_id = ? "
                            "AND account_id = ''",
                            (
                                (str(item["account_id"] or "").strip(), source_key, int(item["id"]))
                                for item in account_rows
                                if str(item["account_id"] or "").strip()
                            ),
                        )
                    backfill_complete = True
            except sqlite3.Error:
                return 0
            finally:
                if "source" in locals():
                    source.close()
            highest = last_id
            for item in rows:
                identity = _history_metadata(item["metadata"])
                kind, conversation_id = _plain_conversation_id(
                    str(item["session_id"]), str(item["group_id"])
                )
                target.execute(
                    """
                    INSERT OR IGNORE INTO messages (
                        source_key, source_message_id, account_id, session_id,
                        conversation_id, conversation_type, role, sender_id,
                        sender_name, group_id, group_name, group_nickname,
                        wechat_name, wechat_id, avatar, message_kind, quote_sender,
                        quote_content, media_previews, content, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_key,
                        int(item["id"]),
                        str(item["account_id"] or "").strip(),
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
                        identity.get("wechat_id", ""),
                        identity.get("avatar", ""),
                        identity.get("message_kind", ""),
                        identity.get("quote_sender", ""),
                        identity.get("quote_content", ""),
                        json.dumps(_media_previews(identity.get("media_previews"))),
                        str(item["content"]),
                        float(item["timestamp"]),
                    ),
                )
                highest = max(highest, int(item["id"]))
            target.execute(
                """
                INSERT INTO import_sources(
                    source_key, last_message_id, account_backfill_complete, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    last_message_id = excluded.last_message_id,
                    account_backfill_complete = excluded.account_backfill_complete,
                    updated_at = excluded.updated_at
                """,
                (
                    source_key,
                    highest,
                    int(backfill_complete),
                    datetime.now().astimezone().isoformat(),
                ),
            )
            return len(rows)

    def conversations(
        self,
        *,
        kind: str = "all",
        search: str = "",
        start_time: float | None = None,
        end_time: float | None = None,
        account_id: str = "",
        include_unassigned: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        self.sync()
        clauses: list[str] = []
        params: list[object] = []
        normalized_account = str(account_id or "").strip()
        if normalized_account:
            clauses.append("account_id = ?")
            params.append(normalized_account)
        elif include_unassigned:
            clauses.append("account_id = ''")
        else:
            return {
                "items": [],
                "summary": {
                    "messages": 0,
                    "conversations": 0,
                    "private_messages": 0,
                    "group_messages": 0,
                },
            }
        if kind in {"private", "group"}:
            clauses.append("conversation_type = ?")
            params.append(kind)
        if search.strip():
            term = search.strip()
            if term.casefold().startswith("wxid_"):
                clauses.append("(sender_id = ? OR wechat_id = ?)")
                params.extend((term, term))
            else:
                pattern = f"%{term}%"
                clauses.append(
                    "(content LIKE ? OR sender_name LIKE ? OR sender_id LIKE ? OR conversation_id LIKE ? OR group_name LIKE ? OR group_nickname LIKE ? OR wechat_name LIKE ? OR wechat_id LIKE ?)"
                )
                params.extend((pattern,) * 8)
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
                   MAX(group_name) AS group_name,
                   MAX(CASE WHEN conversation_type = 'private' AND role = 'user'
                            THEN avatar ELSE '' END) AS avatar
            FROM messages
            {where}
            GROUP BY conversation_id, conversation_type
            ORDER BY last_timestamp DESC
            LIMIT ?
        """
        total_params = tuple(params)
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            totals = connection.execute(
                f"""
                SELECT COUNT(*) AS messages,
                       COUNT(DISTINCT conversation_id) AS conversations,
                       SUM(CASE WHEN conversation_type = 'private' THEN 1 ELSE 0 END) AS private_messages,
                       SUM(CASE WHEN conversation_type = 'group' THEN 1 ELSE 0 END) AS group_messages
                FROM messages
                {where}
                """,
                total_params,
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
                    "avatar": row["avatar"] or "",
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
        before_id: int | None = None,
        search: str = "",
        start_time: float | None = None,
        end_time: float | None = None,
        account_id: str = "",
        include_unassigned: bool = False,
        limit: int = 200,
    ) -> tuple[dict[str, Any], ...]:
        self.sync()
        clauses = ["conversation_id = ?"]
        params: list[object] = [conversation_id]
        normalized_account = str(account_id or "").strip()
        if normalized_account:
            clauses.append("account_id = ?")
            params.append(normalized_account)
        elif include_unassigned:
            clauses.append("account_id = ''")
        else:
            return ()
        if search.strip():
            term = search.strip()
            if term.casefold().startswith("wxid_"):
                clauses.append("(sender_id = ? OR wechat_id = ?)")
                params.extend((term, term))
            else:
                pattern = f"%{term}%"
                clauses.append(
                    "(content LIKE ? OR sender_name LIKE ? OR sender_id LIKE ? OR "
                    "group_name LIKE ? OR group_nickname LIKE ? OR wechat_name LIKE ? OR "
                    "wechat_id LIKE ? OR conversation_id LIKE ?)"
                )
                params.extend((pattern,) * 8)
        if start_time is not None:
            clauses.append("timestamp >= ?")
            params.append(float(start_time))
        if end_time is not None:
            clauses.append("timestamp <= ?")
            params.append(float(end_time))
        scope_params = tuple(params)
        page_clauses = ["1 = 1"]
        if before is not None:
            if before_id is None:
                page_clauses.append("timestamp < ?")
                params.append(float(before))
            else:
                page_clauses.append("(timestamp < ? OR (timestamp = ? AND id < ?))")
                params.extend((float(before), float(before), int(before_id)))
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                WITH page AS (
                    SELECT * FROM messages
                    WHERE {' AND '.join(clauses + page_clauses)}
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                ), sender_stats AS (
                    SELECT role, sender_id, COUNT(*) AS sender_message_count,
                           MAX(timestamp) AS sender_last_timestamp
                    FROM messages
                    WHERE {' AND '.join(clauses)}
                      AND (role, sender_id) IN (SELECT role, sender_id FROM page)
                    GROUP BY role, sender_id
                )
                SELECT id, conversation_type, role, sender_id, sender_name,
                       group_id, group_name, group_nickname, wechat_name, wechat_id,
                       avatar, message_kind, quote_sender, quote_content, media_previews,
                       content, timestamp, sender_message_count, sender_last_timestamp
                FROM page JOIN sender_stats USING (role, sender_id)
                ORDER BY timestamp DESC, id DESC
                """,
                (*params, *scope_params),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in reversed(rows):
            presentation = _message_presentation(
                row["content"],
                message_kind=row["message_kind"],
                quote_sender=row["quote_sender"],
                quote_content=row["quote_content"],
            )
            messages.append({
                "id": row["id"],
                "type": row["conversation_type"],
                "role": row["role"],
                "sender_id": row["sender_id"],
                "sender_message_count": row["sender_message_count"],
                "sender_last_timestamp": row["sender_last_timestamp"],
                "sender_name": row["sender_name"] or (
                    "机器人" if row["role"] == "assistant" else row["sender_id"]
                ),
                "group_id": _plain_conversation_id("", row["group_id"])[1]
                if row["group_id"]
                else "",
                "group_name": row["group_name"],
                "group_nickname": row["group_nickname"],
                "wechat_name": row["wechat_name"],
                "wechat_id": row["wechat_id"],
                "avatar": row["avatar"],
                **presentation,
                "media_previews": _media_previews(row["media_previews"]),
                "timestamp": row["timestamp"],
            })
        return tuple(messages)

    def image_source_id(
        self, conversation_id: str, message_id: int | str, image_index: int,
        *, account_id: str,
    ) -> int:
        """Resolve a visible record to its account-scoped bridge journal row."""
        if (not isinstance(account_id, str) or not account_id
                or not isinstance(conversation_id, str) or not conversation_id
                or isinstance(message_id, bool)
                or not str(message_id).isascii() or not str(message_id).isdigit()
                or len(str(message_id)) > 18
                or isinstance(image_index, bool) or not isinstance(image_index, int)
                or not 0 <= image_index < 4):
            raise ValueError("消息图片标识无效，请刷新消息中心后重试。")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source_message_id, media_previews FROM messages "
                "WHERE id = ? AND account_id = ? AND conversation_id = ? AND source_key = ?",
                (int(message_id), account_id, conversation_id,
                 str(self.bridge_source.resolve()).casefold()),
            ).fetchone()
        if row is None or image_index >= len(_media_previews(row["media_previews"])):
            raise ValueError("这条记录没有可获取的原图，已显示缩略图。")
        return int(row["source_message_id"])

    def delete_conversation(
        self,
        conversation_id: str,
        *,
        account_id: str = "",
        include_unassigned: bool = False,
    ) -> int:
        clauses = ["conversation_id = ?"]
        params: list[object] = [conversation_id]
        normalized_account = str(account_id or "").strip()
        if normalized_account:
            clauses.append("account_id = ?")
            params.append(normalized_account)
        elif include_unassigned:
            clauses.append("account_id = ''")
        else:
            return 0
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM messages WHERE {' AND '.join(clauses)}", params
            )
            return max(0, cursor.rowcount)

    def delete_before(
        self,
        timestamp: float,
        *,
        account_id: str = "",
        include_unassigned: bool = False,
    ) -> int:
        clauses = ["timestamp < ?"]
        params: list[object] = [float(timestamp)]
        normalized_account = str(account_id or "").strip()
        if normalized_account:
            clauses.append("account_id = ?")
            params.append(normalized_account)
        elif include_unassigned:
            clauses.append("account_id = ''")
        else:
            return 0
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM messages WHERE {' AND '.join(clauses)}", params
            )
            return max(0, cursor.rowcount)

    def export(
        self,
        target: str | Path,
        *,
        format: str = "csv",
        account_id: str = "",
        include_unassigned: bool = False,
    ) -> Path:
        normalized_account = str(account_id or "").strip()
        if normalized_account:
            where = "WHERE account_id = ?"
            params = (normalized_account,)
        elif include_unassigned:
            where = "WHERE account_id = ''"
            params = ()
        else:
            raise ValueError("导出聊天记录必须指定机器人微信账号。")
        output = Path(target).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT conversation_type, conversation_id, role, sender_id,
                       sender_name, group_name, group_nickname, wechat_name, wechat_id,
                       message_kind, quote_sender, quote_content,
                       content, timestamp FROM messages
                {where}
                ORDER BY timestamp
                """,
                params,
            ).fetchall()
        if format.casefold() == "json":
            exported = []
            for row in rows:
                item = dict(row)
                item["content"] = _display_content(
                    item.get("content"),
                    message_kind=item.get("message_kind", ""),
                    quote_sender=item.get("quote_sender", ""),
                    quote_content=item.get("quote_content", ""),
                )
                exported.append(item)
            output.write_text(
                json.dumps(exported, ensure_ascii=False, indent=2)
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
                            row["wechat_id"] or row["sender_id"],
                            row["wechat_name"] or row["sender_name"],
                            row["group_nickname"],
                            _display_content(
                                row["content"],
                                message_kind=row["message_kind"],
                                quote_sender=row["quote_sender"],
                                quote_content=row["quote_content"],
                            ),
                            datetime.fromtimestamp(
                                row["timestamp"], tz=timezone.utc
                            ).astimezone().isoformat(timespec="seconds"),
                        )
                    )
        return output


__all__ = ["ChatHistoryService"]
