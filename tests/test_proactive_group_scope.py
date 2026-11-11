"""Exercise managed proactive group scope without starting a robot."""
from __future__ import annotations

import asyncio
from datetime import timezone
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_background_plugin_permissions import background
from test_hot_config_runtime import _runtime_function
from test_plugin_portability import _patched


MODULE = "astrbot_plugin_proactive_chat"
SESSION = "wechat:GroupMessage:room@chatroom"
PACKAGE = "proactive_scope_fixture.core"


def _load_module(monkeypatch, name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scoped(background, tmp_path, monkeypatch):
    checkout = tmp_path / "plugin"
    checkout.mkdir()
    _patched(checkout, MODULE, "core/session_config.py", all_patches=True)
    for name, path in (("proactive_scope_fixture", checkout), (PACKAGE, checkout / "core")):
        package = ModuleType(name)
        package.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, "astrbot.api", NS(logger=Mock()))
    monkeypatch.setitem(sys.modules, "astrbot.core.platform.platform", NS(
        PlatformStatus=NS(RUNNING="running")))
    scope = _load_module(monkeypatch, f"{PACKAGE}.wechat_group_scope",
                         checkout / "core/wechat_group_scope.py")
    config = _load_module(monkeypatch, f"{PACKAGE}.session_config",
                          checkout / "core/session_config.py")
    parser = _load_module(monkeypatch, f"{PACKAGE}.session_parser",
                          checkout / "core/session_parser.py")
    scheduler = _load_module(monkeypatch, f"{PACKAGE}.task_scheduler",
                             checkout / "core/task_scheduler.py")
    background.context.platform_manager = NS(get_insts=lambda: [background.platform])
    background.context.get_platform_inst = (
        lambda platform_id: background.platform if platform_id == "wechat" else None
    )
    plugin = type("Plugin", (config.ConfigMixin, parser.SessionMixin, scheduler.SchedulerMixin), {})()
    plugin.context = background.context
    plugin.config = {
        "group_settings": {"enable": True, "session_list": []},
        "friend_settings": {"enable": True, "session_list": []},
    }
    plugin.session_data = {SESSION: {"self_id": "wxid_bot"}}
    plugin.session_override_manager = None
    plugin.data_lock = asyncio.Lock()
    plugin._save_data_internal = AsyncMock()
    plugin.scheduler = NS(get_job=Mock(return_value=None), add_job=Mock())
    plugin._cleanup_invalid_session_data = Mock(return_value=0)
    plugin.check_and_chat = AsyncMock()
    plugin.timezone = timezone.utc
    return NS(plugin=plugin, scope=scope, checkout=checkout, background=background,
              policy=Path.cwd().parent.parent / "data/manager/group-policies.json")


def test_managed_group_uses_conversation_grant_without_plugin_list(scoped):
    config = scoped.plugin._get_session_config(SESSION)
    assert config is not None
    assert config["_session_type"] == "group"
    assert config["enable"] is True
    config["enable"] = False
    assert scoped.plugin.config["group_settings"]["enable"] is True


@pytest.mark.parametrize("policy_change", ["feature", "disabled", "reply_off", "global"])
def test_old_plugin_list_cannot_override_managed_group_denial(scoped, policy_change):
    scoped.plugin.config["group_settings"]["session_list"] = [SESSION]
    if policy_change == "feature":
        scoped.background.save([])
    elif policy_change == "global":
        scoped.background.config["plugin_set"] = []
    else:
        payload = json.loads(scoped.policy.read_text("utf-8"))
        group = payload["accounts"]["wxid_bot"]["groups"]["room@chatroom"]
        group.update({"bot_enabled": False} if policy_change == "disabled"
                     else {"reply_mode": "off"})
        scoped.policy.write_text(json.dumps(payload), encoding="utf-8")
    assert scoped.plugin._get_session_config(SESSION) is None


@pytest.mark.parametrize("identity", ["", "other-account", None])
def test_missing_or_different_account_has_no_managed_group_config(scoped, identity):
    scoped.plugin.session_data[SESSION]["self_id"] = identity
    assert scoped.plugin._get_session_config(SESSION) is None


