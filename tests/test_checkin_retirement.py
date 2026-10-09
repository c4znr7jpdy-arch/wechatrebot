import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_hot_config_runtime import _runtime_function
from test_plugin_portability import _patched
from test_plugin_center import _install
from wechatrebot_manager.plugin_center import PluginCenterService


ROOT = Path(__file__).resolve().parents[1]


def test_retired_checkin_has_no_api_import_or_command_and_removes_old_jobs(tmp_path):
    source = _patched(tmp_path, "jiang_schedule", "main.py", all_patches=True,
                      source_root=ROOT / "vendor/data/plugins")
    tree = ast.parse(source)
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_cb_checkin" not in names
    assert not any(name.startswith("page_checkin") for name in names)
    assert "ai_plugin.checkin" not in source and "_CHECKIN_MANAGER" not in source
    assert "|签到)" not in source
    registered = AsyncMock()
    cron = SimpleNamespace(list_jobs=AsyncMock(side_effect=[[
        SimpleNamespace(name="checkin_room", job_id="old-checkin"),
        SimpleNamespace(name="other_job", job_id="unrelated"),
    ], []]), delete_job=AsyncMock())
    sync = _runtime_function(source, "_sync_jobs", {
        "logger": SimpleNamespace(info=lambda *_: None),
        "_load_tasks": lambda: [
            {"type": "checkin", "enabled": True}, {"type": "weather", "enabled": True},
        ],
        "_CALLBACKS": {"weather": object()}, "_register_task": registered,
    })
    context = SimpleNamespace(cron_manager=cron)
    asyncio.run(sync(context))
    cron.delete_job.assert_awaited_once_with("old-checkin")
    registered.assert_awaited_once_with(context, {"type": "weather", "enabled": True})


def test_old_installation_hides_checkin_page_but_keeps_task_center(tmp_path):
    install, _ = _install(tmp_path)
    page = install / "app/astrbot/data/plugins/jiang_schedule/pages/checkin"
    page.mkdir(parents=True)
    (page / "index.html").write_text("<title>签到管理</title>", encoding="utf-8")
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False
    feature = next(p for p in service.catalog()["plugins"] if p["display_name"] == "定时任务")
    assert not feature["pages"]
    assert feature["scheduled_action"] == "weather"


def test_payload_excludes_retired_page_and_account_executor():
    entries = json.loads((ROOT / "release/source-manifest.json").read_text("utf-8"))["entries"]
    schedule = next(e for e in entries if e["source"] == "data/plugins/jiang_schedule")
    library = next(e for e in entries if e["source"] == "ai_plugin/ai_plugin")
    assert "pages" in schedule["exclude"]
    assert "checkin.py" in library["exclude"]
