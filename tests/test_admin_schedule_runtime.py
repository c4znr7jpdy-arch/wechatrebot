from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from test_admin_plugin_runtime import (
    At, AtAll, Event, ToolSet, context, control, control_hook,
    dispatch_hook_before_provider, group_scope, runtime_function,
)


def receipt(**changes):
    return {
        **group_scope(), "scheduled_enabled": True, "task_count": 2, "enabled_count": 2,
        "revision": "saved-revision",
        "tasks": [
            {"id": "weather", "name": "早间天气", "schedule_text": "每天 08:00", "enabled": True, "draft": False},
            {"id": "news", "name": "早间新闻", "schedule_text": "每天 09:00", "enabled": True, "draft": False},
        ], **changes,
    }


def test_group_task_list_reads_manager_tasks_and_reports_group_pause_separately():
    calls = []

    async def request(payload):
        calls.append(payload)
        return receipt(scheduled_enabled=False)

    event = Event(message="本群有哪些定时任务？")
    listing = runtime_function("current_group_scheduled_tasks_tool", request_control=request)
    result = asyncio.run(listing(NS(context=context()), event))
    assert "2 个定时任务" in result and "早间天气" in result and "早间新闻" in result
    assert "本群定时接收已暂停" in result and result.count("任务已启用") == 2
    assert "任务中的其他聊天不受影响" in result
    assert calls == [{"resource": "scheduled_tasks", "action": "list", **group_scope(),
                      "sender_id": "wxid_admin", "message": "本群有哪些定时任务？"}]


@pytest.mark.parametrize("changes", [
    {"tasks": None}, {"tasks": []}, {"task_count": "2"}, {"scheduled_enabled": None},
    {"tasks": [{"name": "天气"}, {"name": "新闻"}]}, {"scope": "global"},
    {"group_id": "456@chatroom"}, {"account_id": "wxid_other"},
])
def test_invalid_task_read_cannot_be_reported_as_empty_or_expose_other_group(changes):
    async def request(_payload):
        return receipt(**changes)

    listing = runtime_function("current_group_scheduled_tasks_tool", request_control=request)
    result = asyncio.run(listing(NS(context=context()), Event()))
    assert "无法确认" in result and "没有" not in result and "0 个" not in result
    assert "早间天气" not in result


@pytest.mark.parametrize("message,enabled", [
    ("停止该群的定时任务 @姜小妹", False),
    ("暂停本群全部定时任务", False),
    ("恢复本群定时任务", True),
])
def test_explicit_schedule_control_uses_real_scope_and_stops_before_provider(message, enabled):
    calls = []
    event = Event(message=message)
    event.message_obj.message = [At(event.account)]
    event.get_message_str = lambda: "模型改写的文本不作为授权"
    response = receipt(scheduled_enabled=enabled, applied=True, message="本群定时接收设置已保存。")

    async def request(payload):
        calls.append(payload)
        if payload["action"] == "resolve_message":
            return {**group_scope(), "resolved": True, "enabled": enabled}
        return response

    plugin, hook = control_hook(request)
    req = NS(system_prompt="original")
    assert asyncio.run(dispatch_hook_before_provider(plugin, hook, event, req)) == 0
    assert asyncio.run(dispatch_hook_before_provider(plugin, hook, event, req)) == 0
    assert [item["action"] for item in calls] == ["resolve_message", "set_enabled"]
    assert all(item["resource"] == "scheduled_tasks" and item["message"] == message for item in calls)
    assert all(item["account_id"] == event.account and item["group_id"] == event.group for item in calls)
    assert calls[-1]["enabled"] is enabled
    assert event.sent == [response["message"]]


