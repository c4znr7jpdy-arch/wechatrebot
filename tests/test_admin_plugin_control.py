from __future__ import annotations

import json
import asyncio
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wechatrebot_manager import app_shell, plugin_center_web
from wechatrebot_manager.admin_plugin_control import CONTROL_DESCRIPTOR, CONTROL_PATH, handle_plugin_control
from wechatrebot_manager.group_policy import GroupPolicyService
from wechatrebot_manager.plugin_errors import PluginCenterError


ACCOUNT = "wxid_robot"
GROUP = "11111@chatroom"
MUSIC = "astrbot_plugin_music"
PROACTIVE = "astrbot_plugin_proactive_chat"


@pytest.fixture
def plugins(tmp_path):
    legal = tmp_path / "legal/feature-set.json"
    legal.parent.mkdir(parents=True)
    legal.write_text(json.dumps({"features": [
        {"module": MUSIC, "display_name": "点歌", "configuration": "none"},
        {"module": PROACTIVE, "display_name": "主动聊天", "configuration": "none"},
        {"module": "jiang_commands", "display_name": "常用指令", "configuration": "none"},
        {"module": "astrbot_plugin_angel_heart", "display_name": "机器人核心", "configuration": "none"},
        {"module": "jiang_capability_router", "display_name": "路由", "configuration": "none", "visible": False},
    ]}), encoding="utf-8")
    engine = tmp_path / "app/astrbot/data/cmd_config.json"
    engine.parent.mkdir(parents=True)
    engine.write_text(json.dumps({"admins_id": ["wxid_admin", "wxid_admin2"], "plugin_set": ["*"]}), encoding="utf-8")
    policies = GroupPolicyService(tmp_path)
    policies.save(ACCOUNT, GROUP, {"allowed_features": [MUSIC, "jiang_commands", "jiang_commands.setu"], "daily_limit": 7})
    return SimpleNamespace(install_root=tmp_path)


def _payload(**changes):
    return {
        "action": "set_enabled", "scope": "current_group", "account_id": ACCOUNT, "group_id": GROUP,
        "sender_id": "wxid_admin", "message": "帮我在本群关闭点歌插件", "module": MUSIC, "enabled": False,
        **changes,
    }


def test_each_configured_admin_controls_current_group_only(plugins):
    policies = GroupPolicyService(plugins.install_root)
    other_group = "22222@chatroom"
    policies.save(ACCOUNT, other_group, {"allowed_features": [MUSIC]})
    policies.save("wxid_other_robot", GROUP, {"allowed_features": [MUSIC]})
    before_other = policies.get(ACCOUNT, other_group)
    before_account = policies.get("wxid_other_robot", GROUP)
    engine_path = plugins.install_root / "app/astrbot/data/cmd_config.json"
    before_engine = engine_path.read_bytes()
    private_path = plugins.install_root / "data/manager/private-chat-policies.json"
    private_path.write_text('{"unrelated":true}', encoding="utf-8")
    before_private = private_path.read_bytes()

    disabled = handle_plugin_control(plugins, _payload(sender_id="wxid_admin2"))
    assert disabled["enabled"] is False and disabled["applied"] is True
    assert {key: disabled[key] for key in ("scope", "account_id", "group_id")} == {
        "scope": "current_group", "account_id": ACCOUNT, "group_id": GROUP,
    }
    assert "当前群" in disabled["message"] and "全局" not in disabled["message"]
    assert policies.get(ACCOUNT, GROUP)["daily_limit"] == 7
    assert policies.get(ACCOUNT, GROUP)["updated_by"] == "wxid_admin2"
    assert policies.get(ACCOUNT, other_group) == before_other
    assert policies.get("wxid_other_robot", GROUP) == before_account
    assert engine_path.read_bytes() == before_engine
    assert private_path.read_bytes() == before_private
    assert not handle_plugin_control(plugins, _payload(action="list"))["plugins"][0]["enabled"]
    enabled = handle_plugin_control(plugins, _payload(enabled=True, message="打开点歌插件"))
    assert enabled["enabled"] is True and MUSIC in policies.get(ACCOUNT, GROUP)["allowed_features"]


