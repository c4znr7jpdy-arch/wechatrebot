"""Exercise mention handoff from the patched adapter through the actual core gate."""
import __future__
import ast
import asyncio
from dataclasses import dataclass, field
import itertools
import json
import logging
from pathlib import Path
import shutil
import sys
from types import MethodType, SimpleNamespace as NS
import time
from unittest.mock import AsyncMock, Mock

import pytest

from test_hot_config_runtime import _patched_runtime_source, _runtime_function
from test_plugin_portability import _patched


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "vendor/data/plugins/astrbot_plugin_angel_heart"
CORE = ROOT / "vendor/engine/astrbot/core"


class Component(NS):
    pass


class At(Component):
    pass


class AtAll(At):
    pass


class Plain(Component):
    pass


class Image(Component):
    pass


class File(Component):
    pass


class Reply(Component):
    pass


class Record(Component):
    pass


class Video(Component):
    pass


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    scope = {"__name__": __name__, "__package__": "empty_mention.roles", "json": json,
             "logger": logging.getLogger(__name__), "At": At, "AtAll": AtAll, "Plain": Plain,
             "Image": Image, "File": File, "Reply": Reply, "Record": Record, "Video": Video,
             "SecretaryDecision": NS}
    scope.update(Face=type("Face", (Component,), {}), Forward=type("Forward", (Component,), {}))
    monkeypatch.setitem(sys.modules, "empty_mention.core.utils",
                        NS(json_serialize_context=lambda *_: "ledger-snapshot"))
    # Apply every shipped AngelHeart patch, in the same order as staging.
    (tmp_path / "plugin/roles").mkdir(parents=True)
    shutil.copy2(PLUGIN / "roles/front_desk.py", tmp_path / "plugin/roles/front_desk.py")
    source = _patched(tmp_path / "plugin", "astrbot_plugin_angel_heart", "roles/front_desk.py", all_patches=True)
    front = NS()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "_activate_group_event", "_call_secretary_and_execute", "_execute_secretary_decision",
            "_process_decision_result", "_record_last_decision", "_ensure_prompt_text_for_empty_at",
        }:
            setattr(front, node.name, MethodType(_runtime_function(source, node.name, scope), front))
    ledger_scope = {"__name__": __name__}
    exec(compile((PLUGIN / "core/work_ledger.py").read_text("utf-8"), "production-ledger", "exec"), ledger_scope)
    clock = [100.0]
    ledger = ledger_scope["WorkLedger"](time_func=lambda: clock[0])
    config = NS(waiting_time=0, force_reply_when_summoned=True, direct_wake_fast_path=True,
                block_unapproved_wake_non_command=False)
    debounce = NS(get_debounce_kind=lambda _: "assistant", get_must_reply=lambda _: True,
                  get_end_message_id=lambda _: "message-1", get_leave_reply_trigger=lambda _: None,
                  start_assistant_rest=AsyncMock())
    front.context = NS(debounce_manager=debounce, work_ledger=ledger)
    front.config_manager = NS(for_chat=lambda _: config)
    front._ensure_minimum_context = AsyncMock()
    front._ensure_message_id = lambda _: "message-1"
    front._get_event_message_id = lambda _: "message-1"
    front._freeze_direct_wake_decision_context = Mock()
    front._get_decision_context_for_rewrite = lambda *_: ([], [], 0)
    front._finish_secretary_dispatch = AsyncMock()
    front.last_decisions = None
    checker = NS(_is_silenced=lambda _: False)
    checker.is_event_wake = MethodType(_runtime_function(
        (PLUGIN / "core/angel_heart_status.py").read_text("utf-8"), "is_event_wake", scope), checker)
    front.status_checker = checker

    def event(components, text=""):
        extras = {}
        item = NS(unified_msg_origin="wechat:GroupMessage:room", message_str=text,
                  message_obj=NS(message=components, message_str=text), is_at_or_wake_command=True,
                  get_self_id=lambda: "wxid_bot", get_sender_id=lambda: "wxid_user",
                  get_extra=lambda key, default=None: extras.get(key, default),
                  set_extra=lambda key, value: extras.__setitem__(key, value))
        event_source = (CORE / "platform/astr_message_event.py").read_text("utf-8")
        for name in ["get_messages", "get_message_outline", "_outline_chain"]:
            setattr(item, name, MethodType(_runtime_function(event_source, name, scope), item))
        return item

    busy = _runtime_function((PLUGIN / "core/debounce_manager.py").read_text("utf-8"),
                             "_has_running_assistant_work", scope)
    return NS(front=front, ledger=ledger, clock=clock, event=event, scope=scope,
              busy=lambda: busy(NS(work_ledger=ledger), "wechat:GroupMessage:room"))


