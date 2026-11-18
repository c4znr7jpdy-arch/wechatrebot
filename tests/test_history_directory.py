"""Merged directories stay complete and stable beyond one page."""
import sqlite3
from types import SimpleNamespace

import pytest

from wechatrebot_manager.chat_history import ChatHistoryService
from test_chat_history import _bridge_source
from test_app_shell import _api


def test_directory_pages_all_history_before_merging_groups(tmp_path):
    source = _bridge_source(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute("DELETE FROM messages")
        connection.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            (i, "wxid_bot", f"hook:FriendMessage:friend-{i:03d}", "user", "searchable",
             f"friend-{i:03d}", f"Friend {i}", "", "hook", 100, "{}", str(i))
            for i in range(1, 251)
        ))
        connection.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            251, "wxid_bot", "hook:GroupMessage:old@chatroom", "user", "old searchable",
            "member", "Member", "old@chatroom", "hook", 1, "{}", "251",
        ))
    api = _api(tmp_path)
    api._history = ChatHistoryService(tmp_path)
    api._history_account_id = lambda: "wxid_bot"
    api._groups = SimpleNamespace(list=lambda _: {"account_id": "wxid_bot", "items": [
        {"group_id": "old@chatroom", "name": "Old group"},
        {"group_id": "empty@chatroom", "name": "Empty group"},
    ]})
    first = api.get_conversations({"page_size": 200})["data"]
    assert first["has_more"] and len(first["items"]) == 200
    assert first["summary"]["messages"] == 251
    assert first["summary"]["visible_conversations"] == 252
    second = api.get_conversations({"cursor": first["next_cursor"]})["data"]
    assert not second["has_more"]
    assert len(second["items"]) == 52
    assert len({item["conversation_id"] for item in first["items"] + second["items"]}) == 252
    groups = {item["conversation_id"]: item for item in second["items"] if item["type"] == "group"}
    assert groups["old@chatroom"]["message_count"] == 1
    assert groups["old@chatroom"]["last_timestamp"] == 1
    assert groups["empty@chatroom"]["message_count"] == 0
    filtered = api.get_conversations({"kind": "group"})["data"]
    assert len(filtered["items"]) == 2 and not filtered["has_more"]
    searched = api.get_conversations({"search": "old searchable"})["data"]
    assert [item["conversation_id"] for item in searched["items"]] == ["old@chatroom"]


@pytest.mark.parametrize("cursor", ["invalid", {}, {"timestamp": float("nan"), "type": "group", "id": "g"}])
def test_directory_rejects_invalid_cursor(tmp_path, cursor):
    api = _api(tmp_path)
    api._history_account_id = lambda: "wxid_bot"
    api._history = SimpleNamespace(conversations=lambda **_: {"items": [], "summary": {}})
    api._groups = SimpleNamespace(list=lambda _: {"account_id": "wxid_bot", "items": []})
    assert api.get_conversations({"cursor": cursor})["ok"] is False