def test_corrupt_group_policy_fails_closed(scoped):
    scoped.policy.write_text("{broken", encoding="utf-8")
    assert scoped.plugin._get_session_config(SESSION) is None
    assert asyncio.run(scoped.scope.discover_group_sessions(scoped.plugin)) == []


def test_discovery_only_uses_authorized_groups_of_confirmed_account(scoped):
    payload = json.loads(scoped.policy.read_text("utf-8"))
    payload["accounts"]["wxid_bot"]["groups"].update({
        "denied@chatroom": {"allowed_features": []},
        "disabled@chatroom": {"allowed_features": [MODULE], "bot_enabled": False},
    })
    payload["accounts"]["other-account"] = {"groups": {
        "other@chatroom": {"allowed_features": [MODULE]},
    }}
    scoped.policy.write_text(json.dumps(payload), encoding="utf-8")
    scoped.plugin.session_data.clear()
    assert asyncio.run(scoped.scope.discover_group_sessions(scoped.plugin)) == [SESSION]
    assert scoped.plugin.session_data[SESSION]["self_id"] == "wxid_bot"
    assert scoped.scope.configured_group_sessions(scoped.plugin) == [SESSION]
    scoped.background.save([])
    assert scoped.scope.configured_group_sessions(scoped.plugin) == []


def test_discovery_does_not_guess_account_when_login_is_unavailable(scoped):
    scoped.background.platform.bot.get_login_info = AsyncMock(side_effect=TimeoutError)
    scoped.plugin.session_data.clear()
    assert asyncio.run(scoped.scope.discover_group_sessions(scoped.plugin)) == []
    assert scoped.plugin.session_data == {}


def test_startup_discovers_groups_when_platform_connects_after_initialization(scoped, monkeypatch):
    available = []
    scoped.background.context.platform_manager.get_insts = lambda: list(available)
    scoped.plugin.session_data.clear()
    scoped.plugin.auto_trigger_timers = {}
    scoped.plugin.last_message_times = {}
    scoped.plugin._setup_auto_trigger_for_session_config = AsyncMock()

    async def connect(_seconds):
        available.append(scoped.background.platform)

    monkeypatch.setattr(scoped.scope.asyncio, "sleep", AsyncMock(side_effect=connect))
    asyncio.run(scoped.scope.refresh_group_scope(scoped.plugin))
    scoped.plugin._setup_auto_trigger_for_session_config.assert_awaited_once_with(
        scoped.plugin.config["group_settings"], SESSION)
    assert scoped.plugin.session_data[SESSION]["self_id"] == "wxid_bot"


def test_persisted_group_plan_survives_platform_startup_and_restores_original_time(scoped):
    available = []
    scoped.background.context.platform_manager.get_insts = lambda: list(available)
    scoped.background.context.get_platform_inst = lambda pid: (
        scoped.background.platform if available and pid == "wechat" else None
    )
    due = int(time.time()) + 300
    saved = {"self_id": "wxid_bot", "next_trigger_time": due, "unanswered_count": 1}
    scoped.plugin.session_data[SESSION] = saved.copy()
    asyncio.run(scoped.plugin._init_jobs_from_data())
    assert scoped.plugin.session_data[SESSION] == saved
    scoped.plugin.scheduler.add_job.assert_not_called()

    friend = "wechat:FriendMessage:friend"
    private_saved = {"self_id": "wxid_bot", "next_trigger_time": due + 60, "unanswered_count": 2}
    scoped.plugin.session_data[friend] = private_saved.copy()
    scoped.plugin.auto_trigger_timers = {}
    scoped.plugin.last_message_times = {}
    available.append(scoped.background.platform)
    asyncio.run(scoped.scope.refresh_group_scope(scoped.plugin))
    scoped.plugin.scheduler.add_job.assert_called_once()
    restored = scoped.plugin.scheduler.add_job.call_args.kwargs
    assert restored["id"] == SESSION
    assert restored["run_date"].timestamp() == due
    assert scoped.plugin.session_data[SESSION] == saved
    assert scoped.plugin.session_data[friend] == private_saved


def test_group_global_switch_still_disables_managed_group(scoped):
    scoped.plugin.config["group_settings"]["enable"] = False
    assert scoped.plugin._get_session_config(SESSION) is None