@pytest.mark.parametrize("changes", [
    {"sender_id": "wxid_other"}, {"scope": None}, {"scope": "global"},
    {"group_id": ""}, {"group_id": "wxid_friend"}, {"account_id": ""},
    {"module": "jiang_capability_router"}, {"module": "astrbot_plugin_angel_heart"},
    {"enabled": "false"}, {"action": "install"},
    {"message": "怎么关闭点歌"}, {"message": "不要关闭点歌"}, {"message": "不想关闭点歌"},
    {"message": "能不能关闭点歌？"}, {"message": "可以关闭点歌吗？"},
    {"message": "如果关闭点歌会影响歌曲吗？"}, {"message": "点歌是否已经关闭"},
    {"message": "关闭定时任务"}, {"message": "开启点歌", "enabled": False},
    {"message": "关闭点歌再开启"}, {"message": "全局关闭点歌"},
    {"message": "所有群关闭点歌"}, {"message": "在其他群关闭点歌"},
    {"message": "在测试群关闭点歌"}, {"message": "关闭私聊点歌"},
    {"message": "他说关闭点歌"},
    {"message": "请勿关闭点歌"}, {"message": "禁止关闭点歌"}, {"message": "没让你关闭点歌"},
    {"message": "‘关闭点歌’是什么意思"}, {"message": "请解释关闭点歌这句话"},
    {"message": "明天关闭点歌"}, {"message": "关闭点歌是不是可以"},
    {"module": "jiang_commands", "message": "关闭jiang_commands.setu"},
    {"message": "关闭点歌吗？"}, {"message": "上次关闭点歌"},
    {"module": "jiang_commands.setu", "message": "关闭点歌，保留图来"},
    {"module": "jiang_commands.setu", "message": "关闭点歌保留图来"},
    {"module": "jiang_commands.setu", "message": "关闭点歌，图来保留"},
    {"module": "jiang_commands.setu", "message": "关闭点歌\n图来保留"},
])
def test_untrusted_or_ambiguous_requests_leave_group_state_unchanged(plugins, changes):
    path = plugins.install_root / "data/manager/group-policies.json"
    before = path.read_bytes()
    with pytest.raises(PluginCenterError):
        handle_plugin_control(plugins, _payload(**changes))
    assert path.read_bytes() == before


def test_control_does_not_claim_success_without_readback(plugins, monkeypatch):
    monkeypatch.setattr(GroupPolicyService, "set_feature_enabled", lambda *a, **k: {"saved": True, "allowed_features": [MUSIC]})
    with pytest.raises(PluginCenterError, match="状态未确认"):
        handle_plugin_control(plugins, _payload())


def test_picture_alias_only_toggles_independent_group_feature(plugins):
    result = handle_plugin_control(plugins, _payload(module="jiang_commands.setu", message="关闭涩图"))
    allowed = GroupPolicyService(plugins.install_root).get(ACCOUNT, GROUP)["allowed_features"]
    assert result["applied"] is True
    assert "jiang_commands.setu" not in allowed and "jiang_commands" in allowed and MUSIC in allowed
    catalog = handle_plugin_control(plugins, _payload(action="list"))["plugins"]
    assert {item["module"] for item in catalog} == {MUSIC, PROACTIVE, "jiang_commands", "jiang_commands.setu"}


def test_quoted_plugin_name_is_still_an_explicit_command(plugins):
    result = handle_plugin_control(plugins, _payload(message="关闭“点歌”插件"))
    assert result["enabled"] is False


