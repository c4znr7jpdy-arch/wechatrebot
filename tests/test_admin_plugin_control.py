from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wechatrebot_manager import admin_plugin_control, app_shell, plugin_center_web
from wechatrebot_manager.admin_plugin_control import CONTROL_DESCRIPTOR, CONTROL_PATH, handle_plugin_control
from wechatrebot_manager.plugin_errors import PluginCenterError


class _Plugins:
    def __init__(self, root: Path) -> None:
        self.install_root = root
        self.enabled = True
        self.calls = []

    def global_control_catalog(self):
        return [{"id": "music", "module": "astrbot_plugin_music", "name": "点歌", "enabled": self.enabled}]

    def set_enabled(self, public_id, enabled, *, require_online=False):
        assert require_online
        self.calls.append((public_id, enabled))
        self.enabled = enabled
        return {"applied": True}


@pytest.fixture
def plugins(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_plugin_control, "load_robot_settings", lambda root: SimpleNamespace(admin_id="wxid_admin"))
    return _Plugins(tmp_path)


def _payload(**changes):
    return {"action": "set_enabled", "sender_id": "wxid_admin", "message": "帮我全局关闭点歌插件", "module": "astrbot_plugin_music", "enabled": False, **changes}


def test_admin_can_disable_and_reenable_with_confirmed_global_state(plugins):
    disabled = handle_plugin_control(plugins, _payload())
    assert disabled["module"] == "astrbot_plugin_music" and disabled["enabled"] is False
    assert handle_plugin_control(plugins, _payload(action="list"))["plugins"][0]["enabled"] is False
    enabled = handle_plugin_control(plugins, _payload(enabled=True, message="打开点歌插件"))
    assert enabled["enabled"] is True
    assert plugins.calls == [("music", False), ("music", True)]


@pytest.mark.parametrize("changes", [
    {"sender_id": "wxid_other"},
    {"module": "jiang_capability_router"},
    {"enabled": "false"},
    {"action": "install"},
    {"message": "怎么关闭点歌"},
    {"message": "不要关闭点歌"},
    {"message": "不想关闭点歌"},
    {"message": "能不能关闭点歌？"},
    {"message": "可以关闭点歌吗？"},
    {"message": "如果关闭点歌会影响歌曲吗？"},
    {"message": "点歌是否已经关闭"},
    {"message": "关闭定时任务"},
    {"message": "开启点歌", "enabled": False},
    {"message": "关闭点歌再开启"},
])
def test_untrusted_or_non_action_requests_cannot_change_plugins(plugins, changes):
    with pytest.raises(PluginCenterError):
        handle_plugin_control(plugins, _payload(**changes))
    assert not plugins.calls


def test_control_does_not_claim_success_without_readback(plugins):
    plugins.set_enabled = lambda *_args, **_kwargs: None
    with pytest.raises(PluginCenterError, match="状态未确认"):
        handle_plugin_control(plugins, _payload())


def test_saved_offline_state_is_not_reported_as_applied(plugins):
    def persist_only(*_args, **_kwargs):
        plugins.enabled = False
        return {"applied": False}
    plugins.set_enabled = persist_only
    result = handle_plugin_control(plugins, _payload())
    assert result["applied"] is False
    assert "尚未确认" in result["message"]


def test_alias_targets_control_the_real_module_and_explain_linked_features(plugins):
    read = plugins.global_control_catalog
    plugins.global_control_catalog = lambda: [{**read()[0], "name": "常用指令", "aliases": ["图来", "涩图"]}]
    result = handle_plugin_control(plugins, _payload(message="关闭图来插件"))
    assert result["applied"] is True
    assert "一并同步" in result["message"]


def test_internal_http_control_requires_secret_and_rejects_foreign_origin(plugins):
    server = ThreadingHTTPServer(("127.0.0.1", 0), plugin_center_web._handler(plugins, plugin_control_token="secret-token"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}{CONTROL_PATH}"

    def request(headers):
        return urlopen(Request(url, data=json.dumps(_payload()).encode(), headers={"Content-Type": "application/json", **headers}), timeout=3)

    try:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer secret-token", "Origin": "https://foreign.example"}):
            with pytest.raises(HTTPError):
                request(headers)
        assert not plugins.calls
        with request({"Authorization": "Bearer secret-token"}) as response:
            assert json.load(response)["data"]["enabled"] is False
        assert plugins.calls == [("music", False)]
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


def test_global_state_api_revision_changes_with_state(tmp_path, monkeypatch):
    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    api._install_root = tmp_path
    states = {"astrbot_plugin_music": True}
    monkeypatch.setattr(app_shell, "global_plugin_states", lambda root: states.copy())
    before = api.get_global_plugin_states()["data"]
    assert "get_global_plugin_states" in app_shell.PUBLIC_API_METHODS
    states["astrbot_plugin_music"] = False
    after = api.get_global_plugin_states()["data"]
    assert after["states"]["astrbot_plugin_music"] is False
    assert before["revision"] != after["revision"]
