"""Exercise patched lifecycle code without starting AstrBot or sending messages."""
import ast
import asyncio
import logging
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_plugin_portability import _patched


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "vendor/data/plugins/astrbot_plugin_angel_heart"


def method(source, name, namespace):
    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), name, "exec"), namespace)
    return namespace[name]


@pytest.fixture
def runner_step(tmp_path):
    relative = Path("astrbot/core/agent/runners/tool_loop_agent_runner.py")
    destination = tmp_path / relative
    destination.parent.mkdir(parents=True)
    destination.write_bytes((ROOT / "vendor/engine" / relative).read_bytes())
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run([
        "git", "apply", "--whitespace=error-all", "-p2",
        f"--include={relative.as_posix()}",
        str(ROOT / "patches/astrbot-4.26.6-wechat.patch"),
    ], cwd=tmp_path, check=True, capture_output=True)
    namespace = {
        "time": time, "logger": logging.getLogger(__name__),
        "AgentState": SimpleNamespace(IDLE="idle", RUNNING="running", DONE="done"),
        "_ToolExecutionInterrupted": RuntimeError,
        "AssistantMessageSegment": lambda **kw: kw,
        "ToolCallsResult": lambda **kw: SimpleNamespace(
            to_openai_messages_model=lambda: [kw["tool_calls_info"], *kw["tool_calls_result"]]),
    }
    source = destination.read_text(encoding="utf-8")
    namespace["_should_emit_llm_result"] = method(
        source, "_should_emit_llm_result", {**namespace, "LLMResponse": object})
    return method(source, "step", namespace)


@pytest.mark.parametrize("direct_result,hook_error", [(True, False), (True, True), (False, False)])
def test_tool_batch_finishes_before_done_hook(runner_step, direct_result, hook_error):
    response = SimpleNamespace(
        is_chunk=False, usage=None, role="assistant", tools_call_name=["music", "second"],
        reasoning_content=None, reasoning_signature=None, completion_text="",
        to_openai_to_calls_model=lambda: ["music", "second"],
    )
    context = SimpleNamespace(messages=[])
    request = SimpleNamespace(conversation=None, append_tool_calls_result=Mock())
    hooks = SimpleNamespace(on_agent_done=AsyncMock())
    runner = SimpleNamespace(
        req=request, _state="running", stats=SimpleNamespace(),
        run_context=context, request_context_manager=SimpleNamespace(process=AsyncMock(return_value=[])),
        _simple_print_message_role=Mock(), _is_stop_requested=lambda: False,
        tool_schema_mode="full", agent_hooks=hooks, _resolve_unconsumed_follow_ups=Mock(),
        final_llm_resp=None,
    )
    runner._transition_state = lambda state: setattr(runner, "_state", state)

    async def responses():
        yield response

    async def tools(*_):
        if direct_result:
            runner._state = "done"
        # A second tool is still executing after the first ended the loop.
        hooks.on_agent_done.assert_not_awaited()
        yield SimpleNamespace(kind="tool_call_result_blocks", tool_call_result_blocks=["music-result", "second-result"])

    async def completed(ctx, resp):
        assert ctx.messages[-2:] == ["music-result", "second-result"]
        request.append_tool_calls_result.assert_called_once()
        assert resp is response
        if hook_error:
            raise ValueError("hook failed")

    hooks.on_agent_done.side_effect = completed
    runner._iter_llm_responses_with_fallback = responses
    runner._handle_function_tools = tools

    async def run():
        assert [item async for item in runner_step(runner)] == []

    asyncio.run(run())
    if direct_result:
        hooks.on_agent_done.assert_awaited_once()
        runner._resolve_unconsumed_follow_ups.assert_called_once()
        assert runner.final_llm_resp is response
    else:
        hooks.on_agent_done.assert_not_awaited()
        runner._resolve_unconsumed_follow_ups.assert_not_called()
        assert runner.final_llm_resp is None


@pytest.mark.parametrize("history_mode", ["recorded", "empty", "error"])
@pytest.mark.parametrize("direct_result,owned_event", [(True, True), (False, True), (True, False)])
def test_completed_tool_releases_only_its_work(tmp_path, history_mode, direct_result, owned_event):
    source = _patched(tmp_path, "astrbot_plugin_angel_heart", "main.py")
    recorded = Mock()
    if history_mode == "error":
        recorded.side_effect = ValueError("history unavailable")
    namespace = {
        "AstrMessageEvent": object, "Any": object, "LLMResponse": object,
        "time": time, "logger": logging.getLogger(__name__),
        "extract_completed_agent_messages": lambda *_: [] if history_mode == "empty" else ["tool-result"],
        "serialize_agent_run_message": lambda message, **_: message,
    }
    capture = method(source, "capture_completed_agent_messages", namespace)
    # Use the real work ledger, with no network or running engine imports.
    ledger_namespace = {"__name__": __name__}
    exec(compile((PLUGIN / "core/work_ledger.py").read_text(encoding="utf-8"), "work-ledger", "exec"), ledger_namespace)
    ledger = ledger_namespace["WorkLedger"]()
    for chat, work in [("room", "first-song"), ("other-room", "other-work")]:
        ledger.start_work(chat_id=chat, work_id=work, trigger_message_id=work, trigger_summary="music", kind="assistant")
    event = SimpleNamespace(unified_msg_origin="room", get_self_id=lambda: "bot",
                            get_extra=lambda key, default=None: "first-song" if key == "angelheart_work_id" else default)
    context = SimpleNamespace(messages=[], context=SimpleNamespace(event=event if owned_event else object()))
    plugin = SimpleNamespace(angel_context=SimpleNamespace(
        work_ledger=ledger, conversation_ledger=SimpleNamespace(add_messages=recorded)))
    response = SimpleNamespace(tools_call_name=["music"] if direct_result else [])
    asyncio.run(capture(plugin, event, context, response))
    assert bool(ledger.get_active_works("room")) is not (direct_result and owned_event)
    assert len(ledger.get_active_works("other-room")) == 1
    if direct_result and owned_event:
        # The actual debounce gate must allow the follow-up without waiting 300s.
        busy = method((PLUGIN / "core/debounce_manager.py").read_text(encoding="utf-8"),
                      "_has_running_assistant_work", {"logger": logging.getLogger(__name__)})
        assert busy(SimpleNamespace(work_ledger=ledger), "room") is False
        assert ledger.get_recent_works("room")[0].result_summary == "工具调用已结束"
