from __future__ import annotations

import __future__
import ast
import asyncio
import base64
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from wechatrebot_manager import group_analysis_schedule, task_store


PLUGIN = Path(__file__).parents[1] / "runtime/astrbot_plugin_qq_group_daily_analysis"
PACKAGE = "group_analysis_unified_test"
ACCOUNT, GROUP = "wxid_robot", "123456@chatroom"


@pytest.fixture
def modules(monkeypatch):
    for relative in ("", "src", "src.application", "src.application.services", "src.infrastructure",
                     "src.infrastructure.reporting", "src.infrastructure.scheduler", "src.shared", "src.utils"):
        name = PACKAGE + ("." + relative if relative else "")
        module = ModuleType(name)
        module.__path__ = [str(PLUGIN / relative.replace(".", "/"))]
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, PACKAGE + ".task_store", task_store)
    monkeypatch.setitem(sys.modules, PACKAGE + ".group_analysis_schedule", group_analysis_schedule)
    return lambda name: importlib.import_module(PACKAGE + ".src." + name)


def _plugin(modules, *, image=True):
    calls, rendered = [], []
    identity = {"id": ACCOUNT}
    trace = modules("shared.trace_context").TraceContext
    adapter = SimpleNamespace(_current_wechat_account=lambda: identity["id"])

    async def avatar(_user, size=40):
        return ""

    adapter.get_user_avatar_url = avatar

    async def analyze(**kwargs):
        calls.append(kwargs)
        assert trace.current().metadata["delivery_owner"] == "unified_schedule"
        return {"success": True, "analysis_result": {"group": kwargs["group_id"]}}

    async def render(analysis, group, html_render, **kwargs):
        rendered.append((analysis, group, kwargs))
        return "base64://" + base64.b64encode(b"\x89PNG\r\n\x1a\nreport").decode(), "<html>report</html>"

    plugin = SimpleNamespace(
        _initialized=True, _terminating=False,
        context=SimpleNamespace(get_all_providers=lambda: [object()]),
        bot_manager=SimpleNamespace(get_all_adapters=lambda: {"wechat": adapter}),
        config_manager=SimpleNamespace(is_group_allowed=lambda _group: True,
                                       get_output_format=lambda: ["image" if image else "text"],
                                       get_report_template=lambda: "scrapbook"),
        analysis_service=SimpleNamespace(execute_daily_analysis=analyze),
        report_generator=SimpleNamespace(generate_image_report=render, generate_text_report=lambda data: "分析 " + data["group"]),
        html_render=object(),
    )
    return plugin, calls, rendered, identity


def test_unified_report_reuses_analysis_and_renderer_without_plugin_send_side_effects(modules):
    module = modules("application.services.unified_report_service")
    plugin, calls, rendered, _identity = _plugin(modules)

    result = asyncio.run(module.UnifiedReportService(plugin).generate(ACCOUNT, GROUP, 3))

    assert result["type"] == "image"
    assert calls == [{"group_id": GROUP, "platform_id": "wechat", "manual": False, "days": 3}]
    assert rendered[0][1] == GROUP
    assert rendered[0][2]["avatar_cache_namespace"] == "wechat:wxid_robot"
    assert modules("shared.trace_context").TraceContext.current() is None


def test_unified_report_text_output_and_image_failure_use_existing_text_generator(modules):
    module = modules("application.services.unified_report_service")
    plugin, _calls, rendered, _identity = _plugin(modules, image=False)
    service = module.UnifiedReportService(plugin)
    assert asyncio.run(service.generate(ACCOUNT, GROUP, 1)) == {"type": "text", "text": "分析 " + GROUP}
    assert rendered == []

    plugin.config_manager.get_output_format = lambda: ["image"]

    async def failed_renderer(*_args, **_kwargs):
        raise OSError("unavailable renderer")

    plugin.report_generator.generate_image_report = failed_renderer
    assert asyncio.run(service.generate(ACCOUNT, GROUP, 1))["type"] == "text"


