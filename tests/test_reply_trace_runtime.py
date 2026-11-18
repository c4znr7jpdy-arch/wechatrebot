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
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from wechatrebot_manager import reply_trace_runtime as trace
from wechatrebot_manager.response_trace import ResponseTraceService
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

    closed = []

    async def stream(**_kwargs):
        try:
            yield NS(role="assistant")
            yield NS(role="assistant")
        finally:
            closed.append(True)

    owner = NS(run_context=NS(context=NS(event=event()), messages=[]),
               _sanitize_contexts_for_provider=lambda value: value, _func_tool_for_provider=lambda: None,
               req=NS(session_id="", extra_user_content_parts=[], model=""),
               _abort_signal=None, request_max_retries=1, streaming=True,
               provider=NS(text_chat_stream=stream))

    async def run():
        responses = iterate(owner)
        await anext(responses)
        await responses.aclose()
        assert closed == [True], "underlying provider must close before wrapper.aclose returns"
        assert records(traced)[-1]["status"] == "cancelled"

    asyncio.run(run())


@pytest.fixture
def fallback_runner(patched_engine):
    source = (patched_engine / "astrbot/core/agent/runners/tool_loop_agent_runner.py").read_text("utf-8")
    iterate = _runtime_function(source, "_iter_llm_responses", {})
    fallback = _runtime_function(source, "_iter_llm_responses_with_fallback", {
        "AsyncRetrying": AsyncRetrying, "retry_if_exception_type": retry_if_exception_type,
        "stop_after_attempt": stop_after_attempt, "wait_exponential": wait_exponential,
        "EmptyModelOutputError": type("EmptyModelOutputError", (Exception,), {}),
        "LLMResponse": lambda **fields: NS(is_chunk=False, **fields),
        "logger": logging.getLogger("test.fallback"),
    })

    def create(item, *streams):
        providers = [NS(text_chat_stream=stream, provider_config={"id": str(index)})
                     for index, stream in enumerate(streams)]
        owner = NS(run_context=NS(context=NS(event=item), messages=[]),
                   _sanitize_contexts_for_provider=lambda value: value, _func_tool_for_provider=lambda: None,
                   req=NS(session_id="", extra_user_content_parts=[], model=""),
                   _abort_signal=None, request_max_retries=1, streaming=True,
                   provider=providers[0], fallback_providers=providers[1:],
                   EMPTY_OUTPUT_RETRY_ATTEMPTS=1, EMPTY_OUTPUT_RETRY_WAIT_MIN_S=0,
                   EMPTY_OUTPUT_RETRY_WAIT_MAX_S=0, _sanitize_malformed_tool_calls=lambda response: None)
        owner._iter_llm_responses = lambda **kwargs: iterate(owner, **kwargs)
        return fallback(owner)

    return create


def recorded_projection(tmp_path, caplog):
    service = ResponseTraceService(tmp_path)
    path = service.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(
        "[2026-11-18 12:00:00] [Core] [INFO] [core.wechat_reply_trace:1]: WRB_REPLY_TRACE "
        + json.dumps(value) for value in records(caplog)), encoding="utf-8")
    return service.recent()[0]


def test_real_fallback_final_finishes_model_before_consumer_return(fallback_runner, traced, tmp_path):
    closed = []

    async def stream(**_kwargs):
        try:
            yield NS(role="assistant", is_chunk=True)
            yield NS(role="assistant", is_chunk=False)
        finally:
            closed.append(True)

    async def run():
        item = event()
        trace.trace_received(item)
        responses = fallback_runner(item, stream)
        with trace.trace_stage(item, "pipeline"):
            async for response in responses:
                if not response.is_chunk:
                    assert closed == [True]
                    assert records(traced)[-1]["stage"] == "model"
                    assert records(traced)[-1]["status"] == "success"
                    with trace.trace_stage(item, "send"):
                        pass
                    break  # The caller also stops on final without advancing again.
        before_close = records(traced)
        await responses.aclose()
        assert records(traced) == before_close

    asyncio.run(run())
    assert [value["status"] for value in records(traced) if value["stage"] == "model"] == ["pending", "success"]
    projected = recorded_projection(tmp_path, traced)
    assert projected["complete"] and projected["status"] == "success"


@pytest.mark.parametrize("failure", ["error_response", "exception"])
def test_real_fallback_closes_failed_attempt_before_next_provider(fallback_runner, traced, tmp_path, failure):
    closed = []

    async def primary(**_kwargs):
        try:
            if failure == "exception":
                raise RuntimeError("provider failed")
            yield NS(role="err", is_chunk=False)
        finally:
            closed.append("primary")

    async def secondary(**_kwargs):
        assert closed == ["primary"]
        assert records(traced)[-2]["status"] == "failed"
        try:
            yield NS(role="assistant", is_chunk=True)
            yield NS(role="assistant", is_chunk=False)
        finally:
            closed.append("secondary")

    async def run():
        item = event()
        trace.trace_received(item)
        with trace.trace_stage(item, "pipeline"):
            responses = [response async for response in fallback_runner(item, primary, secondary)]
            assert [response.role for response in responses] == ["assistant", "assistant"]
            assert closed == ["primary", "secondary"]
            with trace.trace_stage(item, "send"):
                pass

    asyncio.run(run())
    assert [value["status"] for value in records(traced)
            if value["stage"] == "model" and value["phase"] == "end"] == ["failed", "success"]
    projected = recorded_projection(tmp_path, traced)
    assert projected["complete"] and projected["status"] == "success"