def _converted_message(tmp_path, name):
    source = _patched_runtime_source(tmp_path, Path("astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py"))
    scope = {"time": time, "itertools": itertools, "AstrBotMessage": NS, "Group": lambda group: NS(group_id=group),
             "MessageMember": lambda user_id, nickname: NS(user_id=user_id, nickname=nickname), "At": At,
             "MessageType": NS(GROUP_MESSAGE="group", FRIEND_MESSAGE="private"),
             "logger": logging.getLogger(__name__), "ActionFailed": RuntimeError}
    convert = _runtime_function(source, "_convert_handle_message_event", scope)

    class Payload(dict):
        __getattr__ = dict.__getitem__

    payload = Payload(post_type="message", message_type="group", self_id="wxid_bot", group_id="room",
                      sender={"user_id": "wxid_user", "nickname": "用户"}, message_id="message-1",
                      message=[{"type": "at", "data": {"qq": "wxid_bot", "name": name}}])
    adapter = NS(bot=NS(call_action=AsyncMock()))
    message = asyncio.run(convert(adapter, payload))
    adapter.bot.call_action.assert_not_awaited()
    return message


@pytest.mark.parametrize("name", ["机器人", ""])
def test_wechat_adapter_empty_self_mention_reaches_core_instead_of_orphaning_work(runtime, tmp_path, name):
    message = _converted_message(tmp_path / "adapter", name)
    assert message.message_str == ""
    assert len(message.message) == 1 and message.message[0].qq == "wxid_bot"
    event = runtime.event(message.message)
    should_process = _runtime_function((PLUGIN / "main.py").read_text("utf-8"), "_should_process", runtime.scope)
    assert should_process(NS(_is_upstream_command_event=lambda _: False,
                             _is_blocked_by_provider_wake_prefix=lambda _: False,
                             _is_private_chat=lambda _: False), event) is True
    original_components = list(event.get_messages())

    class ReachedAgent(Exception):
        pass

    async def decorate(_event, request, *_args, **_kwargs):
        assert request.prompt == ("@机器人" if name else "@wxid_bot")
        raise ReachedAgent

    scope = {**runtime.scope, "dataclass": dataclass, "field": field}
    request = next(node for node in ast.parse((CORE / "provider/entities.py").read_text("utf-8")).body
                   if isinstance(node, ast.ClassDef) and node.name == "ProviderRequest")
    exec(compile(ast.Module(body=[request], type_ignores=[]), "production-request", "exec",
                 flags=__future__.annotations.compiler_flag), scope)
    scope.update(_get_quoted_message_parser_settings=lambda _: {},
                 _get_session_conv=AsyncMock(return_value=NS(history="[]")),
                 normalize_and_dedupe_strings=lambda items: items, _decorate_llm_request=decorate)
    core = _patched_runtime_source(tmp_path / "core", Path("astrbot/core/astr_main_agent.py"))
    build = _runtime_function(core, "build_main_agent", scope)

    async def run():
        await runtime.front._activate_group_event(event)
        assert runtime.busy()
        with pytest.raises(ReachedAgent):
            await build(event=event, plugin_context=NS(), provider=object(), config=NS(
                provider_wake_prefix="", provider_settings={}, file_extract_enabled=False))
        runtime.ledger.complete_work(event.unified_msg_origin, "message-1", result_summary="完成")
        assert not runtime.busy()

    asyncio.run(run())
    assert event.get_messages() == original_components
    assert event.message_obj.message_str == ""
    runtime.front._freeze_direct_wake_decision_context.assert_called_once()


@pytest.mark.parametrize("components,text,expected", [
    ([At(qq="wxid_bot", name="机器人")], "", "@机器人"),
    ([At(qq="wxid_bot", name="")], "", "@wxid_bot"),
    ([At(qq="wxid_bot", display="显示名")], "  ", "@显示名"),
    ([At(qq="wxid_bot", name="机器人"), At(qq="wxid_other", name="小明")], "", "@机器人 @小明"),
    ([At(qq="wxid_bot", name="机器人")], "在吗", "在吗"),
    ([Plain(text="")], "", ""),
    ([At(qq="", name="")], "", ""),
    *[([At(qq="wxid_bot", name="机器人"), component()], "", "")
      for component in [Image, File, Reply, Record, Video]],
])
def test_handoff_only_fills_empty_mention_text_and_preserves_original_chain(runtime, components, text, expected):
    event = runtime.event(components, text)
    original = list(components)
    asyncio.run(runtime.front._process_decision_result(
        NS(should_reply=True, reply_strategy="reply"), [], [], 0, event, event.unified_msg_origin))
    assert event.message_str == expected
    assert event.get_messages() == original
    assert event.message_obj.message_str == text


def test_handoff_component_read_failure_keeps_existing_dispatch_behavior(runtime):
    event = runtime.event([])
    event.get_messages = Mock(side_effect=ValueError("message unavailable"))
    asyncio.run(runtime.front._process_decision_result(
        NS(should_reply=True, reply_strategy="reply"), [], [], 0, event, event.unified_msg_origin))
    assert event.message_str == ""
    assert event.is_at_or_wake_command
    runtime.front._finish_secretary_dispatch.assert_awaited_once()