@pytest.mark.parametrize("stage", ["resolve_message", "set_enabled"])
def test_schedule_bridge_failure_returns_uncertainty_without_empty_or_repeat(stage):
    calls = []

    async def request(payload):
        calls.append(payload)
        if payload["action"] == stage:
            raise control.PluginControlError("管理接口暂不可用")
        return {**group_scope(), "resolved": True, "enabled": False}

    event, req = Event(message="停止该群的定时任务"), NS(system_prompt="original")
    plugin, hook = control_hook(request)
    assert asyncio.run(dispatch_hook_before_provider(plugin, hook, event, req)) == 0
    assert asyncio.run(dispatch_hook_before_provider(plugin, hook, event, req)) == 0
    assert len(calls) == (1 if stage == "resolve_message" else 2)
    assert len(event.sent) == 1 and "无法确认" in event.sent[0]
    assert "没有" not in event.sent[0] and "已暂停" not in event.sent[0]


@pytest.mark.parametrize("changes", [
    {"applied": None}, {"scheduled_enabled": True}, {"revision": None},
    {"message": ""}, {"scope": "global"}, {"group_id": "456@chatroom"},
])
def test_schedule_write_requires_confirmed_same_group_receipt(changes):
    calls = []

    async def request(payload):
        calls.append(payload)
        return receipt(scheduled_enabled=False, applied=True, message="已暂停本群定时接收。") | changes

    toggle = runtime_function("current_group_scheduled_tasks_tool", request_control=request)
    event, plugin = Event(message="停止本群定时任务"), NS(context=context())
    result = asyncio.run(toggle(plugin, event, "set_enabled", False))
    assert "无法确认" in result and "已暂停" not in result
    assert asyncio.run(toggle(plugin, event, "set_enabled", False)) == result
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["private", "non-admin", "other-mention", "all-mention", "unverified-trailing-mention"])
def test_schedule_tool_is_hidden_and_denied_outside_addressed_admin_group(kind):
    event = Event(message="停止本群定时任务")
    if kind == "private":
        event.private = True
    elif kind == "non-admin":
        event.sender = "wxid_other"
    elif kind == "unverified-trailing-mention":
        event.message_obj.message_str += " @张三"
    else:
        event.message_obj.message = [AtAll() if kind == "all-mention" else At("wxid_other")]

    async def forbidden(_payload):
        pytest.fail("unauthorized schedule request reached manager")

    plugin = NS(context=context())
    schedule = runtime_function("current_group_scheduled_tasks_tool", request_control=forbidden)
    assert "只管理当前群" in asyncio.run(schedule(plugin, event))
    assert "只管理当前群" in asyncio.run(schedule(plugin, event, "set_enabled", False))
    tools = ToolSet([NS(name="current_group_scheduled_tasks"), NS(name="normal_tool")])
    req = NS(func_tool=tools)
    runtime_function("_remove_group_plugin_tools_outside_admin_group")(plugin, req, event)
    assert [item.name for item in req.func_tool] == ["normal_tool"]
    assert len(tools.tools) == 2


def test_unresolved_schedule_request_falls_through_without_writing():
    calls = []

    async def request(payload):
        calls.append(payload)
        return {**group_scope(), "resolved": False}

    event, req = Event(message="怎么停止本群定时任务？"), NS(system_prompt="original")
    plugin, hook = control_hook(request)
    assert asyncio.run(dispatch_hook_before_provider(plugin, hook, event, req)) == 1
    assert [item["action"] for item in calls] == ["resolve_message"]
    assert not event.stopped and event.sent == []


def test_schedule_prompt_rejects_old_cron_empty_result_as_group_task_evidence():
    plugin = NS(context=context(), config={"inject_capability_prompt": False})
    remove = runtime_function("_remove_group_plugin_tools_outside_admin_group")
    plugin._remove_group_plugin_tools_outside_admin_group = lambda req, event: remove(plugin, req, event)
    req = NS(func_tool=[], system_prompt="original")
    asyncio.run(runtime_function("inject_capability_context")(plugin, Event(), req))
    assert "current_group_scheduled_tasks" in req.system_prompt
    assert "其空列表不能代表本群没有定时任务" in req.system_prompt
