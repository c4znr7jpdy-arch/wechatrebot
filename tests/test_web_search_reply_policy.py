"""Exercise the patched search/voice boundary without an engine or network."""
from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
import random
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from wechatrebot_manager.reply_policy_runtime import (
    WEB_SEARCH_TURN_EXTRA,
    is_voice_message_component,
    is_web_search_turn,
    mark_web_search_turn,
    voice_reply_allowed,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNER = "astrbot/core/agent/runners/tool_loop_agent_runner.py"
DECORATOR = "astrbot/core/pipeline/result_decorate/stage.py"
SEND_TOOL = "astrbot/core/tools/message_tools.py"
POLICY = "astrbot/core/wechat_reply_policy.py"
EXECUTOR = "astrbot/core/astr_agent_tool_exec.py"
SEARCH_SOURCE = ROOT / "vendor/engine/astrbot/core/tools/web_search_tools.py"
SEARCH_TOOLS = next(
    ast.literal_eval(node.value)
    for node in ast.parse(SEARCH_SOURCE.read_text(encoding="utf-8")).body
    if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == "WEB_SEARCH_TOOL_NAMES"
            for target in node.targets)
)


class Event:
    def __init__(self):
        self.extras = {}
        self.unified_msg_origin = "wechat:GroupMessage:room@chatroom"

    def get_extra(self, name, default=None):
        return self.extras.get(name, default)

    def set_extra(self, name, value):
        self.extras[name] = value


def extract_method(source, name, namespace):
    node = next(node for node in ast.walk(ast.parse(source))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name)
    node.decorator_list = []
    exec(compile("from __future__ import annotations\n" + ast.unparse(node),
                 name, "exec"), namespace)
    return namespace[name]


@pytest.fixture(scope="module")
def engine_source(tmp_path_factory):
    directory = tmp_path_factory.mktemp("search-voice-engine")
    paths = (RUNNER, DECORATOR, SEND_TOOL, POLICY, EXECUTOR)
    for relative in paths:
        source = ROOT / "vendor/engine" / relative
        if source.exists():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    subprocess.run(["git", "init", "--quiet"], cwd=directory, check=True)
    subprocess.run([
        "git", "apply", "--whitespace=error-all", "-p2",
        *(f"--include={relative}" for relative in paths),
        str(ROOT / "patches/astrbot-4.26.6-wechat.patch"),
    ], cwd=directory, check=True, capture_output=True)
    return {relative: (directory / relative).read_text(encoding="utf-8")
            for relative in paths}


