import sqlite3
from pathlib import Path

from wechatrebot_manager.chat_history import ChatHistoryService, _display_content


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
                    '{"group_name":"客户交流群","group_nickname":"客服小李","wechat_name":"李四"}',
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
            '<msg><appmsg><title>收到，我来处理</title><type>57</type><refermsg /></appmsg></msg>',
            "[引用回复] 收到，我来处理",
        ),
        (
            '<msg><appmsg><title>活动邀请</title><type>2001</type></appmsg></msg>',
            "[卡片消息] 活动邀请",
        ),
        ('<sysmsg><revokemsg><msgid>1</msgid></revokemsg></sysmsg>', "[撤回消息]"),
        ('<msg><unknown value="1" /></msg>', "[特殊消息]"),
        ("普通文本", "普通文本"),
    )

    for content, expected in samples:
        assert _display_content(content) == expected


def test_history_hides_structured_xml_in_views_and_exports(tmp_path: Path) -> None:
    path = _bridge_source(tmp_path)
    structured_messages = (
        ('<msg><emoji md5="abc" cdnurl="https://example.invalid/emoji" /></msg>', "[表情]"),
        (
            '<msg><appmsg><title>季度报价.xlsx</title><type>6</type><appattach /></appmsg></msg>',
            "[文件] 季度报价.xlsx",
        ),
        (
            '<msg><appmsg><title>本周汇总</title><type>19</type><recorditem /></appmsg></msg>',
            "[聊天记录] 本周汇总",
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
                for index, (content, _expected) in enumerate(
                    structured_messages, start=2
                )
            ),
        )
    service = ChatHistoryService(tmp_path)

    contents = [item["content"] for item in service.messages("456@chatroom")]
    for _content, expected in structured_messages:
        assert expected in contents

    csv_path = service.export(tmp_path / "structured-history.csv")
    json_path = service.export(tmp_path / "structured-history.json", format="json")
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    json_text = json_path.read_text(encoding="utf-8")
    for _content, expected in structured_messages:
        assert expected in csv_text
        assert expected in json_text
    assert "<emoji" not in csv_text and "<appmsg" not in csv_text
    assert "<emoji" not in json_text and "<appmsg" not in json_text
