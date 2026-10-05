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


def _patched(tmp_path: Path, module: str, relative: str, *, all_patches: bool = False,
             source_root: Path = PROJECT_PLUGINS) -> str:
    source = source_root / module / relative
    if not source.is_file():
        pytest.skip("Local plugin source checkout is unavailable")
    base_patch = REPOSITORY / "patches" / f"astrbot-plugin-{module}.patch"
    patches = [base_patch]
    if all_patches:
        patches = sorted(
            REPOSITORY.glob(f"patches/astrbot-plugin-{module}*.patch"),
            key=lambda path: (0 if "--" not in path.stem else 1, path.name.casefold()),
        )
    # Each portability patch may also adapt lifecycle code in other files.
    # Use an isolated repository so git does not silently skip paths outside
    # a parent checkout when pytest's temporary directory happens to be in it.
    for patch in patches:
        for path in re.findall(r"^--- a/(.+)$", patch.read_text(encoding="utf-8"), re.MULTILINE):
            original = source_root / module / path
            destination = tmp_path / path
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    for patch in patches:
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


def test_memory_hot_reload_stops_partially_initialized_instance(tmp_path: Path) -> None:
    source = _patched(tmp_path, "astrbot_plugin_mnemosyne", "main.py")

    def ignore(*_args, **_kwargs):
        return None

    terminate = _function(
        source,
        "terminate",
        {
            "asyncio": __import__("asyncio"),
            "logger": SimpleNamespace(
                info=ignore,
                warning=ignore,
                debug=ignore,
                error=ignore,
            ),
        },
    )

    async def scenario() -> None:
        import asyncio

        init_task = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
        ready = asyncio.Event()
        ready.set()
        plugin = SimpleNamespace(
            _commercial_init_task=init_task,
            _milvus_manager_ready=ready,
            _ensure_milvus_connection_task=None,
            _embedding_provider_task=None,
            admin_panel_server=None,
            msg_counter=None,
            milvus_manager=None,
        )

        await terminate(plugin)

        assert init_task.cancelled()
        assert not ready.is_set()
        assert plugin._summary_check_task is None

    __import__("asyncio").run(scenario())


def test_report_patch_preserves_browser_fallback(tmp_path: Path) -> None:
    source = (REPOSITORY / "runtime/astrbot_plugin_qq_group_daily_analysis/src/infrastructure/reporting/generators.py").read_text("utf-8")
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


def test_group_member_notice_cards_are_routed_as_reactive_group_events(
    tmp_path: Path,
) -> None:
    source = _patched(tmp_path, "jiang_group_notice", "main.py")
    calls = []

    class Bot:
        async def call_action(self, **kwargs):
            calls.append(kwargs)

    namespace = {
        "AstrMessageEvent": object,
        "logger": SimpleNamespace(info=lambda *_args: None, error=lambda *_args: None),
    }
    send_link_card = _function(source, "_send_link_card", namespace)

    group_event = SimpleNamespace(
        message_obj=SimpleNamespace(group_id="room@chatroom"),
        bot=Bot(),
    )
    numeric_event = SimpleNamespace(
        message_obj=SimpleNamespace(group_id="123456"),
        bot=Bot(),
    )

    __import__("asyncio").run(
        send_link_card(group_event, "title", "desc", "https://example.test", "image")
    )
    __import__("asyncio").run(
        send_link_card(numeric_event, "title", "desc", "https://example.test", "image")
    )

    assert calls[0]["_wechatrebot_delivery"] == "reply"
    assert calls[0]["_wechatrebot_wake"] is True
    assert "_wechatrebot_delivery" not in calls[1]
    assert calls[1]["group_id"] == 123456


def test_image_generation_common_config_applies_stored_api_keys(tmp_path: Path) -> None:
    source = _patched(tmp_path, "jiang_image", "main.py", all_patches=True)
    schema = json.loads((tmp_path / "_conf_schema.json").read_text(encoding="utf-8"))
    assert {
        "primary_api_key",
        "secondary_api_key",
    } <= set(schema)
    assert all(schema[key]["default"] == "" for key in (
        "primary_api_key", "secondary_api_key"
    ))
    assert "tertiary_api_key" not in schema

    apply_config = _function(
        source,
        "_apply_plugin_config",
        {"re": re, "ImageModelConfigError": ValueError},
    )
    updates = []
    manager = SimpleNamespace(
        public_config=lambda: {
            "active_model": "primary",
            "models": [{
                "id": "primary", "name": "Primary", "enabled": True,
                "base_url": "https://old.invalid/v1", "model": "old-image",
                "fallback_priority": 0, "api_key_source": {"type": "environment", "env_name": "OLD_KEY"},
            }],
        },
        upsert_profile=lambda payload: updates.append(payload),
    )
    plugin = SimpleNamespace(
        config={
            "primary_api_key": "new-secret",
            "primary_base_url": "https://images.example/v1",
            "primary_model": "image-model",
        },
        model_manager=manager,
    )

    apply_config(plugin)

    assert updates[0]["api_key_source"] == {"type": "stored"}
    assert updates[0]["api_key"] == "new-secret"
    assert updates[0]["base_url"] == "https://images.example/v1"
    assert updates[0]["model"] == "image-model"


def test_link_parser_default_template_enables_douyin_without_customer_config() -> None:
    template_path = PROJECT_PLUGINS / "astrbot_plugin_parser" / "default_template.json"
    if not template_path.is_file():
        pytest.skip("Local plugin source checkout is unavailable")
    templates = json.loads(template_path.read_text(encoding="utf-8-sig"))
    douyin = next(item for item in templates if item.get("__template_key") == "douyin")

    assert douyin["enable"] is True


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
    platform = (tmp_path / "core/platform/__init__.py").read_text(encoding="utf-8")
    assert "except ModuleNotFoundError as exc" in platform
    assert "class MetingMusic" in platform
    compile(platform, "music-platform-init", "exec")


def test_shared_tool_sources_are_part_of_payload() -> None:
    entries = json.loads((REPOSITORY / "release/source-manifest.json").read_text(encoding="utf-8"))["entries"]
    targets = {entry["target"] for entry in entries}
    assert {
        "app/astrbot/nonebot-plugin-mystool/src/nonebot_plugin_mystool",
        "app/astrbot/nonebot-plugin-mystool/LICENSE",
        "app/astrbot/ai_plugin/nonebot_plugin_rocom",
    } <= targets