def test_runtime_policy_matches_delivered_engine(engine_source):
    assert engine_source[POLICY] == (
        ROOT / "src/wechatrebot_manager/reply_policy_runtime.py"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("tool_name", SEARCH_TOOLS)
@pytest.mark.parametrize("voice_first", [True, False])
@pytest.mark.parametrize("schema", ["full", "skills_like"])
def test_selected_search_batch_marks_before_any_tool(
    engine_source, monkeypatch, tool_name, voice_first, schema,
):
    monkeypatch.setitem(sys.modules, "astrbot.core.tools.web_search_tools",
                        SimpleNamespace(WEB_SEARCH_TOOL_NAMES=SEARCH_TOOLS))
    event = Event()
    names = ["mimo_tts_speak", tool_name]
    if not voice_first:
        names.reverse()
    tool_set = SimpleNamespace(get_tool=lambda name: object() if name in names else None)
    runner = SimpleNamespace(
        run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
        tool_schema_mode=schema, _skill_like_raw_tool_set=tool_set,
        _track_tool_call_streak=Mock(return_value=1),
    )
    response = SimpleNamespace(tools_call_name=names,
                               tools_call_args=[{}, {}], tools_call_ids=["1", "2"])
    method = extract_method(engine_source[RUNNER], "_handle_function_tools", {
        "logger": logging.getLogger(__name__), "mark_web_search_turn": mark_web_search_turn,
        "_HandleFunctionToolsResult": SimpleNamespace(from_message_chain=lambda chain: chain),
        "MessageChain": lambda **kwargs: kwargs, "Json": lambda **kwargs: kwargs,
        "time": time,
    })

    async def run():
        stream = method(runner, SimpleNamespace(func_tool=tool_set), response)
        first_tool = await anext(stream)
        assert first_tool["chain"][0]["data"]["name"] == names[0]
        assert not voice_reply_allowed(event)
        await stream.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("called,available", [
    (["mimo_tts_speak"], ["mimo_tts_speak", "web_search_tavily"]),
    (["web_search_tavily"], ["mimo_tts_speak"]),
    (["search_memes"], ["search_memes"]),
])
def test_unselected_missing_and_non_web_search_do_not_disable_voice(
    engine_source, monkeypatch, called, available,
):
    monkeypatch.setitem(sys.modules, "astrbot.core.tools.web_search_tools",
                        SimpleNamespace(WEB_SEARCH_TOOL_NAMES=SEARCH_TOOLS))
    event = Event()
    tool_set = SimpleNamespace(get_tool=lambda name: object() if name in available else None)
    runner = SimpleNamespace(
        run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
        tool_schema_mode="full", _track_tool_call_streak=Mock(return_value=1),
    )
    response = SimpleNamespace(tools_call_name=called,
                               tools_call_args=[{}], tools_call_ids=["1"])
    method = extract_method(engine_source[RUNNER], "_handle_function_tools", {
        "logger": logging.getLogger(__name__), "mark_web_search_turn": mark_web_search_turn,
        "_HandleFunctionToolsResult": SimpleNamespace(from_message_chain=lambda chain: chain),
        "MessageChain": lambda **kwargs: kwargs, "Json": lambda **kwargs: kwargs,
        "time": time,
    })

    async def run():
        stream = method(runner, SimpleNamespace(func_tool=tool_set), response)
        await anext(stream)
        assert voice_reply_allowed(event)
        await stream.aclose()

    asyncio.run(run())


def test_search_marker_stays_for_followup_tools_but_not_next_message():
    event = Event()
    mark_web_search_turn(event, ["web_search_tavily"], SEARCH_TOOLS)
    mark_web_search_turn(event, ["mimo_tts_speak"], SEARCH_TOOLS)
    assert is_web_search_turn(event)
    assert voice_reply_allowed(Event())
    assert voice_reply_allowed(SimpleNamespace())


@pytest.mark.parametrize("search_used", [False, True])
def test_core_tts_keeps_search_answer_as_text(engine_source, search_used):
    class Plain:
        def __init__(self, text):
            self.text = text

    class Record:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    event = Event()
    event.extras[WEB_SEARCH_TURN_EXTRA] = search_used
    result = SimpleNamespace(chain=[Plain("查询结果")], result_content_type="normal",
                             is_llm_result=lambda: True, use_t2i_=False)
    event.get_result = lambda: result
    event.get_platform_name = lambda: "wechat"
    event.track_temporary_local_file = Mock()
    provider = SimpleNamespace(get_audio=AsyncMock(return_value="/tmp/audio.wav"))
    session_gate = AsyncMock(return_value=True)
    stage = SimpleNamespace(
        content_safe_check_reply=False, reply_prefix="", enable_segmented_reply=False,
        tts_trigger_probability=1, show_reasoning=False, reply_with_mention=False,
        reply_with_quote=False,
        ctx=SimpleNamespace(
            plugin_manager=SimpleNamespace(context=SimpleNamespace(
                get_using_tts_provider=lambda _: provider)),
            astrbot_config={"provider_tts_settings": {
                "enable": True, "use_file_service": False, "dual_output": False,
            }, "t2i": False, "callback_api_base": ""},
        ),
    )
    process = extract_method(engine_source[DECORATOR], "process", {
        "voice_reply_allowed": voice_reply_allowed,
        "ResultContentType": SimpleNamespace(STREAMING_RESULT="stream", STREAMING_FINISH="finish"),
        "EventType": SimpleNamespace(OnDecoratingResultEvent="decorate"),
        "star_handlers_registry": SimpleNamespace(get_handlers_by_event_type=lambda *_, **__: []),
        "SessionServiceManager": SimpleNamespace(should_process_tts_request=session_gate),
        "Plain": Plain, "Record": Record, "Image": type("Image", (), {}),
        "random": random, "logger": logging.getLogger(__name__),
    })
    event.plugins_name = None

    async def run():
        assert [item async for item in process(stage, event)] == []

    asyncio.run(run())
    if search_used:
        provider.get_audio.assert_not_awaited()
        session_gate.assert_not_awaited()
        assert isinstance(result.chain[0], Plain)
        assert result.chain[0].text == "查询结果"
    else:
        provider.get_audio.assert_awaited_once_with("查询结果")
        assert isinstance(result.chain[0], Record)


@pytest.mark.parametrize("message", [
    {"type": "record", "url": "https://example.test/voice"},
    {"type": "file", "path": r"C:\voice\answer.SILK"},
    {"type": "file", "url": "https://example.test/answer%2Emp3?token=x"},
    {"type": "file", "url": "https://example.test/download", "text": "answer.wav"},
])
def test_core_send_blocks_voice_and_audio_fallback_before_media_io(engine_source, message):
    event = Event()
    event.set_extra(WEB_SEARCH_TURN_EXTRA, True)
    context = SimpleNamespace(context=SimpleNamespace(event=event))
    resolve = AsyncMock()
    tool = SimpleNamespace(_resolve_path_from_sandbox=resolve)
    call = extract_method(engine_source[SEND_TOOL], "call", {
        "voice_reply_allowed": voice_reply_allowed,
        "is_voice_message_component": is_voice_message_component,
    })
    result = asyncio.run(call(tool, context, messages=[message]))
    assert "Web-search replies must use text" in result
    resolve.assert_not_awaited()


@pytest.mark.parametrize("message", [
    {"type": "plain", "text": "voice.wav 是文件名"},
    {"type": "image", "url": "https://example.test/image.png"},
    {"type": "file", "path": "report.pdf"},
    {"type": "file", "url": "https://invalid[/report.pdf"},
    "not-a-component",
])
def test_audio_file_detection_preserves_non_voice_content(message):
    assert not is_voice_message_component(message)


@pytest.fixture
def local_executor(engine_source, monkeypatch):
    permission = AsyncMock(return_value=True)
    owner = SimpleNamespace(root_dir_name="plugin", reserved=False)
    stars = {"plugins.plugin.main": owner}
    monkeypatch.setitem(sys.modules, "astrbot.core.platform.wechat_group_policy",
                        SimpleNamespace(plugin_allowed=permission))
    monkeypatch.setitem(sys.modules, "astrbot.core.star.star", SimpleNamespace(star_map=stars))

    class FunctionTool:
        async def call(self):
            pass

    tool = FunctionTool()
    tool.active = True
    tool.handler_module_path = "plugins.plugin.main"
    tool.handler = Mock()
    tool.name = "plugin_tool"
    event = Event()
    event.send = AsyncMock()
    event.get_result = lambda: None
    context = SimpleNamespace(context=SimpleNamespace(event=event, context=object()),
                              tool_call_timeout=1)
    calls = []

    async def invoke(**kwargs):
        calls.append(kwargs)
        yield "first"
        calls.append("second side effect")
        yield "second"

    class Result:
        def __init__(self, content):
            self.content = content

    method = extract_method(engine_source[EXECUTOR], "_execute_local", {
        "FunctionTool": FunctionTool, "call_local_llm_tool": invoke, "asyncio": asyncio,
        "mcp": SimpleNamespace(types=SimpleNamespace(
            CallToolResult=Result, TextContent=lambda **kwargs: kwargs)),
        "logger": logging.getLogger(__name__),
    })
    return SimpleNamespace(permission=permission, stars=stars, owner=owner, tool=tool,
                           event=event, context=context, calls=calls, method=method)


@pytest.mark.parametrize("reason", ["revoked", "owner_removed", "inactive"])
def test_plugin_tool_rechecks_before_first_handler_side_effect(local_executor, reason):
    fixture = local_executor
    if reason == "revoked":
        fixture.permission.return_value = False
    elif reason == "owner_removed":
        fixture.stars.clear()
    else:
        fixture.tool.active = False

    async def run():
        with pytest.raises(PermissionError):
            await anext(fixture.method(None, fixture.tool, fixture.context))

    asyncio.run(run())
    assert fixture.calls == []
    fixture.event.send.assert_not_awaited()


def test_generator_tool_cannot_continue_after_permissions_are_revoked(local_executor):
    fixture = local_executor

    async def run():
        stream = fixture.method(None, fixture.tool, fixture.context)
        assert (await anext(stream)).content[0]["text"] == "first"
        fixture.permission.return_value = False
        with pytest.raises(PermissionError):
            await anext(stream)
        await stream.aclose()

    asyncio.run(run())
    assert len(fixture.calls) == 1
    fixture.event.send.assert_not_awaited()


def test_direct_tool_result_rechecks_permissions_after_awaiting_handler(local_executor):
    fixture = local_executor
    fixture.event.get_result = lambda: SimpleNamespace(chain=["pending result"])

    async def invoke(**kwargs):
        fixture.permission.return_value = False
        yield None

    fixture.method.__globals__["call_local_llm_tool"] = invoke

    async def run():
        with pytest.raises(PermissionError):
            await anext(fixture.method(None, fixture.tool, fixture.context))

    asyncio.run(run())
    fixture.event.send.assert_not_awaited()


@pytest.mark.parametrize("owner_kind", ["builtin", "reserved"])
def test_core_tools_keep_existing_permission_paths(local_executor, owner_kind):
    fixture = local_executor
    if owner_kind == "builtin":
        fixture.tool.handler_module_path = None
    else:
        fixture.owner.reserved = True

    async def run():
        return [item async for item in fixture.method(None, fixture.tool, fixture.context)]

    assert len(asyncio.run(run())) == 2
    fixture.permission.assert_not_awaited()
