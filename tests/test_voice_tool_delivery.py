"""Verify patched voice receipts and request-local duplicate protection offline."""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import json
import pathlib
import sys
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


PLUGIN = "astrbot_plugin_mimo_tts_clone"
SESSION = "wechat:GroupMessage:room"
SPEECH = dict(text="hello", emotion="neutral", voice="", style="")


@pytest.fixture
def delivery(patched_plugins):
    path = patched_plugins / PLUGIN / "core/tool_delivery.py"
    spec = importlib.util.spec_from_file_location("tested_voice_tool_delivery", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


class Event:
    def __init__(self, *, session=SESSION, user="user", extras=None):
        self.unified_msg_origin = session
        self.user = user
        self.extras = {} if extras is None else extras
        self.clear_result = Mock()
        self.send = AsyncMock(return_value=None)

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_sender_id(self):
        return self.user

    def chain_result(self, components):
        return components


class SendTool:
    name = "send_message_to_user"
    handler = None

    def __init__(self, result="original send result"):
        self.invocations = []
        self.result = result

    async def call(self, context, **kwargs):
        self.invocations.append((context, copy.deepcopy(kwargs)))
        return self.result


class ToolSet:
    def __init__(self, tools):
        self.tools = tools

    def get_tool(self, name):
        return next((tool for tool in self.tools if tool.name == name), None)

    def add_tool(self, tool):
        for index, existing in enumerate(self.tools):
            if existing.name == tool.name:
                self.tools[index] = tool
                return
        self.tools.append(tool)


def context_for(event):
    return SimpleNamespace(context=SimpleNamespace(event=event))


def plugin_for(path, *, media_type="record"):
    return SimpleNamespace(
        synthesize_text=AsyncMock(return_value=path),
        _send_audio_result=AsyncMock(return_value=media_type),
        _conversation_id=lambda event: "conversation",
        logger=Mock(),
    )


async def speak(delivery, plugin, event, **changes):
    return json.loads(await delivery.speak_once(plugin, event, **{**SPEECH, **changes}))


def protected_tool(delivery, original=None):
    original = original or SendTool()
    request = SimpleNamespace(func_tool=ToolSet([original]))
    delivery.protect_request(request)
    return request.func_tool.get_tool("send_message_to_user"), original, request


def patched_method(patched_plugins, name, **namespace):
    path = patched_plugins / PLUGIN / "main.py"
    tree = ast.parse(path.read_text("utf-8-sig"))
    node = next(node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name)
    node.decorator_list = []
    scope = {"AstrMessageEvent": object, "Any": object, "pathlib": pathlib,
             "asyncio": asyncio, **namespace}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), scope)
    return scope[name]


@pytest.mark.parametrize("media_type", ["record", "file"])
def test_success_receipt_and_same_event_normalized_retry(delivery, tmp_path, media_type):
    async def run():
        event = Event()
        output = tmp_path / "voice.wav"
        plugin = plugin_for(output, media_type=media_type)
        first = await speak(delivery, plugin, event)
        second = await speak(delivery, plugin, event, text="  hello  ", voice=" ", style=" ")
        assert first["status"] == second["status"] == "sent"
        assert first["media_type"] == media_type
        assert str(output) not in json.dumps(first, ensure_ascii=False)
        plugin.synthesize_text.assert_awaited_once()
        plugin._send_audio_result.assert_awaited_once_with(event, output, retry_as_file=False)

    asyncio.run(run())


