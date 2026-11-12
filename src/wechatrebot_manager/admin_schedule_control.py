"""Administrator controls for scheduled deliveries to the current group only."""
from __future__ import annotations

from collections.abc import Mapping
import re
import sqlite3
from typing import Any, TYPE_CHECKING

from .customer_config import load_robot_admin_ids
from .group_policy import GroupPolicyError, GroupPolicyService
from .plugin_errors import PluginCenterError
from .task_store import TaskStore

if TYPE_CHECKING:
    from .plugin_center import PluginCenterService


_GROUP = r"(?:当前群聊|当前群|这个群聊|这个群|本群聊|本群|该群聊|该群|此群|群聊内|群内|群里)"
_TARGET = rf"(?:{_GROUP}的?)?(?:(?:全部|所有)的?)?定时(?:任务|推送)"
_ACTION = r"停止|暂停|关闭|停用|禁用|停掉|关掉|恢复|开启|启用|打开|启动"
_IMPERATIVE = re.compile(
    rf"(?:请|麻烦|帮我|帮忙|劳烦|给我|把|将|在)*(?:{_GROUP}的?)?"
    rf"(?:把|将)?(?:(?P<before>{_ACTION}){_TARGET}|{_TARGET}(?P<after>{_ACTION}))"
    r"(?:一下|吧|呀|啊|谢谢|[，,。.!！])*"
)
_ENABLED_ACTIONS = frozenset({"恢复", "开启", "启用", "打开", "启动"})


def resolve_schedule_intent(message: object) -> bool | None:
    """Accept only a complete present-tense command, never model-supplied intent."""
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        return None
    text = message.strip()
    # Only standalone display mentions at the ends are address text. Quoted,
    # negated, deferred and multi-target instructions cannot match the grammar.
    text = re.sub(r"^(?:@[^\s@]+\s+)+", "", text)
    text = re.sub(r"(?:\s+@[^\s@]+)+$", "", text)
    text = re.sub(r"[^\S\n]+", "", text)
    match = _IMPERATIVE.fullmatch(text)
    if match is None:
        return None
    return (match["before"] or match["after"]) in _ENABLED_ACTIONS


def _current_group_tasks(service: PluginCenterService, account: str, group: str) -> list[dict[str, object]]:
    try:
        tasks = TaskStore(service.install_root / "data/manager/scheduled-tasks.db").list(account)
        return [
            {key: task[key] for key in ("id", "name", "schedule_text", "enabled", "draft")}
            for task in tasks
            if any(target.get("type") == "group" and target.get("id") == group
                   for target in task["targets"])
        ]
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PluginCenterError("本群定时任务读取失败，无法确认任务数量；请在管理器检查后重试。") from exc


def handle_schedule_control(service: PluginCenterService, payload: Mapping[str, Any]) -> dict[str, object]:
    """Use the unified task store and the same recipient switch as the manager."""
    sender = payload.get("sender_id")
    if not isinstance(sender, str) or sender not in load_robot_admin_ids(service.install_root):
        raise PluginCenterError("这个管理功能只有配置中的管理员微信号可用。")
    if payload.get("scope") != "current_group":
        raise PluginCenterError("这里只支持当前群聊的定时任务管理。")
    action = payload.get("action")
    if action not in {"list", "set_enabled", "resolve_message"}:
        raise PluginCenterError("不支持这项定时任务管理操作。")
    policies = GroupPolicyService(service.install_root)
    try:
        policy = policies.get(payload.get("account_id"), payload.get("group_id"))
        account, group = str(policy["account_id"]), str(policy["group_id"])
        scope = {"scope": "current_group", "account_id": account, "group_id": group}
        if action == "resolve_message":
            enabled = resolve_schedule_intent(payload.get("message"))
            return {**scope, "resolved": enabled is not None,
                    **({"enabled": enabled} if enabled is not None else {})}
        if action == "set_enabled":
            enabled = payload.get("enabled")
            if type(enabled) is not bool or resolve_schedule_intent(payload.get("message")) is not enabled:
                raise PluginCenterError("当前消息未明确要求这次操作，请直接说“停止本群的定时任务”或“恢复本群的定时任务”。")
        tasks = _current_group_tasks(service, account, group)
        summary = {**scope, "tasks": tasks, "task_count": len(tasks),
                   "enabled_count": sum(task["enabled"] and not task["draft"] for task in tasks)}
        if action == "list":
            return {**summary, "scheduled_enabled": policy["scheduled_enabled"], "revision": policy["revision"]}
        revision = payload.get("revision", policy["revision"])
        if not isinstance(revision, str) or not revision:
            raise PluginCenterError("群聊策略版本无效，请重新查询本群定时任务后重试。")
        saved = policies.save(account, group, {"scheduled_enabled": enabled},
                              expected_revision=revision, actor_id=sender,
                              _preserve_existing_features=True)
        if saved.get("saved") is not True or saved.get("scheduled_enabled") is not enabled:
            raise PluginCenterError("本群定时推送状态未确认，请在管理器检查后重试。")
    except GroupPolicyError as exc:
        raise PluginCenterError(str(exc)) from exc
    state = "恢复" if enabled else "暂停"
    return {**summary, "scheduled_enabled": enabled, "enabled": enabled, "applied": True,
            "revision": saved["revision"],
            "message": f"已{state}本群接收定时推送。本群已登记 {len(tasks)} 个任务"
                       f"（{summary['enabled_count']} 个已启用）；任务设置保留，仅调整本群接收开关。"}
