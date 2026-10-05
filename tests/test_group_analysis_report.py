"""Exercise the delivered analysis pipeline without starting AstrBot or calling a model."""
from __future__ import annotations

import asyncio
import ast
import __future__
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "runtime/astrbot_plugin_qq_group_daily_analysis"
PACKAGE = "group_analysis_test_runtime"


@pytest.fixture
def modules(monkeypatch):
    # Namespace parents avoid importing the host's entrypoint and unrelated
    # optional renderers. Each module below is the real delivered source.
    for relative in ("", "infrastructure", "infrastructure.analysis",
                     "infrastructure.analysis.utils", "infrastructure.reporting",
                     "infrastructure.visualization", "application", "application.services"):
        name = PACKAGE + ("." + relative if relative else "")
        package = ModuleType(name)
        package.__path__ = [str(PLUGIN / "src" / relative.replace(".", "/"))]
        monkeypatch.setitem(sys.modules, name, package)
    provider = ModuleType("astrbot.api.provider")
    provider.LLMResponse = SimpleNamespace
    monkeypatch.setitem(sys.modules, "astrbot.api.provider", provider)
    return lambda name: importlib.import_module(PACKAGE + "." + name)


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_empty_model_response_retries_then_uses_configured_fallback(modules, empty):
    llm = modules("infrastructure.analysis.utils.llm_utils")
    llm._circuit_breakers.clear()
    llm.GlobalRateLimiter._instance = None
    calls = []

    class Context:
        def get_provider_by_id(self, provider_id):
            return SimpleNamespace(id=provider_id, provider_config={"model": provider_id})

        async def llm_generate(self, **kwargs):
            pid = kwargs["chat_provider_id"]
            calls.append(pid)
            if pid == "topic-model":
                return None if empty is None else SimpleNamespace(completion_text=empty)
            return SimpleNamespace(completion_text='[{"topic":"有效话题"}]')

    config = SimpleNamespace(
        get_llm_retries=lambda: 2, get_llm_backoff=lambda: 0,
        get_llm_hard_timeout=lambda: 10, get_enable_streaming_llm_call=lambda: False,
        get_topic_provider_id=lambda: "topic-model", get_llm_provider_id=lambda: "main-model",
    )
    result = asyncio.run(llm.call_provider_with_retry(
        Context(), config, "整理群聊话题", provider_id_key="topic_provider_id",
    ))
    assert result.completion_text == '[{"topic":"有效话题"}]'
    assert calls == ["topic-model", "topic-model", "main-model"]


class ReportConfig:
    def __init__(self):
        schema = json.loads((PLUGIN / "_conf_schema.json").read_text("utf-8"))
        self.values = {key: field.get("default") for section in schema.values()
                       for key, field in section.get("items", {}).items()}

    def __getattr__(self, name):
        key = name.removeprefix("get_")
        if key in self.values:
            return lambda: self.values[key]
        if name in {"get_t2i_google_fonts_mirror", "get_t2i_gstatic_mirror",
                    "get_t2i_atri_font_mirror"}:
            return lambda: ""
        if name == "get_custom_report_template_dir":
            return lambda *args: None
        raise AttributeError(name)


class Avatars:
    async def get_user_avatar(self, *args):
        return ""

    def register_reusable_avatar(self, *args, **kwargs):
        pass

    def get_avatar_cache_key(self, user_id, namespace):
        return str(user_id)


def render_report(modules, days=7, empty=False, disabled=False):
    values = modules("domain.value_objects")
    period = modules("shared.report_period")
    config = ReportConfig()
    config.values["report_template"] = "scrapbook"
    config.values["topic_analysis_enabled"] = not disabled
    templates = modules("infrastructure.reporting.templates").HTMLTemplates(config)
    visualizer = modules("infrastructure.visualization.activity_charts").ActivityVisualizer()
    preparer = modules("infrastructure.reporting.render_data_preparer").RenderDataPreparer(
        config, Avatars(), templates, visualizer,
    )
    stats = values.GroupStatistics(
        message_count=591, participant_count=48, total_characters=32628,
        emoji_count=12, most_active_period="14:00–15:00", golden_quotes=[],
    )
    stats.activity_visualization = values.ActivityVisualization(hourly_activity={14: 120, 15: 85})
    analysis = {
        "statistics": stats, "topics": [], "user_titles": [], "user_analysis": {},
        "chat_quality_review": None, "report_period": period.build_report_period(days),
    }
    if not empty:
        analysis["topics"] = [values.SummaryTopic(
            topic="插件更新讨论", contributors=["小明"], detail="群友讨论如何让报告完整显示。",
        )]
        analysis["user_titles"] = [values.UserTitle(
            user_id="wxid_member", name="小明", title="热心群友", mbti="ENTP", reason="帮助大家整理信息。",
        )]
        stats.golden_quotes = [values.GoldenQuote(
            user_id="wxid_member", sender="小明", content="先把问题说明白，再一起解决。", reason="清晰而实用。",
        )]
        analysis["chat_quality_review"] = values.QualityReview(
            title="一起解决问题", subtitle="有问有答", summary="这是一场有收获的讨论。",
            dimensions=[values.QualityDimension(name="交流质量", percentage=80, comment="有具体建议。")],
        )
    payload = asyncio.run(preparer.prepare_render_data(analysis, allow_alphanumeric_user_ids=True))
    return templates.render_template("image_template.html", **payload), analysis


@pytest.mark.parametrize("days", [1, 7])
def test_complete_report_has_all_sections_and_correct_period(modules, days):
    rendered, analysis = render_report(modules, days)
    for text in (f"近 {days} 天", "插件更新讨论", "热心群友", "先把问题说明白", "交流质量", "591"):
        assert text in rendered
    assert "analysis-section-status" not in rendered
    serializer = modules("application.services.analysis_serializer").AnalysisResultSerializer
    restored = serializer.deserialize(serializer.serialize(analysis))
    assert restored["report_period"] == analysis["report_period"]


def test_missing_results_are_visible_and_disabled_is_distinguished(modules):
    rendered, _ = render_report(modules, empty=True, disabled=True)
    assert rendered.count('class="analysis-section-status"') == 4
    assert "此板块未启用" in rendered
    assert rendered.count("本次未获得有效分析内容") == 3
    for text in ("热门话题", "群友画像", "群聊金句", "聊天质量点评"):
        assert text in rendered


def test_text_fallback_keeps_period_and_quality_review(modules):
    _, analysis = render_report(modules, days=7)
    source = (PLUGIN / "src/infrastructure/reporting/generators.py").read_text("utf-8")
    method = next(node for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.FunctionDef) and node.name == "generate_text_report")
    scope = {"normalize_report_period": modules("shared.report_period").normalize_report_period}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "text-report", "exec",
                 flags=__future__.annotations.compiler_flag), scope)
    text = scope["generate_text_report"](SimpleNamespace(config_manager=ReportConfig()), analysis)
    for content in ("近 7 天", "插件更新讨论", "热心群友", "先把问题说明白", "交流质量", "有具体建议"):
        assert content in text
