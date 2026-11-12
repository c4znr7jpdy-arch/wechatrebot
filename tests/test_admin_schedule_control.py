import json
import sqlite3
from types import SimpleNamespace

import pytest

from wechatrebot_manager.admin_plugin_control import handle_plugin_control
from wechatrebot_manager.admin_schedule_control import resolve_schedule_intent
from wechatrebot_manager.group_policy import GroupPolicyService
from wechatrebot_manager.plugin_errors import PluginCenterError
from wechatrebot_manager.task_store import TaskStore


ACCOUNT = "wxid_robot"
GROUP = "11111@chatroom"
OTHER_GROUP = "22222@chatroom"


def _payload(**changes):
    return {"resource": "scheduled_tasks", "action": "set_enabled", "scope": "current_group",
            "account_id": ACCOUNT, "group_id": GROUP, "sender_id": "wxid_admin",
            "message": "停止该群的定时任务 @姜小妹", "enabled": False, **changes}


def _task(identity, *, account=ACCOUNT, group=GROUP, **changes):
    return {"id": identity, "account_id": account, "name": f"提醒 {identity}", "action": "text",
            "arguments": {"text": "不应暴露的内容"}, "targets": [{"type": "group", "id": group}],
            "schedule": {"kind": "daily", "time": "08:30", "timezone": "Asia/Shanghai"},
            "enabled": True, "draft": False, **changes}


@pytest.fixture
def installation(tmp_path):
    config = tmp_path / "app/astrbot/data/cmd_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"admins_id": ["wxid_admin", "wxid_admin2"]}), encoding="utf-8")
    policies = GroupPolicyService(tmp_path)
    policies.save(ACCOUNT, GROUP, {"daily_limit": 7, "reply_mode": "natural"})
    policies.save(ACCOUNT, OTHER_GROUP, {"daily_limit": 4})
    policies.save("wxid_other_robot", GROUP, {"daily_limit": 5})
    store = TaskStore(tmp_path / "data/manager/scheduled-tasks.db")
    store.save(_task("single"))
    store.save(_task("shared", targets=[{"type": "group", "id": GROUP},
                                        {"type": "group", "id": OTHER_GROUP}]))
    store.save(_task("other-group", group=OTHER_GROUP))
    store.save(_task("other-account", account="wxid_other_robot"))
    store.save(_task("private", targets=[{"type": "private", "id": "wxid_friend"}]))
    return SimpleNamespace(install_root=tmp_path)


def test_lists_manager_tasks_for_the_current_account_and_group_only(installation):
    result = handle_plugin_control(installation, _payload(action="list"))
    assert result["task_count"] == result["enabled_count"] == 2
    assert {task["id"] for task in result["tasks"]} == {"single", "shared"}
    assert all(set(task) == {"id", "name", "schedule_text", "enabled", "draft"}
               for task in result["tasks"])
    assert result["scheduled_enabled"] is True
    assert {key: result[key] for key in ("scope", "account_id", "group_id")} == {
        "scope": "current_group", "account_id": ACCOUNT, "group_id": GROUP,
    }


@pytest.mark.parametrize("sender", ["wxid_admin", "wxid_admin2"])
def test_pause_and_resume_only_change_current_group_receiving(installation, sender):
    policies = GroupPolicyService(installation.install_root)
    store = TaskStore(installation.install_root / "data/manager/scheduled-tasks.db")
    before = policies.get(ACCOUNT, GROUP)
    before_other = policies.get(ACCOUNT, OTHER_GROUP)
    before_account = policies.get("wxid_other_robot", GROUP)
    before_tasks = store.list(ACCOUNT)
    for message, enabled in (("停止该群的定时任务 @姜小妹", False), ("@姜小妹 恢复本群的定时推送", True)):
        result = handle_plugin_control(installation, _payload(sender_id=sender, message=message, enabled=enabled))
        assert result["applied"] is True and result["enabled"] is enabled
        assert result["scheduled_enabled"] is enabled
        assert "2 个任务" in result["message"] and "仅调整本群接收开关" in result["message"]
        current = policies.get(ACCOUNT, GROUP)
        assert current["scheduled_enabled"] is enabled and current["updated_by"] == sender
        for key in ("allowed_features", "daily_limit", "quiet_hours", "reply_mode", "bot_enabled"):
            assert current[key] == before[key]
        assert policies.get(ACCOUNT, OTHER_GROUP) == before_other
        assert policies.get("wxid_other_robot", GROUP) == before_account
        assert store.list(ACCOUNT) == before_tasks


