"""Exercise shipped trace boundaries without starting the engine or WeChat."""
from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from wechatrebot_manager import reply_trace_runtime as trace
from test_hot_config_runtime import _runtime_function


ROOT = Path(__file__).parents[1]


def event(*, group=False, identifier=None):
    extras = {}
    return NS(trace=NS(span_id=str(identifier or uuid4())),
              get_platform_id=lambda: "aiocqhttp_hook41827",
              get_group_id=lambda: "secret@chatroom" if group else "",
              get_extra=lambda name: extras.get(name),
              set_extra=lambda name, value: extras.__setitem__(name, value))


def records(caplog):
    return [json.loads(record.getMessage().removeprefix("WRB_REPLY_TRACE "))
            for record in caplog.records if record.name == "wechatrebot.reply_trace"]


@pytest.fixture
def traced(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="wechatrebot.reply_trace")
    monkeypatch.setitem(sys.modules, "astrbot.core.wechat_reply_trace", trace)
    return caplog


@pytest.fixture(scope="module")
def patched_engine(tmp_path_factory):
    directory = tmp_path_factory.mktemp("reply-trace-engine")
    shutil.copytree(ROOT / "vendor/engine", directory, dirs_exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=directory, check=True, capture_output=True)
    for name in ("wechat", "outbound-policy", "background-permissions", "license"):
        subprocess.run(["git", "apply", "-p2", str(ROOT / f"patches/astrbot-4.26.6-{name}.patch")],
                       cwd=directory, check=True, capture_output=True)
    return directory


def test_shipped_writer_matches_authoritative_source_and_patches_compile(patched_engine):
    assert (patched_engine / "astrbot/core/wechat_reply_trace.py").read_text("utf-8") == (
        ROOT / "src/wechatrebot_manager/reply_trace_runtime.py").read_text("utf-8")
    for name in ("event_bus.py", "pipeline/scheduler.py", "wechat_reply_trace.py",
                 "agent/runners/tool_loop_agent_runner.py",
                 "platform/sources/aiocqhttp/aiocqhttp_message_event.py"):
        ast.parse((patched_engine / "astrbot/core" / name).read_text("utf-8"))


def test_concurrent_events_reuse_random_ids_without_chat_or_body(traced):
    async def run():
        gate = asyncio.Event()
        first, second = event(group=True), event(group=True)

        async def process(item):
            trace.trace_received(item)
            with trace.trace_stage(item, "pipeline"):
                with trace.trace_stage(item, "model"):
                    gate.set()
                    await asyncio.sleep(0)
                with trace.trace_context(item), trace.trace_stage(None, "send"):
                    await asyncio.sleep(0)

        await asyncio.gather(process(first), process(second))
        return {first.trace.span_id.replace("-", ""), second.trace.span_id.replace("-", "")}

    ids = asyncio.run(run())
    values = records(traced)
    assert {value["trace_id"] for value in values} == ids
    for identifier in ids:
        own = [value for value in values if value["trace_id"] == identifier]
        assert [value["sequence"] for value in own] == list(range(1, 8))
        assert [value["stage"] for value in own] == ["received", "pipeline", "model", "model", "send", "send", "pipeline"]
        assert all(value["duration_ms"] >= 0 for value in own if value["phase"] == "end")
    assert "secret" not in json.dumps(values)
    assert all(set(value) <= {"schema", "trace_id", "sequence", "operation", "kind", "stage", "phase", "status", "elapsed_ms", "duration_ms", "time"} for value in values)


def test_cancellation_and_failure_propagate_and_context_is_reset(traced):
    async def run():
        item = event()
        trace.trace_received(item)
        with pytest.raises(asyncio.CancelledError):
            with trace.trace_context(item), trace.trace_stage(None, "model"):
                raise asyncio.CancelledError("private text")
        with pytest.raises(ValueError):
            with trace.trace_stage(item, "send"):
                raise ValueError("secret body and token")
        with trace.trace_stage(None, "send"):
            pass  # No context may survive the failed operation.

    asyncio.run(run())
    values = records(traced)
    assert [value["status"] for value in values if value["phase"] == "end"] == ["cancelled", "failed"]
    assert "private text" not in json.dumps(values)
    assert "secret" not in json.dumps(values)