def test_private_and_other_platform_groups_keep_existing_target_lists(scoped):
    friend = "wechat:FriendMessage:friend"
    assert scoped.plugin._get_session_config(friend) is None
    scoped.plugin.config["friend_settings"]["session_list"] = [friend]
    assert scoped.plugin._get_session_config(friend)["_session_type"] == "friend"

    telegram = NS(meta=lambda: NS(name="telegram", id="telegram"), status="running")
    scoped.background.context.platform_manager.get_insts = lambda: [scoped.background.platform, telegram]
    scoped.background.context.get_platform_inst = lambda pid: {
        "wechat": scoped.background.platform, "telegram": telegram,
    }.get(pid)
    other_group = "telegram:GroupMessage:-12345"
    assert scoped.plugin._get_session_config(other_group) is None
    scoped.plugin.config["group_settings"]["session_list"] = [other_group]
    assert scoped.plugin._get_session_config(other_group)["_session_type"] == "group"


def test_offline_wechat_session_does_not_fall_back_to_another_platform(scoped):
    scoped.background.platform.status = "stopped"
    other = NS(meta=lambda: NS(name="telegram", id="telegram"), status="running")
    scoped.background.context.platform_manager.get_insts = lambda: [scoped.background.platform, other]
    assert scoped.plugin._normalize_session_id(SESSION) == SESSION


def test_account_change_rejects_existing_group_task(scoped):
    scoped.background.account[0] = "other-account"
    assert not asyncio.run(scoped.scope.authorize_group_chat(scoped.plugin, SESSION))
    assert scoped.plugin.session_data[SESSION]["self_id"] == "wxid_bot"


def test_discovery_does_not_rebind_old_tasks_to_another_authorized_account(scoped):
    payload = json.loads(scoped.policy.read_text("utf-8"))
    payload["accounts"]["other-account"] = {"groups": {
        "room@chatroom": {"allowed_features": [MODULE]},
    }}
    scoped.policy.write_text(json.dumps(payload), encoding="utf-8")
    scoped.background.account[0] = "other-account"
    assert asyncio.run(scoped.scope.discover_group_sessions(scoped.plugin)) == []
    assert scoped.plugin.session_data[SESSION]["self_id"] == "wxid_bot"
    assert scoped.plugin._get_session_config(SESSION) is None


def test_inflight_generation_keeps_account_when_new_event_rebinds_chat(scoped):
    async def generate_then_switch():
        assert await scoped.scope.authorize_group_chat(scoped.plugin, SESSION)
        token = scoped.scope.pin_group_generation(scoped.plugin, SESSION)
        try:
            scoped.background.account[0] = "other-account"
            scoped.plugin.session_data[SESSION]["self_id"] = "other-account"
            scoped.scope.remember_group_account(scoped.plugin, NS(
                unified_msg_origin=SESSION, get_self_id=lambda: "other-account"))
            with pytest.raises(PermissionError):
                scoped.scope.bound_group_account(scoped.plugin, SESSION)
        finally:
            scoped.scope.reset_group_generation(token)
        assert scoped.scope.bound_group_account(scoped.plugin, SESSION) == "other-account"

    asyncio.run(generate_then_switch())


def test_revoked_group_does_not_prepare_or_generate_model_request(scoped):
    source = (scoped.checkout / "core/chat_flow.py").read_text("utf-8")
    namespace = {"__package__": PACKAGE, "logger": Mock(),
                 "authorize_group_chat": scoped.scope.authorize_group_chat,
                 "is_quiet_time": Mock(return_value=False)}
    allowed = _runtime_function(source, "_is_chat_allowed", namespace)
    run = _runtime_function(source, "check_and_chat", namespace)
    owner = scoped.plugin
    owner._is_chat_allowed = lambda session: allowed(owner, session)
    owner._prepare_llm_request = AsyncMock()
    owner._generate_llm_response = AsyncMock()
    owner._send_proactive_message = AsyncMock()
    owner._schedule_next_chat_and_save = AsyncMock()
    owner._clear_manual_trigger_state = AsyncMock()
    owner._get_session_log_str = lambda session, *_: session
    owner.telemetry = None
    scoped.background.save([])
    asyncio.run(run(owner, SESSION))
    owner._prepare_llm_request.assert_not_called()
    owner._generate_llm_response.assert_not_called()
    owner._send_proactive_message.assert_not_called()
