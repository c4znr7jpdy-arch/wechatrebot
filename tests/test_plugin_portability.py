from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


REPOSITORY = Path(__file__).parents[1]
PROJECT_PLUGINS = Path("E:/Project/data/plugins")


def _patched(tmp_path: Path, module: str, relative: str) -> str:
    source = PROJECT_PLUGINS / module / relative
    if not source.is_file():
        pytest.skip("Local plugin source checkout is unavailable")
    patch = REPOSITORY / "patches" / f"astrbot-plugin-{module}.patch"
    # Each portability patch may also adapt lifecycle code in other files.
    # Use an isolated repository so git does not silently skip paths outside
    # a parent checkout when pytest's temporary directory happens to be in it.
    for path in re.findall(r"^--- a/(.+)$", patch.read_text(encoding="utf-8"), re.MULTILINE):
        original = PROJECT_PLUGINS / module / path
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "apply", "--ignore-space-change", str(patch)],
        cwd=tmp_path, check=True, capture_output=True,
    )
    return (tmp_path / relative).read_text(encoding="utf-8")


def _function(source: str, name: str, namespace: dict):
    tree = ast.parse(source)
    selected = next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    exec(compile(ast.Module(body=[selected], type_ignores=[]), "plugin-test", "exec"), namespace)
    return namespace[name]


def test_youtube_uses_bundled_node_without_global_install(tmp_path: Path, monkeypatch) -> None:
    source = _patched(tmp_path, "astrbot_plugin_parser", "core/download.py")
    playwright_root = tmp_path / "playwright"
    (playwright_root / "driver").mkdir(parents=True)
    node = playwright_root / "driver" / "node.exe"
    node.write_bytes(b"node")
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace(__file__=str(playwright_root / "__init__.py")))
    helper = _function(source, "_bundled_node_runtimes", {"Path": Path})
    assert helper() == {"node": {"path": str(node)}}


def test_windows_memory_waits_for_milvus_configuration(tmp_path: Path) -> None:
    source = _patched(tmp_path, "astrbot_plugin_mnemosyne", "main.py")
    pending = _function(source, "_commercial_setup_pending", {"sys": SimpleNamespace(platform="win32")})
    instance = SimpleNamespace(config={})
    assert pending(instance) is True
    instance.config["address"] = "https://milvus.example.test:19530"
    assert pending(instance) is False


def test_report_patch_preserves_browser_fallback(tmp_path: Path) -> None:
    source = _patched(tmp_path, "astrbot_plugin_qq_group_daily_analysis", "src/infrastructure/reporting/generators.py")
    tree = ast.parse(source)
    launches = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "launch"]
    channels = [[keyword.value.value for keyword in node.keywords if keyword.arg == "channel" and isinstance(keyword.value, ast.Constant)] for node in launches]
    assert channels == [["msedge"], []]


def test_group_member_notice_has_independent_hot_read_switches(tmp_path: Path) -> None:
    source = _patched(tmp_path, "jiang_group_notice", "main.py")
    schema = json.loads((tmp_path / "_conf_schema.json").read_text(encoding="utf-8"))

    assert {
        "notify_join",
        "notify_leave",
        "notify_profile_change",
        "leave_title",
        "leave_body",
        "profile_change_title",
        "profile_change_body",
    } <= set(schema)
    assert schema["notify_leave"]["default"] is True
    assert 'self._is_enabled("notify_join")' in source
    assert 'self._is_enabled("notify_leave")' in source
    assert 'self._is_enabled("notify_profile_change")' in source
    assert 'self._get_config_value("leave_title"' in source
    assert 'self._get_config_value("profile_change_body"' in source
    assert "old_label=old_label" in source


def test_music_command_accepts_wechat_whitespace_and_marks_card_as_reply(
    tmp_path: Path,
) -> None:
    source = _patched(tmp_path, "astrbot_plugin_music", "main.py")
    namespace = {"re": re}
    parser = _function(source, "_parse_music_command", namespace)
    plugin = SimpleNamespace(keywords=["网易点歌"])

    assert parser(plugin, "点歌 七里香") == ("点歌", "七里香")
    assert parser(plugin, "点歌\u2005七里香") == ("点歌", "七里香")
    assert parser(plugin, "七里香") is None
    assert "event.is_at_or_wake_command = True" in source
    assert 'event.plain_result("点歌服务暂时不可用，请稍后再试。")' in source
    assert 'logger.error(f"点歌搜索失败: {type(exc).__name__}")' in source
    assert source.count("event.stop_event()") >= 4
    sender = (tmp_path / "core/sender.py").read_text(encoding="utf-8")
    assert '"_wechatrebot_delivery": "reply"' in sender
    assert '"_wechatrebot_wake": True' in sender


def test_shared_tool_sources_are_part_of_payload() -> None:
    entries = json.loads((REPOSITORY / "release/source-manifest.json").read_text(encoding="utf-8"))["entries"]
    targets = {entry["target"] for entry in entries}
    assert {
        "app/astrbot/nonebot-plugin-mystool/src/nonebot_plugin_mystool",
        "app/astrbot/nonebot-plugin-mystool/LICENSE",
        "app/astrbot/ai_plugin/nonebot_plugin_rocom",
    } <= targets