def test_non_wechat_events_and_empty_context_do_not_record(traced):
    for item in (None, NS(), NS(get_platform_id=lambda: "other")):
        trace.trace_received(item)
        with trace.trace_stage(item, "model"):
            pass
    assert records(traced) == []


def test_actual_adapter_dispatch_records_accepted_and_rejected_actions(patched_engine, traced):
    source = (patched_engine / "astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py").read_text("utf-8")
    dispatch = _runtime_function(source, "_dispatch_send", {"Event": dict})
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("private transport failure")

    async def run():
        item = event()
        trace.trace_received(item)
        with trace.trace_context(item):
            await dispatch(None, NS(send_private_msg=send), None, False, "123", [], "reply", True)
            with pytest.raises(RuntimeError):
                await dispatch(None, NS(send_private_msg=send), None, False, "123", [], "reply", True)

    asyncio.run(run())
    assert len(calls) == 2
    assert [value["status"] for value in records(traced) if value["phase"] == "end"] == ["success", "failed"]


def test_actual_provider_iterator_records_error_response(patched_engine, traced):
    source = (patched_engine / "astrbot/core/agent/runners/tool_loop_agent_runner.py").read_text("utf-8")
    iterate = _runtime_function(source, "_iter_llm_responses", {})

    async def request(**_kwargs):
        return NS(role="err", completion_text="private provider body")

    owner = NS(run_context=NS(context=NS(event=event()), messages=[]),
               _sanitize_contexts_for_provider=lambda value: value, _func_tool_for_provider=lambda: None,
               req=NS(session_id="private session", extra_user_content_parts=[], model="private model"),
               _abort_signal=None, request_max_retries=1, streaming=False,
               provider=NS(text_chat=request))

    async def run():
        return [value async for value in iterate(owner)]

    result = asyncio.run(run())
    assert result[0].role == "err"
    assert [value["status"] for value in records(traced)] == ["pending", "failed"]
    assert "provider body" not in traced.text and "private model" not in traced.text


def test_streaming_model_duration_excludes_consumer_send_time(traced, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(trace.time, "monotonic", lambda: clock[0])

    async def stream(**_kwargs):
        clock[0] += 2
        yield NS(role="assistant")
        clock[0] += 3
        yield NS(role="assistant")

    async def run():
        async for _ in trace.trace_model_responses(event(), NS(text_chat_stream=stream), {}, streaming=True):
            clock[0] += 100  # Downstream processing is not provider time.

    asyncio.run(run())
    values = records(traced)
    assert values[-1]["duration_ms"] == 5000
    assert values[-1]["elapsed_ms"] == 205000


def test_closing_streaming_provider_iterator_records_cancelled_immediately(patched_engine, traced):
    source = (patched_engine / "astrbot/core/agent/runners/tool_loop_agent_runner.py").read_text("utf-8")
    iterate = _runtime_function(source, "_iter_llm_responses", {})

    async def stream(**_kwargs):
        yield NS(role="assistant")
        yield NS(role="assistant")

    owner = NS(run_context=NS(context=NS(event=event()), messages=[]),
               _sanitize_contexts_for_provider=lambda value: value, _func_tool_for_provider=lambda: None,
               req=NS(session_id="", extra_user_content_parts=[], model=""),
               _abort_signal=None, request_max_retries=1, streaming=True,
               provider=NS(text_chat_stream=stream))

    async def run():
        responses = iterate(owner)
        await anext(responses)
        await responses.aclose()
        assert records(traced)[-1]["status"] == "cancelled"

    asyncio.run(run())


@pytest.mark.parametrize("error,status", [(RuntimeError("secret"), "failed"), (asyncio.CancelledError(), "cancelled")])
def test_pipeline_wrapper_preserves_exception_and_records_outcome(traced, error, status):
    async def execute(_event):
        raise error

    with pytest.raises(type(error)):
        asyncio.run(trace.trace_pipeline(NS(execute=execute), event()))
    assert records(traced)[-1]["status"] == status


def test_event_limit_bounds_log_growth_and_does_not_change_behavior(traced):
    item = event()
    for _ in range(600):
        with trace.trace_stage(item, "send"):
            pass
    assert len(records(traced)) == 512