@pytest.mark.parametrize("message,enabled", [
    ("停止该群的定时任务 @姜小妹", False), ("@姜小妹 停止该群的定时任务", False),
    ("暂停本群全部定时推送", False), ("请在这个群暂停全部定时推送", False),
    ("帮我把本群所有的定时任务停掉", False), ("关闭定时任务", False),
    ("恢复该群定时任务", True), ("开启本群全部定时任务", True),
    ("本群的定时推送打开", True), ("麻烦帮我恢复本群的定时任务，谢谢", True),
])
def test_resolver_accepts_only_explicit_current_group_commands(installation, message, enabled):
    path = installation.install_root / "data/manager/group-policies.json"
    before = path.read_bytes()
    resolved = handle_plugin_control(installation, _payload(action="resolve_message", message=message))
    assert resolved["resolved"] is True and resolved["enabled"] is enabled
    assert path.read_bytes() == before
    result = handle_plugin_control(installation, _payload(message=message, enabled=enabled))
    assert result["applied"] is True


@pytest.mark.parametrize("message", [
    "不要停止该群的定时任务", "别暂停本群定时任务", "不用关闭定时任务", "关闭定时任务吗？",
    "能不能关闭本群定时任务", "怎么关闭本群定时任务", "如果停止本群定时任务", "本群定时任务已经停止",
    "他说停止本群的定时任务", "“停止本群的定时任务”", "引用：停止本群定时任务", "转发 停止本群定时任务",
    "停止所有群的定时任务", "停止其他群的定时任务", "停止测试群的定时任务", "全局关闭定时任务",
    "停止本群的天气定时任务", "停止本群第一个定时任务", "停止本群一个定时任务", "停止本群定时任务 task-1",
    "停止本群定时任务，保留天气", "停止本群的定时任务然后恢复", "停止本群的定时任务\n恢复该群定时任务",
    "明天停止本群的定时任务", "稍后停止本群的定时任务", "停止本群 @姜小妹 的定时任务", "你好",
    "停止11111@chatroom的定时任务", "停止私聊定时任务", "本群有几个定时任务", "停止定时任务及主动聊天",
    "@姜小妹 不要停止本群定时任务", "不要 @姜小妹 停止本群定时任务", "停止本群定时任务 @姜小妹 请保留天气",
])
def test_ambiguous_or_unauthorized_intent_never_changes_policy(installation, message):
    path = installation.install_root / "data/manager/group-policies.json"
    before = path.read_bytes()
    assert resolve_schedule_intent(message) is None
    result = handle_plugin_control(installation, _payload(action="resolve_message", message=message))
    assert result["resolved"] is False and "enabled" not in result
    with pytest.raises(PluginCenterError, match="未明确要求"):
        handle_plugin_control(installation, _payload(message=message))
    assert path.read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"sender_id": "wxid_member"}, {"scope": "global"}, {"scope": None},
    {"account_id": ""}, {"group_id": "wxid_friend"}, {"group_id": ""},
    {"enabled": "false"}, {"action": "delete"}, {"enabled": True}, {"revision": ""},
])
def test_invalid_requests_do_not_write(installation, changes):
    path = installation.install_root / "data/manager/group-policies.json"
    before = path.read_bytes()
    with pytest.raises(PluginCenterError):
        handle_plugin_control(installation, _payload(**changes))
    assert path.read_bytes() == before


def test_stale_revision_does_not_overwrite_another_administrators_edit(installation):
    original = handle_plugin_control(installation, _payload(action="list"))
    policies = GroupPolicyService(installation.install_root)
    policies.save(ACCOUNT, GROUP, {"daily_limit": 9})
    with pytest.raises(PluginCenterError, match="其他窗口修改"):
        handle_plugin_control(installation, _payload(revision=original["revision"]))
    assert policies.get(ACCOUNT, GROUP)["daily_limit"] == 9
    assert policies.get(ACCOUNT, GROUP)["scheduled_enabled"] is True


def test_pause_does_not_claim_success_without_saved_state_readback(installation, monkeypatch):
    monkeypatch.setattr(GroupPolicyService, "save", lambda *a, **k: {"saved": True, "scheduled_enabled": True})
    with pytest.raises(PluginCenterError, match="状态未确认"):
        handle_plugin_control(installation, _payload())


