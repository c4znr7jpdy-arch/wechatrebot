from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import types
import typing
from unittest.mock import AsyncMock, Mock
import zipfile

import pytest

from test_hot_config_runtime import _runtime_function
from test_plugin_portability import PROJECT_PLUGINS, _patched
from wechatrebot_manager.group_feature_runtime import group_plugins
from wechatrebot_manager.group_policy import GroupPolicyService


ROOT = Path(__file__).resolve().parents[1]
MODULE = "astrbot_plugin_bittorrent"
REGISTERED_NAME = "astrbot_plugin_BitTorrent"
GROUP = "room@chatroom"


class Event:
    def __init__(self, message):
        self.message_str = message
        self.is_at_or_wake_command = False
        self.is_wake = False
        self._extras = {}
        self.stopped = False
        self.message_obj = SimpleNamespace(type="group")
        self.unified_msg_origin = "hook:GroupMessage:" + GROUP

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return [SimpleNamespace(text=self.message_str)]

    def get_self_id(self):
        return "wxid_bot"

    def get_sender_id(self):
        return "wxid_user"

    def get_sender_name(self):
        return "user"

    def get_group_id(self):
        return GROUP

    def get_platform_name(self):
        return "aiocqhttp"

    def is_private_chat(self):
        return False

    def get_extra(self, key=None, default=None):
        return self._extras.get(key, default) if key else self._extras

    def set_extra(self, key, value):
        self._extras[key] = value

    def stop_event(self):
        self.stopped = True

    def chain_result(self, chain):
        return chain

    async def send(self, *_):
        pytest.fail("Command filtering unexpectedly produced an error reply")


@pytest.fixture
def command_runtime(tmp_path):
    engine = ROOT / "vendor/engine"
    plugin_path = PROJECT_PLUGINS / MODULE / "main.py"
    if not engine.is_dir() or not plugin_path.is_file():
        pytest.fail("Pinned engine and plugin source are required for command routing verification")
    stage_path = "astrbot/core/pipeline/waking_check/stage.py"
    registry_path = "astrbot/core/star/star_handler.py"
    for relative in (stage_path, registry_path):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((engine / relative).read_bytes())
    command_source = (engine / "astrbot/core/star/filter/command.py").read_text("utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    for patch, includes in [
        ("astrbot-4.26.6-wechat.patch", [registry_path]),
        ("astrbot-4.26.6-outbound-policy.patch", [stage_path, "astrbot/core/platform/wechat_group_policy.py"]),
    ]:
        subprocess.run(
            ["git", "apply", "-p2", "--whitespace=error-all",
             *["--include=" + path for path in includes], str(ROOT / "patches" / patch)],
            cwd=tmp_path, check=True, capture_output=True,
        )
    command_scope = {"HandlerFilter": object, "inspect": inspect, "re": re,
                     "types": types, "typing": typing}
    tree = ast.parse(command_source)
    tree.body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                 *[node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]]
    exec(compile(ast.fix_missing_locations(tree), "pinned-command-filter", "exec"), command_scope)
    command_filter = command_scope["CommandFilter"]

    plugin_dir = tmp_path / "bt-plugin"
    plugin_dir.mkdir()
    plugin_source = _patched(plugin_dir, MODULE, "main.py", all_patches=True)
    sort_scope = {}
    utils = next(node for node in ast.parse(plugin_source).body
                 if isinstance(node, ast.ClassDef) and node.name == "MagnetUtils")
    utils.body = [node for node in utils.body if isinstance(node, ast.Assign)
                  or getattr(node, "name", None) in {"is_sort_keyword", "get_sort_param"}]
    exec(compile(ast.Module(body=[utils], type_ignores=[]), "plugin-sort", "exec"), sort_scope)
    handler = _runtime_function(plugin_source, "magnet_search_handler", {
        "MagnetUtils": sort_scope["MagnetUtils"],
        "Comp": SimpleNamespace(Plain=lambda text: SimpleNamespace(text=text),
                                Node=lambda **kwargs: kwargs),
    })
    metadata = SimpleNamespace(handler=handler, handler_module_path="data.plugins." + MODULE + ".main",
                               handler_full_name="bt-search", enabled=True, event_type="message")
    metadata.event_filters = [command_filter("bt", handler_md=metadata)]
    star = SimpleNamespace(root_dir_name=MODULE, name=REGISTERED_NAME, activated=True, reserved=False)
    star_map = {metadata.handler_module_path: star}
    registry_scope = {"star_map": star_map, "EventType": SimpleNamespace(
        OnAstrBotLoadedEvent="loaded", OnPlatformLoadedEvent="platform",
        OnPluginLoadedEvent="plugin-loaded", OnPluginUnloadedEvent="plugin-unloaded")}
    get_handlers = _runtime_function((tmp_path / registry_path).read_text("utf-8"),
                                     "get_handlers_by_event_type", registry_scope, last=True)
    registry = SimpleNamespace(_handlers=[metadata])
    registry.get_handlers_by_event_type = lambda *args, **kwargs: get_handlers(registry, *args, **kwargs)
    policy_scope = {}
    exec((tmp_path / "astrbot/core/platform/wechat_group_policy.py").read_text("utf-8"), policy_scope)
    process = _runtime_function((tmp_path / stage_path).read_text("utf-8"), "process", {
        "Path": Path, "group_plugins": policy_scope["group_plugins"], "star_map": star_map,
        "logger": Mock(), "At": type("At", (), {}), "AtAll": type("AtAll", (), {}),
        "Reply": type("Reply", (), {}), "PermissionTypeFilter": type("PermissionTypeFilter", (), {}),
        "CommandGroupFilter": type("CommandGroupFilter", (), {}),
        "EventType": SimpleNamespace(AdapterMessageEvent="message"), "star_handlers_registry": registry,
        "SessionPluginManager": SimpleNamespace(filter_handlers_by_session=AsyncMock(side_effect=lambda _e, hs: hs)),
    })
    stage = SimpleNamespace(unique_session=False, ignore_bot_self_message=False,
                            friend_message_needs_wake_prefix=False, ignore_at_all=True,
                            disable_builtin_commands=False, ctx=SimpleNamespace(astrbot_config={
                                "admins_id": [], "wake_prefix": ["/", "\\"], "plugin_set": [MODULE]}))
    return process, stage, metadata, handler, plugin_source


