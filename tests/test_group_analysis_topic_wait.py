"""Topic generation waits as a whole and degrades to a normal report placeholder."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from test_group_analysis_report import ReportConfig, modules, render_report, render_text_report  # noqa: F401


@pytest.fixture
def topic_runtime(modules):
    modules("infrastructure.analysis.utils").InfoUtils = modules("infrastructure.analysis.utils.info_utils").InfoUtils
    llm = modules("infrastructure.analysis.utils.llm_utils")
    llm._circuit_breakers.clear()
    llm.GlobalRateLimiter._instance = None
    config = ReportConfig()
    config.values.update(llm_retries=2, llm_backoff=0, llm_hard_timeout=0,
                         topic_provider_id="topic-model", llm_provider_id="topic-model",
                         enable_streaming_llm_call=False, bot_self_ids=[])
    config.get_topic_analysis_prompt = lambda: "请整理群聊话题：${messages_text}"
    provider = SimpleNamespace(provider_config={"timeout": 0.01})
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completion_text='[{"topic":"有效话题","detail":"一起讨论测试方法","contributors":["test-user"]}]')

    context = SimpleNamespace(get_provider_by_id=lambda provider_id: provider, llm_generate=generate)
    analyzer = modules("infrastructure.analysis.analyzers.topic_analyzer").TopicAnalyzer(context, config)
    messages = [{"sender": {"user_id": "test-user", "nickname": "群友"}, "time": 1,
                 "message": [{"type": "text", "data": {"text": "一起讨论测试方法"}}]}]
    return SimpleNamespace(llm=llm, config=config, provider=provider, calls=calls,
                           context=context, analyzer=analyzer, messages=messages)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("configured,provider_timeout,expected", [(0, 35, 300), (0, 450, 450), (90, 450, 90)])
def test_topic_wait_budget_reaches_sdk_and_keeps_shared_provider(topic_runtime, configured, provider_timeout, expected, streaming):
    env = topic_runtime
    env.config.values.update(llm_hard_timeout=configured, enable_streaming_llm_call=streaming)
    env.provider.provider_config["timeout"] = provider_timeout

    async def stream(**kwargs):
        response = await env.context.llm_generate(**kwargs)
        response.is_chunk = False
        yield response

    env.provider.text_chat_stream = stream
    topics, _ = asyncio.run(env.analyzer.analyze_topics(env.messages))
    assert topics[0].topic == "有效话题"
    assert env.calls[0]["timeout"] == expected
    assert env.provider.provider_config["timeout"] == provider_timeout


def test_topic_waits_past_short_chat_timeout_for_real_response(topic_runtime):
    env = topic_runtime
    generate = env.context.llm_generate

    async def slow(**kwargs):
        await asyncio.sleep(0.03)
        return await generate(**kwargs)

    env.context.llm_generate = slow
    topics, _ = asyncio.run(env.analyzer.analyze_topics(env.messages))
    assert topics[0].topic == "有效话题"
    assert len(env.calls) == 1


@pytest.mark.parametrize("outcome", ["empty", "invalid_json", "empty_fields", "timeout", "network_error"])
def test_topic_unavailable_is_normal_placeholder_but_real_faults_keep_diagnostics(topic_runtime, caplog, outcome):
    env = topic_runtime
    env.config.values["llm_retries"] = 1
    responses = {"empty": "", "invalid_json": "模型仍在整理", "empty_fields": '[{"topic":"","detail":"","contributors":[]}]'}

    async def incomplete(**_kwargs):
        if outcome == "timeout":
            raise TimeoutError("provider waiting")
        if outcome == "network_error":
            raise ConnectionError("provider disconnected")
        return SimpleNamespace(completion_text=responses[outcome])

    env.context.llm_generate = incomplete
    with caplog.at_level(logging.DEBUG):
        topics, _ = asyncio.run(env.analyzer.analyze_topics(env.messages))
    assert topics == []
    diagnostics = [record for record in caplog.records if record.levelno >= logging.WARNING or record.exc_info]
    assert bool(diagnostics) is (outcome == "network_error")


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_whole_topic_budget_cancels_and_awaits_inflight_request(topic_runtime, cancel, streaming):
    env = topic_runtime
    env.config.values.update(llm_hard_timeout=0.04, llm_retries=2,
                             enable_streaming_llm_call=streaming)
    started = asyncio.Event()
    cleaned = []

    async def blocked(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            cleaned.append(True)

    async def stream(**kwargs):
        yield await blocked(**kwargs)

    env.context.llm_generate = blocked
    env.provider.text_chat_stream = stream

    async def run():
        task = asyncio.create_task(env.analyzer.analyze_topics(env.messages))
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            topics, _ = await task
            assert topics == []
        assert cleaned == [True]
        assert env.llm.GlobalRateLimiter.get_instance().available_slots == env.llm.GlobalRateLimiter.get_instance().max_concurrency
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]

    asyncio.run(run())


def test_schema_repair_shares_topic_budget(topic_runtime):
    env = topic_runtime
    env.config.values.update(llm_hard_timeout=0.5, llm_retries=2)
    calls = []
    cleaned = []

    async def generate(**_kwargs):
        calls.append(True)
        try:
            if len(calls) > 1:
                await asyncio.Event().wait()
            return SimpleNamespace(completion_text="还没有整理成话题")
        finally:
            cleaned.append(True)

    env.context.llm_generate = generate
    topics, _ = asyncio.run(env.analyzer.analyze_topics(env.messages))
    assert topics == []
    assert len(calls) == len(cleaned) == 2  # Third schema attempt never receives a new budget.


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("outcome", ["empty", "exception", "cancel"])
def test_concurrent_topic_incomplete_is_not_trace_error_and_cancel_propagates(modules, topic_runtime, incremental, outcome):
    values = modules("domain.value_objects")
    trace_type = modules("shared.trace_context").TraceContext
    stage = modules("shared.constants").AnalysisStage
    analyzer = modules("infrastructure.analysis.llm_analyzer").LLMAnalyzer(topic_runtime.context, topic_runtime.config)

    async def incomplete(*_args):
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "exception":
            raise RuntimeError("private upstream response")
        return [], values.TokenUsage()

    analyzer.topic_analyzer.analyze_topics = incomplete
    with trace_type() as trace, trace.span(stage.LLM_ANALYSIS.value) as span:
        call = (analyzer.analyze_incremental_concurrent([], golden_quote_enabled=False)
                if incremental else analyzer.analyze_all_concurrent([], {}, user_title_enabled=False, golden_quote_enabled=False))
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(call)
        else:
            assert asyncio.run(call)[0] == []
            assert span["payload"]["topic_status"] == "模型等待未生成"
            assert not span["payload"].get("subtask_errors")
            assert "private upstream response" not in str(span["payload"])
    if outcome != "cancel":
        trace.finish()
        assert trace.status == "succeeded"


def test_current_and_legacy_empty_topics_share_safe_report_status(modules):
    for timeout_sections in ([], ["topic"]):
        rendered, analysis = render_report(modules, empty=True, timeout_sections=timeout_sections)
        for output in (rendered, render_text_report(modules, analysis)):
            assert output.count("模型等待未生成") == 1
            assert "模型请求超时" not in output
