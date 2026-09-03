import sqlite3
from pathlib import Path

from wechatrebot_manager.chat_history import ChatHistoryService


def _source(root: Path) -> Path:
    path = (
        root
        / "app"
        / "astrbot"
        / "data"
        / "plugin_data"
        / "astrbot_plugin_livingmemory"
        / "conversations.db"
    )
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                sender_id TEXT, sender_name TEXT, group_id TEXT, platform TEXT,
                timestamp REAL, metadata TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (1, "source:FriendMessage:wxid_a", "user", "你好", "wxid_a", "张三", "", "source", 100.0, "{}"),
                (2, "source:FriendMessage:wxid_a", "assistant", "你好呀", "bot", "", "", "source", 101.0, "{}"),
                (
                    3,
                    "source:GroupMessage:123@chatroom",
                    "user",
                    "群消息",
                    "wxid_b",
                    "李四",
                    "source:GroupMessage:123@chatroom",
                    "source",
                    102.0,
                    '{"group_name":"客户交流群","group_nickname":"客服小李","wechat_name":"李四"}',
                ),
            ),
        )
    return path


def test_history_import_search_delete_and_no_reimport(tmp_path: Path) -> None:
    _source(tmp_path)
    service = ChatHistoryService(tmp_path)

    result = service.conversations()
    private = service.conversations(kind="private", search="张三")

    assert result["summary"]["messages"] == 3
    assert len(result["items"]) == 2
    assert private["items"][0]["display_name"] == "张三"
    group = next(item for item in result["items"] if item["type"] == "group")
    assert group["display_name"] == "客户交流群"
    group_message = service.messages("123@chatroom")[0]
    assert group_message["group_name"] == "客户交流群"
    assert group_message["group_nickname"] == "客服小李"
    assert group_message["wechat_name"] == "李四"
    assert service.messages("wxid_a")[1]["sender_name"] == "机器人"
    assert service.delete_conversation("wxid_a") == 2
    assert service.conversations()["summary"]["messages"] == 1


def test_history_export_csv(tmp_path: Path) -> None:
    _source(tmp_path)
    service = ChatHistoryService(tmp_path)
    service.sync()

    target = service.export(tmp_path / "history.csv")

    text = target.read_text(encoding="utf-8-sig")
    assert "聊天类型" in text
    assert "群消息" in text