def test_unified_report_rejects_ambiguous_account_before_reading_messages(modules):
    module = modules("application.services.unified_report_service")
    plugin, calls, _rendered, _identity = _plugin(modules)
    same_adapter = next(iter(plugin.bot_manager.get_all_adapters().values()))
    plugin.bot_manager.get_all_adapters = lambda: {"wechat-a": same_adapter, "wechat-b": same_adapter}
    with pytest.raises(ValueError, match="无法确认"):
        asyncio.run(module.UnifiedReportService(plugin).generate(ACCOUNT, GROUP, 1))
    assert calls == []


def test_unified_report_detects_account_change_before_rendering(modules):
    module = modules("application.services.unified_report_service")
    plugin, _calls, rendered, identity = _plugin(modules)
    original = plugin.analysis_service.execute_daily_analysis

    async def change_account(**kwargs):
        result = await original(**kwargs)
        identity["id"] = "wxid_other"
        return result

    plugin.analysis_service.execute_daily_analysis = change_account
    with pytest.raises(ValueError, match="无法确认"):
        asyncio.run(module.UnifiedReportService(plugin).generate(ACCOUNT, GROUP, 1))
    assert rendered == []


@pytest.mark.parametrize("reason", ["no_messages", "below_threshold", "muted"])
def test_unified_report_records_expected_skips_without_rendering(modules, reason):
    module = modules("application.services.unified_report_service")
    plugin, _calls, rendered, _identity = _plugin(modules)

    async def skipped(**_kwargs):
        return {"success": False, "reason": reason}

    plugin.analysis_service.execute_daily_analysis = skipped
    result = asyncio.run(module.UnifiedReportService(plugin).generate(ACCOUNT, GROUP, 1))
    assert result["type"] == "skipped"
    assert rendered == []


def test_plugin_crash_recovery_never_replays_a_unified_owned_report(modules):
    module = modules("application.services.crash_recovery_service")
    saved = []
    store = SimpleNamespace(
        get_crashed_traces_on_startup=lambda: [{"trace_id": "unified-1", "group_id": GROUP,
                                              "trigger_type": "auto", "extra": {"delivery_owner": "unified_schedule"}}],
        save_trace=saved.append,
    )
    # No checkpoint or recovery API is supplied: an accidental attempt to
    # regenerate or send will fail this test rather than look successful.
    service = module.CrashRecoveryService(store, object(), object(), object())
    result = asyncio.run(service.recover_crashed_tasks())
    assert result == {"recovered": 0, "archived": 0, "aborted": 1}
    assert saved[0]["status"] == "aborted"
    assert "不补发" in saved[0]["error_message"]


def _method(path: Path, name: str, scope: dict):
    node = next(item for item in ast.walk(ast.parse(path.read_text("utf-8")))
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec",
                 flags=__future__.annotations.compiler_flag), scope)
    return scope[name]


def test_retired_plugin_does_not_register_or_run_old_timer_or_immediate_reports(modules):
    path = PLUGIN / "src/infrastructure/scheduler/auto_scheduler.py"
    scope = {"legacy_analysis_retired": lambda _path: True, "__file__": str(path),
             "asyncio": asyncio, "logger": modules("utils.logger").logger}
    scheduler = SimpleNamespace(_terminating=False, unschedule_jobs=lambda _context: None,
                                _immediate_report_tasks={}, _immediate_report_versions={})
    _method(path, "schedule_jobs", scope)(scheduler, object())
    _method(path, "_request_immediate_incremental_report", scope)(scheduler, GROUP, "wechat")
    asyncio.run(_method(path, "_run_scheduled_report", scope)(scheduler))
    asyncio.run(_method(path, "_run_immediate_incremental_report", scope)(scheduler, "state", GROUP, "wechat"))
    assert scheduler._immediate_report_tasks == {}