@pytest.mark.parametrize("failure", [sqlite3.DatabaseError("private details"), ValueError("private details")])
def test_unreadable_tasks_fail_without_misreporting_zero_or_changing_policy(installation, monkeypatch, failure):
    path = installation.install_root / "data/manager/group-policies.json"
    before = path.read_bytes()

    def fail(*_args):
        raise failure

    monkeypatch.setattr(TaskStore, "list", fail)
    for action in ("list", "set_enabled"):
        with pytest.raises(PluginCenterError, match="读取失败") as captured:
            handle_plugin_control(installation, _payload(action=action))
        assert "private details" not in str(captured.value)
    assert path.read_bytes() == before


def test_empty_group_is_reported_truthfully_and_can_pause_future_reception(installation):
    result = handle_plugin_control(installation, _payload(group_id="empty@chatroom"))
    assert result["task_count"] == result["enabled_count"] == 0
    assert result["tasks"] == [] and result["scheduled_enabled"] is False
    assert "0 个任务" in result["message"] and "已暂停本群接收" in result["message"]


def test_paused_and_draft_tasks_are_counted_without_claiming_enabled(installation):
    store = TaskStore(installation.install_root / "data/manager/scheduled-tasks.db")
    single = store.get(ACCOUNT, "single")
    store.set_enabled(ACCOUNT, "single", False, single["revision"])
    store.save(_task("draft", enabled=False, draft=True))
    result = handle_plugin_control(installation, _payload(action="list"))
    assert result["task_count"] == 3 and result["enabled_count"] == 1


def test_screenshot_stop_and_resume_through_authenticated_runtime_bridge(installation, monkeypatch):
    import asyncio

    from test_admin_plugin_runtime import At, Event, control, control_hook, dispatch_hook_before_provider
    from wechatrebot_manager import app_shell

    policies = GroupPolicyService(installation.install_root)
    store = TaskStore(installation.install_root / "data/manager/scheduled-tasks.db")
    before_tasks = store.list(ACCOUNT)
    before_other_group = policies.get(ACCOUNT, OTHER_GROUP)
    before_other_account = policies.get("wxid_other_robot", GROUP)
    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    api._install_root, api._features = installation.install_root, installation
    api._group_policies = policies
    monkeypatch.setattr(control, "_install_root", lambda: installation.install_root)
    calls, receipts = [], []

    async def request(payload):
        calls.append(payload)
        response = await control.request_control(payload)
        receipts.append(response)
        return response

    plugin, hook = control_hook(request, admins=["wxid_admin", "wxid_admin2"])
    api._ensure_feature_page_server()
    try:
        for sender, message, enabled in (
            ("wxid_admin2", "停止该群的定时任务 @姜小妹", False),
            ("wxid_admin", "@姜小妹 恢复该群的定时任务", True),
        ):
            event = Event(sender=sender, account=ACCOUNT, group=GROUP, message=message)
            event.message_obj.message = [At(ACCOUNT)]
            req = SimpleNamespace(system_prompt="original")
            assert asyncio.run(dispatch_hook_before_provider(plugin, hook, event, req)) == 0
            assert len(event.sent) == 1 and "2 个任务" in event.sent[0]
            assert ("已恢复本群接收" if enabled else "已暂停本群接收") in event.sent[0]
            applied = receipts[-1]
            assert applied["task_count"] == applied["enabled_count"] == 2
            assert applied["scheduled_enabled"] is enabled and applied["applied"] is True
            manager = api.get_group_policy(ACCOUNT, GROUP, True)
            assert manager["ok"] is True
            assert manager["data"]["policy"]["scheduled_enabled"] is enabled
            assert manager["data"]["policy"]["updated_by"] == sender
            assert store.list(ACCOUNT) == before_tasks
            assert policies.get(ACCOUNT, OTHER_GROUP) == before_other_group
            assert policies.get("wxid_other_robot", GROUP) == before_other_account
            assert asyncio.run(dispatch_hook_before_provider(plugin, hook, event, req)) == 0
            assert len(event.sent) == 1
        assert [call["action"] for call in calls] == ["resolve_message", "set_enabled"] * 2
        assert all(call["resource"] == "scheduled_tasks" for call in calls)
    finally:
        api._close_feature_page_server()