@pytest.mark.parametrize("message,keyword,sort", [
    ("/bt 无间道", "无间道", ""),
    ("/bt 热门 无间道 2002", "无间道 2002", "hot"),
    ("\\bt\u3000无间道 ", "无间道", ""),
])
def test_bt_command_reaches_search_and_skips_conversation_core(command_runtime, tmp_path, monkeypatch,
                                                             message, keyword, sort):
    process, stage, metadata, handler, plugin_source = command_runtime
    runtime_cwd = tmp_path / "app/astrbot"
    runtime_cwd.mkdir(parents=True)
    monkeypatch.chdir(runtime_cwd)
    event = Event(message)

    async def run():
        await process(stage, event)
        assert event.get_extra("activated_handlers") == [metadata]
        assert event.is_at_or_wake_command is True
        assert event.stopped is False
        angel_source = (PROJECT_PLUGINS / "astrbot_plugin_angel_heart/main.py").read_text("utf-8-sig")
        is_command = _runtime_function(angel_source, "_is_upstream_command_event", {
            "CommandFilter": type(metadata.event_filters[0]), "CommandGroupFilter": type("CommandGroupFilter", (), {})})
        assert is_command(None, event) is True
        search = AsyncMock(return_value=[{"title": "无间道", "magnet_link": None,
                                         "size": "1 GB", "create_time": "2026-09-16"}])
        plugin = SimpleNamespace(search_service=SimpleNamespace(search=search), enable_preview=False,
                                 _format_result=_runtime_function(plugin_source, "_format_result", {}))
        event.send = AsyncMock()
        await handler(plugin, event)
        replies = [call.args[0] for call in event.send.await_args_list]
        search.assert_awaited_once_with(keyword, sort)
        assert event.stopped is True
        assert len(replies) == 1
        assert "无间道" in "".join(segment.text for segment in replies[0])

    asyncio.run(run())


@pytest.mark.parametrize("policy,globally_enabled", [
    ({"allowed_features": []}, None),
    ({"allowed_features": [MODULE]}, ["jiang_commands"]),
    ({"allowed_features": [MODULE], "bot_enabled": False}, None),
    ({"allowed_features": [MODULE], "reply_mode": "off"}, None),
])
def test_bt_default_does_not_override_explicit_restrictions(tmp_path, policy, globally_enabled):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"schema_version": 1, "accounts": {
        "wxid_bot": {"groups": {GROUP: policy}}}}), encoding="utf-8")
    star = SimpleNamespace(root_dir_name=MODULE, name=REGISTERED_NAME)
    assert MODULE not in (group_plugins(path, "wxid_bot", GROUP, globally_enabled, [star]) or [])


def test_manager_allows_bt_for_groups_using_defaults(tmp_path):
    policy = GroupPolicyService(tmp_path).get("wxid_bot", GROUP)
    assert policy["configured"] is False
    assert MODULE in policy["allowed_features"]


@pytest.mark.parametrize("allowed", [[], ["jiang_commands"], ["jiang_commands.setu"], ["jiang_commands", "jiang_commands.setu"]])
@pytest.mark.parametrize("handler_name", ["random_image", "weather", "news"])
def test_patched_waking_stage_obeys_independent_setu_checkbox(command_runtime, tmp_path, monkeypatch, allowed, handler_name):
    process, stage, metadata, _, _ = command_runtime
    star = process.__globals__["star_map"][metadata.handler_module_path]
    star.root_dir_name = star.name = "jiang_commands"
    metadata.handler_name = handler_name
    metadata.event_filters = [SimpleNamespace(filter=lambda *_: True)]
    stage.ctx.astrbot_config["plugin_set"] = ["jiang_commands"]
    policy = tmp_path / "data/manager/group-policies.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"schema_version": 1, "accounts": {"wxid_bot": {"groups": {GROUP: {"allowed_features": allowed}}}}}), encoding="utf-8")
    cwd = tmp_path / "app/astrbot"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    event = Event("图来" if handler_name == "random_image" else "/新闻")
    asyncio.run(process(stage, event))
    key = "jiang_commands.setu" if handler_name == "random_image" else "jiang_commands"
    assert (metadata in event.get_extra("activated_handlers", [])) == (key in allowed)
