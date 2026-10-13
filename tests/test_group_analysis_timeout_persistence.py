"""Keep safe timeout reasons through real recovery and incremental service paths."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_group_analysis_report import ReportConfig, modules  # noqa: F401


@pytest.fixture
def dependencies(modules, monkeypatch):
    star = ModuleType("astrbot.api.star")
    star.StarTools = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "astrbot.api.star", star)
    values = modules("domain.value_objects")
    config = ReportConfig()
    config.values.update(
        topic_analysis_enabled=True, user_title_analysis_enabled=False,
        golden_quote_analysis_enabled=True, chat_quality_analysis_enabled=False,
        incremental_min_messages=1, bot_self_ids=[],
    )
    stats = values.GroupStatistics(1, 8, 1, "12:00", [], 0)
    message = values.UnifiedMessage(
        message_id="test-message", sender_id="test-user", sender_name="群友",
        group_id="test-group", text_content="一起讨论测试方法",
        contents=(values.MessageContent(values.MessageContentType.TEXT, "一起讨论测试方法"),),
        timestamp=int(time.time()),
    )
    adapter = SimpleNamespace(
        platform_id="wechat", is_group_muted=AsyncMock(return_value=False),
        fetch_messages=AsyncMock(return_value=[message]),
    )
    guard = SimpleNamespace(
        group_lock=lambda *_args: nullcontext(), llm_slot=lambda *_args: nullcontext(),
    )
    return SimpleNamespace(
        values=values, stats=stats, config=config, message=message,
        bot_manager=SimpleNamespace(get_adapter=lambda _platform: adapter),
        history=SimpleNamespace(save_analysis=AsyncMock(return_value=True)),
        statistics=SimpleNamespace(
            calculate_group_statistics=lambda _messages: stats,
            _convert_to_legacy_dict=lambda messages: messages,
        ),
        guard=guard, trace_type=modules("shared.trace_context").TraceContext,
        stage=modules("shared.constants").AnalysisStage,
        status=modules("shared.report_status"),
    )


@pytest.mark.parametrize("outcome", ["timeout", "success", "other_error"])
def test_resume_updates_timeout_reasons_and_persists_snapshot(modules, dependencies, outcome):
    deps = dependencies
    serializer = modules("application.services.analysis_serializer").AnalysisResultSerializer
    cached = serializer.serialize({
        "statistics": deps.stats, "topics": [], "user_titles": [], "user_analysis": {},
        "chat_quality_review": None, "timeout_sections": ["topic", "user_title"],
    })
    clean = {"statistics": deps.stats.to_dict(), "unified_messages": [deps.message]}
    checkpoints = SimpleNamespace(
        get_checkpoint=lambda _group, _date, stage, **_kwargs: (
            cached if stage == deps.stage.LLM_ANALYSIS.value else clean
        ),
        save_checkpoint=Mock(),
    )

    async def analyze(*_args, **_kwargs):
        if outcome != "success":
            reason = (deps.status.ANALYSIS_TIMEOUT_MESSAGE if outcome == "timeout"
                      else "upstream private response")
            deps.trace_type.current()._spans[-1]["payload"]["subtask_errors"] = [f"topic: {reason}"]
        topics = [deps.values.SummaryTopic("有效话题", ["群友"], "一起讨论测试方法")] if outcome == "success" else []
        quotes = [deps.values.GoldenQuote("一起讨论测试方法", "群友", "有用的建议")]
        return topics, [], quotes, deps.values.TokenUsage(), None

    service_type = modules("application.services.analysis_recovery_service").AnalysisRecoveryService
    service = service_type(
        deps.config, deps.bot_manager, deps.history, None,
        SimpleNamespace(analyze_all_concurrent=analyze), deps.statistics, deps.guard,
        checkpoint_store=checkpoints,
    )
    with deps.trace_type() as trace:
        result = asyncio.run(service.resume_analysis(trace.trace_id, "test-group", "wechat"))
    assert result["success"] is True
    expected = ["topic", "user_title"] if outcome == "timeout" else ["user_title"]
    assert result["analysis_result"]["timeout_sections"] == expected
    saved = checkpoints.save_checkpoint.call_args.kwargs["data"]
    assert serializer.deserialize(saved)["timeout_sections"] == expected
    assert "upstream private response" not in str(saved)


def test_incremental_timeout_survives_batch_storage_merge_and_final_report(modules, dependencies):
    deps = dependencies
    batch_type = modules("domain.entities.incremental_state").IncrementalBatch
    merge_type = modules("domain.services.incremental_merge_service").IncrementalMergeService
    stored = []

    async def save_batch(batch):
        stored.append(batch_type.from_dict(batch.to_dict()))
        return True

    async def analyze(*_args, **_kwargs):
        deps.trace_type.current()._spans[-1]["payload"]["subtask_errors"] = [
            f"topic: {deps.status.ANALYSIS_TIMEOUT_MESSAGE}",
            "golden_quote: upstream private response",
        ]
        return [], [], deps.values.TokenUsage(), None

    store = SimpleNamespace(
        get_last_analyzed_cursor=AsyncMock(return_value=(0, set())),
        save_batch=save_batch, update_last_analyzed_cursor=AsyncMock(),
        query_batches=AsyncMock(side_effect=lambda *_args: stored),
    )
    service_type = modules("application.services.incremental_analysis_service").IncrementalAnalysisService
    service = service_type(
        deps.config, deps.bot_manager, deps.history,
        SimpleNamespace(analyze_incremental_concurrent=analyze), deps.statistics,
        SimpleNamespace(analyze_user_activity=lambda *_args: {}), deps.guard,
        incremental_store=store, incremental_merge_service=merge_type(),
    )
    with deps.trace_type():
        batch_result = asyncio.run(service.execute_incremental_analysis("test-group", "wechat"))
        final_result = asyncio.run(service.execute_incremental_final_report("test-group", "wechat"))
    assert batch_result["success"] is final_result["success"] is True
    assert stored[0].timeout_sections == ["topic"]
    analysis = final_result["analysis_result"]
    assert analysis["timeout_sections"] == ["topic"]
    assert deps.status.section_unavailable_reason(analysis, "topic", True) == deps.status.ANALYSIS_TIMEOUT_MESSAGE
    assert "upstream private response" not in str(stored[0].to_dict())
    assert "upstream private response" not in str(analysis)


@pytest.mark.parametrize("raw", [None, "topic", {"topic": True}, ["topic", "private response", "topic"]])
def test_legacy_or_invalid_batch_status_is_safe(modules, raw):
    batch_type = modules("domain.entities.incremental_state").IncrementalBatch
    assert batch_type.from_dict({}).timeout_sections == []
    batch = batch_type.from_dict({"timeout_sections": raw})
    expected = ["topic"] if isinstance(raw, list) else []
    assert batch.timeout_sections == batch.to_dict()["timeout_sections"] == expected