def test_same_event_concurrent_call_does_not_synthesize_twice(delivery, tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        plugin = plugin_for(tmp_path / "voice.wav")

        async def synthesize(*args, **kwargs):
            entered.set()
            await release.wait()
            return tmp_path / "voice.wav"

        plugin.synthesize_text.side_effect = synthesize
        event = Event()
        first = asyncio.create_task(speak(delivery, plugin, event))
        await entered.wait()
        try:
            duplicate = await speak(delivery, plugin, event)
            assert duplicate["status"] == "in_progress"
        finally:
            release.set()
        assert (await first)["status"] == "sent"
        plugin.synthesize_text.assert_awaited_once()
        plugin._send_audio_result.assert_awaited_once()

    asyncio.run(run())


def test_two_users_can_synthesize_concurrently_without_sharing_state(delivery, tmp_path):
    async def run():
        both_entered, release = asyncio.Event(), asyncio.Event()
        users = []
        plugin = plugin_for(tmp_path / "voice.wav")

        async def synthesize(*args, **kwargs):
            users.append(kwargs["user_id"])
            if len(users) == 2:
                both_entered.set()
            await release.wait()
            return tmp_path / (kwargs["user_id"] + ".wav")

        plugin.synthesize_text.side_effect = synthesize
        tasks = [asyncio.create_task(speak(delivery, plugin, Event(user=user)))
                 for user in ("alice", "bob")]
        try:
            await asyncio.wait_for(both_entered.wait(), timeout=2)
        finally:
            release.set()
        assert [result["status"] for result in await asyncio.gather(*tasks)] == ["sent", "sent"]
        assert sorted(users) == ["alice", "bob"]
        assert plugin._send_audio_result.await_count == 2

    asyncio.run(run())


def test_different_inputs_in_same_event_are_independent(delivery, tmp_path):
    async def run():
        plugin, event = plugin_for(tmp_path / "voice.wav"), Event()
        for changes in ({}, {"text": "other"}, {"voice": "other"}, {"style": "soft"}, {"emotion": "happy"}):
            assert (await speak(delivery, plugin, event, **changes))["status"] == "sent"
        assert plugin.synthesize_text.await_count == 5

    asyncio.run(run())


def test_failed_synthesis_is_retryable_without_success_cache(delivery, tmp_path):
    async def run():
        plugin, event = plugin_for(tmp_path / "voice.wav"), Event()
        plugin.synthesize_text.side_effect = [RuntimeError("upstream unavailable"), tmp_path / "voice.wav"]
        failed = await speak(delivery, plugin, event)
        assert failed["status"] == "failed"
        assert failed["stage"] == "synthesis"
        plugin._send_audio_result.assert_not_awaited()
        assert (await speak(delivery, plugin, event))["status"] == "sent"
        assert plugin.synthesize_text.await_count == 2

    asyncio.run(run())


@pytest.mark.parametrize("send_error", [RuntimeError("transport interrupted"), None])
def test_uncertain_delivery_is_not_automatically_retried(delivery, tmp_path, send_error):
    async def run():
        plugin, event = plugin_for(tmp_path / "voice.wav", media_type=None), Event()
        plugin._send_audio_result.side_effect = send_error
        first = await speak(delivery, plugin, event)
        second = await speak(delivery, plugin, event)
        assert first["status"] == second["status"] == "unknown"
        assert first["stage"] == "delivery"
        plugin.synthesize_text.assert_awaited_once()
        plugin._send_audio_result.assert_awaited_once()

    asyncio.run(run())


def test_cancelled_delivery_keeps_unknown_record(delivery, tmp_path):
    async def run():
        entered = asyncio.Event()
        plugin, event = plugin_for(tmp_path / "voice.wav"), Event()

        async def send(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        plugin._send_audio_result.side_effect = send
        task = asyncio.create_task(speak(delivery, plugin, event))
        await entered.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert (await speak(delivery, plugin, event))["status"] == "unknown"
        plugin._send_audio_result.assert_awaited_once()

    asyncio.run(run())


def test_clear_result_failure_does_not_turn_success_into_failure(delivery, tmp_path):
    async def run():
        plugin, event = plugin_for(tmp_path / "voice.wav"), Event()
        event.clear_result.side_effect = RuntimeError("cleanup failed")
        assert (await speak(delivery, plugin, event))["status"] == "sent"
        assert (await speak(delivery, plugin, event))["status"] == "sent"
        plugin._send_audio_result.assert_awaited_once()

    asyncio.run(run())


def test_shallow_copied_event_extras_do_not_inherit_delivery(delivery, tmp_path):
    async def run():
        plugin, first = plugin_for(tmp_path / "voice.wav"), Event()
        assert (await speak(delivery, plugin, first))["status"] == "sent"
        second = copy.copy(first)
        assert second.extras is first.extras
        assert (await speak(delivery, plugin, second))["status"] == "sent"
        assert (await speak(delivery, plugin, first))["status"] == "sent"
        assert plugin._send_audio_result.await_count == 2

    asyncio.run(run())


@pytest.mark.parametrize("location,alias", [
    ("path", r"c:\speech\VOICE.wav"),
    ("path", "C:/SPEECH/voice.wav"),
    ("path", "file:///C:/Speech/voice.wav"),
    ("url", "file:///C:/Speech/voice.wav"),
])
@pytest.mark.parametrize("component_type", ["record", "file", "Record", "RECORD"])
def test_sent_audio_path_alias_is_suppressed_before_original_tool(delivery, alias, component_type, location):
    async def run():
        event = Event()
        await speak(delivery, plugin_for(pathlib.Path("C:/Speech/voice.wav")), event)
        tool, original, _ = protected_tool(delivery)
        result = json.loads(await tool.call(context_for(event), messages=[{"type": component_type, location: alias}]))
        assert result["status"] == "sent"
        assert original.invocations == []

    asyncio.run(run())


def test_guard_reads_delivery_state_from_each_runtime_event(delivery, tmp_path):
    async def run():
        tool, original, request = protected_tool(delivery)
        delivery.protect_request(request)
        assert request.func_tool.get_tool("send_message_to_user") is tool
        first, second = Event(user="alice"), Event(user="bob")
        output = tmp_path / "voice.wav"
        await speak(delivery, plugin_for(output), first)
        message = [{"type": "record", "path": str(output)}]
        assert json.loads(await tool.call(context_for(first), messages=message))["status"] == "sent"
        assert await tool.call(context_for(second), messages=message) == original.result
        assert len(original.invocations) == 1
        assert original.invocations[0][0].context.event is second

    asyncio.run(run())


@pytest.mark.parametrize("status", ["unknown", "in_progress"])
def test_uncertain_or_pending_audio_is_not_sent_again(delivery, tmp_path, status):
    async def run():
        output, event = tmp_path / "voice.wav", Event()
        plugin = plugin_for(output, media_type=None)
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return "record"

        task = None
        if status == "in_progress":
            plugin._send_audio_result.side_effect = send
            task = asyncio.create_task(speak(delivery, plugin, event))
            await entered.wait()
        else:
            assert (await speak(delivery, plugin, event))["status"] == "unknown"
        tool, original, _ = protected_tool(delivery)
        try:
            result = json.loads(await tool.call(context_for(event), messages=[{"type": "record", "path": str(output)}]))
            assert result["status"] in {"unknown", "in_progress"}
            assert original.invocations == []
        finally:
            release.set()
            if task is not None:
                await task

    asyncio.run(run())


@pytest.mark.parametrize("original_result", ["message sent", "error: new file does not exist"])
def test_mixed_messages_keep_new_content_and_original_result(delivery, tmp_path, original_result):
    async def run():
        output, event = tmp_path / "voice.wav", Event()
        await speak(delivery, plugin_for(output), event)
        tool, original, _ = protected_tool(delivery, SendTool(result=original_result))
        messages = [{"type": "record", "path": str(output)}, {"type": "plain", "text": "new reply"},
                    {"type": "file", "path": str(tmp_path / "new.pdf")}]
        saved = copy.deepcopy(messages)
        result = json.loads(await tool.call(context_for(event), messages=messages, session=SESSION))
        assert result["previous_audio"]
        assert result["other_messages_result"] == original_result
        assert original.invocations[0][1] == {"messages": messages[1:], "session": SESSION}
        assert messages == saved

    asyncio.run(run())


@pytest.mark.parametrize("case", ["new_event", "other_session", "different_path", "url", "plain", "invalid", "malformed_file_uri"])
def test_unrelated_send_calls_retain_original_behavior(delivery, tmp_path, case):
    async def run():
        output, event = tmp_path / "voice.wav", Event()
        await speak(delivery, plugin_for(output), event)
        tool, original, _ = protected_tool(delivery)
        messages = [{"type": "record", "path": str(output)}]
        kwargs = {"messages": messages}
        current_event = Event() if case == "new_event" else event
        if case == "other_session":
            kwargs["session"] = "wechat:GroupMessage:another-room"
        elif case == "different_path":
            messages[0]["path"] = str(tmp_path / "other.wav")
        elif case == "url":
            messages[:] = [{"type": "record", "url": "https://example.test/voice.wav"}]
        elif case == "plain":
            messages[:] = [{"type": "plain", "text": "new text"}]
        elif case == "invalid":
            kwargs["messages"] = "invalid"
        elif case == "malformed_file_uri":
            messages[:] = [{"type": "record", "url": "file://["}]
        assert await tool.call(context_for(current_event), **kwargs) == original.result
        assert original.invocations[0][1] == kwargs

    asyncio.run(run())


def test_protection_does_not_mutate_shared_toolset_or_tool(delivery):
    original, unrelated = SendTool(), SimpleNamespace(name="other_tool", handler=None)
    original_call = original.call
    shared_tools = [original, unrelated]
    shared = ToolSet(shared_tools)
    first = SimpleNamespace(func_tool=shared)
    second = SimpleNamespace(func_tool=shared)
    delivery.protect_request(first)
    delivery.protect_request(second)
    assert shared.tools is shared_tools
    assert shared.tools == [original, unrelated]
    assert original.call == original_call
    assert "call" not in original.__dict__
    assert first.func_tool is not shared and second.func_tool is not shared
    assert first.func_tool is not second.func_tool
    assert first.func_tool.tools is not shared_tools
    assert first.func_tool.tools is not second.func_tool.tools
    assert first.func_tool.get_tool("send_message_to_user") is not original
    assert second.func_tool.get_tool("send_message_to_user") is not original
    assert first.func_tool.get_tool("other_tool") is unrelated


def test_custom_handler_and_missing_toolset_are_not_replaced(delivery):
    original = SendTool()
    original.handler = object()
    request = SimpleNamespace(func_tool=ToolSet([original]))
    delivery.protect_request(request)
    assert request.func_tool.get_tool("send_message_to_user") is original
    empty = SimpleNamespace(func_tool=None)
    delivery.protect_request(empty)
    assert empty.func_tool is None


@pytest.mark.parametrize("transport,record_available,record_error,retry_as_file,expected", [
    ("local_path", True, False, False, "record"),
    ("shared_path", True, False, False, "record"),
    ("local_path", True, True, True, "file"),
    ("local_path", True, True, False, "error"),
    ("local_path", False, False, True, "file"),
    ("base64", True, True, True, "error"),
])
def test_actual_send_method_reports_transport_without_unsafe_tool_retry(
    patched_plugins, delivery, tmp_path, transport, record_available, record_error, retry_as_file, expected,
):
    record = (lambda **kwargs: {"type": "record", **kwargs}) if record_available else None
    send = patched_method(patched_plugins, "_send_audio_result", Record=record,
                          check_send_response=delivery.check_send_response,
                          File=lambda **kwargs: {"type": "file", **kwargs})
    output, event = tmp_path / "voice.wav", Event()
    if record_error:
        event.send.side_effect = [RuntimeError("send interrupted"), None]
    plugin = SimpleNamespace(
        _transport_source=lambda path: (str(path), False),
        plugin_config=SimpleNamespace(audio_transport=transport, file_fallback_enabled=True),
        _send_shared_path_audio=AsyncMock(), logger=Mock(),
    )
    if expected == "error":
        with pytest.raises(Exception):
            asyncio.run(send(plugin, event, output, retry_as_file=retry_as_file))
        assert event.send.await_count == 1
    else:
        assert asyncio.run(send(plugin, event, output, retry_as_file=retry_as_file)) == expected
        if transport == "shared_path":
            plugin._send_shared_path_audio.assert_awaited_once()
            event.send.assert_not_awaited()
        else:
            assert event.send.await_args.args[0][0]["type"] == expected


def test_no_record_and_disabled_fallback_is_not_silent_success(patched_plugins, tmp_path):
    send = patched_method(patched_plugins, "_send_audio_result", Record=None, File=Mock())
    event = Event()
    plugin = SimpleNamespace(
        _transport_source=lambda path: (str(path), False),
        plugin_config=SimpleNamespace(audio_transport="local_path", file_fallback_enabled=False),
        logger=Mock(),
    )
    with pytest.raises(Exception):
        asyncio.run(send(plugin, event, tmp_path / "voice.wav"))
    event.send.assert_not_awaited()


@pytest.mark.parametrize("response", [False, {"status": "failed"}, {"retcode": 1}, {"status": "ok", "retcode": "100"}])
def test_actual_record_send_rejects_negative_adapter_receipt(patched_plugins, delivery, tmp_path, response):
    send = patched_method(patched_plugins, "_send_audio_result",
                          Record=lambda **kwargs: kwargs, File=Mock(),
                          check_send_response=delivery.check_send_response)
    event = Event()
    event.send.return_value = response
    plugin = SimpleNamespace(
        _transport_source=lambda path: (str(path), False),
        plugin_config=SimpleNamespace(audio_transport="local_path", file_fallback_enabled=True),
        logger=Mock(),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(send(plugin, event, tmp_path / "voice.wav", retry_as_file=False))
    event.send.assert_awaited_once()


@pytest.mark.parametrize("use_named_method", [True, False])
@pytest.mark.parametrize("response", [False, {"status": "failed", "retcode": 1}, {"status": "ok", "retcode": 0}])
def test_actual_onebot_send_checks_platform_receipt(patched_plugins, delivery, use_named_method, response):
    send = patched_method(patched_plugins, "_call_onebot_message",
                          check_send_response=delivery.check_send_response)
    caller = AsyncMock(return_value=response)
    bot = SimpleNamespace(**{"send_group_msg" if use_named_method else "call_action": caller})
    operation = send(bot, is_group=True, target_id=42, source="C:/Speech/voice.wav", self_id="bot")
    if response is False or response.get("status") == "failed":
        with pytest.raises(RuntimeError):
            asyncio.run(operation)
    else:
        asyncio.run(operation)
    caller.assert_awaited_once()


def test_actual_tool_entry_uses_shared_delivery_helper(patched_plugins):
    helper = AsyncMock(return_value='{"status":"sent"}')
    entry = patched_method(patched_plugins, "mimo_tts_speak", speak_once=helper)
    plugin, event = SimpleNamespace(), Event()

    async def run():
        results = [value async for value in entry(plugin, event, **SPEECH)]
        assert results == ['{"status":"sent"}']

    asyncio.run(run())
    helper.assert_awaited_once_with(plugin, event, **SPEECH)


def test_actual_request_hook_protects_request_tools(patched_plugins):
    path = patched_plugins / PLUGIN / "main.py"
    tree = ast.parse(path.read_text("utf-8-sig"))
    hooks = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
             and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                     and call.func.id == "protect_request" for call in ast.walk(node))]
    assert len(hooks) == 1
    hook = hooks[0]
    assert any("on_llm_request" in ast.unparse(decorator) for decorator in hook.decorator_list)
    protect = Mock()
    method = patched_method(patched_plugins, hook.name, protect_request=protect, ProviderRequest=object)
    request = SimpleNamespace(func_tool=ToolSet([SendTool()]))
    asyncio.run(method(SimpleNamespace(), Event(), request))
    protect.assert_called_once_with(request)
