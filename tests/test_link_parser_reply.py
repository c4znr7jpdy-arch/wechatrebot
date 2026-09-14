from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_hook_bridge_outbound_policy_patch import patched_bridge, patched_modules
from test_plugin_portability import PROJECT_PLUGINS, _function, _patched


LINK = "https://v.douyin.com/example/"
GROUP = "new-room@chatroom"


class Plain:
    def __init__(self, text):
        self.text = text


class At:
    def __init__(self, qq):
        self.qq = qq


class Json:
    def __init__(self, data):
        self.data = data


class Reply:
    def __init__(self, chain):
        self.chain = chain


class Event:
    def __init__(self, *, chain=None, wake=False, group=GROUP, self_id="wxid_bot"):
        self.chain = [Plain(LINK)] if chain is None else chain
        self.message_str = "".join(seg.text for seg in self.chain if isinstance(seg, Plain))
        self.unified_msg_origin = "hook:GroupMessage:" + group
        self.is_at_or_wake_command = wake
        self.group = group
        self.self_id = self_id
        self.message_obj = SimpleNamespace(raw_message={"self_id": self_id})

    def get_messages(self):
        return self.chain

    def get_self_id(self):
        return self.self_id

    def get_group_id(self):
        return self.group

    def is_private_chat(self):
        return not self.group

    def chain_result(self, segments):
        return segments


@pytest.fixture
def parser(tmp_path):
    source = _patched(tmp_path, "astrbot_plugin_parser", "main.py")
    namespace = {
        "filter": SimpleNamespace(
            event_message_type=lambda *_: lambda method: method,
            EventMessageType=SimpleNamespace(ALL="all"),
        ),
        "re": re,
        "logger": Mock(),
        "At": At, "Plain": Plain, "Json": Json, "Reply": Reply,
        "AiocqhttpMessageEvent": Event,
        "extract_json_url": lambda data: data["url"],
    }
    on_message = _function(source, "on_message", namespace)
    result = SimpleNamespace(get_resource_id=lambda: "video-123")
    plugin = SimpleNamespace(
        cfg=SimpleNamespace(whitelist=[], blacklist=[], require_at_in_group=True),
        key_pattern_list=[("v.douyin.com", re.compile(r"https://v\.douyin\.com/\w+/"))],
        parser_map={"v.douyin.com": SimpleNamespace(parse=AsyncMock(return_value=result))},
        debouncer=SimpleNamespace(hit_link=Mock(return_value=False), hit_resource=Mock(return_value=False)),
        sender=SimpleNamespace(send_parse_result=AsyncMock()),
        arbiter=SimpleNamespace(compete=AsyncMock()),
    )
    return plugin, on_message


@pytest.mark.parametrize("fallback", [False, True], ids=["media", "text-fallback"])
@pytest.mark.parametrize("wake", [False, True], ids=["shared-link", "already-mentioned"])
def test_wechat_link_result_passes_real_outbound_policy(parser, patched_modules, tmp_path, fallback, wake):
    plugin, on_message = parser
    event = Event(wake=wake)
    policy = patched_modules.policy.OutboundGroupPolicy(tmp_path / "group-policies.json")
    sent = []

    async def send(segments):
        decision = await policy.authorize(
            event.get_self_id(), event.get_group_id(),
            delivery="reply", wake=event.is_at_or_wake_command,
        )
        assert decision.allowed, decision.message
        sent.extend(segments)

    event.send = send
    sender_source = (PROJECT_PLUGINS / "astrbot_plugin_parser/core/sender.py").read_text(encoding="utf-8")
    send_result = _function(sender_source, "send_parse_result", {"logger": Mock()})

    async def send_group(target, result, group):
        if fallback:
            return False
        await target.send(["video"])
        return True

    sender = SimpleNamespace(
        _resolve_groups=lambda result: [object()],
        _send_group=send_group,
        _build_text_fallback=lambda result: ["parsed title"],
    )
    plugin.sender.send_parse_result = lambda target, result: send_result(sender, target, result)

    asyncio.run(on_message(plugin, event))

    assert sent == (["parsed title"] if fallback else ["video"])
    assert event.is_at_or_wake_command is wake
    plugin.arbiter.compete.assert_not_awaited()


@pytest.mark.parametrize("chain", [[Json({"url": LINK})], [Plain("解析"), Reply([Plain(LINK)])]])
def test_wechat_card_and_quoted_link_are_feature_replies(parser, chain):
    plugin, on_message = parser
    event = Event(chain=chain)
    seen_wake = []

    async def send(target, result):
        seen_wake.append(target.is_at_or_wake_command)

    plugin.sender.send_parse_result.side_effect = send
    asyncio.run(on_message(plugin, event))

    assert seen_wake == [True]
    assert event.is_at_or_wake_command is False


@pytest.mark.parametrize("reason", ["ordinary", "other-bot", "whitelist", "blacklist", "duplicate-link", "duplicate-resource"])
def test_ignored_messages_do_not_send_or_change_wake_state(parser, reason):
    plugin, on_message = parser
    event = Event()
    if reason == "ordinary":
        event = Event(chain=[Plain("普通聊天")])
    elif reason == "other-bot":
        event.chain.insert(0, At("wxid_other"))
    elif reason == "whitelist":
        plugin.cfg.whitelist = ["another-session"]
    elif reason == "blacklist":
        plugin.cfg.blacklist = [event.unified_msg_origin]
    elif reason == "duplicate-link":
        plugin.debouncer.hit_link.return_value = True
    elif reason == "duplicate-resource":
        plugin.debouncer.hit_resource.return_value = True

    asyncio.run(on_message(plugin, event))

    plugin.sender.send_parse_result.assert_not_awaited()
    assert event.is_at_or_wake_command is False


@pytest.mark.parametrize("wake", [False, True])
def test_failed_delivery_restores_original_wake_state(parser, wake):
    plugin, on_message = parser
    event = Event(wake=wake)

    async def fail(target, result):
        assert target.is_at_or_wake_command is True
        raise RuntimeError("delivery failed")

    plugin.sender.send_parse_result.side_effect = fail
    with pytest.raises(RuntimeError, match="delivery failed"):
        asyncio.run(on_message(plugin, event))

    assert event.is_at_or_wake_command is wake


def test_private_link_preserves_existing_delivery_state(parser):
    plugin, on_message = parser
    event = Event(group="")
    seen_wake = []

    async def send(target, result):
        seen_wake.append(target.is_at_or_wake_command)

    plugin.sender.send_parse_result.side_effect = send
    asyncio.run(on_message(plugin, event))

    assert seen_wake == [False]
    assert event.is_at_or_wake_command is False
