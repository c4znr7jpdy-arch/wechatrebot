from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import pytest

ROOT = Path(__file__).parents[1]


def load_module(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "runtime/jiang_capability_router" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


discovery = load_module("discovery")
execution = load_module("execution")


def stub(monkeypatch, name, **members):
    module = ModuleType(name)
    vars(module).update(members)
    monkeypatch.setitem(sys.modules, name, module)


def plugin(name="sample", **kwargs):
    values = dict(name=name, root_dir_name=name, activated=True, reserved=False,
                  star_cls=NS(), module=None, module_path=name + ".main", desc="后台处理", display_name=name)
    values.update(kwargs)
    return NS(**values)


def command(**kwargs):
    values = dict(plugin="sample", enabled=True, handler_full_name="sample.main.search",
                  effective_command="查找", permission="everyone", sub_commands=[])
    values.update(kwargs)
    return values


class Event:
    plugins_name = None
    unified_msg_origin = "test:FriendMessage:user"
    role = "member"
    _has_send_oper = False

    def __init__(self, message="帮我查找猫"):
        self.message_str = message
        self.message_obj = NS(message_str=message, message=[])
        self._extras = {}
        self._result = None
        self._force_stopped = False
        self.sent = []

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return self.message_obj.message

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_platform_name(self):
        return "test"

    def get_group_id(self):
        return ""

    def get_self_id(self):
        return "bot"

    def is_admin(self):
        return self.role == "admin"

    def is_stopped(self):
        return self._force_stopped

    def get_result(self):
        return self._result

    def clear_result(self):
        self._result = None

    async def send(self, result):
        self.sent.append(result)
        self._has_send_oper = True


@pytest.fixture
def engine(monkeypatch):
    disabled = set()

    async def enabled(session, name):
        return name not in disabled

    stub(monkeypatch, "astrbot.core.star.session_plugin_manager",
         SessionPluginManager=NS(is_plugin_enabled_for_session=enabled))
    registry = NS(get_handler_by_full_name=lambda name: None, get_handlers_by_module_name=lambda name: [])
    stub(monkeypatch, "astrbot.core.star.star_handler", star_handlers_registry=registry)
    return disabled


def context(stars, config=None):
    return NS(get_all_stars=lambda: stars, get_config=lambda session: config or {"plugin_set": ["*"]})


def test_discovery_tracks_install_disable_uninstall_and_session_grants(engine):
    stars = [plugin("a")]
    ctx, event = context(stars), Event()
    read = lambda: asyncio.run(discovery.available_plugins(ctx, event))
    assert [x.name for x in read()] == ["a"]
    stars.append(plugin("b"))
    assert [x.name for x in read()] == ["a", "b"]
    stars[0].activated = False
    assert [x.name for x in read()] == ["b"]
    engine.add("b")
    assert read() == []
    engine.clear()
    event.plugins_name = []
    assert read() == []
    event.plugins_name = None
    stars.clear()
    assert read() == []


def test_global_and_event_scope_intersect(engine):
    event = Event()
    event.plugins_name = ["b"]
    ctx = context([plugin("a"), plugin("b")], {"plugin_set": ["a"]})
    assert asyncio.run(discovery.available_plugins(ctx, event)) == []


def test_group_grants_are_rechecked_and_setu_does_not_grant_news(engine, monkeypatch):
    calls = []
    def grants(*args, **kwargs):
        calls.append(kwargs)
        return []
    stub(monkeypatch, "astrbot.core.platform.wechat_group_policy", group_plugins=grants)
    event = Event()
    event.get_platform_name = lambda: "aiocqhttp"
    event.get_group_id = lambda: "group@chatroom"
    event.plugins_name = ["jiang_commands"]
    assert asyncio.run(discovery.available_plugins(context([plugin("jiang_commands")]), event)) == []
    assert calls == [{"command_handler": "capability_invoke"}]


def test_missing_bridge_is_not_advertised():
    spec = NS(delivery="plugin_bridge")
    assert not discovery.natural_available(spec, [])
    assert not discovery.natural_available(spec, [plugin("jiang_raid_signup")])
    star = plugin("jiang_raid_signup", star_cls=NS(invoke_natural_capability=lambda: None))
    assert discovery.natural_available(spec, [star])


def test_natural_wrapper_cannot_bypass_disabled_or_admin_command():
    def _run_news():
        pass
    spec = NS(delivery="direct", handler=_run_news)
    stars = [plugin("jiang_commands")]
    disabled = command(plugin="jiang_commands", handler_name="news", enabled=False)
    assert not discovery.natural_available(spec, stars, [disabled])
    restricted = command(plugin="jiang_commands", handler_name="news", permission="admin")
    assert not discovery.natural_available(spec, stars, [restricted])
    assert discovery.natural_available(spec, stars, [restricted], is_admin=True)


def test_commands_filter_disabled_conflicting_admin_and_nested():
    commands = [command(enabled=False), command(has_conflict=True), command(permission="admin"),
                command(plugin="missing"), command(reserved=True),
                command(is_group=True, sub_commands=[command(effective_command="新命令")])]
    result = discovery.command_entries(commands, [plugin()], False)
    assert [x["effective_command"] for x in result] == ["新命令"]
    assert len(discovery.command_entries(commands, [plugin()], True)) == 2


def declaration(tmp_path, payload):
    (tmp_path / "capabilities.json").write_text(json.dumps(payload), encoding="utf-8")
    return plugin(module=NS(__file__=str(tmp_path / "main.py")))


def test_custom_plugin_declaration_defaults_to_confirmation(tmp_path):
    star = declaration(tmp_path, {"schema_version": 1, "capabilities": [
        {"id": "change", "description": "修改", "handler": "change", "message_pattern": "修改 .+"}
    ]})
    item = discovery.load_declarations(star)[0]
    assert item["effect"] == "write"
    assert item["id"] == "decl:sample:change"


def test_declaration_disappears_when_handler_removed_or_disabled(tmp_path, engine):
    star = declaration(tmp_path, {"schema_version": 1, "capabilities": [
        {"id": "search", "description": "查询", "handler": "search",
         "message_pattern": "查找 .+", "effect": "read"}
    ]})
    registry = sys.modules["astrbot.core.star.star_handler"].star_handlers_registry
    handler = NS(handler_name="search", handler_full_name="sample.main.search", enabled=True)
    registry.get_handlers_by_module_name = lambda name: [handler]
    read = lambda commands: asyncio.run(discovery.discover(context([star]), Event(), commands))["declarations"]
    assert len(read([])) == 1
    assert read([command(enabled=False)]) == []
    handler.enabled = False
    assert read([]) == []
    registry.get_handlers_by_module_name = lambda name: []
    assert read([]) == []


@pytest.mark.parametrize("item", [
    {"id": "../outside", "description": "bad"},
    {"id": "x", "description": "bad", "handler": "change"},
    {"id": "x", "description": "bad", "handler": "change", "message_pattern": "["},
    {"id": "x", "description": "bad", "effect": "anything"},
])
def test_malformed_declarations_fail_closed(tmp_path, item):
    assert discovery.load_declarations(declaration(tmp_path, {
        "schema_version": 1, "capabilities": [item]
    })) == []


def test_catalog_includes_event_only_plugins_and_native_tools(engine):
    snapshot = asyncio.run(discovery.discover(context([plugin()]), Event(), []))
    text = discovery.render_catalog(snapshot, "", "", [{"name": "play_song", "description": "点歌"}])
    assert "后台处理" in text and "play_song" in text
    assert "简介不代表已开放 AI 执行" in text


def test_removed_plugin_tool_is_not_advertised_from_an_earlier_request():
    snapshot = {"plugins": [], "commands": [], "declarations": []}
    text = discovery.render_catalog(snapshot, "", "", [
        {"name": "stale_tool", "plugin": "removed", "description": "old"}
    ])
    assert "stale_tool" not in text


def test_command_identity_survives_rename_but_old_message_is_rejected():
    snapshot = {"commands": [command(effective_command="新命令")], "declarations": []}
    key = "command:sample.main.search"
    with pytest.raises(ValueError, match="改名"):
        execution.select_entry(snapshot, key, "查找 猫")
    assert execution.select_entry(snapshot, key, "新命令 猫")[1] == "sample.main.search"


@pytest.mark.parametrize("text", ["可以删除吗", "怎么删除 猫", "取消 执行 删除 猫", "删除 狗"])
def test_model_cannot_forge_write_confirmation(text):
    assert not execution.authorized_message(text, "删除 猫", "write")


def test_explicit_confirmation_matches_exact_target():
    assert execution.authorized_message("执行 删除 猫", "删除 猫", "write")
    assert execution.authorized_message("查找猫", "查找 猫", "read")


@pytest.fixture
def dispatch(monkeypatch):
    class Plain:
        def __init__(self, text):
            self.text = text
    calls = []
    target = NS(handler_name="search", handler_full_name="sample.main.search", handler_module_path="sample.main",
                handler=lambda event, **params: None)
    state = NS(allowed=True, target=target, calls=calls, response="查找结果")

    class Stage:
        async def initialize(self, ctx):
            assert ctx.astrbot_config["wake_prefix"] == []
        async def process(self, event):
            event._force_stopped = not state.allowed
            event.set_extra("activated_handlers", [target])
            event.set_extra("handlers_parsed_params", {target.handler_full_name: {"query": "猫"}})

    async def call_handler(event, handler, **params):
        calls.append((event, params))
        event._result = state.response
        yield None

    stub(monkeypatch, "astrbot.api.message_components", Plain=Plain)
    stub(monkeypatch, "astrbot.core.pipeline.context_utils", call_handler=call_handler)
    stub(monkeypatch, "astrbot.core.pipeline.waking_check.stage", WakingCheckStage=Stage)
    stub(monkeypatch, "astrbot.core.provider.entities", ProviderRequest=type("Request", (), {}))
    stub(monkeypatch, "astrbot.core.star.star", star_map={"sample.main": plugin()})
    state.Plain = Plain
    return state


def dispatch_snapshot():
    return {"commands": [], "declarations": [{"id": "decl:sample:search", "plugin": "sample",
            "handler": "search", "effect": "read", "message_pattern": "查找 .+"}]}


def test_execution_preserves_identity_and_payload_and_deduplicates(dispatch):
    event = Event()
    image = NS(type="image", path="existing-image")
    event.message_obj.message = [dispatch.Plain("original"), image]
    args = (context([plugin()]), event, dispatch_snapshot(), "decl:sample:search", "查找 猫")
    result = asyncio.run(execution.execute(*args))
    assert "已发送反馈" in result
    assert event.get_message_str() == "帮我查找猫"
    assert event.message_obj.message[0].text == "original"
    clone, params = dispatch.calls[0]
    assert clone.unified_msg_origin == event.unified_msg_origin and clone.role == event.role
    assert clone.message_obj.message[1].path == image.path
    assert clone.message_obj is not event.message_obj
    assert params == {"query": "猫"}
    assert event.sent == ["查找结果"]
    assert "不再重复执行" in asyncio.run(execution.execute(*args))
    assert len(dispatch.calls) == 1


def test_native_filter_denial_does_not_execute(dispatch):
    dispatch.allowed = False
    result = asyncio.run(execution.execute(context([]), Event(), dispatch_snapshot(),
                                          "decl:sample:search", "查找 猫"))
    assert "未执行" in result and not dispatch.calls


def test_missing_execution_evidence_does_not_claim_success(dispatch):
    dispatch.response = None
    result = asyncio.run(execution.execute(context([]), Event(), dispatch_snapshot(),
                                          "decl:sample:search", "查找 猫"))
    assert "没有可确认" in result


def test_all_shipped_declarations_validate_and_are_packaged(tmp_path):
    manifest = json.loads((ROOT / "release/source-manifest.json").read_text(encoding="utf-8"))
    for path in (ROOT / "runtime/capability_declarations").glob("*.json"):
        star = declaration(tmp_path, json.loads(path.read_text(encoding="utf-8")))
        assert discovery.load_declarations(star), path.name
        assert any(e["source"] == path.relative_to(ROOT).as_posix() for e in manifest["entries"])
    gateway = [e for e in manifest["entries"] if e["target"] == "app/astrbot/data/plugins/jiang_capability_router"]
    assert gateway == [{"origin": "commercial", "source": "runtime/jiang_capability_router",
                        "target": "app/astrbot/data/plugins/jiang_capability_router"}]
    assert not (ROOT / "patches/astrbot-plugin-jiang_capability_router--zz-command-cleanup.patch").exists()
