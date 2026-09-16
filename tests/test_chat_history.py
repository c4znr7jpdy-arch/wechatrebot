import json
import sqlite3
from pathlib import Path

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


def test_history_import_search_delete_and_no_reimport(tmp_path: Path) -> None:
    _source(tmp_path)
    service = ChatHistoryService(tmp_path)

    result = service.conversations()
    private = service.conversations(kind="private", search="张三")

    assert result["summary"]["messages"] == 3
    assert len(result["items"]) == 2
    assert private["items"][0]["display_name"] == "张三"
    assert private["items"][0]["avatar"] == "https://example.test/zhangsan.jpg"
    group = next(item for item in result["items"] if item["type"] == "group")
    assert group["display_name"] == "客户交流群"
    group_message = service.messages("123@chatroom")[0]
    assert group_message["group_name"] == "客户交流群"
    assert group_message["group_nickname"] == "客服小李"
    assert group_message["wechat_name"] == "李四"
    assert group_message["wechat_id"] == "lisi_work"
    assert service.messages("wxid_a")[0]["avatar"] == (
        "https://example.test/zhangsan.jpg"
    )
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


def test_history_prefers_bridge_journal_without_memory_plugin(tmp_path: Path) -> None:
    _bridge_source(tmp_path)
    service = ChatHistoryService(tmp_path)

    result = service.conversations(kind="group")

    assert result["summary"]["messages"] == 1
    assert result["items"][0]["conversation_id"] == "456@chatroom"
    assert result["items"][0]["display_name"] == "测试群"
    assert service.messages("456@chatroom")[0]["content"] == "点歌 发如雪"


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

    latest = service.messages("wxid_page", limit=20)
    middle = service.messages(
        "wxid_page",
        before=latest[0]["timestamp"],
        before_id=latest[0]["id"],
        limit=20,
    )
    oldest = service.messages(
        "wxid_page",
        before=middle[0]["timestamp"],
        before_id=middle[0]["id"],
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

    messages = service.messages("123@chatroom", search="wxid_b")

    assert [message["sender_id"] for message in messages] == ["wxid_b"]
    assert service.conversations(search="wxid_b")["summary"]["messages"] == 1


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

    messages = service.messages("456@chatroom")
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

    csv_path = service.export(tmp_path / "structured-history.csv")
    json_path = service.export(tmp_path / "structured-history.json", format="json")
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    json_text = json_path.read_text(encoding="utf-8")
    json_contents = [item["content"] for item in json.loads(json_text)]
    for _content, _expected, exported in structured_messages:
        assert exported in csv_text
        assert exported in json_contents
    assert "<emoji" not in csv_text and "<appmsg" not in csv_text
    assert "<emoji" not in json_text and "<appmsg" not in json_text