@pytest.mark.parametrize("cancellation", ["close_after_chunk", "cancel_during_await"])
def test_real_fallback_cancellation_closes_provider_before_pipeline(fallback_runner, traced, tmp_path, cancellation):
    closed = []

    async def run():
        waiting = asyncio.Event()

        async def stream(**_kwargs):
            try:
                yield NS(role="assistant", is_chunk=True)
                waiting.set()
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        item = event()
        trace.trace_received(item)
        responses = fallback_runner(item, stream)
        with pytest.raises(asyncio.CancelledError):
            with trace.trace_stage(item, "pipeline"):
                assert (await anext(responses)).is_chunk
                if cancellation == "close_after_chunk":
                    await responses.aclose()
                else:
                    pending = asyncio.create_task(anext(responses))
                    await waiting.wait()
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                assert closed == [True]
                assert records(traced)[-1]["stage"] == "model"
                assert records(traced)[-1]["status"] == "cancelled"
                raise asyncio.CancelledError()
        before_close = records(traced)
        await responses.aclose()
        assert records(traced) == before_close

    asyncio.run(run())
    projected = recorded_projection(tmp_path, traced)
    assert projected["complete"] and projected["status"] == "cancelled"


def test_actual_step_stop_after_chunk_closes_fallback_before_finalizing(patched_engine, fallback_runner, traced):
    source = (patched_engine / "astrbot/core/agent/runners/tool_loop_agent_runner.py").read_text("utf-8")
    step = _runtime_function(source, "step", {
        "AgentState": NS(IDLE="idle", RUNNING="running"),
        "LLMResponse": lambda **fields: NS(is_chunk=False, **fields),
    })
    closed = []

    async def stream(**_kwargs):
        try:
            yield NS(role="assistant", is_chunk=True, reasoning_content=None,
                     reasoning_signature=None, result_chain=None, completion_text="")
        finally:
            closed.append(True)

    async def process(messages, **_kwargs):
        return messages

    async def finalize(response):
        assert closed == [True]
        assert records(traced)[-1]["stage"] == "model"
        assert records(traced)[-1]["status"] == "cancelled"
        return response

    async def run():
        item = event()
        trace.trace_received(item)
        owner = NS(req=NS(conversation=None), _state="running", USER_INTERRUPTION_MESSAGE="stopped",
                   stats=NS(time_to_first_token=1), run_context=NS(messages=[]),
                   request_context_manager=NS(process=process), _transition_state=lambda state: None,
                   _simple_print_message_role=lambda *_: None, _is_stop_requested=lambda: True,
                   _iter_llm_responses_with_fallback=lambda: fallback_runner(item, stream),
                   _finalize_aborted_step=finalize)
        with trace.trace_stage(item, "pipeline"):
            assert len([response async for response in step(owner)]) == 1
        assert records(traced)[-1]["stage"] == "pipeline"

    asyncio.run(run())


def test_stream_iterator_without_aclose_is_supported(traced):
    class Responses:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def run():
        return [value async for value in trace.trace_model_responses(
            event(), NS(text_chat_stream=lambda **_: Responses()), {}, streaming=True)]

    assert asyncio.run(run()) == []
    assert records(traced)[-1]["status"] == "success"


@pytest.mark.parametrize("close_error,expected", [(ValueError("private close failure"), "failed"), (asyncio.CancelledError(), "cancelled")])
@pytest.mark.parametrize("primary", [None, RuntimeError("original private error"), asyncio.CancelledError()])
def test_stream_cleanup_records_secondary_error_and_preserves_primary(traced, close_error, expected, primary):
    class Responses:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if primary is not None:
                raise primary
            raise StopAsyncIteration

        async def aclose(self):
            self.closed = True
            raise close_error

    responses = Responses()

    async def run():
        return [value async for value in trace.trace_model_responses(
            event(), NS(text_chat_stream=lambda **_: responses), {}, streaming=True)]

    error = primary if primary is not None else close_error
    with pytest.raises(type(error)) as caught:
        asyncio.run(run())
    assert caught.value is error and responses.closed
    assert records(traced)[-1]["cleanup_status"] == expected
    assert records(traced)[-1]["status"] == (
        "cancelled" if isinstance(error, asyncio.CancelledError) else "failed")
    assert "private" not in traced.text.replace('"private"', '"kind"')


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