def test_internal_http_control_requires_secret_and_rejects_foreign_origin(plugins):
    server = ThreadingHTTPServer(("127.0.0.1", 0), plugin_center_web._handler(plugins, plugin_control_token="secret-token"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}{CONTROL_PATH}"

    def request(headers):
        return urlopen(Request(url, data=json.dumps(_payload()).encode(), headers={"Content-Type": "application/json", **headers}), timeout=3)

    try:
        path = plugins.install_root / "data/manager/group-policies.json"
        before = path.read_bytes()
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer secret-token", "Origin": "https://foreign.example"}):
            with pytest.raises(HTTPError):
                request(headers)
        assert path.read_bytes() == before
        with request({"Authorization": "Bearer secret-token"}) as response:
            assert json.load(response)["data"]["enabled"] is False
        assert MUSIC not in GroupPolicyService(plugins.install_root).get(ACCOUNT, GROUP)["allowed_features"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_manager_creates_bridge_and_cleans_up_only_its_descriptor(plugins):
    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    api._install_root = plugins.install_root
    api._features = plugins
    path = plugins.install_root / CONTROL_DESCRIPTOR
    server = api._ensure_feature_page_server()
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        assert descriptor["url"] == f"http://127.0.0.1:{server.server_port}{CONTROL_PATH}"
        assert len(descriptor["token"]) >= 32
        assert api._ensure_feature_page_server() is server
    finally:
        api._close_feature_page_server()
    assert not path.exists()
    api._ensure_feature_page_server()
    path.write_text(json.dumps({"token": "a-new-manager-instance"}), encoding="utf-8")
    api._close_feature_page_server()
    assert path.exists()


def test_unwritable_bridge_descriptor_keeps_existing_manager_pages_available(plugins, monkeypatch):
    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    api._install_root = plugins.install_root
    api._features = plugins

    def unwritable(*_args):
        raise PermissionError("read-only descriptor directory")

    monkeypatch.setattr(app_shell, "write_json_atomic", unwritable)
    server = api._ensure_feature_page_server()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=3) as response:
            assert response.status == 200
        assert not (plugins.install_root / CONTROL_DESCRIPTOR).exists()
    finally:
        api._close_feature_page_server()


def test_runtime_bridge_uses_real_manager_authentication_and_group_policy(plugins, monkeypatch):
    from test_admin_plugin_runtime import control

    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    api._install_root = plugins.install_root
    api._features = plugins
    monkeypatch.setattr(control, "_install_root", lambda: plugins.install_root)
    api._ensure_feature_page_server()
    try:
        assert control._request(_payload(action="list"))["plugins"][0]["enabled"] is True
        result = control._request(_payload())
        assert result["enabled"] is False and result["applied"] is True
        assert result["module"] == MUSIC and result["group_id"] == GROUP
        assert control._request(_payload(action="list"))["plugins"][0]["enabled"] is False
        with pytest.raises(control.PluginControlError, match="管理员微信号"):
            control._request(_payload(sender_id="wxid_other"))
    finally:
        api._close_feature_page_server()


def test_checkbox_refresh_api_does_not_query_or_modify_schedules(plugins, monkeypatch):
    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    api._install_root = plugins.install_root
    api._group_policies = GroupPolicyService(plugins.install_root)
    monkeypatch.setattr(api._group_policies.schedules, "list", lambda **kwargs: pytest.fail("unrelated schedule query"))
    result = api.get_group_policy(ACCOUNT, GROUP, True)
    assert result["ok"] is True
    assert result["data"]["policy"]["group_id"] == GROUP
    assert result["data"]["features"][0]["allowed"] is True
    assert "proactive" not in result["data"]


@pytest.mark.parametrize("message,enabled", [
    ("@姜小妹 关闭该群的主动消息", False),
    ("关闭该群聊的主动消息", False),
    ("帮我把本群的主动消息关掉", False),
    ("请在这个群开启主动消息", True),
    ("打开“主动聊天”插件", True),
    ("关闭点歌", False),
])
def test_explicit_group_command_resolves_without_writing_then_uses_existing_toggle(plugins, message, enabled):
    path = plugins.install_root / "data/manager/group-policies.json"
    before = path.read_bytes()
    result = handle_plugin_control(plugins, _payload(action="resolve_message", message=message))
    assert result["resolved"] is True and result["enabled"] is enabled
    assert result["scope"] == "current_group" and result["account_id"] == ACCOUNT and result["group_id"] == GROUP
    assert result["module"] == (MUSIC if "点歌" in message else PROACTIVE)
    assert path.read_bytes() == before
    applied = handle_plugin_control(plugins, _payload(message=message, module=result["module"], enabled=enabled))
    assert applied["applied"] is True
    assert (result["module"] in GroupPolicyService(plugins.install_root).get(ACCOUNT, GROUP)["allowed_features"]) is enabled


@pytest.mark.parametrize("message", [
    "关闭窗户", "你好", "关闭未知插件", "怎么关闭该群主动消息", "不要关闭该群主动消息",
    "全局关闭主动消息", "关闭其他群的主动消息", "关闭该群的主动消息吗？",
    "他说关闭该群主动消息", "明天关闭该群主动消息", "‘关闭主动消息’是什么意思",
    "你觉得关闭主动消息会怎样", "关闭主动消息可能会更好吗", "关闭点歌和主动消息",
    "关闭点歌，保留主动消息", "我想了解关闭主动消息后的效果",
])
def test_automatic_resolution_does_not_treat_discussion_as_a_command(plugins, message):
    path = plugins.install_root / "data/manager/group-policies.json"
    before = path.read_bytes()
    result = handle_plugin_control(plugins, _payload(action="resolve_message", message=message))
    assert result["resolved"] is False
    assert "module" not in result and "enabled" not in result
    assert path.read_bytes() == before


def test_screenshot_command_through_real_bridge_stops_provider_and_only_changes_current_group(plugins, monkeypatch):
    from test_admin_plugin_runtime import At, Event, control, control_hook, dispatch_hook_before_provider

    policies = GroupPolicyService(plugins.install_root)
    policies.set_feature_enabled(ACCOUNT, GROUP, PROACTIVE, True)
    policies.save(ACCOUNT, "other@chatroom", {"allowed_features": [PROACTIVE]})
    policies.save("wxid_other_bot", GROUP, {"allowed_features": [PROACTIVE]})
    other_group = policies.get(ACCOUNT, "other@chatroom")
    other_account = policies.get("wxid_other_bot", GROUP)
    engine = plugins.install_root / "app/astrbot/data/cmd_config.json"
    engine_before = engine.read_bytes()
    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    api._install_root, api._features = plugins.install_root, plugins
    monkeypatch.setattr(control, "_install_root", lambda: plugins.install_root)
    calls = []

    async def request(payload):
        calls.append(payload)
        return await control.request_control(payload)

    plugin, hook = control_hook(request, admins=["wxid_admin", "wxid_admin2"])
    api._ensure_feature_page_server()
    try:
        for message, enabled in (("@姜小妹 关闭该群的主动消息", False), ("开启该群的主动消息", True)):
            event = Event(sender="wxid_admin2", account=ACCOUNT, group=GROUP, message=message)
            event.message_obj.message = [At(ACCOUNT)]
            req = SimpleNamespace(system_prompt="original")
            assert asyncio.run(dispatch_hook_before_provider(plugin, hook, event, req)) == 0
            assert len(event.sent) == 1 and "已在当前群" in event.sent[0]
            assert (PROACTIVE in policies.get(ACCOUNT, GROUP)["allowed_features"]) is enabled
            assert policies.get(ACCOUNT, GROUP)["scheduled_enabled"] is True
            assert policies.get(ACCOUNT, GROUP)["daily_limit"] == 7
            feature = next(item for item in api.get_group_policy(ACCOUNT, GROUP, True)["data"]["features"] if item["id"] == PROACTIVE)
            assert feature["allowed"] is enabled
            assert asyncio.run(dispatch_hook_before_provider(plugin, hook, event, req)) == 0
            assert len(event.sent) == 1
        assert [call["action"] for call in calls] == ["resolve_message", "set_enabled"] * 2
        assert policies.get(ACCOUNT, "other@chatroom") == other_group
        assert policies.get("wxid_other_bot", GROUP) == other_account
        assert engine.read_bytes() == engine_before
    finally:
        api._close_feature_page_server()
