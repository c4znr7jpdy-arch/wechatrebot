"""Quoted app messages must reach the existing Reply path, never file download."""
import asyncio
from html import escape
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from test_office_integration import office_bridge


PROMPT = "@姜小妹 这是啥时候的事"
BODY = "10月底前 Model 3 尾款减5000，A&B < 7000"
UNAVAILABLE = "[引用消息内容不可用]"


def quote_xml(content, kind=49, encoding="escaped", prompt=PROMPT):
    if encoding == "escaped":
        content = escape(content)
    elif encoding == "double-escaped":
        content = escape(escape(content))
    elif encoding == "cdata":
        content = f"<![CDATA[{content}]]>"
    return (
        f"<msg><appmsg><title>{escape(prompt)}</title><type>57</type>"
        f"<refermsg><type>{kind}</type><svrid>quoted-id</svrid>"
        "<chatusr>wxid_bot</chatusr><displayname>姜小妹</displayname>"
        f"<content>{content}</content></refermsg></appmsg></msg>"
    )


def prepare_quote(bridge, tmp_path, xml, image_downloaded=False):
    application = object.__new__(bridge.app.BridgeApplication)
    application.settings = NS(media_root=tmp_path, media_max_bytes=100000,
                              request_timeout_seconds=.01)
    application.state = NS(bot_nickname="姜小妹")
    application.hook = NS(download_file=AsyncMock())
    async def download_image(destination, candidates, message_id):
        if image_downloaded:
            destination.write_bytes(b"downloaded-image")
        return image_downloaded

    application._download_cdn_candidates = AsyncMock(side_effect=download_image)
    event = bridge.models.NormalizedEvent(
        kind="message", message_type=49, message_id="current-id",
        sender_id="wxid_user", conversation_id="room@chatroom", content=xml,
        raw={"data": {"content": xml}},
    )
    result = asyncio.run(application._prepare_inbound_media(event))
    application.hook.download_file.assert_not_awaited()
    return application, result


@pytest.mark.parametrize("encoding", ["escaped", "double-escaped", "cdata", "element"])
def test_nested_quote_preserves_immediate_reply_and_prompt(office_bridge, tmp_path, encoding):
    inner = quote_xml("更早的原消息，不应替换机器人回复", kind=1, prompt=BODY)
    application, result = prepare_quote(office_bridge, tmp_path, quote_xml(inner, encoding=encoding))
    assert result.quoted_message_type == 1
    assert result.quoted_content == BODY
    assert result.content == PROMPT
    assert result.quoted_sender_id == "wxid_bot"
    assert result.quoted_sender_name == "姜小妹"
    assert result.quoted_message_id == "quoted-id"
    assert result.media_path is None
    application._download_cdn_candidates.assert_not_awaited()

    result = application._ensure_quoted_wake_mention(result, "wxid_bot")
    assert result.mentions == ("wxid_bot",)
    quoted = application._build_quoted_onebot_event(result, "wxid_bot", 100)
    assert quoted["message"] == [{"type": "text", "data": {"text": BODY}}]
    assert quoted["user_id"] == "wxid_bot"
    message = office_bridge.message_codec.encode_inbound(result, "wxid_bot", 101, 100)
    assert message["message"] == [
        {"type": "at", "data": {"qq": "wxid_bot", "name": ""}},
        {"type": "reply", "data": {"id": "100"}},
        {"type": "text", "data": {"text": PROMPT}},
    ]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("content,expected", [
    ("<appmsg><type>57</type><title>机器人回复</title></appmsg>", "机器人回复"),
    ("<broken", UNAVAILABLE),
    ("", UNAVAILABLE),
    ("<appmsg><type>57</type><refermsg><title>不要提取更早的标题</title>"
     "</refermsg></appmsg>", UNAVAILABLE),
    ("<appmsg><type>6</type><title>销售表.xlsx</title></appmsg>",
     "[引用的应用消息] 销售表.xlsx"),
    ("<msg><appmsg><type>5</type><title>新闻链接</title></appmsg></msg>",
     "[引用的应用消息] 新闻链接"),
])
def test_unreadable_or_other_app_quote_keeps_prompt(office_bridge, tmp_path, content, expected):
    application, result = prepare_quote(office_bridge, tmp_path, quote_xml(content))
    assert result.content == PROMPT
    assert result.quoted_message_type == 1
    assert result.quoted_content == expected
    quoted = application._build_quoted_onebot_event(result, "wxid_bot", 100)
    assert quoted["raw_message"] == expected


@pytest.mark.parametrize("kind", [34, 43, 47, 48, 999])
def test_other_quoted_media_never_downloads_as_file(office_bridge, tmp_path, kind):
    _, result = prepare_quote(office_bridge, tmp_path, quote_xml("opaque-media", kind=kind))
    assert result.content == PROMPT
    assert result.quoted_content == UNAVAILABLE
    assert result.quoted_message_type == 1


def test_plain_text_quote_unchanged(office_bridge, tmp_path):
    _, result = prepare_quote(office_bridge, tmp_path, quote_xml("普通文字", kind=1))
    assert result.quoted_message_type == 1
    assert result.quoted_content == "普通文字"
    assert result.content == PROMPT


@pytest.mark.parametrize("downloaded", [False, True])
def test_image_quote_keeps_image_download_and_failure_hint(office_bridge, tmp_path, downloaded):
    xml = quote_xml('<msg><img cdnmidimgurl="image-id" aeskey="image-key"/></msg>', kind=3)
    application, result = prepare_quote(office_bridge, tmp_path, xml, downloaded)
    application._download_cdn_candidates.assert_awaited_once()
    candidates = application._download_cdn_candidates.await_args.args[1]
    assert candidates[0].file_id == "image-id"
    assert result.quoted_message_type == 3
    message = office_bridge.message_codec.encode_inbound(result, "wxid_bot", 101, 100)
    assert message["message"][0]["type"] == "reply"
    text = message["message"][1]["data"]["text"]
    assert ("引用图片下载失败" in text) is (not downloaded)
    assert PROMPT in text
    quoted = application._build_quoted_onebot_event(result, "wxid_bot", 100)
    assert quoted["message"][0]["type"] == ("image" if downloaded else "text")
    if downloaded:
        assert quoted["message"][0]["data"]["file"] == result.media_path.as_uri()


def test_quote_of_native_bot_reply_round_trip(office_bridge, tmp_path):
    client = object.__new__(office_bridge.app.HookClient)
    client._write = AsyncMock()
    target = NS(message_id="older-id", sender_id="wxid_user", sender_name="用户",
                content="最初的提问")
    asyncio.run(client.send_reply_text("room@chatroom", BODY, target))
    path, payload = client._write.await_args.args
    assert path == "/api/send_app_msg"
    assert payload["type"] == 57
    _, result = prepare_quote(office_bridge, tmp_path, quote_xml(payload["content"]))
    assert result.quoted_content == BODY
    assert result.content == PROMPT


def test_nested_quote_does_not_inherit_old_bot_mention(office_bridge, tmp_path):
    inner = quote_xml("旧消息", kind=1)
    application, result = prepare_quote(
        office_bridge, tmp_path, quote_xml(inner, prompt="只是引用，不点名"),
    )
    result = office_bridge.app.replace(result, mentions=("wxid_bot", "wxid_other"))
    result = application._ensure_quoted_wake_mention(result, "wxid_bot")
    assert result.mentions == ("wxid_other",)
