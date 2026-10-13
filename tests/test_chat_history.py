import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from wechatrebot_manager.chat_history import (
    ChatHistoryService,
    _display_content,
    _message_presentation,
)


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
                (
                    1,
                    "source:FriendMessage:wxid_a",
                    "user",
                    "你好",
                    "wxid_a",
                    "张三",
                    "",
                    "source",
                    100.0,
                    '{"avatar":"https://example.test/zhangsan.jpg"}',
                ),
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
                    '{"group_name":"客户交流群","group_nickname":"客服小李","wechat_name":"李四","wechat_id":"lisi_work"}',
                ),
            ),
        )
    return path


def _bridge_source(root: Path) -> Path:
    path = root / "app" / "bridge" / "var" / "message-history.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, account_id TEXT, session_id TEXT, role TEXT, content TEXT,
                sender_id TEXT, sender_name TEXT, group_id TEXT, platform TEXT,
                timestamp REAL, metadata TEXT, dedup_key TEXT UNIQUE
            )
            """
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "wxid_bot",
                "hook_bridge:GroupMessage:456@chatroom",
                "user",
                "点歌 发如雪",
                "wxid_c",
                "王五",
                "456@chatroom",
                "hook_bridge",
                103.0,
                '{"group_name":"测试群","wechat_name":"王五"}',
                "in:message-1",
            ),
        )
    return path


def _dated_history(root: Path, account_id: str = "wxid_bot") -> ChatHistoryService:
    source = _bridge_source(root)
    cutoff = datetime(2026, 10, 12).astimezone().timestamp()
    with sqlite3.connect(source) as connection:
        connection.execute("DELETE FROM messages")
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (index, account, "hook_bridge:GroupMessage:456@chatroom", "user",
                 content, "wxid_member", "测试成员", "456@chatroom", "hook_bridge",
                 timestamp, '{"group_name":"日期清理测试群"}', f"dated:{index}")
                for index, (account, content, timestamp) in enumerate((
                    (account_id, "清理日期之前的消息", cutoff - 1),
                    (account_id, "保留日期零点的消息", cutoff),
                    (account_id, "保留后续日期的消息", cutoff + 86400),
                    ("wxid_other", "其他账号的消息", cutoff - 1),
                    ("", "未分配账号的消息", cutoff - 1),
                ), 1)
            ),
        )
    service = ChatHistoryService(root)
    service.sync()
    return service


def test_date_cleanup_preserves_boundary_other_accounts_and_source(tmp_path: Path) -> None:
    service = _dated_history(tmp_path)
    cutoff = datetime(2026, 10, 12).astimezone().timestamp()

    assert service.delete_before(cutoff, account_id="wxid_bot") == 1
    assert service.sync() == 0
    assert [item["content"] for item in service.messages(
        "456@chatroom", account_id="wxid_bot"
    )] == ["保留日期零点的消息", "保留后续日期的消息"]
    assert service.conversations(account_id="wxid_other")["summary"]["messages"] == 1
    assert service.conversations(include_unassigned=True)["summary"]["messages"] == 1
    with sqlite3.connect(service.bridge_source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5


def test_history_import_search_delete_and_no_reimport(tmp_path: Path) -> None:
    _source(tmp_path)
    service = ChatHistoryService(tmp_path)

    result = service.conversations(include_unassigned=True)
    private = service.conversations(kind="private", search="张三", include_unassigned=True)

    assert result["summary"]["messages"] == 3
    assert len(result["items"]) == 2
    assert private["items"][0]["display_name"] == "张三"
    assert private["items"][0]["avatar"] == "https://example.test/zhangsan.jpg"
    group = next(item for item in result["items"] if item["type"] == "group")
    assert group["display_name"] == "客户交流群"
    group_message = service.messages("123@chatroom", include_unassigned=True)[0]
    assert group_message["group_name"] == "客户交流群"
    assert group_message["group_nickname"] == "客服小李"
    assert group_message["wechat_name"] == "李四"
    assert group_message["wechat_id"] == "lisi_work"
    assert service.messages("wxid_a", include_unassigned=True)[0]["avatar"] == (
        "https://example.test/zhangsan.jpg"
    )
    assert service.messages("wxid_a", include_unassigned=True)[1]["sender_name"] == "机器人"
    assert service.delete_conversation("wxid_a", include_unassigned=True) == 2
    assert service.conversations(include_unassigned=True)["summary"]["messages"] == 1


def test_history_export_csv(tmp_path: Path) -> None:
    _source(tmp_path)
    service = ChatHistoryService(tmp_path)
    service.sync()

    target = service.export(tmp_path / "history.csv", include_unassigned=True)

    text = target.read_text(encoding="utf-8-sig")
    assert "聊天类型" in text
    assert "群消息" in text


def test_history_prefers_bridge_journal_without_memory_plugin(tmp_path: Path) -> None:
    _bridge_source(tmp_path)
    service = ChatHistoryService(tmp_path)

    result = service.conversations(kind="group", account_id="wxid_bot")

    assert result["summary"]["messages"] == 1
    assert result["items"][0]["conversation_id"] == "456@chatroom"
    assert result["items"][0]["display_name"] == "测试群"
    assert service.messages("456@chatroom", account_id="wxid_bot")[0]["content"] == "点歌 发如雪"


def test_empty_account_never_reads_or_mutates_all_history(tmp_path: Path) -> None:
    _bridge_source(tmp_path)
    service = ChatHistoryService(tmp_path)

    assert service.conversations()["summary"]["messages"] == 0
    assert service.messages("456@chatroom") == ()
    assert service.delete_conversation("456@chatroom") == 0
    assert service.delete_before(999.0) == 0
    with pytest.raises(ValueError, match="必须指定机器人微信账号"):
        service.export(tmp_path / "unsafe.csv")
    assert not (tmp_path / "unsafe.csv").exists()
    assert service.conversations(account_id="wxid_bot")["summary"]["messages"] == 1


def test_same_conversation_id_stays_isolated_between_robot_accounts(
    tmp_path: Path,
) -> None:
    path = _bridge_source(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                2,
                "wxid_other_bot",
                "hook_bridge:GroupMessage:456@chatroom",
                "user",
                "另一个机器人账号的消息",
                "wxid_other_member",
                "赵六",
                "456@chatroom",
                "hook_bridge",
                104.0,
                '{"group_name":"另一个账号的同标识群","wechat_name":"赵六"}',
                "in:other-account-message",
            ),
        )
    service = ChatHistoryService(tmp_path)

    first = service.messages("456@chatroom", account_id="wxid_bot")
    second = service.messages("456@chatroom", account_id="wxid_other_bot")

    assert [item["content"] for item in first] == ["点歌 发如雪"]
    assert [item["content"] for item in second] == ["另一个机器人账号的消息"]
    assert service.delete_conversation(
        "456@chatroom", account_id="wxid_bot"
    ) == 1
    assert service.messages("456@chatroom", account_id="wxid_bot") == ()
    assert [
        item["content"]
        for item in service.messages(
            "456@chatroom", account_id="wxid_other_bot"
        )
    ] == ["另一个机器人账号的消息"]

    target = service.export(
        tmp_path / "other-account.csv", account_id="wxid_other_bot"
    )
    exported = target.read_text(encoding="utf-8-sig")
    assert "另一个机器人账号的消息" in exported
    assert "点歌 发如雪" not in exported


def test_history_migrates_legacy_database_to_stable_local_data(
    tmp_path: Path,
) -> None:
    _source(tmp_path)
    stable_app_data = tmp_path / "local-app-data"
    legacy_service = ChatHistoryService(tmp_path)
    legacy_service.sync()

    stable_path = stable_app_data / "WeChatReBot" / "data" / "chat-history.db"
    # Verify the one-time migration on a customer install where LOCALAPPDATA
    # points outside the replaceable install root.
    stable_service = ChatHistoryService(tmp_path, local_app_data=stable_app_data)

    assert stable_service.path == stable_path
    assert stable_service.conversations(include_unassigned=True)["summary"]["messages"] == 3
    assert stable_path.is_file()


def test_history_messages_use_stable_twenty_item_cursor_pages(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute("DELETE FROM messages")
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    message_id,
                    "source:FriendMessage:wxid_page",
                    "user",
                    f"消息 {message_id}",
                    "wxid_page",
                    "分页好友",
                    "",
                    "source",
                    100.0,
                    "{}",
                )
                for message_id in range(1, 46)
            ),
        )
    service = ChatHistoryService(tmp_path)

    latest = service.messages("wxid_page", include_unassigned=True, limit=20)
    middle = service.messages(
        "wxid_page",
        before=latest[0]["timestamp"],
        before_id=latest[0]["id"],
        include_unassigned=True,
        limit=20,
    )
    oldest = service.messages(
        "wxid_page",
        before=middle[0]["timestamp"],
        before_id=middle[0]["id"],
        include_unassigned=True,
        limit=20,
    )

    assert [item["content"] for item in latest] == [
        f"消息 {message_id}" for message_id in range(26, 46)
    ]
    assert [item["content"] for item in middle] == [
        f"消息 {message_id}" for message_id in range(6, 26)
    ]
    assert [item["content"] for item in oldest] == [
        f"消息 {message_id}" for message_id in range(1, 6)
    ]


def test_structured_wechat_messages_have_readable_summaries() -> None:
    samples = (
        ('<msg><emoji md5="abc" /></msg>', "[表情]"),
        ('<msg><img cdnmidimgurl="abc" /></msg>', "[图片]"),
        ('<msg><voicemsg voicelength="1200" /></msg>', "[语音]"),
        ('<msg><videomsg playlength="4" /></msg>', "[视频]"),
        ('<msg><location poiname="西湖景区" /></msg>', "[位置]"),
        (
            '<msg><appmsg><title><![CDATA[报价单.pdf]]></title><type>6</type></appmsg></msg>',
            "[文件] 报价单.pdf",
        ),
        (
            '<msg><appmsg><title>项目说明</title><type>5</type></appmsg></msg>',
            "[链接卡片] 项目说明",
        ),
        (
            '<msg><appmsg><title>门店助手</title><type>33</type></appmsg></msg>',
            "[小程序] 门店助手",
        ),
        (
            '<msg><appmsg><title>合并聊天记录</title><type>19</type><recorditem /></appmsg></msg>',
            "[聊天记录] 合并聊天记录",
        ),
        (
            '<msg><appmsg><title>收到，我来处理</title><type>57</type>'
            '<refermsg><type>1</type><displayname>张三</displayname>'
            '<content><![CDATA[周六算你的]]></content></refermsg></appmsg></msg>',
            "收到，我来处理\n引用 张三：周六算你的",
        ),
        (
            '<msg><appmsg><title>活动邀请</title><type>2001</type></appmsg></msg>',
            "[卡片消息] 活动邀请",
        ),
        (
            '<sysmsg type="revokemsg"><revokemsg><msgid>1</msgid>'
            '<replacemsg><![CDATA[“张三”撤回了一条消息]]></replacemsg>'
            '</revokemsg></sysmsg>',
            "[撤回] “张三”撤回了一条消息",
        ),
        ('<msg><unknown value="1" /></msg>', "[特殊消息]"),
        ("普通文本", "普通文本"),
        (
            "[链接卡片]最新短剧推荐 · 点击查看封面 · https://example.test/player?token=secret",
            "[链接卡片]最新短剧推荐 · 点击查看封面",
        ),
    )

    for content, expected in samples:
        assert _display_content(content) == expected


def test_group_message_search_filters_members_not_only_conversation(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                4,
                "source:GroupMessage:123@chatroom",
                "user",
                "另一位群友的消息",
                "wxid_other",
                "王五",
                "source:GroupMessage:123@chatroom",
                "source",
                103.0,
                '{"group_name":"客户交流群","group_nickname":"王五"}',
            ),
        )
    service = ChatHistoryService(tmp_path)

    messages = service.messages("123@chatroom", search="wxid_b", include_unassigned=True)

    assert [message["sender_id"] for message in messages] == ["wxid_b"]
    assert service.conversations(search="wxid_b", include_unassigned=True)["summary"]["messages"] == 1


def test_quote_and_revoke_presentations_expose_structured_fields() -> None:
    quote = _message_presentation(
        '<msg><appmsg><title>好的</title><type>57</type><refermsg>'
        '<type>47</type><displayname>李四</displayname><content>ignored</content>'
        '</refermsg></appmsg></msg>'
    )
    revoke = _message_presentation(
        '<sysmsg type="revokemsg"><revokemsg>'
        '<replacemsg>李四撤回了一条消息</replacemsg>'
        '</revokemsg></sysmsg>'
    )

    assert quote == {
        "content": "好的",
        "message_kind": "quote",
        "quote_sender": "李四",
        "quote_content": "[表情]",
    }
    assert revoke == {
        "content": "李四撤回了一条消息",
        "message_kind": "revoke",
        "quote_sender": "",
        "quote_content": "",
    }


def test_history_hides_structured_xml_in_views_and_exports(tmp_path: Path) -> None:
    path = _bridge_source(tmp_path)
    structured_messages = (
        ('<msg><emoji md5="abc" cdnurl="https://example.invalid/emoji" /></msg>', "[表情]", "[表情]"),
        (
            '<msg><appmsg><title>季度报价.xlsx</title><type>6</type><appattach /></appmsg></msg>',
            "[文件] 季度报价.xlsx",
            "[文件] 季度报价.xlsx",
        ),
        (
            '<msg><appmsg><title>本周汇总</title><type>19</type><recorditem /></appmsg></msg>',
            "[聊天记录] 本周汇总",
            "[聊天记录] 本周汇总",
        ),
        (
            '<msg><appmsg><title>我来确认</title><type>57</type><refermsg>'
            '<type>1</type><displayname>赵六</displayname>'
            '<content>请确认交付时间</content></refermsg></appmsg></msg>',
            "我来确认",
            "我来确认\n引用 赵六：请确认交付时间",
        ),
        (
            '<sysmsg type="revokemsg"><revokemsg>'
            '<replacemsg>赵六撤回了一条消息</replacemsg>'
            '</revokemsg></sysmsg>',
            "赵六撤回了一条消息",
            "[撤回] 赵六撤回了一条消息",
        ),
    )
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    index,
                    "wxid_bot",
                    "hook_bridge:GroupMessage:456@chatroom",
                    "user",
                    content,
                    "wxid_c",
                    "王五",
                    "456@chatroom",
                    "hook_bridge",
                    102.0 + index,
                    '{"group_name":"测试群","wechat_name":"王五"}',
                    f"in:message-{index}",
                )
                for index, (content, _expected, _exported) in enumerate(
                    structured_messages, start=2
                )
            ),
        )
    service = ChatHistoryService(tmp_path)

    messages = service.messages("456@chatroom", account_id="wxid_bot")
    contents = [item["content"] for item in messages]
    for _content, expected, _exported in structured_messages:
        assert expected in contents
    quote_message = next(item for item in messages if item["message_kind"] == "quote")
    assert quote_message["content"] == "我来确认"
    assert quote_message["quote_sender"] == "赵六"
    assert quote_message["quote_content"] == "请确认交付时间"
    assert next(item for item in messages if item["message_kind"] == "revoke")[
        "content"
    ] == "赵六撤回了一条消息"

    csv_path = service.export(tmp_path / "structured-history.csv", account_id="wxid_bot")
    json_path = service.export(
        tmp_path / "structured-history.json", format="json", account_id="wxid_bot"
    )
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    json_text = json_path.read_text(encoding="utf-8")
    json_contents = [item["content"] for item in json.loads(json_text)]
    for _content, _expected, exported in structured_messages:
        assert exported in csv_text
        assert exported in json_contents
    assert "<emoji" not in csv_text and "<appmsg" not in csv_text
    assert "<emoji" not in json_text and "<appmsg" not in json_text


def test_sender_statistics_cover_all_filtered_messages_not_just_current_page(tmp_path):
    source = _bridge_source(tmp_path)
    with sqlite3.connect(source) as connection:
        original = connection.execute("SELECT * FROM messages WHERE id=1").fetchone()
        for index in range(2, 35):
            row = list(original)
            row[0] = index
            row[5] = "wxid_c" if index < 30 else "wxid_other"
            row[9] = float(100 + index)
            row[11] = "incoming-" + str(index)
            connection.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        row = list(original)
        row[0], row[1], row[11] = 35, "another_bot", "another-account"
        connection.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
    service = ChatHistoryService(tmp_path)
    page = service.messages("456@chatroom", account_id="wxid_bot", limit=3)
    assert len(page) == 3
    assert all(message["sender_message_count"] == 5 for message in page)
    older = service.messages("456@chatroom", account_id="wxid_bot", before=129, limit=3)
    assert all(message["sender_message_count"] == 29 for message in older)
    filtered = service.messages("456@chatroom", account_id="wxid_bot", start_time=125, limit=20)
    assert {message["sender_id"]: message["sender_message_count"] for message in filtered} == {"wxid_c": 5, "wxid_other": 5}
    assert service.messages("456@chatroom", account_id="missing") == ()
